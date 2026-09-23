import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cash_control.closeops import run_firm, summarize_reviews
from cash_control.desk_model import (
    create_client_manifest,
    create_firm_manifest,
    load_review_rows,
    load_run,
    save_review,
)

ROOT = Path(__file__).resolve().parents[1] / "examples"


class AccountantDeskModelTests(unittest.TestCase):
    def test_manifest_wizard_and_review_workflow(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            source_paths = {kind: ROOT / f"{kind}.csv" for kind in ("orders", "charges", "payouts", "ledger")}
            client_path = base / "client.json"
            client = create_client_manifest("example-client", "2026-08", "2026-08-31", source_paths, client_path)
            self.assertEqual(client["sources"]["charges"]["totals_by_currency"], {"USD": "150.00"})
            self.assertIn("not_independent", client["control_origin"])
            firm_path = base / "firm.json"
            firm = create_firm_manifest("example-firm", [client_path], firm_path)
            self.assertEqual(firm["period"], "2026-08")
            run_dir = base / "run"
            run_firm(firm_path, run_dir)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((run_dir / "portfolio.json").stat().st_mode), 0o600)
            portfolio, reports = load_run(run_dir)
            self.assertEqual(portfolio["client_count"], 1)
            self.assertEqual(reports["example-client"]["control_origin"], client["control_origin"])
            row = load_review_rows(run_dir, "example-client")[0]
            save_review(run_dir, "example-client", row["entity_type"], row["entity_id"], row["reason"],
                        "reviewer-1", "needs_source", "2026-09-01", "Check processor export")
            summary = summarize_reviews(run_dir, base / "review-summary.json")
            self.assertEqual(summary["clients"][0]["counts"], {"needs_source": 1})

    def test_bad_decision_does_not_modify_queue(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_firm(ROOT / "firm" / "manifest.json", run_dir)
            queue = run_dir / "clients" / "fictional-store" / "review-queue.csv"
            before = queue.read_bytes()
            row = load_review_rows(run_dir, "fictional-store")[0]
            with self.assertRaisesRegex(ValueError, "invalid review decision"):
                save_review(run_dir, "fictional-store", row["entity_type"], row["entity_id"],
                            row["reason"], "reviewer", "forged", "2026-09-01", "")
            self.assertEqual(queue.read_bytes(), before)

    def test_manifest_never_overwrites_and_mismatched_period_fails(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            path = base / "client.json"
            sources = {kind: ROOT / f"{kind}.csv" for kind in ("orders", "charges", "payouts", "ledger")}
            create_client_manifest("example-client", "2026-08", "2026-08-31", sources, path)
            with self.assertRaisesRegex(ValueError, "already exists"):
                create_client_manifest("example-client", "2026-08", "2026-08-31", sources, path)
            altered = json.loads(path.read_text())
            altered["customer_key"] = "other-client"
            altered["period"] = "2026-07"
            other = base / "other.json"
            other.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError, "periods differ"):
                create_firm_manifest("example-firm", [path, other], base / "firm.json")


if __name__ == "__main__":
    unittest.main()
