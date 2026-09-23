# CloseOps Accountant Desk

Accountant Desk is a **local desktop pilot**, built with Python's Tkinter GUI and the same deterministic engine used by `cash-control`, `closeops` and `stripe-bridge`. It lets a finance user operate the existing local workflow without editing JSON manifests or CSV review rows by hand. It does not host data, authenticate users, sync to QuickBooks, or attest that a reviewer actually resolved an exception.

```mermaid
flowchart LR
  F[Select four approved client CSVs] --> CM[Client manifest wizard]
  CM --> FM[Firm manifest wizard]
  FM --> R[Run closed period locally]
  R --> P[Portfolio view]
  P --> C[Client exception list]
  C --> D[Operator review decision]
  D --> S[Export review summary]
```

## Launch and try the fictional example

```bash
python -m pip install -e .
closeops-desk
```

Choose **Run firm close**, select `examples/firm/manifest.json`, and choose a local folder for the run. The app creates a new timestamped run folder; it will not overwrite a previous run. Click `fictional-store` to see one exception, select it, enter a reviewer name and decision, and save. Then export a review summary to a new JSON path. The second fictional client has no exception. Alternatively, choose **Open existing run** and select a folder produced earlier by `closeops run`.

The **New client manifest** flow asks for a pseudonymous client key, an order period, an as-of date and four approved CSV files. It derives row counts and currency totals from those files, displays them for review, and labels their origin `derived_from_selected_files_not_independent_source_controls`. The operator must compare them with the source system; matching a file against totals computed from that same file does **not** prove export completeness. **New firm manifest** combines client manifests from one period. Manifests store absolute file paths and must stay in customer-controlled storage.

The review form permits `confirmed_issue`, `false_alarm`, `needs_source` and `operator_declared_resolved`. A declared resolution requires notes. It updates only that client's local review queue and checks that the queue still matches the immutable exception report. The reviewer name is typed text; there is no login or identity proof. Review summaries refuse to overwrite earlier files. Financial source files are never sent to a server by this interface.

## Boundaries before a customer pilot

- Use a customer-controlled machine and storage location with appropriate access, encryption and retention. Newly generated run directories are owner-only on Unix-like systems; local administrators and filesystem permissions remain the customer's responsibility.
- Keep real exports, manifests, runs and review summaries out of Git. `customer-data/` and `pilot-runs/` are ignored, but users may choose other paths, so the ignore file is not a data-loss prevention system.
- The UI is a single-operator local pilot. Concurrent edits, authenticated reviewers, append-only decisions, source-system attestations, refund and dispute semantics, bank settlement confirmation, tenant isolation and hosted collaboration are not implemented.
- For a pilot, recruit one consenting firm, run three clients over two closed months, sample both matches and exceptions, and record setup time, exception precision, misses, review minutes and second-month use. Those are future field measurements, not claims established by the fictional fixture.

If Tkinter is unavailable on a computer, the CLI commands remain usable. On macOS, the Python installation may need its Tcl/Tk component. The automated tests exercise the file model and engine; the GUI requires a visual smoke check on each target desktop platform before customer distribution.
