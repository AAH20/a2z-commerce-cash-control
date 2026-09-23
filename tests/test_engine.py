import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from cash_control.cli import run
from cash_control.engine import evaluate

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class CashControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("orders.csv", "charges.csv", "payouts.csv", "ledger.csv", "manifest.json"):
            (self.root / name).write_bytes((EXAMPLES / name).read_bytes())

    def manifest(self):
        return json.loads((self.root / "manifest.json").read_text())

    def save_manifest(self, value):
        (self.root / "manifest.json").write_text(json.dumps(value))

    def test_reconciles_payout_and_flags_unmatched_order(self):
        report = run(self.root / "manifest.json", self.root / "result")
        self.assertEqual(report["ledger_linked_payout_by_currency"], {"USD": "145.00"})
        self.assertEqual(report["exception_counts"], {"missing_processor_charge": 1})
        self.assertEqual(report["exceptions"][0]["entity_id"], "O3")
        self.assertTrue((self.root / "result" / "review-queue.csv").is_file())
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            run(self.root / "manifest.json", self.root / "result")

    def test_declared_control_mismatch_fails_closed(self):
        manifest = self.manifest()
        manifest["sources"]["payouts"]["totals_by_currency"]["USD"] = "146.00"
        self.save_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "totals differ"):
            run(self.root / "manifest.json", self.root / "result")
        self.assertFalse((self.root / "result").exists())

    def test_duplicate_charge_is_rejected(self):
        charges = self.root / "charges.csv"
        charges.write_text(charges.read_text() + charges.read_text().splitlines()[1] + "\n")
        with self.assertRaisesRegex(ValueError, "duplicate charges identifier"):
            evaluate(self.root / "manifest.json")

    def test_fee_arithmetic_fails_closed(self):
        charges = self.root / "charges.csv"
        charges.write_text(charges.read_text().replace("100.00,3.00,97.00", "100.00,3.00,98.00"))
        with self.assertRaisesRegex(ValueError, "gross minus fee"):
            evaluate(self.root / "manifest.json")

    def test_missing_ledger_never_counts_payout_as_linked(self):
        (self.root / "ledger.csv").write_text("entry_id,payout_id,currency,amount,posted_at\n")
        manifest = self.manifest()
        manifest["sources"]["ledger"]["rows"] = 0
        manifest["sources"]["ledger"]["totals_by_currency"] = {}
        self.save_manifest(manifest)
        report = evaluate(self.root / "manifest.json")
        self.assertEqual(report["ledger_linked_payout_by_currency"], {})
        self.assertEqual(report["exception_counts"]["missing_ledger_entry"], 1)

    def test_mismatched_order_blocks_end_to_end_payout(self):
        manifest = self.manifest()
        (self.root / "orders.csv").write_text((self.root / "orders.csv").read_text().replace("O1,USD,100.00", "O1,USD,101.00"))
        manifest["sources"]["orders"]["totals_by_currency"]["USD"] = "181.00"
        self.save_manifest(manifest)
        report = evaluate(self.root / "manifest.json")
        self.assertEqual(report["ledger_linked_payout_by_currency"], {})
        self.assertEqual(report["exception_counts"]["charge_amount_or_currency_mismatch"], 1)
        self.assertEqual(report["exception_counts"]["underlying_charge_requires_review"], 1)

    def test_source_dates_must_fit_closed_period(self):
        manifest = deepcopy(self.manifest())
        manifest["as_of"] = "2026-08-10"
        self.save_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "closed period"):
            evaluate(self.root / "manifest.json")

    def test_spreadsheet_formula_identifier_rejected(self):
        orders = self.root / "orders.csv"
        orders.write_text(orders.read_text().replace("O3,USD", "=1+1,USD"))
        with self.assertRaisesRegex(ValueError, "unsafe or invalid order_id"):
            evaluate(self.root / "manifest.json")


if __name__ == "__main__":
    unittest.main()
