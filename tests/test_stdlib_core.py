"""tests/test_stdlib_core.py — guards the headline stdlib-purity constraint.

looptrip's core package MUST import with zero third-party dependencies; the
OpenTelemetry integration is an OPTIONAL, import-guarded extra.  CI normally
installs the ``[otel]`` extra, so a leaked top-level ``opentelemetry`` import in
a *core* module would crash ``pip install looptrip`` users (who have no otel)
while CI stayed green.

These tests assert BOTH states positively, inside a hermetic child interpreter
whose import system is forced to behave as if ``opentelemetry`` were never
installed: a ``sys.meta_path`` finder raises :class:`ImportError` for every name
under the ``opentelemetry`` namespace.  Running in a subprocess via
``sys.executable`` means the assertions hold regardless of whether the otel
extra is installed in the *parent* interpreter — no monkeypatching of the live
process, no import-cache leakage between cases.

The complementary positive case (``looptrip.otel_live`` *does* import when the
SDK is present) runs only when the otel extra is actually installed.

No network access and no writes to ``$HOME`` — only ``sys.executable -c`` child
processes.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

# Core modules that MUST import with opentelemetry absent.  This is the headline
# "pure-stdlib core" surface: the CLI, the detector pipeline, the offline
# adapters, proof/normalize/attribution, and the detectors subpackage (plus
# every submodule).  None of these may import a third-party package at module
# load time.
_CORE_MODULES = [
    "looptrip",
    "looptrip.cli",
    "looptrip.detector",
    "looptrip.adapters.otel",
    "looptrip.adapters.cast_db",
    "looptrip.proof",
    "looptrip.normalize",
    "looptrip.attribution",
    "looptrip.detectors",
    "looptrip.detectors.types",
    "looptrip.detectors._shared",
    "looptrip.detectors.deadlock",
    "looptrip.detectors.non_termination",
    "looptrip.detectors.ping_pong",
]

# Preamble injected at the top of every blocked child interpreter: purge any
# already-imported opentelemetry modules, then install a meta_path finder that
# makes *any* future ``opentelemetry`` (or submodule) import raise ImportError.
# This simulates a machine where the optional otel extra was never installed,
# even though CI / this dev env has it present.
_BLOCKER_PREAMBLE = '''
import sys

class _OTelBlocker:
    # A meta_path finder whose only job is to veto the opentelemetry namespace.
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "opentelemetry" or fullname.startswith("opentelemetry."):
            raise ImportError(
                "opentelemetry blocked by stdlib-core guard: " + fullname
            )
        return None

for _name in [m for m in list(sys.modules)
              if m == "opentelemetry" or m.startswith("opentelemetry.")]:
    del sys.modules[_name]
sys.meta_path.insert(0, _OTelBlocker())
'''


def _run_with_otel_blocked(body: str) -> "subprocess.CompletedProcess[str]":
    """Run ``body`` in a child interpreter that cannot import opentelemetry.

    The child first installs the import-blocking ``sys.meta_path`` finder
    (:data:`_BLOCKER_PREAMBLE`) and then executes ``body``.  Markers are passed
    back to the parent test via stdout and the process exit code.
    """
    script = _BLOCKER_PREAMBLE + "\n" + body
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _otel_extra_installed() -> bool:
    """True when the optional opentelemetry SDK is importable in this env."""
    return importlib.util.find_spec("opentelemetry") is not None


def test_harness_actually_blocks_opentelemetry():
    """Sanity: the meta_path blocker really makes ``import opentelemetry`` fail.

    Without this guard the rest of the suite would silently degrade into a
    no-op on any machine that happens not to have the otel extra installed.
    """
    body = (
        "try:\n"
        "    import opentelemetry  # noqa: F401\n"
        "except ImportError:\n"
        "    print('BLOCKED-OK')\n"
        "    raise SystemExit(0)\n"
        "print('NOT-BLOCKED')\n"
        "raise SystemExit(1)\n"
    )
    proc = _run_with_otel_blocked(body)
    assert proc.returncode == 0, (
        "blocker did not veto opentelemetry:\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "BLOCKED-OK" in proc.stdout


@pytest.mark.parametrize("module_name", _CORE_MODULES)
def test_core_module_imports_without_opentelemetry(module_name):
    """Every core module imports cleanly with opentelemetry absent.

    A leaked top-level ``import opentelemetry`` (or any other third-party
    import) in a core module surfaces here as a non-zero child exit code.
    """
    body = (
        "import importlib\n"
        "try:\n"
        f"    importlib.import_module({module_name!r})\n"
        "except BaseException as exc:\n"
        "    print('IMPORT-FAIL: ' + type(exc).__name__ + ': ' + str(exc))\n"
        "    raise SystemExit(1)\n"
        "print('IMPORT-OK')\n"
    )
    proc = _run_with_otel_blocked(body)
    assert proc.returncode == 0, (
        f"{module_name} must import with opentelemetry blocked, but failed:\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "IMPORT-OK" in proc.stdout


def test_otel_live_raises_importerror_when_otel_blocked():
    """``import looptrip.otel_live`` MUST raise ImportError without the SDK.

    The live integration is the one part of the package that legitimately
    depends on the optional extra; importing it without opentelemetry installed
    is expected to fail loudly (an ImportError), never to silently degrade.
    """
    body = (
        "import importlib\n"
        "try:\n"
        "    importlib.import_module('looptrip.otel_live')\n"
        "except ImportError:\n"
        "    print('OTEL-LIVE-BLOCKED-OK')\n"
        "    raise SystemExit(0)\n"
        "except BaseException as exc:\n"
        "    print('WRONG-ERROR: ' + type(exc).__name__ + ': ' + str(exc))\n"
        "    raise SystemExit(2)\n"
        "print('OTEL-LIVE-DID-NOT-RAISE')\n"
        "raise SystemExit(3)\n"
    )
    proc = _run_with_otel_blocked(body)
    assert proc.returncode == 0, (
        "looptrip.otel_live must raise ImportError when otel is blocked:\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "OTEL-LIVE-BLOCKED-OK" in proc.stdout


@pytest.mark.skipif(
    not _otel_extra_installed(),
    reason="opentelemetry (the [otel] extra) is not installed in this environment",
)
def test_otel_live_imports_when_otel_available():
    """Positive case: with the SDK present, ``looptrip.otel_live`` imports cleanly.

    Run in a child interpreter WITHOUT the blocker so the real opentelemetry SDK
    is used.  Skipped when the otel extra is not installed (e.g. the
    ``core-stdlib-only`` CI job, which installs ``.[dev]`` only).
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib; "
            "importlib.import_module('looptrip.otel_live'); "
            "print('OTEL-LIVE-OK')",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "looptrip.otel_live must import when otel is installed:\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "OTEL-LIVE-OK" in proc.stdout
