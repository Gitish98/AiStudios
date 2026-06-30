"""
Stdlib test runner — no pytest required (pytest is optional; these tests are
also pytest-discoverable). Runs every test_* function in the tests package.

Usage: python -m tests.run     (or: python tests/run.py)
"""

from __future__ import annotations

import importlib
import traceback
from pathlib import Path


def _discover():
    mods = []
    here = Path(__file__).parent
    for f in sorted(here.glob("test_*.py")):
        mods.append(importlib.import_module(f"tests.{f.stem}"))
    return mods


def main() -> int:
    passed = failed = 0
    failures = []
    for mod in _discover():
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            label = f"{mod.__name__}.{name}"
            try:
                fn()
                passed += 1
                print(f"  PASS  {label}")
            except Exception:
                failed += 1
                failures.append((label, traceback.format_exc()))
                print(f"  FAIL  {label}")
    print("\n" + "─" * 56)
    print(f"  {passed} passed, {failed} failed")
    if failures:
        print("─" * 56)
        for label, tb in failures:
            print(f"\n### {label}\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
