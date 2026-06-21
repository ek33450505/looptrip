#!/usr/bin/env python3
"""run_proof.py — thin shim for backward-compatible standalone execution.

The proof logic has moved to ``looptrip.proof`` (a proper package module) so
that ``looptrip proof`` works under a normal pip install without needing the
repo checkout on sys.path.

This shim preserves ``python proof/run_proof.py`` for convenience: it adds
``src/`` to sys.path only when ``import looptrip`` fails (i.e. when run
directly from the repo without PYTHONPATH=src), then re-exports everything
from ``looptrip.proof`` so existing importers keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import looptrip  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - exercised only when run standalone
    _SRC = Path(__file__).resolve().parent.parent / "src"
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))

# Re-export everything importers may reference from this module.
from looptrip.proof import (  # noqa: E402
    EXPECTED_SAVED,
    EXPECTED_TOTAL,
    SESSIONS,
    TOLERANCE,
    TRANSPARENCY_NOTE,
    _top_report,
    format_proof,
    main,
    run_proof,
)

if __name__ == "__main__":
    raise SystemExit(main())
