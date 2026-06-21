"""looptrip.detectors — Phase-2 pathology detectors subpackage.

This package exposes the public Phase-2 surface: shared types (constants,
:class:`~looptrip.detectors.types.PathologyReport`,
:class:`~looptrip.detectors.types.DetectionConfig`,
:func:`~looptrip.detectors.types.resolve_config`) and, once each detector
module exists, the individual ``detect_*`` free functions.

Import safety note
------------------
The detector free functions (``detect_ping_pong``, ``detect_deadlock``,
``detect_non_termination``) are intentionally NOT re-exported here.  Those
modules depend on :mod:`looptrip.detectors.types`, which is a submodule of
this very package; Python executes *this* ``__init__.py`` whenever any
submodule inside ``looptrip.detectors`` is imported.  If this ``__init__.py``
tried to import the not-yet-created detector modules it would fail at package
install time / unit-1 test time.  More importantly, once those modules exist,
re-exporting them here would run their module-level code unconditionally on
every ``import looptrip.detectors.types`` — importing unused detectors as a
side effect.  Instead, the final ``detector.py`` re-export layer (which
already imports the detector functions explicitly) is the canonical place for
top-of-tree consumers; direct submodule imports
(``from looptrip.detectors.ping_pong import detect_ping_pong``) work as
expected without any action here.

Similarly, this package MUST NOT import :mod:`looptrip.detector` (the
top-level detector module) — ``detector.py`` imports from this package,
so a reverse import would be a circular dependency.
"""

from __future__ import annotations

from looptrip.detectors.types import (
    ALL_DETECTORS,
    KIND_DEADLOCK,
    KIND_DUPLICATE_WORK,
    KIND_NON_TERMINATION,
    KIND_PING_PONG,
    DetectionConfig,
    PathologyReport,
    resolve_config,
)

__all__ = [
    "PathologyReport",
    "DetectionConfig",
    "resolve_config",
    "KIND_DUPLICATE_WORK",
    "KIND_PING_PONG",
    "KIND_DEADLOCK",
    "KIND_NON_TERMINATION",
    "ALL_DETECTORS",
]
