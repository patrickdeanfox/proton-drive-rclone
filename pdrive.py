#!/usr/bin/env python3
"""rclone sync CLI — simple wrapper with folder pickers."""

import json
import shutil
import subprocess
import time
import tkinter as tk
from tkinter import filedialog


# ── Helpers ───────────────────────────────────────────────────────────────────

_stdbuf = shutil.which("stdbuf")


def rclone(*args, stream=False):
    """Run rclone command. If stream=True, print output live and return exit code."""
    base = ([_stdbuf, "-oL", "-eL"] if _stdbuf else []) + ["rclone"]
    cmd = base + list(args)
    if stream:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        output_lines = []
        while True:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if not line:
                continue
            line = line.rstrip()
            print(line)
            output_lines.append(line)
        proc.wait()
        return proc.returncode, "\n".join(output_lines)
    else:
        r = subprocess.run(["rclone"] + list(args), capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr


def get_remotes():
    _, out = rclone("listremotes")
    return [r.rstrip(":") for r in out.splitlines() if r.strip()]


def pick_local_folder(title="Select local folder"):
    """Open a native folder picker dialog."""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(title=title)
    root.destroy()
    return path or None


def pick_remote_folder(remote):
    """Browse remote folders with a simple numbered CLI list."""
    path = ""
    while True:
        rc, out = rclone("lsjson", f"{remote}:{path}", "--no-modtime", "--no-mimetype")
        if rc != 0:
            print(f"  Error listing remote: {out[:200]}")
            return path

        try:
            items = [i for i in json.loads(out) if i.get("IsDir")]
        except Exception:
            items = []

        print(f"\n  Remote: {remote}:/{path or ''}")
        print(f"  [0] (select this folder)")
        if path:
            print(f"  [b] .. (go back)")
        for i, item in enumerate(items, 1):
            print(f"  [{i}] {item['Name']}/")

        choice = input("  Choose: ").strip().lower()

        if choice == "0":
            return path
        elif choice == "b" and path:
            path = "/".join(path.rstrip("/").split("/")[:-1])
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(items):
                    path = (path.rstrip("/") + "/" + items[idx]["Name"]).lstrip("/")
            except ValueError:
                pass


def select_remote(prompt="Select remote: "):
    remotes = get_remotes()
    if not remotes:
        print("No remotes configured. Run: rclone config")
        return None
    print("\nRemotes:")
    for i, r in enumerate(remotes, 1):
        print(f"  {i}. {r}")
    try:
        return remotes[int(input(prompt)) - 1]
    except (ValueError, IndexError):
        print("Invalid choice.")
        return None


def hr():
    print("─" * 50)


def _run_batched(src, dst, batch):
    """Loop rclone copy src→dst in batches until nothing left."""
    run_n = 0
    while True:
        run_n += 1
        print(f"\n── Batch {run_n} ────────────────────────────────")
        t0 = time.time()
        rc, out = rclone("copy", src, dst,
                         "--progress", "--transfers", "4", "--checkers", "8",
                         "--ignore-existing", "--max-transfer", batch,
                         "--drive-pacer-min-sleep", "10ms", "--drive-pacer-burst", "200",
                         stream=True)
        elapsed = int(time.time() - t0)
        print(f"\nBatch {run_n} done in {elapsed//60}m {elapsed%60}s")

        nothing_left = "0 B / 0 B" in out or ("Transferred:" in out and ", 0 B" in out)
        if nothing_left:
            print("\n✓ All done — nothing left to transfer.")
            break

        if rc not in (0, 9):
            print(f"Warning: rclone exit code {rc}")
            if input("Continue? (y/n): ").strip().lower() != "y":
                break

        if rc == 9:
            print("Pausing 3s…")
            time.sleep(3)


# ── Menu actions ──────────────────────────────────────────────────────────────

def cmd_push():
    """Upload: local folder → remote (Proton Drive, Dropbox, etc.)"""
    remote = select_remote()
    if not remote:
        return

    print("\nPick local source folder…")
    local = pick_local_folder("Select local source folder")
    if not local:
        print("No folder selected."); return
    print(f"  Local:  {local}")

    print("\nBrowse remote destination (press 0 to select current):")
    remote_path = pick_remote_folder(remote)
    remote_full = f"{remote}:{remote_path}" if remote_path else f"{remote}:"
    print(f"  Remote: {remote_full}")

    batch = input("\nBatch size per run [2G]: ").strip() or "2G"
    dry = input("Dry run first? (y/n) [y]: ").strip().lower()
    if dry != "n":
        print("\n── Dry run ──")
        rclone("copy", local, remote_full, "--dry-run", "--progress", "--ignore-existing",
               stream=True)
        if input("\nProceed with real sync? (y/n): ").strip().lower() != "y":
            return

    hr()
    print(f"Uploading  {local}  →  {remote_full}  (batch {batch})")
    hr()
    _run_batched(local, remote_full, batch)


def cmd_pull():
    """Download: remote (Dropbox, Proton Drive, etc.) → local folder"""
    remote = select_remote()
    if not remote:
        return

    print("\nBrowse remote source folder (press 0 to select current):")
    remote_path = pick_remote_folder(remote)
    remote_full = f"{remote}:{remote_path}" if remote_path else f"{remote}:"
    print(f"  Remote: {remote_full}")

    print("\nPick local destination folder…")
    local = pick_local_folder("Select local destination folder")
    if not local:
        print("No folder selected."); return
    print(f"  Local:  {local}")

    batch = input("\nBatch size per run [2G]: ").strip() or "2G"
    dry = input("Dry run first? (y/n) [y]: ").strip().lower()
    if dry != "n":
        print("\n── Dry run ──")
        rclone("copy", remote_full, local, "--dry-run", "--progress", "--ignore-existing",
               stream=True)
        if input("\nProceed with real sync? (y/n): ").strip().lower() != "y":
            return

    hr()
    print(f"Downloading  {remote_full}  →  {local}  (batch {batch})")
    hr()
    _run_batched(remote_full, local, batch)


def cmd_status():
    """Check connection and storage quota for all remotes."""
    remotes = get_remotes()
    if not remotes:
        print("No remotes configured.")
        return
    for r in remotes:
        print(f"\n{r}:")
        rc, out = rclone("about", f"{r}:")
        if rc == 0:
            for line in out.splitlines():
                print(f"  {line}")
        else:
            rc2, _ = rclone("lsd", f"{r}:", "--max-depth", "0")
            print(f"  {'Connected ✓' if rc2==0 else 'Not reachable ✗'}")


def cmd_browse():
    """Browse remote folders."""
    remote = select_remote()
    if not remote:
        return
    pick_remote_folder(remote)


# ── Main menu ─────────────────────────────────────────────────────────────────

MENU = [
    ("Upload  local → remote  (push)",  cmd_push),
    ("Download  remote → local  (pull)", cmd_pull),
    ("Connection status",                cmd_status),
    ("Browse remote folders",            cmd_browse),
]


def main():
    print("\n╔══════════════════════════════════╗")
    print("║       rclone Sync CLI            ║")
    print("╚══════════════════════════════════╝")

    while True:
        print()
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {i}. {label}")
        print("  q. Quit")

        choice = input("\n> ").strip().lower()
        if choice == "q":
            break
        try:
            _, fn = MENU[int(choice) - 1]
            hr()
            fn()
        except (ValueError, IndexError):
            print("Invalid choice.")
        except KeyboardInterrupt:
            print("\nCancelled.")

    print("Bye.")


if __name__ == "__main__":
    main()
