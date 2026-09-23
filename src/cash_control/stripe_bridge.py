"""Read-only Stripe automatic-payout snapshot and conservative USD normalization."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.stripe.com"
MAX_PAGES = 100
FIELDS = ("id", "type", "source", "currency", "amount", "fee", "net", "created", "available_on", "exchange_rate")


def _utc_date(value: object, field: str) -> str:
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid Stripe {field} timestamp")
    try:
        return datetime.fromtimestamp(value, tz=UTC).date().isoformat()
    except (OverflowError, ValueError):
        raise ValueError(f"invalid Stripe {field} timestamp") from None


def _cents(value: object, field: str, *, allow_zero: bool = False) -> str:
    if type(value) is not int or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"invalid Stripe {field} minor-unit amount")
    return f"{value // 100}.{value % 100:02d}"


class StripeReader:
    """Fixed-host GET-only client. Pass a restricted read-only key through the environment."""

    def __init__(self, api_key: str):
        if not api_key or not api_key.strip():
            raise ValueError("STRIPE_RESTRICTED_KEY is required")
        self.api_key = api_key.strip()

    def get(self, path: str, params: dict[str, str | int] | None = None) -> dict:
        query = "?" + urlencode(params) if params else ""
        request = Request(API_ROOT + path + query, headers={
            "Authorization": "Bearer " + self.api_key,
            "User-Agent": "a2z-commerce-cash-control/0.3 read-only",
        }, method="GET")
        with urlopen(request, timeout=20) as response:
            payload = response.read(8_000_001)
        if len(payload) > 8_000_000:
            raise ValueError("Stripe response exceeds 8 MB limit")
        return json.loads(payload)


def fetch_snapshot(payout_id: str, reader: StripeReader) -> dict:
    if not re.fullmatch(r"po_[A-Za-z0-9]+", payout_id):
        raise ValueError("invalid Stripe payout ID")
    payout = reader.get("/v1/payouts/" + payout_id)
    if not isinstance(payout, dict) or payout.get("id") != payout_id or payout.get("object") != "payout":
        raise ValueError("Stripe payout response identity mismatch")
    if payout.get("automatic") is not True or payout.get("reconciliation_status") != "completed":
        raise ValueError("only completed automatic payouts can be enumerated")
    transactions = []
    seen = set()
    cursor = None
    for _ in range(MAX_PAGES):
        params: dict[str, str | int] = {"payout": payout_id, "limit": 100}
        if cursor:
            params["starting_after"] = cursor
        page = reader.get("/v1/balance_transactions", params)
        if not isinstance(page, dict) or page.get("object") != "list" or not isinstance(page.get("data"), list):
            raise ValueError("invalid Stripe balance transaction page")
        items = page["data"]
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen:
                raise ValueError("duplicate or invalid Stripe balance transaction")
            seen.add(item["id"])
            transactions.append({field: item.get(field) for field in FIELDS})
        if page.get("has_more") is False:
            break
        if page.get("has_more") is not True or not items:
            raise ValueError("Stripe pagination is incomplete")
        cursor = items[-1]["id"]
    else:
        raise ValueError("Stripe payout exceeds pagination limit")
    minimal_payout = {field: payout.get(field) for field in
                      ("id", "object", "amount", "currency", "automatic", "status",
                       "reconciliation_status", "created", "livemode")}
    return {"schema_version": "1.0", "origin": "stripe_api_read_only",
            "payout": minimal_payout, "transactions": transactions,
            "complete_pagination": True,
            "boundary": "GET-only API snapshot. Stored fields exclude customer and bank details; source authorization and completeness are not attested by this file."}


def _charge_map(path: Path) -> dict[str, str]:
    result = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)) or not {"charge_id", "order_id"}.issubset(reader.fieldnames):
            raise ValueError("charge map requires charge_id,order_id")
        for row in reader:
            if None in row or not row.get("charge_id") or not row.get("order_id"):
                raise ValueError("charge map has missing or extra fields")
            charge_id, order_id = row["charge_id"].strip(), row["order_id"].strip()
            if not re.fullmatch(r"ch_[A-Za-z0-9]+", charge_id) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", order_id):
                raise ValueError("charge map has invalid ID")
            if charge_id in result:
                raise ValueError("duplicate charge map ID")
            result[charge_id] = order_id
    return result


def _write_csv(path: Path, headers: tuple[str, ...], rows: list[dict]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def normalize(snapshot_path: Path, charge_map_path: Path, output_dir: Path) -> dict:
    """Produce v1 CSVs only when the entire payout is representable as USD charges."""
    if output_dir.exists():
        raise ValueError("output directory exists; normalization never overwrites prior runs")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payout = snapshot.get("payout")
    transactions = snapshot.get("transactions")
    if snapshot.get("schema_version") != "1.0" or snapshot.get("complete_pagination") is not True or not isinstance(payout, dict) or not isinstance(transactions, list):
        raise ValueError("Stripe snapshot is incomplete or invalid")
    payout_id = payout.get("id")
    if not isinstance(payout_id, str) or not re.fullmatch(r"po_[A-Za-z0-9]+", payout_id):
        raise ValueError("invalid Stripe payout identity")
    mapping = _charge_map(charge_map_path)
    holds = []
    if payout.get("automatic") is not True or payout.get("reconciliation_status") != "completed":
        holds.append({"entity_id": payout_id, "reason": "not_completed_automatic_payout"})
    if payout.get("status") != "paid":
        holds.append({"entity_id": payout_id, "reason": "payout_not_paid"})
    if payout.get("currency") != "usd":
        holds.append({"entity_id": payout_id, "reason": "unsupported_payout_currency"})
    amount = payout.get("amount")
    if type(amount) is not int or amount <= 0:
        holds.append({"entity_id": payout_id, "reason": "invalid_payout_amount"})
    charges = []
    seen = set()
    total_net = 0
    for item in transactions:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen:
            raise ValueError("invalid or duplicate Stripe transaction")
        seen.add(item["id"])
        transaction_id = item["id"]
        if item.get("type") != "charge":
            holds.append({"entity_id": transaction_id, "reason": "unsupported_transaction_type"})
            continue
        charge_id = item.get("source")
        if not isinstance(charge_id, str) or not re.fullmatch(r"ch_[A-Za-z0-9]+", charge_id):
            holds.append({"entity_id": transaction_id, "reason": "missing_charge_source"})
            continue
        if charge_id not in mapping:
            holds.append({"entity_id": transaction_id, "reason": "charge_order_mapping_missing"})
            continue
        if item.get("currency") != "usd" or item.get("exchange_rate") is not None:
            holds.append({"entity_id": transaction_id, "reason": "currency_or_conversion_unsupported"})
            continue
        gross, fee, net = item.get("amount"), item.get("fee"), item.get("net")
        if (type(gross) is not int or type(fee) is not int or type(net) is not int
                or gross <= 0 or fee < 0 or net <= 0 or gross - fee != net):
            holds.append({"entity_id": transaction_id, "reason": "charge_amounts_invalid"})
            continue
        try:
            available = _utc_date(item.get("available_on"), "available_on")
        except ValueError:
            holds.append({"entity_id": transaction_id, "reason": "availability_date_invalid"})
            continue
        charges.append({"charge_id": charge_id, "order_id": mapping[charge_id],
                        "currency": "USD", "gross": _cents(gross, "gross"),
                        "fee": _cents(fee, "fee", allow_zero=True),
                        "net": _cents(net, "net"), "payout_id": payout_id,
                        "settled_at": available})
        total_net += net
    if len({item["charge_id"] for item in charges}) != len(charges):
        holds.append({"entity_id": payout_id, "reason": "duplicate_charge_source"})
    if not transactions:
        holds.append({"entity_id": payout_id, "reason": "empty_payout_transactions"})
    if total_net != amount:
        holds.append({"entity_id": payout_id, "reason": "payout_net_not_all_supported_charges"})
    unused = set(mapping) - {item["charge_id"] for item in charges}
    if unused:
        holds.append({"entity_id": payout_id, "reason": "charge_map_contains_unmatched_ids"})
    try:
        paid_at = _utc_date(payout.get("created"), "created")
    except ValueError:
        holds.append({"entity_id": payout_id, "reason": "payout_date_invalid"})
        paid_at = None
    if paid_at and any(item["settled_at"] > paid_at for item in charges):
        holds.append({"entity_id": payout_id, "reason": "charge_available_after_payout_created"})
    report = {
        "schema_version": "1.0", "payout_id": payout_id,
        "status": "HELD" if holds else "NORMALIZED",
        "source_sha256": {"snapshot": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                          "charge_map": hashlib.sha256(charge_map_path.read_bytes()).hexdigest()},
        "transaction_count": len(transactions), "charge_count": len(charges),
        "holds": sorted(holds, key=lambda item: (item["entity_id"], item["reason"])),
        "boundary": "Only completed paid automatic USD payouts made entirely of mapped positive charges normalize. Payout created date is not bank settlement. Source snapshot and mapping require customer verification.",
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stripe-normalize-", dir=output_dir.parent) as staging:
        staged = Path(staging) / "run"
        staged.mkdir(mode=0o700)
        _write_json(staged / "normalization.json", report)
        if not holds:
            _write_csv(staged / "charges.csv", ("charge_id", "order_id", "currency", "gross", "fee", "net", "payout_id", "settled_at"), charges)
            _write_csv(staged / "payouts.csv", ("payout_id", "currency", "amount", "paid_at"),
                       [{"payout_id": payout_id, "currency": "USD", "amount": _cents(amount, "payout amount"), "paid_at": paid_at}])
        staged.rename(output_dir)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Stripe automatic payout bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = commands.add_parser("fetch")
    fetch_cmd.add_argument("payout_id")
    fetch_cmd.add_argument("--output", type=Path, required=True)
    normalize_cmd = commands.add_parser("normalize")
    normalize_cmd.add_argument("snapshot", type=Path)
    normalize_cmd.add_argument("charge_map", type=Path)
    normalize_cmd.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "fetch":
        if args.output.exists():
            raise ValueError("snapshot output already exists")
        reader = StripeReader(os.environ.get("STRIPE_RESTRICTED_KEY", ""))
        snapshot = fetch_snapshot(args.payout_id, reader)
        _write_json(args.output, snapshot)
        print(f"fetched {len(snapshot['transactions'])} balance transactions; output={args.output}")
    else:
        result = normalize(args.snapshot, args.charge_map, args.output_dir)
        print(f"{result['status']}: {result['charge_count']} supported charges; output={args.output_dir}")


if __name__ == "__main__":
    main()
