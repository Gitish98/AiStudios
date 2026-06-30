#!/usr/bin/env python3
"""
One-command home setup. Cross-platform (Windows / macOS / Linux).

When you get to your computer:

    git clone https://github.com/Gitish98/AiStudios.git
    cd AiStudios
    python bootstrap.py

It creates your local config from the templates, installs dependencies, runs the
test suite, and prints a status report. It is idempotent and NEVER overwrites an
existing config.yaml / .env / watchlist.yaml. The only manual step left is logging
IB Gateway into your paper account (see docs/09-home-setup.md).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _force_utf8_console() -> None:
    """Windows consoles often default to a legacy code page (cp1252) that cannot
    encode the box-drawing / emoji characters this script prints — which crashes
    a fresh setup on the very first line. Reconfigure stdout/stderr to UTF-8 where
    the runtime supports it (Python 3.7+); a no-op elsewhere."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def step(msg: str) -> None:
    print(f"\n{'─' * 60}\n  {msg}\n{'─' * 60}")


def copy_if_missing(example: str, target: str) -> None:
    e, t = ROOT / example, ROOT / target
    if t.exists():
        print(f"  keep   {target}  (already exists — not overwritten)")
    elif e.exists():
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(e, t)
        print(f"  create {target}  (from {example})")
    else:
        print(f"  !      {example} missing — skipped")


def run(cmd: list[str]) -> int:
    print("  $ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def main() -> int:
    _force_utf8_console()
    step("Python version")
    print("  Python", sys.version.split()[0])
    if sys.version_info < (3, 11):
        print("  ! Python 3.11+ recommended. Please upgrade before going further.")

    step("Local config (created from templates if missing)")
    copy_if_missing("config/config.example.yaml", "config/config.yaml")
    copy_if_missing("config/watchlist.example.yaml", "config/watchlist.yaml")
    copy_if_missing(".env.example", ".env")
    print("\n  Your config is set for: IBKR paper, CA margin account, ETF universe.")
    print("  Edit config/config.yaml to change risk limits or the watchlist.")

    step("Dependencies  (pip install -r requirements.txt)")
    rc = run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    if rc:
        print("\n  ! pip reported a problem above. The sim broker only needs "
              "pandas/numpy/pyyaml; ib_async is only needed to reach IB Gateway.")

    step("Tests")
    test_rc = run([sys.executable, "-m", "tests.run"])

    step("Status  (will fall back to SIM until IB Gateway is running)")
    run([sys.executable, "cli.py", "status"])

    step("You're set. Last step: connect IBKR paper")
    print(
        "  1. Install + open IB Gateway, log in with your PAPER credentials.\n"
        "  2. Configure → Settings → API: enable socket clients, port 4002,\n"
        "     Trusted IP 127.0.0.1, Read-Only API OFF.\n"
        "  3. Re-run:  python cli.py status\n"
        "     It should flip from 'BROKER: sim' to 'BROKER: ibkr_paper' with your\n"
        "     real paper balances. Then:  python cli.py dry-run  →  python cli.py run-cycle\n\n"
        "  Full details: docs/09-home-setup.md  and  docs/08-ibkr-gateway-runbook.md"
    )
    print("\n  Test suite:", "PASSED ✓" if test_rc == 0 else "had failures ✗ (see above)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
