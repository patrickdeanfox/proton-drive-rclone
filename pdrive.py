#!/usr/bin/env python3
"""Proton Drive sync CLI — simple rclone wrapper with folder pickers."""

import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog


# ── Helpers ───────────────────────────────────────────────────────────────────

def rclone(*args, stream=False):
    """Run rclone command. If stream=True, print output live and return exit code."""
    cmd = ["rclone"] + list(args)
    if stream:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        output_lines = []
        for line in proc.stdout:
            line = line.rstrip()
            print(line)
            output_lines.append(line)
        proc.wait()
        return proc.returncode, "\n".join(output_lines)
    else:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr


def get_remotes():
    _, out = rclone("listremotes")
    return [r.rstrip(":") for r in out.splitlines() if r.strip()]


def pick_local_folder():
    """Open a native folder picker dialog."""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(title="Select local folder")
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

        import json
        try:
            items = [i for i in json.loads(out) if i.get("IsDir")]
        except Exception:
            items = []

        print(f"\n  Remote: {remote}:/{path or ''}")
        print(f"  {'[0] (select this folder)'}")
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


def hr():
    print("─" * 50)


# ── Menu actions ──────────────────────────────────────────────────────────────

def cmd_sync():
    """Run a batched push sync from local to Proton Drive."""
    remotes = get_remotes()
    if not remotes:
        print("No remotes configured. Run: rclone config")
        return

    print("\nRemotes:")
    for i, r in enumerate(remotes, 1):
        print(f"  {i}. {r}")
    try:
        remote = remotes[int(input("Select remote: ")) - 1]
    except (ValueError, IndexError):
        print("Invalid choice."); return

    print("\nPick local folder…")
    local = pick_local_folder()
    if not local:
        print("No folder selected."); return
    print(f"  Local: {local}")

    print("\nBrowse remote folder (press 0 to select current):")
    remote_path = pick_remote_folder(remote)
    remote_full = f"{remote}:{remote_path}" if remote_path else f"{remote}:"
    print(f"  Remote: {remote_full}")

    batch = input("\nBatch size per run [2G]: ").strip() or "2G"
    dry = input("Dry run first? (y/n) [y]: ").strip().lower()
    if dry != "n":
        print("\n── Dry run ──")
        rclone("copy", local, remote_full,
               "--dry-run", "--progress", "--ignore-existing",
               stream=True)
        if input("\nProceed with real sync? (y/n): ").strip().lower() != "y":
            return

    hr()
    print(f"Syncing {local} → {remote_full}  (batch {batch})")
    hr()

    run_n = 0
    while True:
        run_n += 1
        print(f"\n── Batch {run_n} ────────────────────────────────")
        t0 = time.time()
        rc, out = rclone("copy", local, remote_full,
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

        print("Pausing 3s…")
        time.sleep(3)


def cmd_status():
    """Check connection and storage quota."""
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
    remotes = get_remotes()
    if not remotes:
        print("No remotes configured.")
        return
    print("\nRemotes:")
    for i, r in enumerate(remotes, 1):
        print(f"  {i}. {r}")
    try:
        remote = remotes[int(input("Select remote: ")) - 1]
    except (ValueError, IndexError):
        print("Invalid choice."); return
    pick_remote_folder(remote)


# ── Main menu ─────────────────────────────────────────────────────────────────

MENU = [
    ("Sync local → Proton Drive", cmd_sync),
    ("Connection status",         cmd_status),
    ("Browse remote folders",     cmd_browse),
]

def main():
    print("\n╔══════════════════════════════════╗")
    print("║    Proton Drive Sync             ║")
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
