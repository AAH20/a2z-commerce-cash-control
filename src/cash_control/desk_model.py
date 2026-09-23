"""Local file operations shared by the Accountant Desk UI and tests."""

from __future__ import annotations

import calendar
import csv
import json
import os
import tempfile
from datetime import date
from pathlib import Path

from .cli import _spreadsheet_safe
from .closeops import DECISIONS, _slug
from .engine import AMOUNT, FIELDS, read_csv, totals

REVIEW_COLUMNS = ("entity_type", "entity_id", "reason", "reviewer", "decision", "reviewed_at", "notes")


def _write_new_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def create_client_manifest(customer_key: str, period: str, as_of_text: str,
                           source_paths: dict[str, Path], output_path: Path) -> dict:
    """Build a first-run manifest with visibly self-derived controls."""
    if output_path.exists():
        raise ValueError("client manifest already exists")
    customer_key = _slug(customer_key, "customer_key")
    try:
        as_of = date.fromisoformat(as_of_text)
        year, month = map(int, period.split("-"))
        date(year, month, 1)
    except (ValueError, TypeError):
        raise ValueError("period or as_of date is invalid") from None
    if len(period) != 7 or as_of < date(year, month, calendar.monthrange(year, month)[1]):
        raise ValueError("period must be YYYY-MM and closed by as_of")
    if set(source_paths) != set(FIELDS):
        raise ValueError("exactly four source files are required")
    sources = {}
    for kind in FIELDS:
        path = source_paths[kind].resolve()
        if not path.is_file():
            raise ValueError(f"{kind} file does not exist")
        rows = read_csv(path, kind, as_of)
        sources[kind] = {"path": str(path), "rows": len(rows),
                         "totals_by_currency": totals(rows, AMOUNT[kind])}
    manifest = {
        "schema_version": "1.0", "customer_key": customer_key,
        "period": period, "as_of": as_of.isoformat(), "sources": sources,
        "control_origin": "derived_from_selected_files_not_independent_source_controls",
    }
    _write_new_json(output_path, manifest)
    return manifest


def create_firm_manifest(firm_key: str, client_paths: list[Path], output_path: Path) -> dict:
    if output_path.exists():
        raise ValueError("firm manifest already exists")
    firm_key = _slug(firm_key, "firm_key")
    if not client_paths:
        raise ValueError("select at least one client manifest")
    clients = []
    seen = set()
    period = None
    for path in client_paths:
        source = json.loads(path.read_text(encoding="utf-8"))
        key = _slug(source.get("customer_key"), "customer_key")
        if key in seen:
            raise ValueError("duplicate customer_key")
        seen.add(key)
        if period is None:
            period = source.get("period")
        elif source.get("period") != period:
            raise ValueError("client periods differ")
        clients.append({"client_key": key, "manifest": str(path.resolve())})
    manifest = {"schema_version": "1.0", "firm_key": firm_key,
                "period": period, "clients": clients}
    _write_new_json(output_path, manifest)
    return manifest


def load_run(run_dir: Path) -> tuple[dict, dict[str, dict]]:
    portfolio = json.loads((run_dir / "portfolio.json").read_text(encoding="utf-8"))
    if portfolio.get("schema_version") != "1.0" or not isinstance(portfolio.get("clients"), list):
        raise ValueError("invalid firm run portfolio")
    reports = {}
    for client in portfolio["clients"]:
        key = _slug(client["client_key"], "client_key")
        report = json.loads((run_dir / "clients" / key / "report.json").read_text(encoding="utf-8"))
        if report.get("customer_key") != key or not isinstance(report.get("exceptions"), list):
            raise ValueError("client report does not match portfolio")
        reports[key] = report
    return portfolio, reports


def load_review_rows(run_dir: Path, client_key: str) -> list[dict[str, str]]:
    client_key = _slug(client_key, "client_key")
    path = run_dir / "clients" / client_key / "review-queue.csv"
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(REVIEW_COLUMNS):
            raise ValueError("review queue has invalid columns")
        rows = list(reader)
    if any(None in row or any(row.get(field) is None for field in REVIEW_COLUMNS) for row in rows):
        raise ValueError("review queue has missing or extra fields")
    return rows


def save_review(run_dir: Path, client_key: str, entity_type: str, entity_id: str,
                reason: str, reviewer: str, decision: str, reviewed_at: str, notes: str) -> None:
    """Update an operator-managed queue atomically; this is not authenticated evidence."""
    portfolio, reports = load_run(run_dir)
    client_key = _slug(client_key, "client_key")
    if client_key not in reports:
        raise ValueError("unknown client")
    if decision not in DECISIONS:
        raise ValueError("invalid review decision")
    reviewer = reviewer.strip()
    notes = notes.strip()
    if not reviewer or len(reviewer) > 100 or len(notes) > 2000:
        raise ValueError("reviewer or notes length is invalid")
    if decision == "operator_declared_resolved" and not notes:
        raise ValueError("declared resolution requires notes")
    try:
        reviewed = date.fromisoformat(reviewed_at)
    except ValueError:
        raise ValueError("reviewed_at must be YYYY-MM-DD") from None
    if reviewed < date.fromisoformat(reports[client_key]["as_of"]):
        raise ValueError("review predates source snapshot")
    if portfolio["period"] != reports[client_key]["period"]:
        raise ValueError("client period differs from firm run")
    path = run_dir / "clients" / client_key / "review-queue.csv"
    rows = load_review_rows(run_dir, client_key)
    expected = {(item["entity_type"], item["entity_id"], item["reason"])
                for item in reports[client_key]["exceptions"]}
    found = {(item["entity_type"], item["entity_id"], item["reason"]) for item in rows}
    if len(found) != len(rows) or found != expected:
        raise ValueError("review queue no longer matches immutable report")
    target = (entity_type, entity_id, reason)
    if target not in expected:
        raise ValueError("unknown exception")
    for row in rows:
        if (row["entity_type"], row["entity_id"], row["reason"]) == target:
            row.update(reviewer=_spreadsheet_safe(reviewer), decision=decision,
                       reviewed_at=reviewed.isoformat(), notes=_spreadsheet_safe(notes))
            break
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=path.parent,
                                         prefix=".review-", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=REVIEW_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
