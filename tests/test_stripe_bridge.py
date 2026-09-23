import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from cash_control.engine import evaluate
from cash_control.stripe_bridge import fetch_snapshot, normalize

ROOT = Path(__file__).resolve().parents[1] / "examples" / "stripe"


class FakeReader:
    def __init__(self, payout, pages):
        self.payout = payout
        self.pages = pages
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path.startswith("/v1/payouts/"):
            return self.payout
        return self.pages.pop(0)


class StripeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = json.loads((ROOT / "automatic-payout-snapshot.json").read_text())
        self.path = self.root / "snapshot.json"
        self.path.write_text(json.dumps(self.snapshot))

    def test_fictional_complete_payout_normalizes(self):
        report = normalize(self.path, ROOT / "charge-order-map.csv", self.root / "out")
        self.assertEqual(report["status"], "NORMALIZED")
        self.assertEqual(report["charge_count"], 2)
        self.assertIn("po_demo1,USD,145.00,2026-08-08", (self.root / "out" / "payouts.csv").read_text())
        self.assertIn("ch_demo1,O1,USD,100.00,3.00,97.00", (self.root / "out" / "charges.csv").read_text())

    def test_normalized_files_work_with_cash_control(self):
        normalize(self.path, ROOT / "charge-order-map.csv", self.root / "out")
        ledger = self.root / "ledger.csv"
        ledger.write_text("entry_id,payout_id,currency,amount,posted_at\nL1,po_demo1,USD,145.00,2026-08-09\n")
        manifest = json.loads((ROOT.parent / "manifest.json").read_text())
        manifest["sources"]["orders"]["path"] = str(ROOT.parent / "orders.csv")
        manifest["sources"]["charges"]["path"] = str(self.root / "out" / "charges.csv")
        manifest["sources"]["payouts"]["path"] = str(self.root / "out" / "payouts.csv")
        manifest["sources"]["ledger"]["path"] = str(ledger)
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        result = evaluate(self.root / "manifest.json")
        self.assertEqual(result["ledger_linked_payout_by_currency"], {"USD": "145.00"})
        self.assertEqual(result["exception_counts"], {"missing_processor_charge": 1})

    def test_unsupported_refund_holds_entire_payout(self):
        modified = deepcopy(self.snapshot)
        modified["transactions"].append({"id": "txn_refund", "type": "refund", "source": "re_demo", "currency": "usd", "amount": -100,
                                         "fee": 0, "net": -100, "created": 1786060800, "available_on": 1786060800, "exchange_rate": None})
        self.path.write_text(json.dumps(modified))
        report = normalize(self.path, ROOT / "charge-order-map.csv", self.root / "out")
        self.assertEqual(report["status"], "HELD")
        self.assertIn("unsupported_transaction_type", {item["reason"] for item in report["holds"]})
        self.assertFalse((self.root / "out" / "charges.csv").exists())
        self.assertFalse((self.root / "out" / "payouts.csv").exists())

    def test_unmapped_charge_holds(self):
        mapping = self.root / "map.csv"
        mapping.write_text("charge_id,order_id\nch_demo1,O1\n")
        report = normalize(self.path, mapping, self.root / "out")
        self.assertEqual(report["status"], "HELD")
        self.assertIn("charge_order_mapping_missing", {item["reason"] for item in report["holds"]})

    def test_manual_or_incomplete_payout_rejected_by_fetch(self):
        payout = deepcopy(self.snapshot["payout"])
        payout["automatic"] = False
        reader = FakeReader(payout, [])
        with self.assertRaisesRegex(ValueError, "completed automatic"):
            fetch_snapshot("po_demo1", reader)
        self.assertEqual(len(reader.calls), 1)

    def test_fetch_paginates_without_secret_or_extra_fields(self):
        payout = deepcopy(self.snapshot["payout"])
        payout["destination"] = "ba_sensitive"
        page1 = {"object": "list", "data": [dict(self.snapshot["transactions"][0], description="do not store")], "has_more": True}
        page2 = {"object": "list", "data": [self.snapshot["transactions"][1]], "has_more": False}
        reader = FakeReader(payout, [page1, page2])
        result = fetch_snapshot("po_demo1", reader)
        self.assertEqual(len(result["transactions"]), 2)
        self.assertEqual(reader.calls[-1][1]["starting_after"], "txn_demo1")
        self.assertNotIn("ba_sensitive", json.dumps(result))
        self.assertNotIn("do not store", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
