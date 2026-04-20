#!/usr/bin/env python3
"""rclone sync GUI — tkinter front end, rclone logs stream to terminal."""

import json
import shutil
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


# ── Saved connections ─────────────────────────────────────────────────────────

SAVES_FILE = Path.home() / ".pdrive_saves.json"


def load_saves():
    try:
        if SAVES_FILE.exists():
            return json.loads(SAVES_FILE.read_text())
    except Exception:
        pass
    return []


def upsert_save(entry):
    saves = [s for s in load_saves() if s["name"] != entry["name"]]
    saves.append(entry)
    SAVES_FILE.write_text(json.dumps(saves, indent=2))


def delete_save(name):
    saves = [s for s in load_saves() if s["name"] != name]
    SAVES_FILE.write_text(json.dumps(saves, indent=2))


# ── rclone helpers ────────────────────────────────────────────────────────────

_stdbuf = shutil.which("stdbuf")


def rclone(*args, stream=False):
    base = ([_stdbuf, "-oL", "-eL"] if _stdbuf else []) + ["rclone"]
    cmd = base + list(args)
    if stream:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        lines = []
        while True:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if not line:
                continue
            line = line.rstrip()
            print(line)
            lines.append(line)
        proc.wait()
        return proc.returncode, "\n".join(lines)
    else:
        r = subprocess.run(["rclone"] + list(args), capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr


def get_remotes():
    _, out = rclone("listremotes")
    return [r.rstrip(":") for r in out.splitlines() if r.strip()]


def _run_sync(src, dst, batch, dry_run, auto_batch):
    """Run in background thread — all output goes to terminal."""
    print(f"\n{'─'*52}")
    print(f"  {src}  →  {dst}")
    print(f"  batch={batch}  auto={'yes' if auto_batch else 'one run'}")
    print(f"{'─'*52}")

    if dry_run:
        print("\n── Dry run (no files will be copied) ──")
        rclone("copy", src, dst, "--dry-run", "--progress", "--ignore-existing", stream=True)
        print("\nDry run complete. Uncheck 'Dry run first' and press Start to transfer for real.")
        return

    run_n = 0
    while True:
        run_n += 1
        print(f"\n── Batch {run_n} ──────────────────────────────")
        t0 = time.time()
        rc, out = rclone("copy", src, dst,
                         "--progress", "--transfers", "4", "--checkers", "8",
                         "--ignore-existing", "--max-transfer", batch,
                         "--drive-pacer-min-sleep", "10ms", "--drive-pacer-burst", "200",
                         stream=True)
        elapsed = int(time.time() - t0)
        print(f"\nBatch {run_n} done in {elapsed//60}m {elapsed%60}s  (exit {rc})")

        nothing_left = "0 B / 0 B" in out or ("Transferred:" in out and ", 0 B" in out)
        if nothing_left:
            print("\n✓ All done — nothing left to transfer.")
            return

        if not auto_batch:
            print("\nOne batch complete. Run again to continue.")
            return

        if rc == 9:
            print("Pausing 3s before next batch…")
            time.sleep(3)
            continue

        if rc not in (0, 9):
            print(f"\nError: rclone exited {rc}. Check output above.")
            return


# ── Remote browser ────────────────────────────────────────────────────────────

class RemoteBrowser(tk.Toplevel):
    """Modal dialog for navigating and selecting a remote folder."""

    def __init__(self, parent, remote):
        super().__init__(parent)
        self.title(f"Browse  {remote}:")
        self.remote = remote
        self.path = ""
        self.result = None
        self.items = []
        self.resizable(True, True)
        self.minsize(460, 380)
        self._build()
        self.transient(parent)
        self.grab_set()
        self._load()
        self.wait_window()

    def _build(self):
        top = tk.Frame(self, pady=6, padx=10)
        top.pack(fill="x")
        self.path_lbl = tk.Label(top, text="", anchor="w", font=("TkFixedFont", 10))
        self.path_lbl.pack(side="left", fill="x", expand=True)

        mid = tk.Frame(self)
        mid.pack(fill="both", expand=True, padx=10)
        sb = tk.Scrollbar(mid)
        sb.pack(side="right", fill="y")
        self.lb = tk.Listbox(mid, yscrollcommand=sb.set, font=("TkFixedFont", 10),
                             activestyle="dotbox", selectmode="browse")
        self.lb.pack(side="left", fill="both", expand=True)
        sb.config(command=self.lb.yview)
        self.lb.bind("<Double-Button-1>", lambda _: self._open())
        self.lb.bind("<Return>",          lambda _: self._open())

        bot = tk.Frame(self, pady=8, padx=10)
        bot.pack(fill="x")
        tk.Button(bot, text="← Back",  width=9,  command=self._back).pack(side="left", padx=3)
        tk.Button(bot, text="Open →",  width=9,  command=self._open).pack(side="left", padx=3)
        tk.Button(bot, text="Select this folder", width=20,
                  bg="#1a73e8", fg="white", command=self._select).pack(side="right", padx=3)

    def _load(self):
        self.path_lbl.config(text=f"  {self.remote}:/{self.path or ''}")
        self.lb.delete(0, "end")
        self.lb.insert("end", "  loading…")
        self.lb.config(state="disabled")
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        rc, out = rclone("lsjson", f"{self.remote}:{self.path}",
                         "--no-modtime", "--no-mimetype")
        try:
            self.items = [i for i in json.loads(out) if i.get("IsDir")] if rc == 0 else []
        except Exception:
            self.items = []
        self.after(0, self._populate)

    def _populate(self):
        self.lb.config(state="normal")
        self.lb.delete(0, "end")
        if not self.items:
            self.lb.insert("end", "  (no subfolders)")
        for item in self.items:
            self.lb.insert("end", f"  {item['Name']}/")

    def _selected_item(self):
        sel = self.lb.curselection()
        return self.items[sel[0]] if sel and sel[0] < len(self.items) else None

    def _open(self):
        item = self._selected_item()
        if item:
            self.path = (self.path.rstrip("/") + "/" + item["Name"]).lstrip("/")
            self._load()

    def _back(self):
        if self.path:
            self.path = "/".join(self.path.rstrip("/").split("/")[:-1])
            self._load()

    def _select(self):
        self.result = self.path
        self.destroy()


# ── Sync dialog ───────────────────────────────────────────────────────────────

class SyncDialog(tk.Toplevel):
    """Configure and launch a push or pull. Accepts optional prefill dict."""

    def __init__(self, parent, remotes, direction, prefill=None):
        super().__init__(parent)
        self.title("Upload  (local → remote)" if direction == "push"
                   else "Download  (remote → local)")
        self.direction = direction
        self.remotes   = remotes
        self._remote_path = None
        self.resizable(False, False)
        self._build()
        if prefill:
            self._apply_prefill(prefill)
        self.transient(parent)
        self.grab_set()
        self.wait_window()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self):
        f = tk.Frame(self, padx=16, pady=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        # Remote dropdown
        tk.Label(f, text="Remote:", anchor="e", width=20).grid(
            row=0, column=0, sticky="e", pady=5, padx=(0, 8))
        self.remote_var = tk.StringVar(value=self.remotes[0] if self.remotes else "")
        ttk.Combobox(f, textvariable=self.remote_var, values=self.remotes,
                     state="readonly", width=30).grid(row=0, column=1, columnspan=2,
                                                       sticky="w", pady=5)

        if self.direction == "push":
            self._local_row(f, 1, "Local source:")
            self._remote_row(f, 2, "Remote destination:")
        else:
            self._remote_row(f, 1, "Remote source:")
            self._local_row(f, 2, "Local destination:")

        # Batch size
        tk.Label(f, text="Batch size:", anchor="e", width=20).grid(
            row=3, column=0, sticky="e", pady=5, padx=(0, 8))
        self.batch_var = tk.StringVar(value="2G")
        tk.Entry(f, textvariable=self.batch_var, width=10).grid(
            row=3, column=1, sticky="w", pady=5)
        tk.Label(f, text="e.g. 500M · 2G · 10G", fg="gray").grid(
            row=3, column=2, sticky="w")

        # Checkboxes
        self.dry_var  = tk.BooleanVar(value=True)
        self.auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(f, text="Dry run first  (safe preview — no files copied)",
                       variable=self.dry_var).grid(
            row=4, column=1, columnspan=2, sticky="w", pady=2)
        tk.Checkbutton(f, text="Auto-batch until fully done",
                       variable=self.auto_var).grid(
            row=5, column=1, columnspan=2, sticky="w", pady=2)

        # Save section
        sf = tk.LabelFrame(f, text="Save this connection", padx=8, pady=6)
        sf.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        tk.Label(sf, text="Name:").pack(side="left")
        self.name_var = tk.StringVar()
        tk.Entry(sf, textvariable=self.name_var, width=26).pack(side="left", padx=6)
        tk.Button(sf, text="Save", command=self._save).pack(side="left")

        # Start
        tk.Button(f, text="  Start  ", bg="#1a73e8", fg="white",
                  font=("TkDefaultFont", 11, "bold"),
                  command=self._start).grid(row=7, column=1, pady=(12, 4), sticky="w")

    def _local_row(self, f, row, label):
        tk.Label(f, text=label, anchor="e", width=20).grid(
            row=row, column=0, sticky="e", pady=5, padx=(0, 8))
        self.local_var = tk.StringVar()
        tk.Entry(f, textvariable=self.local_var, width=31).grid(
            row=row, column=1, sticky="w", pady=5)
        tk.Button(f, text="Browse…", command=self._pick_local).grid(
            row=row, column=2, padx=4, pady=5)

    def _remote_row(self, f, row, label):
        tk.Label(f, text=label, anchor="e", width=20).grid(
            row=row, column=0, sticky="e", pady=5, padx=(0, 8))
        self.remote_lbl_var = tk.StringVar(value="(click Browse…)")
        tk.Label(f, textvariable=self.remote_lbl_var, fg="#1a73e8",
                 anchor="w", width=31).grid(row=row, column=1, sticky="w", pady=5)
        tk.Button(f, text="Browse…", command=self._pick_remote).grid(
            row=row, column=2, padx=4, pady=5)

    # ── actions ───────────────────────────────────────────────────────────────

    def _pick_local(self):
        path = filedialog.askdirectory(parent=self, title="Select folder")
        if path:
            self.local_var.set(path)

    def _pick_remote(self):
        remote = self.remote_var.get()
        if not remote:
            messagebox.showwarning("Select remote", "Choose a remote first.", parent=self)
            return
        browser = RemoteBrowser(self, remote)
        if browser.result is not None:
            self._remote_path = browser.result
            label = f"{remote}:/{browser.result}" if browser.result else f"{remote}:  (root)"
            self.remote_lbl_var.set(label)

    def _apply_prefill(self, p):
        self.remote_var.set(p["remote"])
        self.local_var.set(p["local"])
        self._remote_path = p["remote_path"]
        label = f"{p['remote']}:/{p['remote_path']}" if p["remote_path"] else f"{p['remote']}:  (root)"
        self.remote_lbl_var.set(label)
        self.batch_var.set(p.get("batch", "2G"))
        self.name_var.set(p.get("name", ""))
        self.auto_var.set(p.get("auto_batch", True))

    def _save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("Name required", "Enter a name for this connection.", parent=self)
            return
        local = self.local_var.get().strip()
        if not local or self._remote_path is None:
            messagebox.showwarning("Incomplete", "Fill in all paths before saving.", parent=self)
            return
        upsert_save({
            "name":       name,
            "direction":  self.direction,
            "remote":     self.remote_var.get(),
            "remote_path": self._remote_path,
            "local":      local,
            "batch":      self.batch_var.get().strip() or "2G",
            "auto_batch": self.auto_var.get(),
        })
        messagebox.showinfo("Saved", f"'{name}' saved.", parent=self)

    def _start(self):
        remote = self.remote_var.get()
        local  = self.local_var.get().strip()
        batch  = self.batch_var.get().strip() or "2G"
        if not remote:
            messagebox.showwarning("Missing", "Select a remote.", parent=self); return
        if not local:
            messagebox.showwarning("Missing", "Select a local folder.", parent=self); return
        if self._remote_path is None:
            messagebox.showwarning("Missing", "Browse and select a remote folder.", parent=self); return

        remote_full = f"{remote}:{self._remote_path}" if self._remote_path else f"{remote}:"
        src, dst = (local, remote_full) if self.direction == "push" else (remote_full, local)
        dry  = self.dry_var.get()
        auto = self.auto_var.get()
        self.destroy()
        threading.Thread(target=_run_sync, args=(src, dst, batch, dry, auto), daemon=True).start()


# ── Status window ─────────────────────────────────────────────────────────────

class StatusWindow(tk.Toplevel):
    def __init__(self, parent, remotes):
        super().__init__(parent)
        self.title("Connection Status")
        self.resizable(True, True)
        self.minsize(440, 200)
        txt = tk.Text(self, font=("TkFixedFont", 10), padx=10, pady=8, wrap="none")
        sb  = tk.Scrollbar(self, command=txt.yview)
        txt.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.insert("end", "Checking remotes…\n")
        txt.config(state="disabled")
        self.transient(parent)
        threading.Thread(target=self._fetch, args=(txt, remotes), daemon=True).start()

    def _fetch(self, txt, remotes):
        lines = []
        for r in remotes:
            lines.append(f"\n{r}:")
            rc, out = rclone("about", f"{r}:")
            if rc == 0:
                lines += [f"  {l}" for l in out.splitlines()]
            else:
                rc2, _ = rclone("lsd", f"{r}:", "--max-depth", "0")
                lines.append(f"  {'Connected ✓' if rc2 == 0 else 'Not reachable ✗'}")
        self.after(0, lambda: self._show(txt, "\n".join(lines)))

    def _show(self, txt, text):
        txt.config(state="normal")
        txt.delete("1.0", "end")
        txt.insert("end", text)
        txt.config(state="disabled")


# ── Main window ───────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("rclone Sync")
        self.resizable(False, False)
        self._build()
        self.remotes = []
        self.after(100, self._load_remotes)

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg="#1a1a2e", pady=16)
        hdr.pack(fill="x")
        tk.Label(hdr, text="rclone Sync", font=("TkDefaultFont", 16, "bold"),
                 bg="#1a1a2e", fg="white").pack()
        tk.Label(hdr, text="logs stream to terminal", font=("TkDefaultFont", 9),
                 bg="#1a1a2e", fg="#666").pack()

        # Main action buttons
        body = tk.Frame(self, padx=24, pady=16)
        body.pack(fill="x")
        btn = dict(width=28, height=2, font=("TkDefaultFont", 11), cursor="hand2")
        tk.Button(body, text="⬆  Upload  (local → remote)",
                  bg="#1a73e8", fg="white",
                  command=lambda: self._open_sync("push"), **btn).pack(pady=4)
        tk.Button(body, text="⬇  Download  (remote → local)",
                  bg="#0f9d58", fg="white",
                  command=lambda: self._open_sync("pull"), **btn).pack(pady=4)
        tk.Button(body, text="◎  Connection Status",
                  bg="#444", fg="white",
                  command=self._open_status, **btn).pack(pady=4)

        # Saved connections panel
        sep = tk.Frame(self, height=1, bg="#ddd")
        sep.pack(fill="x", padx=16)
        tk.Label(self, text="SAVED CONNECTIONS",
                 font=("TkDefaultFont", 8, "bold"), fg="#888").pack(pady=(10, 2))
        self.saves_frame = tk.Frame(self, padx=16)
        self.saves_frame.pack(fill="x", pady=(0, 8))
        self._refresh_saves()

        # Footer status
        self.status_lbl = tk.Label(self, text="Loading remotes…",
                                   fg="gray", font=("TkDefaultFont", 9), pady=8)
        self.status_lbl.pack()

    # ── saved connections ─────────────────────────────────────────────────────

    def _refresh_saves(self):
        for w in self.saves_frame.winfo_children():
            w.destroy()
        saves = load_saves()
        if not saves:
            tk.Label(self.saves_frame, text="None yet — fill in a sync dialog and hit Save",
                     fg="#aaa", font=("TkDefaultFont", 9)).pack(pady=4)
            return
        for s in saves:
            icon = "⬆" if s["direction"] == "push" else "⬇"
            row = tk.Frame(self.saves_frame)
            row.pack(fill="x", pady=2)
            tk.Button(row, text=f"{icon}  {s['name']}", anchor="w",
                      relief="groove", cursor="hand2",
                      command=lambda s=s: self._open_sync(s["direction"], prefill=s)
                      ).pack(side="left", fill="x", expand=True, ipady=3)
            tk.Button(row, text="×", fg="#c0392b", width=3, cursor="hand2",
                      command=lambda n=s["name"]: self._del_save(n)
                      ).pack(side="right", padx=(4, 0))

    def _del_save(self, name):
        if messagebox.askyesno("Delete", f"Delete '{name}'?", parent=self):
            delete_save(name)
            self._refresh_saves()

    # ── navigation ────────────────────────────────────────────────────────────

    def _load_remotes(self):
        threading.Thread(target=lambda: self.after(
            0, self._set_remotes, get_remotes()), daemon=True).start()

    def _set_remotes(self, remotes):
        self.remotes = remotes
        if remotes:
            self.status_lbl.config(text=f"Remotes: {', '.join(remotes)}", fg="#0f9d58")
        else:
            self.status_lbl.config(text="No remotes — run: rclone config", fg="#c0392b")

    def _open_sync(self, direction, prefill=None):
        if not self.remotes:
            messagebox.showinfo("No remotes", "No rclone remotes configured.\nRun: rclone config")
            return
        SyncDialog(self, self.remotes, direction, prefill=prefill)
        self._refresh_saves()          # pick up any saves made inside the dialog

    def _open_status(self):
        if not self.remotes:
            messagebox.showinfo("No remotes", "No rclone remotes configured.\nRun: rclone config")
            return
        StatusWindow(self, self.remotes)


if __name__ == "__main__":
    App().mainloop()
