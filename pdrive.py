#!/usr/bin/env python3
"""rclone sync GUI — tkinter front end, rclone logs stream to terminal."""

import json
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


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


def _run_sync(src, dst, batch, dry_run):
    """Run in background thread — all output goes to terminal."""
    arrow = "→"
    print(f"\n{'─'*52}")
    print(f"  {src}  {arrow}  {dst}")
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

        if rc == 9:
            print("Pausing 3s before next batch…")
            time.sleep(3)
            continue

        if rc not in (0, 9):
            print(f"\nError: rclone exited {rc}. Check output above.")
            return


# ── Remote browser window ─────────────────────────────────────────────────────

class RemoteBrowser(tk.Toplevel):
    """Modal dialog for navigating and selecting a remote folder."""

    def __init__(self, parent, remote):
        super().__init__(parent)
        self.title(f"Browse  {remote}:")
        self.remote = remote
        self.path = ""
        self.result = None          # set when user confirms selection
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
        self.lb.bind("<Return>", lambda _: self._open())

        bot = tk.Frame(self, pady=8, padx=10)
        bot.pack(fill="x")
        tk.Button(bot, text="← Back",   width=9,  command=self._back).pack(side="left", padx=3)
        tk.Button(bot, text="Open →",   width=9,  command=self._open).pack(side="left", padx=3)
        tk.Button(bot, text="Select this folder", width=18,
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
        if sel and sel[0] < len(self.items):
            return self.items[sel[0]]
        return None

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


# ── Push / Pull dialog ────────────────────────────────────────────────────────

class SyncDialog(tk.Toplevel):
    """Collect source, destination, and options for a push or pull."""

    def __init__(self, parent, remotes, direction):
        super().__init__(parent)
        self.title("Upload  (local → remote)" if direction == "push"
                   else "Download  (remote → local)")
        self.direction = direction
        self.remotes = remotes
        self._remote_path = None    # selected remote subfolder path

        self.resizable(False, False)
        self._build()
        self.transient(parent)
        self.grab_set()
        self.wait_window()

    def _build(self):
        f = tk.Frame(self, padx=16, pady=12)
        f.pack(fill="both", expand=True)

        def row(r, label, widget_fn, col_span=1):
            tk.Label(f, text=label, anchor="e", width=18).grid(
                row=r, column=0, sticky="e", pady=5, padx=(0, 8))
            widget_fn(r)

        # Remote selector
        tk.Label(f, text="Remote:", anchor="e", width=18).grid(
            row=0, column=0, sticky="e", pady=5, padx=(0, 8))
        self.remote_var = tk.StringVar(value=self.remotes[0] if self.remotes else "")
        ttk.Combobox(f, textvariable=self.remote_var, values=self.remotes,
                     state="readonly", width=32).grid(row=0, column=1, columnspan=2,
                                                       sticky="w", pady=5)

        if self.direction == "push":
            self._build_row_local(f, 1, "Local source:")
            self._build_row_remote(f, 2, "Remote destination:")
        else:
            self._build_row_remote(f, 1, "Remote source:")
            self._build_row_local(f, 2, "Local destination:")

        # Batch size
        tk.Label(f, text="Batch size:", anchor="e", width=18).grid(
            row=3, column=0, sticky="e", pady=5, padx=(0, 8))
        self.batch_var = tk.StringVar(value="2G")
        tk.Entry(f, textvariable=self.batch_var, width=10).grid(
            row=3, column=1, sticky="w", pady=5)
        tk.Label(f, text="e.g. 500M, 2G, 10G", fg="gray").grid(
            row=3, column=2, sticky="w")

        # Dry run
        self.dry_var = tk.BooleanVar(value=True)
        tk.Checkbutton(f, text="Dry run first (safe preview — no files copied)",
                       variable=self.dry_var).grid(
            row=4, column=1, columnspan=2, sticky="w", pady=4)

        # Start button
        tk.Button(f, text="Start", width=14, bg="#1a73e8", fg="white",
                  font=("TkDefaultFont", 11, "bold"),
                  command=self._start).grid(row=5, column=1, pady=(14, 4), sticky="w")

    def _build_row_local(self, f, row, label):
        tk.Label(f, text=label, anchor="e", width=18).grid(
            row=row, column=0, sticky="e", pady=5, padx=(0, 8))
        self.local_var = tk.StringVar()
        tk.Entry(f, textvariable=self.local_var, width=33).grid(
            row=row, column=1, sticky="w", pady=5)
        tk.Button(f, text="Browse…", command=self._pick_local).grid(
            row=row, column=2, padx=4, pady=5)

    def _build_row_remote(self, f, row, label):
        tk.Label(f, text=label, anchor="e", width=18).grid(
            row=row, column=0, sticky="e", pady=5, padx=(0, 8))
        self.remote_lbl_var = tk.StringVar(value="(click Browse…)")
        tk.Label(f, textvariable=self.remote_lbl_var, fg="#1a73e8",
                 anchor="w", width=33).grid(row=row, column=1, sticky="w", pady=5)
        tk.Button(f, text="Browse…", command=self._pick_remote).grid(
            row=row, column=2, padx=4, pady=5)

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
            display = f"{remote}:/{browser.result}" if browser.result else f"{remote}:  (root)"
            self.remote_lbl_var.set(display)

    def _start(self):
        remote = self.remote_var.get()
        local  = self.local_var.get().strip()
        batch  = self.batch_var.get().strip() or "2G"
        dry    = self.dry_var.get()

        if not remote:
            messagebox.showwarning("Missing", "Select a remote.", parent=self); return
        if not local:
            messagebox.showwarning("Missing", "Select a local folder.", parent=self); return
        if self._remote_path is None:
            messagebox.showwarning("Missing", "Browse and select a remote folder.", parent=self); return

        remote_full = f"{remote}:{self._remote_path}" if self._remote_path else f"{remote}:"
        src, dst = (local, remote_full) if self.direction == "push" else (remote_full, local)

        self.destroy()
        threading.Thread(target=_run_sync, args=(src, dst, batch, dry), daemon=True).start()


# ── Status window ─────────────────────────────────────────────────────────────

class StatusWindow(tk.Toplevel):
    """Show quota and connection info for all remotes."""

    def __init__(self, parent, remotes):
        super().__init__(parent)
        self.title("Connection Status")
        self.resizable(True, True)
        self.minsize(440, 200)

        txt = tk.Text(self, font=("TkFixedFont", 10), padx=10, pady=8,
                      state="normal", wrap="none")
        sb = tk.Scrollbar(self, command=txt.yview)
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
                for line in out.splitlines():
                    lines.append(f"  {line}")
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
        hdr = tk.Frame(self, bg="#1a1a2e", pady=18)
        hdr.pack(fill="x")
        tk.Label(hdr, text="rclone Sync", font=("TkDefaultFont", 18, "bold"),
                 bg="#1a1a2e", fg="white").pack()
        tk.Label(hdr, text="logs stream to terminal", font=("TkDefaultFont", 9),
                 bg="#1a1a2e", fg="#888").pack()

        body = tk.Frame(self, padx=30, pady=24)
        body.pack()

        btn_style = dict(width=26, height=2, font=("TkDefaultFont", 11), cursor="hand2")

        tk.Button(body, text="⬆  Upload  (local → remote)",
                  bg="#1a73e8", fg="white",
                  command=lambda: self._open_sync("push"),
                  **btn_style).pack(pady=6)

        tk.Button(body, text="⬇  Download  (remote → local)",
                  bg="#0f9d58", fg="white",
                  command=lambda: self._open_sync("pull"),
                  **btn_style).pack(pady=6)

        tk.Button(body, text="◎  Connection Status",
                  bg="#444", fg="white",
                  command=self._open_status,
                  **btn_style).pack(pady=6)

        self.status_lbl = tk.Label(self, text="Loading remotes…",
                                   fg="gray", font=("TkDefaultFont", 9), pady=6)
        self.status_lbl.pack()

    def _load_remotes(self):
        def fetch():
            remotes = get_remotes()
            self.after(0, lambda: self._set_remotes(remotes))
        threading.Thread(target=fetch, daemon=True).start()

    def _set_remotes(self, remotes):
        self.remotes = remotes
        if remotes:
            self.status_lbl.config(text=f"Remotes: {', '.join(remotes)}", fg="#0f9d58")
        else:
            self.status_lbl.config(text="No remotes found — run: rclone config", fg="#c0392b")

    def _open_sync(self, direction):
        if not self.remotes:
            messagebox.showinfo("No remotes", "No rclone remotes configured.\nRun: rclone config")
            return
        SyncDialog(self, self.remotes, direction)

    def _open_status(self):
        if not self.remotes:
            messagebox.showinfo("No remotes", "No rclone remotes configured.\nRun: rclone config")
            return
        StatusWindow(self, self.remotes)


if __name__ == "__main__":
    App().mainloop()
