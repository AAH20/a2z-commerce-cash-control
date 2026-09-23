"""Tkinter front end for the local Accountant Desk pilot."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread

from .closeops import run_firm, summarize_reviews
from .desk_model import (
    create_client_manifest,
    create_firm_manifest,
    load_review_rows,
    load_run,
    save_review,
)
from .engine import FIELDS


def main() -> None:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, simpledialog, ttk
    except ImportError as exc:
        raise RuntimeError("Tkinter is required for Accountant Desk on this computer") from exc

    class AccountantDesk(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("A2Z CloseOps Accountant Desk")
            self.geometry("1100x730")
            self.minsize(850, 570)
            self.run_dir: Path | None = None
            self.reports: dict[str, dict] = {}
            self.exceptions: list[dict] = []
            self.client_key: str | None = None
            self.status = tk.StringVar(value="Open a firm run or create manifests to begin. Local files only.")
            self.reviewer = tk.StringVar()
            self.decision = tk.StringVar(value="confirmed_issue")
            self.reviewed_at = tk.StringVar(value=datetime.now(UTC).date().isoformat())
            self._build(ttk, tk)

        def _build(self, ttk, tk):
            outer = ttk.Frame(self, padding=12)
            outer.pack(fill="both", expand=True)
            ttk.Label(outer, text="CloseOps Accountant Desk", font=("Helvetica", 20, "bold")).pack(anchor="w")
            ttk.Label(outer, text="Local, read-only source analysis · Reviewer entries are operator declarations, not authenticated proof.",
                      foreground="#4b5563").pack(anchor="w", pady=(2, 12))
            actions = ttk.Frame(outer)
            actions.pack(fill="x", pady=(0, 10))
            for label, callback in (("New client manifest", self.new_client),
                                    ("New firm manifest", self.new_firm),
                                    ("Run firm close", self.run_close),
                                    ("Open existing run", self.open_run),
                                    ("Export review summary", self.export_reviews)):
                ttk.Button(actions, text=label, command=callback).pack(side="left", padx=(0, 6))
            main_frame = ttk.PanedWindow(outer, orient="horizontal")
            main_frame.pack(fill="both", expand=True)
            left = ttk.Frame(main_frame, padding=4)
            right = ttk.Frame(main_frame, padding=4)
            main_frame.add(left, weight=1)
            main_frame.add(right, weight=3)
            ttk.Label(left, text="Clients", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 5))
            self.client_tree = ttk.Treeview(left, columns=("exceptions", "linked"), show="tree headings", height=20)
            self.client_tree.heading("#0", text="Client")
            self.client_tree.heading("exceptions", text="Exceptions")
            self.client_tree.heading("linked", text="Ledger-linked payout")
            self.client_tree.column("#0", width=150)
            self.client_tree.column("exceptions", width=75, anchor="center")
            self.client_tree.column("linked", width=135)
            self.client_tree.pack(fill="both", expand=True)
            self.client_tree.bind("<<TreeviewSelect>>", self.select_client)
            ttk.Label(right, text="Exceptions requiring accountant review", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 5))
            self.exception_tree = ttk.Treeview(right, columns=("entity", "id", "reason", "decision"), show="headings", height=12)
            for column, width in (("entity", 90), ("id", 145), ("reason", 250), ("decision", 175)):
                self.exception_tree.heading(column, text=column.replace("_", " ").title())
                self.exception_tree.column(column, width=width)
            self.exception_tree.pack(fill="both", expand=True)
            self.exception_tree.bind("<<TreeviewSelect>>", self.select_exception)
            form = ttk.LabelFrame(right, text="Record an operator decision", padding=10)
            form.pack(fill="x", pady=(12, 0))
            ttk.Label(form, text="Reviewer name").grid(row=0, column=0, sticky="w")
            ttk.Entry(form, textvariable=self.reviewer, width=25).grid(row=0, column=1, sticky="ew", padx=(6, 12))
            ttk.Label(form, text="Decision").grid(row=0, column=2, sticky="w")
            decision_box = ttk.Combobox(form, textvariable=self.decision, state="readonly", width=26,
                                        values=("confirmed_issue", "false_alarm", "needs_source", "operator_declared_resolved"))
            decision_box.grid(row=0, column=3, sticky="ew", padx=(6, 0))
            ttk.Label(form, text="Reviewed date (YYYY-MM-DD)").grid(row=1, column=0, sticky="w", pady=(8, 0))
            ttk.Entry(form, textvariable=self.reviewed_at, width=25).grid(row=1, column=1, sticky="ew", padx=(6, 12), pady=(8, 0))
            ttk.Label(form, text="Notes").grid(row=2, column=0, sticky="nw", pady=(8, 0))
            self.notes = tk.Text(form, height=3, width=60, wrap="word")
            self.notes.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=(8, 0))
            ttk.Button(form, text="Save review", command=self.save_selected_review).grid(row=3, column=3, sticky="e", pady=(10, 0))
            form.columnconfigure(1, weight=1)
            form.columnconfigure(3, weight=1)
            ttk.Label(outer, textvariable=self.status, foreground="#374151").pack(anchor="w", pady=(10, 0))

        def _error(self, title: str, exc: Exception) -> None:
            self.status.set(f"{title}: {exc}")
            messagebox.showerror(title, str(exc), parent=self)

        def new_client(self) -> None:
            key = simpledialog.askstring("Client key", "Pseudonymous client key (letters, digits, - or _):", parent=self)
            if not key:
                return
            period = simpledialog.askstring("Closed period", "Order period YYYY-MM:", parent=self)
            if not period:
                return
            as_of = simpledialog.askstring("As-of date", "Export cutoff YYYY-MM-DD:",
                                           initialvalue=datetime.now(UTC).date().isoformat(), parent=self)
            if not as_of:
                return
            paths = {}
            for kind in FIELDS:
                selected = filedialog.askopenfilename(title=f"Select {kind} CSV", filetypes=[("CSV", "*.csv")], parent=self)
                if not selected:
                    return
                paths[kind] = Path(selected)
            if not messagebox.askokcancel("Check source controls",
                                         "The app will calculate row counts and totals from the selected files. "
                                         "These are self-derived controls, not independent proof the exports are complete. "
                                         "Compare them with the source system before relying on results. Continue?", parent=self):
                return
            destination = filedialog.asksaveasfilename(title="Save client manifest", defaultextension=".json",
                                                        filetypes=[("JSON", "*.json")], parent=self)
            if not destination:
                return
            try:
                manifest = create_client_manifest(key, period, as_of, paths, Path(destination))
                controls = "\n".join(f"{name}: {source['rows']} rows; {source['totals_by_currency']}"
                                     for name, source in manifest["sources"].items())
                messagebox.showinfo("Client manifest saved", f"Saved to {destination}\n\nSelf-derived controls:\n{controls}", parent=self)
                self.status.set(f"Client manifest saved: {destination}")
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                self._error("Client manifest failed", exc)

        def new_firm(self) -> None:
            key = simpledialog.askstring("Firm key", "Pseudonymous accounting-firm key:", parent=self)
            if not key:
                return
            selected = filedialog.askopenfilenames(title="Select client manifests from one period",
                                                    filetypes=[("JSON", "*.json")], parent=self)
            if not selected:
                return
            destination = filedialog.asksaveasfilename(title="Save firm manifest", defaultextension=".json",
                                                        filetypes=[("JSON", "*.json")], parent=self)
            if not destination:
                return
            try:
                result = create_firm_manifest(key, [Path(item) for item in selected], Path(destination))
                self.status.set(f"Firm manifest saved: {len(result['clients'])} clients; {destination}")
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                self._error("Firm manifest failed", exc)

        def run_close(self) -> None:
            source = filedialog.askopenfilename(title="Select firm manifest", filetypes=[("JSON", "*.json")], parent=self)
            if not source:
                return
            parent = filedialog.askdirectory(title="Choose a customer-controlled output folder", parent=self)
            if not parent:
                return
            target = Path(parent) / ("closeops-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f"))
            self.status.set("Validating all clients and running local reconciliation…")
            def worker():
                try:
                    run_firm(Path(source), target)
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    message = str(exc)
                    self.after(0, lambda: self._error("Firm run failed", RuntimeError(message)))
                else:
                    self.after(0, lambda: self._load_run(target))
            Thread(target=worker, daemon=True).start()

        def open_run(self) -> None:
            selected = filedialog.askdirectory(title="Open existing CloseOps run", parent=self)
            if selected:
                self._load_run(Path(selected))

        def _load_run(self, path: Path) -> None:
            try:
                portfolio, reports = load_run(path)
                self.run_dir, self.reports = path, reports
                self.client_key = None
                self.client_tree.delete(*self.client_tree.get_children())
                self.exception_tree.delete(*self.exception_tree.get_children())
                for client in portfolio["clients"]:
                    linked = ", ".join(f"{currency} {amount}" for currency, amount in
                                       client["ledger_linked_payout_by_currency"].items()) or "—"
                    self.client_tree.insert("", "end", iid=client["client_key"], text=client["client_key"],
                                            values=(client["exception_count"], linked))
                self.status.set(f"{portfolio['firm_key']} · {portfolio['period']} · {portfolio['client_count']} clients · "
                                f"{portfolio['total_exceptions']} exceptions. Source files stay local.")
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                self._error("Open run failed", exc)

        def select_client(self, _event=None) -> None:
            selection = self.client_tree.selection()
            if not selection or self.run_dir is None:
                return
            self.client_key = selection[0]
            self.exceptions = self.reports[self.client_key]["exceptions"]
            try:
                reviews = {(row["entity_type"], row["entity_id"], row["reason"]): row
                           for row in load_review_rows(self.run_dir, self.client_key)}
            except (OSError, ValueError) as exc:
                self._error("Read review queue failed", exc)
                return
            self.exception_tree.delete(*self.exception_tree.get_children())
            for index, item in enumerate(self.exceptions):
                identity = (item["entity_type"], item["entity_id"], item["reason"])
                self.exception_tree.insert("", "end", iid=str(index),
                                           values=(*identity, reviews.get(identity, {}).get("decision", "")))
            self.status.set(f"{self.client_key}: {len(self.exceptions)} exceptions. Select one to review.")

        def select_exception(self, _event=None) -> None:
            selection = self.exception_tree.selection()
            if not selection or self.run_dir is None or self.client_key is None:
                return
            item = self.exceptions[int(selection[0])]
            try:
                row = next(row for row in load_review_rows(self.run_dir, self.client_key)
                           if (row["entity_type"], row["entity_id"], row["reason"]) ==
                           (item["entity_type"], item["entity_id"], item["reason"]))
            except (OSError, ValueError, StopIteration) as exc:
                self._error("Review entry unavailable", exc)
                return
            self.reviewer.set(row["reviewer"])
            self.decision.set(row["decision"] or "confirmed_issue")
            self.reviewed_at.set(row["reviewed_at"] or datetime.now(UTC).date().isoformat())
            self.notes.delete("1.0", "end")
            self.notes.insert("1.0", row["notes"])

        def save_selected_review(self) -> None:
            selection = self.exception_tree.selection()
            if not selection or self.run_dir is None or self.client_key is None:
                messagebox.showinfo("Select an exception", "Select a client and an exception first.", parent=self)
                return
            item = self.exceptions[int(selection[0])]
            try:
                save_review(self.run_dir, self.client_key, item["entity_type"], item["entity_id"],
                            item["reason"], self.reviewer.get(), self.decision.get(),
                            self.reviewed_at.get(), self.notes.get("1.0", "end-1c"))
                self.select_client()
                self.exception_tree.selection_set(selection[0])
                self.status.set("Operator review saved locally. Export a new summary when ready.")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._error("Save review failed", exc)

        def export_reviews(self) -> None:
            if self.run_dir is None:
                messagebox.showinfo("Open a run", "Open or create a firm run first.", parent=self)
                return
            destination = filedialog.asksaveasfilename(title="Save new review summary", defaultextension=".json",
                                                        filetypes=[("JSON", "*.json")], parent=self)
            if not destination:
                return
            try:
                summarize_reviews(self.run_dir, Path(destination))
                self.status.set(f"Operator-declared review summary saved: {destination}")
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                self._error("Export reviews failed", exc)

    AccountantDesk().mainloop()


if __name__ == "__main__":
    main()
