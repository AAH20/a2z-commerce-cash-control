# CSV and evaluation contract (v1)

| Source | Required columns | Amount control |
|---|---|---|
| Orders | `order_id,currency,amount,paid_at` | Sum `amount` by currency |
| Charges | `charge_id,order_id,currency,gross,fee,net,payout_id,settled_at` | Sum `gross` by currency |
| Payouts | `payout_id,currency,amount,paid_at` | Sum `amount` by currency |
| Ledger | `entry_id,payout_id,currency,amount,posted_at` | Sum `amount` by currency |

Dates use `YYYY-MM-DD`; currency is a three-letter uppercase code; amounts have at most two decimal places. `gross`, `net`, order amount, payout amount and ledger amount are positive; fee may be zero. `gross - fee = net` is required. IDs are unique within each source. The CSV contract represents **settled positive charges only**. Refunds, disputes, reversals, reserves, currency conversion, partial captures, split payouts and complex ledger allocations require separate future contracts, not forced values in these fields.

`report.json` includes source SHA-256 digests, record counts, control pass status, ledger-linked payout totals by currency, and a sorted exception list. An empty exception list means only that the supplied files reconcile under v1 rules. It does not prove the exports were complete or that cash arrived in a bank account.

Exception reasons: `missing_processor_charge`, `multiple_processor_charges`, `charge_amount_or_currency_mismatch`, `charge_settled_before_order_paid`, `missing_order`, `missing_payout`, `missing_processor_charges`, `charge_currency_mismatch`, `payout_before_charge_settlement`, `payout_net_mismatch`, `missing_ledger_entry`, `multiple_ledger_entries`, `ledger_amount_or_currency_mismatch`, `ledger_posted_before_payout`, and `underlying_charge_requires_review`.
