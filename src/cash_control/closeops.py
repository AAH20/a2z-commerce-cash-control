"""Local multi-client accounting-firm run and operator-declared review summary."""

from __future__ import annotations

import argparse
import csv
import json
import re
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

from .cli import _spreadsheet_safe, write_report
from .engine import evaluate

DECISIONS = {"confirmed_issue", "false_alarm", "needs_source", "operator_declared_resolved"}
REVIEW_FIELDS = {"entity_type", "entity_id", "reason", "reviewer", "decision", "reviewed_at", "notes"}


def _slug(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", value):
        raise ValueError(f"{label} must be a pseudonymous slug of 3-64 characters")
    return value


def run_firm(manifest_path: Path, output_dir: Path) -> dict:
    """Preflight every client before writing any output; keep reports in separate folders."""
    if output_dir.exists():
        raise ValueError("output directory exists; prior firm runs are never overwritten")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "1.0":
        raise ValueError("firm manifest schema_version must be 1.0")
    firm_key = _slug(manifest.get("firm_key"), "firm_key")
    period = manifest.get("period")
    if not isinstance(period, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("firm period must be YYYY-MM")
    clients = manifest.get("clients")
    if not isinstance(clients, list) or not clients:
        raise ValueError("firm manifest requires at least one client")
    results = {}
    for client in clients:
        if not isinstance(client, dict):
            raise TypeError("client declaration must be an object")
        key = _slug(client.get("client_key"), "client_key")
        if key in results:
            raise ValueError("duplicate client_key")
        if not isinstance(client.get("manifest"), str) or not client["manifest"]:
            raise ValueError("client manifest path is required")
        path = (manifest_path.parent / client["manifest"]).resolve()
        if not path.is_file():
            raise ValueError("client manifest file does not exist")
        report = evaluate(path)
        if report["customer_key"] != key or report["period"] != period:
            raise ValueError("client identity or period differs from firm manifest")
        results[key] = report
    dashboard = {
        "schema_version": "1.0", "firm_key": firm_key, "period": period,
        "client_count": len(results),
        "total_exceptions": sum(len(item["exceptions"]) for item in results.values()),
        "clients": [
            {"client_key": key, "exception_count": len(report["exceptions"]),
             "exception_counts": report["exception_counts"],
             "ledger_linked_payout_by_currency": report["ledger_linked_payout_by_currency"]}
            for key, report in sorted(results.items())
        ],
        "boundary": "Client reports are separate. Portfolio is an operator-run index, not hosted tenant isolation or verified bank cash.",
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".closeops-", dir=output_dir.parent) as staging:
        staged = Path(staging) / "run"
        staged.mkdir()
        for key, report in results.items():
            write_report(report, staged / "clients" / key)
        (staged / "portfolio.json").write_text(json.dumps(dashboard, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staged.rename(output_dir)
    return dashboard


def summarize_reviews(run_dir: Path, output_path: Path) -> dict:
    """Validate edited queues against immutable reports; count operator declarations."""
    if output_path.exists():
        raise ValueError("review summary already exists; never overwrite it")
    portfolio = json.loads((run_dir / "portfolio.json").read_text(encoding="utf-8"))
    clients = []
    for client in portfolio["clients"]:
        key = _slug(client["client_key"], "client_key")
        client_dir = run_dir / "clients" / key
        report = json.loads((client_dir / "report.json").read_text(encoding="utf-8"))
        expected = {(item["entity_type"], _spreadsheet_safe(item["entity_id"]), item["reason"])
                    for item in report["exceptions"]}
        counts = Counter()
        seen = set()
        with (client_dir / "review-queue.csv").open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)) or not REVIEW_FIELDS.issubset(reader.fieldnames):
                raise ValueError(f"{key} review queue has invalid headers")
            for row in reader:
                if None in row or any(row.get(field) is None for field in REVIEW_FIELDS):
                    raise ValueError(f"{key} review queue has missing or extra fields")
                identity = (row["entity_type"], row["entity_id"], row["reason"])
                if identity not in expected or identity in seen:
                    raise ValueError(f"{key} has unknown or duplicate review exception")
                seen.add(identity)
                decision = row["decision"].strip()
                if not decision:
                    if row["reviewer"].strip() or row["reviewed_at"].strip() or row["notes"].strip():
                        raise ValueError(f"{key} has incomplete review data")
                    counts["unreviewed"] += 1
                    continue
                if decision not in DECISIONS or not row["reviewer"].strip():
                    raise ValueError(f"{key} has invalid decision or missing reviewer")
                try:
                    reviewed_at = date.fromisoformat(row["reviewed_at"].strip())
                except ValueError:
                    raise ValueError(f"{key} has invalid reviewed_at") from None
                if reviewed_at < date.fromisoformat(report["as_of"]):
                    raise ValueError(f"{key} review predates source snapshot")
                if decision == "operator_declared_resolved" and not row["notes"].strip():
                    raise ValueError(f"{key} declared resolution requires notes")
                counts[decision] += 1
        if seen != expected:
            raise ValueError(f"{key} review queue omits exceptions")
        clients.append({"client_key": key, "counts": dict(sorted(counts.items()))})
    summary = {
        "schema_version": "1.0", "firm_key": portfolio["firm_key"], "period": portfolio["period"],
        "clients": clients,
        "boundary": "Reviewer names and decisions are unauthenticated operator-supplied CSV data. Declared resolution is not independently verified or a recovered-cash claim.",
    }
    output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Local accounting-firm commerce close workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run")
    run_cmd.add_argument("manifest", type=Path)
    run_cmd.add_argument("--output-dir", type=Path, required=True)
    review_cmd = commands.add_parser("reviews")
    review_cmd.add_argument("run_dir", type=Path)
    review_cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        result = run_firm(args.manifest, args.output_dir)
        print(f"{result['client_count']} clients; {result['total_exceptions']} exceptions; output={args.output_dir}")
    else:
        result = summarize_reviews(args.run_dir, args.output)
        print(f"{len(result['clients'])} client review summaries; output={args.output}")


if __name__ == "__main__":
    main()
