"""Adversarial edge/error tests for the looptrip CLI (src/looptrip/cli.py).

Companion to the CLI smoke tests in ``test_proof.py``. This file hammers the
error and degenerate paths: unknown scheme, a scheme-less ("malformed") source,
the no-subcommand invocation, and the clean empty-scan branch — plus exact
assertions on the three happy surfaces (``--version``, ``proof``, ``scan``)
including that ``scan`` surfaces the runaway COSTLIEST-FIRST.

All output is asserted via capsys. stdlib + pytest only; the fixture-backed
``scan``/``proof`` paths never touch a real cast.db.
"""

from __future__ import annotations

from looptrip import __version__, cli

SESSION_A = "2e6c0288-b8db-46de-8ec4-164e3685a739"


def _saved_amounts(table_out):
    """Pull the prevented_cost ($) column from each data row of a scan table.

    The table layout is: header line, dashed-rule line, then one row per report.
    Each report row's last whitespace-delimited token is ``$<amount>``.
    """
    lines = [ln for ln in table_out.splitlines() if ln.strip()]
    # Drop the header (contains 'prevented_cost') and the dashed rule beneath it.
    data_rows = []
    seen_header = False
    for ln in lines:
        if "prevented_cost" in ln:
            seen_header = True
            continue
        if seen_header and set(ln.strip()) == {"-"}:
            continue
        if seen_header:
            data_rows.append(ln)
    amounts = []
    for ln in data_rows:
        token = ln.split()[-1]  # e.g. "$320.16"
        amounts.append(float(token.replace("$", "").replace(",", "")))
    return data_rows, amounts


# ---------------------------------------------------------------------------
# Happy surfaces — exact assertions
# ---------------------------------------------------------------------------

def test_version_returns_zero_and_prints_exact_version_line(capsys):
    """``looptrip --version`` returns 0 and prints ``looptrip <version>``."""
    rc = cli.main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"looptrip {__version__}" in out


def test_proof_returns_zero_and_prints_792_96_headline(capsys):
    """``looptrip proof`` returns 0 and prints the $792.96 grand-total headline."""
    rc = cli.main(["proof"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "792.96" in out
    assert "GRAND TOTAL" in out
    assert "workflow-subagent" in out


def test_scan_fixture_surfaces_workflow_subagent_costliest_first(capsys):
    """``scan fixture:<A>`` returns 0 and lists the runaway COSTLIEST-FIRST.

    Session A yields several reports; the workflow-subagent loop ($320.16) must
    be the top data row, and the prevented_cost column must be non-increasing.
    """
    rc = cli.main(["scan", f"fixture:{SESSION_A}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "workflow-subagent" in out

    data_rows, amounts = _saved_amounts(out)
    assert data_rows, "expected at least one report row in the scan table"
    # Costliest first: the top row is the workflow-subagent runaway.
    assert data_rows[0].split()[0] == "workflow-subagent"
    assert amounts[0] == 320.16
    # Descending prevented_cost order (the detect() DESC sort, surfaced by CLI).
    assert amounts == sorted(amounts, reverse=True)


# ---------------------------------------------------------------------------
# Error / degenerate paths
# ---------------------------------------------------------------------------

def test_scan_unknown_scheme_returns_nonzero_and_writes_stderr(capsys):
    """An unknown scheme returns nonzero, writes to stderr, and prints no table."""
    rc = cli.main(["scan", "bogus:whatever"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "unknown source scheme" in captured.err.lower()
    assert "bogus" in captured.err
    # A clean error: nothing leaks onto stdout.
    assert captured.out == ""


def test_scan_malformed_source_without_scheme_returns_nonzero(capsys):
    """A scheme-less source ('noscheme') is 'malformed' — a DISTINCT error path
    from the unknown-scheme case — returning nonzero with a stderr message."""
    rc = cli.main(["scan", "noscheme"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "malformed" in captured.err.lower()
    # Distinguished from the unknown-scheme branch.
    assert "unknown source scheme" not in captured.err.lower()
    assert captured.out == ""


def test_scan_source_with_empty_session_id_is_malformed(capsys):
    """A scheme with an empty id ('fixture:') is malformed, not an empty scan."""
    rc = cli.main(["scan", "fixture:"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "malformed" in err.lower()


def test_scan_unknown_fixture_session_is_clean_empty_scan(capsys):
    """``scan fixture:<unknown>`` returns 0 with the 'no pathologies' line.

    The zero-report branch is a successful, quiet scan — not an error.
    """
    rc = cli.main(["scan", "fixture:does-not-exist-0000"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no duplicate-work pathologies detected" in captured.out
    assert "fixture:does-not-exist-0000" in captured.out
    assert captured.err == ""


def test_no_subcommand_prints_help_and_returns_zero(capsys):
    """``looptrip`` with no subcommand prints help to stdout and returns 0.

    Pins the ACTUAL behavior: not an error, no stderr — argparse help on stdout.
    """
    rc = cli.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert "looptrip" in captured.out
    assert captured.err == ""
