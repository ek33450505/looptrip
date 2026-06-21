"""tests/test_proof.py - THE HEADLINE REGRESSION LOCK.

Locks the verified Phase-1 ground truth: replaying the two committed runaway
sessions through the detector saves $320.16 + $472.80 = $792.96 by tripping at
the second workflow-subagent dispatch. These assertions are the proof's safety
net - if the fixture or detector ever drifts, this file goes red.

The CLI smoke tests cover the three surfaces a user touches: ``--version``,
``proof``, and ``scan``.
"""

from __future__ import annotations

from looptrip import __version__, cli
from looptrip.proof import run_proof

SESSION_A = "2e6c0288-b8db-46de-8ec4-164e3685a739"
SESSION_B = "da27b414-f9f1-4c91-bd50-1a6096555066"


def _by_session_id(result):
    """Index the proof result's per-session entries by their session id."""
    return {entry["session_id"]: entry for entry in result["sessions"]}


# ---------------------------------------------------------------------------
# Headline regression lock - the per-session and grand-total savings
# ---------------------------------------------------------------------------

def test_run_proof_reproduces_per_session_savings():
    """Each session reproduces its verified prevented cost within $0.01."""
    entries = _by_session_id(run_proof())
    assert abs(entries[SESSION_A]["prevented_cost"] - 320.16) < 0.01
    assert abs(entries[SESSION_B]["prevented_cost"] - 472.80) < 0.01


def test_run_proof_grand_total_is_792_96():
    """The grand total saved is $792.96 within $0.01."""
    result = run_proof()
    assert abs(result["grand_total_saved"] - 792.96) < 0.01


def test_run_proof_identifies_workflow_subagent_loop_and_trip_points():
    """The costliest pathology per session is the workflow-subagent loop,
    tripping at the 2nd dispatch (raw_id 555 / 1080)."""
    entries = _by_session_id(run_proof())

    a = entries[SESSION_A]
    assert a["loop_agent"] == "workflow-subagent"
    assert a["total_dispatches"] == 54
    assert a["first_dispatch_raw_id"] == 554
    assert a["trip_dispatch_raw_id"] == 555
    assert a["prevented_runs"] == 52

    b = entries[SESSION_B]
    assert b["loop_agent"] == "workflow-subagent"
    assert b["total_dispatches"] == 49
    assert b["first_dispatch_raw_id"] == 1079
    assert b["trip_dispatch_raw_id"] == 1080
    assert b["prevented_runs"] == 47


def test_run_proof_self_check_raises_on_drift(monkeypatch):
    """The internal self-check trips an AssertionError if a saved amount drifts."""
    import looptrip.proof as proof_mod

    bogus = dict(proof_mod.EXPECTED_SAVED)
    bogus[SESSION_A] = 0.0  # force a mismatch against the real $320.16
    monkeypatch.setattr(proof_mod, "EXPECTED_SAVED", bogus)
    try:
        run_proof()
    except AssertionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("run_proof should fail loudly when savings drift")


# ---------------------------------------------------------------------------
# CLI smoke - version / proof / scan
# ---------------------------------------------------------------------------

def test_cli_version_returns_zero_and_prints_version(capsys):
    """``looptrip --version`` prints ``looptrip <version>`` and returns 0."""
    rc = cli.main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "looptrip" in out
    assert __version__ in out


def test_cli_proof_returns_zero_and_prints_headline(capsys):
    """``looptrip proof`` runs the proof, prints the $792.96 headline, exits 0."""
    rc = cli.main(["proof"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "792.96" in out
    assert "workflow-subagent" in out


def test_cli_scan_fixture_reports_workflow_subagent(capsys):
    """``looptrip scan fixture:<id>`` surfaces the workflow-subagent runaway."""
    rc = cli.main(["scan", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "workflow-subagent" in out


def test_cli_scan_unknown_source_errors_with_nonzero_exit(capsys):
    """An unknown source scheme yields a clean stderr error and nonzero exit."""
    rc = cli.main(["scan", "bogus:whatever"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "unknown source" in err.lower()
