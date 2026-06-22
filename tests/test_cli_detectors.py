"""Tests for the looptrip CLI detector flags: ``scan --all``, ``scan --detectors``, and ``attribute``.

Covers the Phase 3 (counterfactual attribution) and Phase 6 (detector flags) features:
  - ``scan --all`` and ``scan --detectors LIST`` (mutually exclusive).
  - ``scan`` default behavior (no flags: duplicate-work only, no 'kind' column).
  - ``attribute`` subcommand with detector selection (same flags as ``scan``).
  - Error paths: unknown detectors, conflicting flags, malformed sources.
  - Output format validation: table columns, sort order, detail lines.

All output is captured via pytest ``capsys``. No real cast.db; fixture-backed tests only.
Stdlib + pytest; no external dependencies.
"""

from __future__ import annotations

import pytest

from looptrip import __version__, cli

SESSION_A = "2e6c0288-b8db-46de-8ec4-164e3685a739"


def _table_rows_with_kind_column(table_out):
    """Extract data rows from a scan/attribute table that includes a 'kind' column.

    The table layout is: header line, dashed-rule line, then one row per report.
    Each report row is: kind, agent, occurrences, prevented_runs, prevented_cost
    (or for attribute: kind, agent, verdict, decisive, tested).

    Returns a tuple (data_rows, amounts) where amounts is the list of prevented_cost
    values (last whitespace token, stripped of "$" and ",").
    """
    lines = [ln for ln in table_out.splitlines() if ln.strip()]
    # Drop the header (contains 'kind') and the dashed rule beneath it.
    data_rows = []
    seen_header = False
    for ln in lines:
        if "kind" in ln:
            seen_header = True
            continue
        if seen_header and set(ln.strip()) == {"-"}:
            continue
        if seen_header:
            data_rows.append(ln)
    amounts = []
    for ln in data_rows:
        token = ln.split()[-1]  # e.g. "$320.16"
        # Handle prevented_cost format: "$<amount>" with optional commas
        if token.startswith("$"):
            amounts.append(float(token.replace("$", "").replace(",", "")))
    return data_rows, amounts


# ---------------------------------------------------------------------------
# scan --all: all detectors, kind column present
# ---------------------------------------------------------------------------

