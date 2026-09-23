import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cash_control.closeops import run_firm, summarize_reviews

ROOT = Path(__file__).resolve().parents[1] / "examples" / "firm"


class CloseOpsTests(unittest.TestCase):
    def test_two_client_run_and_review_summary(self):
        with TemporaryDirectory() as temp:
            output = Path(temp) / "firm-run"
            dashboard = run_firm(ROOT / "manifest.json", output)
            self.assertEqual(dashboard["client_count"], 2)
            self.assertEqual(dashboard["total_exceptions"], 1)
            self.assertEqual([item["client_key"] for item in dashboard["clients"]],
                             ["fictional-store", "second-store"])
            self.assertEqual(dashboard["clients"][1]["exception_count"], 0)
            self.assertTrue((output / "clients" / "second-store" / "report.json").is_file())
            queue = output / "clients" / "fictional-store" / "review-queue.csv"
            with queue.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            rows[0].update(reviewer="fictional-reviewer", decision="confirmed_issue",
                           reviewed_at="2026-09-01", notes="Check processor export")
            with queue.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0])
                writer.writeheader()
                writer.writerows(rows)
            summary = summarize_reviews(output, Path(temp) / "reviews.json")
            self.assertEqual(summary["clients"][0]["counts"], {"confirmed_issue": 1})
            self.assertEqual(summary["clients"][1]["counts"], {})
            self.assertIn("unauthenticated", summary["boundary"])

    def test_duplicate_client_rejected_before_output(self):
        with TemporaryDirectory() as temp:
            manifest = json.loads((ROOT / "manifest.json").read_text())
            manifest["clients"].append(manifest["clients"][0])
            for client in manifest["clients"]:
                client["manifest"] = str((ROOT / client["manifest"]).resolve())
            source = Path(temp) / "manifest.json"
            source.write_text(json.dumps(manifest))
            output = Path(temp) / "run"
            with self.assertRaisesRegex(ValueError, "duplicate client_key"):
                run_firm(source, output)
            self.assertFalse(output.exists())

    def test_firm_client_identity_must_match(self):
        with TemporaryDirectory() as temp:
            manifest = json.loads((ROOT / "manifest.json").read_text())
            manifest["clients"][0]["client_key"] = "wrong-client"
            for client in manifest["clients"]:
                client["manifest"] = str((ROOT / client["manifest"]).resolve())
            source = Path(temp) / "manifest.json"
            source.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "identity or period"):
                run_firm(source, Path(temp) / "run")

    def test_unknown_review_row_rejected(self):
        with TemporaryDirectory() as temp:
            output = Path(temp) / "run"
            run_firm(ROOT / "manifest.json", output)
            queue = output / "clients" / "fictional-store" / "review-queue.csv"
            queue.write_text(queue.read_text().replace("O3", "UNKNOWN"))
            with self.assertRaisesRegex(ValueError, "unknown or duplicate"):
                summarize_reviews(output, Path(temp) / "review.json")


if __name__ == "__main__":
    unittest.main()
