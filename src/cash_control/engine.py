"""Deterministic order-to-ledger reconciliation for customer-approved CSV exports."""

from __future__ import annotations

import calendar
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

CENT = Decimal("0.01")
FIELDS = {
    "orders": ("order_id", "currency", "amount", "paid_at"),
    "charges": ("charge_id", "order_id", "currency", "gross", "fee", "net", "payout_id", "settled_at"),
    "payouts": ("payout_id", "currency", "amount", "paid_at"),
    "ledger": ("entry_id", "payout_id", "currency", "amount", "posted_at"),
}
IDS = {"orders": "order_id", "charges": "charge_id", "payouts": "payout_id", "ledger": "entry_id"}
AMOUNT = {"orders": "amount", "charges": "gross", "payouts": "amount", "ledger": "amount"}
DATES = {"orders": "paid_at", "charges": "settled_at", "payouts": "paid_at", "ledger": "posted_at"}


def money(value: str, label: str, *, allow_zero: bool = False) -> Decimal:
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError(f"invalid {label}") from None
    if not number.is_finite() or number != number.quantize(CENT) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"invalid {label}: require positive two-decimal amount")
    return number


def read_csv(path: Path, kind: str, as_of: date) -> dict[str, dict]:
    if kind not in FIELDS:
        raise ValueError("unknown source kind")
    result = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"{kind} has missing or duplicate CSV headers")
        if not set(FIELDS[kind]).issubset(reader.fieldnames):
            raise ValueError(f"{kind} is missing required columns")
        for row in reader:
            if None in row:
                raise ValueError(f"{kind} has extra CSV fields")
            for field in FIELDS[kind]:
                if row.get(field) is None or not row[field].strip():
                    raise ValueError(f"{kind} has blank {field}")
                row[field] = row[field].strip()
            identifier = row[IDS[kind]]
            for id_field in ("order_id", "payout_id", "entry_id", "charge_id"):
                if id_field in row and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", row[id_field]):
                    raise ValueError(f"{kind} has unsafe or invalid {id_field}")
            if identifier in result:
                raise ValueError(f"duplicate {kind} identifier: {identifier}")
            if not re.fullmatch(r"[A-Z]{3}", row["currency"]):
                raise ValueError(f"{kind} has invalid currency")
            try:
                parsed_date = date.fromisoformat(row[DATES[kind]])
            except ValueError:
                raise ValueError(f"{kind} has invalid date") from None
            if parsed_date > as_of:
                raise ValueError(f"{kind} contains a date after as_of")
            row[DATES[kind]] = parsed_date
            row[AMOUNT[kind]] = money(row[AMOUNT[kind]], f"{kind} {AMOUNT[kind]}")
            if kind == "charges":
                row["fee"] = money(row["fee"], "charge fee", allow_zero=True)
                row["net"] = money(row["net"], "charge net")
                if row["gross"] - row["fee"] != row["net"]:
                    raise ValueError("charge gross minus fee must equal net")
            result[identifier] = row
    return result


def totals(rows: dict[str, dict], field: str) -> dict[str, str]:
    values: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows.values():
        values[row["currency"]] += row[field]
    return {key: f"{value:.2f}" for key, value in sorted(values.items())}


