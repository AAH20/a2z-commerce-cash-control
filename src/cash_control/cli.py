"""Command-line entry point for local export reconciliation."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .engine import evaluate


def _spreadsheet_safe(value: str) -> str:
    """Prevent export identifiers from being interpreted as spreadsheet formulas."""
    return "'" + value if value and value[0] in "=+-@\t\r\n" else value


def write_report(result: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True)
    (output_dir / "report.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with (output_dir / "review-queue.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("entity_type", "entity_id", "reason", "reviewer",
                                                     "decision", "reviewed_at", "notes"))
        writer.writeheader()
        for exception in result["exceptions"]:
            writer.writerow({key: _spreadsheet_safe(value) for key, value in exception.items()})


def run(manifest: Path, output_dir: Path) -> dict:
    if output_dir.exists():
        raise ValueError("output directory exists; prior runs are never overwritten")
    result = evaluate(manifest)
    write_report(result, output_dir)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only order-to-ledger cash control")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.manifest, args.output_dir)
    print(f"{len(result['exceptions'])} exceptions; report={args.output_dir / 'report.json'}")


if __name__ == "__main__":
    main()
