#!/usr/bin/env python3
"""Interactive rclone batch sync script."""

import subprocess
import sys
import time


def run(cmd):
    """Run a command and return (returncode, stdout+stderr output)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )
    lines = []
    for line in proc.stdout:
        line = line.rstrip()
        print(line)
        lines.append(line)
    proc.wait()
    return proc.returncode, "\n".join(lines)


def get_remotes():
    _, out = run(["rclone", "listremotes"])
    return [r.rstrip(":") for r in out.splitlines() if r.strip()]


def transferred_bytes(output):
    """Parse total bytes transferred from rclone output."""
    import re
    for line in reversed(output.splitlines()):
        m = re.search(r"Transferred:\s+([\d.]+\s*\S+)\s*/\s*([\d.]+\s*\S+),\s*(\d+)%", line)
        if m:
            pct = int(m.group(3))
            return pct
    return 0


def main():
    print("=" * 50)
    print("  rclone Batch Sync")
    print("=" * 50)

    # Pick remote
    remotes = get_remotes()
    if not remotes:
        print("No rclone remotes found. Run 'rclone config' first.")
        sys.exit(1)

    print("\nAvailable remotes:")
    for i, r in enumerate(remotes, 1):
        print(f"  {i}. {r}")
    while True:
        try:
            choice = int(input("\nSelect remote (number): ")) - 1
            remote = remotes[choice]
            break
        except (ValueError, IndexError):
            print("Invalid choice, try again.")

    # Local folder
    local = input("\nLocal folder path: ").strip()
    if not local:
        print("No path entered.")
        sys.exit(1)

    # Remote folder
    remote_path = input(f"Remote folder on {remote} (e.g. data, photos/2024): ").strip().strip("/")
    remote_full = f"{remote}:{remote_path}" if remote_path else f"{remote}:"

    # Batch size
    batch = input("\nBatch size per run (e.g. 1G, 500M, 5G) [default: 2G]: ").strip() or "2G"

    print(f"\nSyncing: {local} → {remote_full}")
    print(f"Batch size: {batch}")
    print("-" * 50)

    base_cmd = [
        "rclone", "copy", local, remote_full,
        "--progress",
        "--transfers", "4",
        "--checkers", "8",
        "--ignore-existing",
        "--max-transfer", batch,
        "--drive-pacer-min-sleep", "10ms",
        "--drive-pacer-burst", "200",
        "--log-file", "/tmp/rclone-batch-sync.log",
        "--log-level", "INFO",
    ]

    run_number = 0
    while True:
        run_number += 1
        print(f"\n── Run {run_number} ──────────────────────────────────")
        start = time.time()

        rc, output = run(base_cmd)

        elapsed = int(time.time() - start)
        mins, secs = divmod(elapsed, 60)
        print(f"\nRun {run_number} finished in {mins}m {secs}s (exit code {rc})")

        # If nothing was transferred this run, we're done
        no_transfer = (
            "Transferred:            0 / 0, -" in output
            or "Transferred:   \t          0 B / 0 B" in output
            or ("Transferred:" in output and "0 B / 0 B" in output)
        )

        if no_transfer or rc == 9:  # rc=9 means --max-transfer limit hit cleanly
            if no_transfer:
                print("\n✓ All done — nothing left to transfer.")
            else:
                print(f"\nRun {run_number} complete. Pausing 3s before next batch…")
                time.sleep(3)
                continue

        if rc not in (0, 9) and not no_transfer:
            print(f"\nWarning: rclone exited with code {rc}. Check /tmp/rclone-batch-sync.log")
            retry = input("Continue anyway? (y/n): ").strip().lower()
            if retry != "y":
                break

        if no_transfer:
            break

        print(f"Pausing 3s before next batch…")
        time.sleep(3)

    print("\nLog saved to: /tmp/rclone-batch-sync.log")
    print("Done.")


if __name__ == "__main__":
    main()