def _source(manifest_path: Path, declaration: dict, kind: str, as_of: date) -> tuple[dict, str]:
    if not isinstance(declaration, dict) or not isinstance(declaration.get("path"), str):
        raise TypeError(f"{kind} source path is required")
    path = (manifest_path.parent / declaration["path"]).resolve()
    if not path.is_file():
        raise ValueError(f"{kind} source file does not exist")
    rows = read_csv(path, kind, as_of)
    if type(declaration.get("rows")) is not int or declaration["rows"] != len(rows):
        raise ValueError(f"{kind} row count differs from customer declaration")
    declared = declaration.get("totals_by_currency")
    if not isinstance(declared, dict):
        raise TypeError(f"{kind} totals_by_currency is required")
    normalized = {}
    for currency, value in declared.items():
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(f"{kind} control has invalid currency")
        normalized[currency] = f"{money(str(value), 'control total', allow_zero=True):.2f}"
    if normalized != totals(rows, AMOUNT[kind]):
        raise ValueError(f"{kind} totals differ from customer declaration")
    return rows, hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "1.0":
        raise ValueError("manifest schema_version must be 1.0")
    customer_key = manifest.get("customer_key")
    if not isinstance(customer_key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", customer_key):
        raise ValueError("customer_key must be a pseudonymous slug")
    period = manifest.get("period")
    if not isinstance(period, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("period must be YYYY-MM")
    as_of = date.fromisoformat(manifest["as_of"])
    year, month = map(int, period.split("-"))
    if date(year, month, calendar.monthrange(year, month)[1]) > as_of:
        raise ValueError("as_of must cover the closed period")
    sources = manifest.get("sources")
    if not isinstance(sources, dict):
        raise TypeError("sources are required")
    data, digests = {}, {}
    for kind in FIELDS:
        data[kind], digests[kind] = _source(manifest_path, sources.get(kind), kind, as_of)
    orders, charges, payouts, ledger = (data[kind] for kind in FIELDS)
    if any(order["paid_at"].year != year or order["paid_at"].month != month for order in orders.values()):
        raise ValueError("every order must have paid_at inside the declared period")
    exceptions = []
    def issue(entity_type: str, entity_id: str, reason: str) -> None:
        exceptions.append({"entity_type": entity_type, "entity_id": entity_id, "reason": reason})

    charges_by_order: dict[str, list[dict]] = defaultdict(list)
    charges_by_payout: dict[str, list[dict]] = defaultdict(list)
    ledger_by_payout: dict[str, list[dict]] = defaultdict(list)
    invalid_charges: set[str] = set()
    for charge in charges.values():
        charges_by_order[charge["order_id"]].append(charge)
        charges_by_payout[charge["payout_id"]].append(charge)
    for entry in ledger.values():
        ledger_by_payout[entry["payout_id"]].append(entry)
    for order_id, order in orders.items():
        linked = charges_by_order.get(order_id, [])
        if not linked:
            issue("order", order_id, "missing_processor_charge")
        elif len(linked) != 1:
            issue("order", order_id, "multiple_processor_charges")
            invalid_charges.update(item["charge_id"] for item in linked)
        elif (linked[0]["currency"], linked[0]["gross"]) != (order["currency"], order["amount"]):
            issue("order", order_id, "charge_amount_or_currency_mismatch")
            invalid_charges.add(linked[0]["charge_id"])
        elif linked[0]["settled_at"] < order["paid_at"]:
            issue("order", order_id, "charge_settled_before_order_paid")
            invalid_charges.add(linked[0]["charge_id"])
    for charge_id, charge in charges.items():
        if charge["order_id"] not in orders:
            issue("charge", charge_id, "missing_order")
            invalid_charges.add(charge_id)
        if charge["payout_id"] not in payouts:
            issue("charge", charge_id, "missing_payout")
            invalid_charges.add(charge_id)
    for entry_id, entry in ledger.items():
        if entry["payout_id"] not in payouts:
            issue("ledger", entry_id, "missing_payout")

    matched: dict[str, Decimal] = defaultdict(Decimal)
    for payout_id, payout in payouts.items():
        linked = charges_by_payout.get(payout_id, [])
        if not linked:
            issue("payout", payout_id, "missing_processor_charges")
            continue
        if any(item["currency"] != payout["currency"] for item in linked):
            issue("payout", payout_id, "charge_currency_mismatch")
            continue
        if any(item["settled_at"] > payout["paid_at"] for item in linked):
            issue("payout", payout_id, "payout_before_charge_settlement")
            continue
        if sum((item["net"] for item in linked), Decimal(0)) != payout["amount"]:
            issue("payout", payout_id, "payout_net_mismatch")
            continue
        entries = ledger_by_payout.get(payout_id, [])
        if not entries:
            issue("payout", payout_id, "missing_ledger_entry")
            continue
        if len(entries) != 1:
            issue("payout", payout_id, "multiple_ledger_entries")
            continue
        entry = entries[0]
        if (entry["currency"], entry["amount"]) != (payout["currency"], payout["amount"]):
            issue("payout", payout_id, "ledger_amount_or_currency_mismatch")
            continue
        if entry["posted_at"] < payout["paid_at"]:
            issue("payout", payout_id, "ledger_posted_before_payout")
            continue
        if any(item["charge_id"] in invalid_charges for item in linked):
            issue("payout", payout_id, "underlying_charge_requires_review")
            continue
        matched[payout["currency"]] += payout["amount"]
    exceptions.sort(key=lambda item: (item["entity_type"], item["entity_id"], item["reason"]))
    return {
        "schema_version": "1.0", "customer_key": customer_key, "period": period,
        "as_of": as_of.isoformat(), "source_sha256": digests,
        "source_row_counts": {kind: len(data[kind]) for kind in FIELDS},
        "source_controls_passed": True,
        "ledger_linked_payout_by_currency": {key: f"{value:.2f}" for key, value in sorted(matched.items())},
        "exception_counts": dict(sorted(Counter(item["reason"] for item in exceptions).items())),
        "exceptions": exceptions,
        "claim_boundary": "Customer-declared controls and file hashes do not attest source completeness. Ledger linkage is not bank settlement, recovered revenue, or proof of causation. No write-back occurs.",
    }