def test_scan_all_returns_zero_with_kind_column_and_data(capsys):
    """``scan --all`` returns 0; header contains 'kind'; stdout lists pathologies."""
    rc = cli.main(["scan", "--all", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out
    # Kind column must be present in the header.
    assert "kind" in out
    # Fixture A contains duplicate_work, ping_pong, non_termination.
    assert "duplicate_work" in out
    assert "ping_pong" in out
    assert "non_termination" in out
    # Check for the key agent (workflow-subagent is the costliest).
    assert "workflow-subagent" in out


def test_scan_all_costliest_first_sort_order(capsys):
    """``scan --all`` sorts by prevented_cost descending (costliest first).

    Parse the trailing "$<amount>" token from each data row and assert the
    list is in descending order.
    """
    rc = cli.main(["scan", "--all", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out

    data_rows, amounts = _table_rows_with_kind_column(out)
    assert data_rows, "expected at least one report row"
    # Costliest first: amounts must be non-increasing.
    assert amounts == sorted(amounts, reverse=True)


def test_scan_detectors_duplicate_work_ping_pong(capsys):
    """``scan --detectors duplicate_work,ping_pong`` includes only those kinds.

    Table has 'kind' column; rows contain only duplicate_work and ping_pong,
    never non_termination.
    """
    rc = cli.main(["scan", "--detectors", "duplicate_work,ping_pong", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "kind" in out
    assert "duplicate_work" in out
    assert "ping_pong" in out
    # non_termination must not appear in the data.
    assert "non_termination" not in out


def test_scan_detectors_single_kind_ping_pong(capsys):
    """``scan --detectors ping_pong`` (single detector) returns rows for ping_pong only.

    Rows exclude duplicate_work and non_termination.
    """
    rc = cli.main(["scan", "--detectors", "ping_pong", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "kind" in out
    assert "ping_pong" in out
    # The other pathology kinds must not appear in the data rows.
    assert "duplicate_work" not in out
    assert "non_termination" not in out


def test_scan_detectors_unknown_kind_returns_rc2_with_stderr(capsys):
    """``scan --detectors bogus`` (unknown detector) returns rc 2.

    Prints an error to stderr containing (case-insensitive) 'unknown detector'
    and the bad token 'bogus'. stdout is empty.
    """
    rc = cli.main(["scan", "--detectors", "bogus", f"fixture:{SESSION_A}"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown detector" in captured.err.lower()
    assert "bogus" in captured.err
    assert captured.out == ""


def test_scan_all_and_detectors_mutually_exclusive_returns_rc2(capsys):
    """``scan --all --detectors duplicate_work`` returns rc 2 (conflicting flags).

    argparse error: "not allowed with". stdout is empty.
    """
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["scan", "--all", "--detectors", "duplicate_work", f"fixture:{SESSION_A}"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "not allowed with" in captured.err
    assert captured.out == ""


def test_scan_detectors_deadlock_empty_on_fixture_a(capsys):
    """``scan --detectors deadlock`` on fixture A finds no deadlock pathologies.

    Returns 0 with the message 'no pathologies detected in <source>'.
    No data rows; stderr is empty.
    """
    rc = cli.main(["scan", "--detectors", "deadlock", f"fixture:{SESSION_A}"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no pathologies detected in" in captured.out
    assert SESSION_A in captured.out
    # Verify the table header (with 'kind' column) does not appear.
    assert "duplicate-work" not in captured.out
    assert captured.err == ""


def test_scan_default_no_flags_regression_guard_no_kind_column(capsys):
    """``scan`` (no flags, default behavior) does NOT include 'kind' column.

    Regression test: the header line must not contain 'kind'. The empty message
    is 'no duplicate-work pathologies detected' (not 'no pathologies detected').
    """
    rc = cli.main(["scan", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out
    # The default table does NOT have a 'kind' column.
    lines = out.splitlines()
    header = None
    for ln in lines:
        if "agent" in ln and "occurrences" in ln:
            header = ln
            break
    assert header is not None
    assert "kind" not in header
    # Check the specific message for default mode.
    assert "duplicate-work pathologies detected" in out or "workflow-subagent" in out


# ---------------------------------------------------------------------------
# attribute: verdict table, detector selection, and detail lines
# ---------------------------------------------------------------------------

def test_attribute_default_returns_zero_with_verdict_table(capsys):
    """``attribute`` (default, duplicate-work only) returns 0.

    Prints a verdict table with columns: kind, agent, verdict, decisive, tested.
    Fixture A default includes workflow-subagent (verdict 'overdetermined').
    """
    rc = cli.main(["attribute", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verdict" in out
    assert "decisive" in out
    assert "tested" in out
    # Check for the key pathology: workflow-subagent is overdetermined.
    assert "workflow-subagent" in out
    assert "overdetermined" in out


def test_attribute_all_includes_all_detector_kinds(capsys):
    """``attribute --all`` runs all detectors before attribution.

    Output includes multiple kinds; fixture A default includes non_termination.
    """
    rc = cli.main(["attribute", "--all", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verdict" in out
    # Fixture A --all should surface non_termination and various verdict types.
    assert "non_termination" in out
    # At least one verdict type should appear (unique, overdetermined, or multiple).
    assert any(v in out for v in ["unique", "overdetermined", "multiple"])


def test_attribute_unknown_source_returns_zero_with_empty_message(capsys):
    """``attribute`` with a non-existent source returns 0, empty-report message.

    No errors; clean exit with 'no pathologies detected to attribute in <source>'.
    """
    rc = cli.main(["attribute", "fixture:does-not-exist-0000"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no pathologies detected to attribute in" in out
    assert "does-not-exist-0000" in out


def test_attribute_malformed_source_returns_rc2(capsys):
    """``attribute`` with a scheme-less source ('noscheme') returns rc 2.

    stderr contains (case-insensitive) 'malformed'; stdout is empty.
    """
    rc = cli.main(["attribute", "noscheme"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "malformed" in captured.err.lower()
    assert captured.out == ""


def test_attribute_detectors_unknown_kind_returns_rc2(capsys):
    """``attribute --detectors bogus`` returns rc 2 on unknown detector."""
    rc = cli.main(["attribute", "--detectors", "bogus", f"fixture:{SESSION_A}"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown detector" in captured.err.lower()


def test_attribute_honest_framing_guard_overdetermined_detail_line(capsys):
    """``attribute`` default (fixture A) includes the 'No single decisive handoff' phrase.

    Fixture A's workflow-subagent pathology is overdetermined (multiple equally
    decisive handoffs). The detail line must begin with or contain exactly the
    phrase 'No single decisive handoff' (the honest framing, never blaming one).
    """
    rc = cli.main(["attribute", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out
    # The overdetermined detail line begins exactly "No single decisive handoff".
    assert "No single decisive handoff" in out
