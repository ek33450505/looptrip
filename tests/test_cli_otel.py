"""tests/test_cli_otel.py — CLI integration tests for the OTel source path.

Covers T7-T13 from the adversarial review findings (Phase 4a fixes).
All tests invoke ``looptrip.cli.main([...])`` and capture stdout/stderr via
``capsys``.  No real cast.db; OTel flat fixture or tmp_path files only.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from looptrip.cli import main

_FLAT_FIXTURE = str(
    pathlib.Path(__file__).parent / "fixtures" / "otel_genai_handoff_spans.json"
)

# ---------------------------------------------------------------------------
# T7: otel flat fixture with #scenario returns 0 and reports the expected kind
# ---------------------------------------------------------------------------


def test_scan_otel_deadlock_scenario_returns_zero_with_deadlock_kind(capsys):
    """``scan otel:<flat>#deadlock --all`` returns 0; stdout contains 'deadlock'."""
    rc = main(["scan", f"otel:{_FLAT_FIXTURE}#deadlock", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "deadlock" in out


# ---------------------------------------------------------------------------
# T8: otel .jsonl source returns 0
# ---------------------------------------------------------------------------


def test_scan_otel_jsonl_returns_zero(tmp_path, capsys):
    """``scan otel:<path>.jsonl --all`` returns 0 for a valid JSONL of flat handoff spans."""
    spans = [
        {
            "span_id": "t8-001",
            "start_time": "2024-06-01T00:00:01Z",
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-a"},
        },
        {
            "span_id": "t8-002",
            "start_time": "2024-06-01T00:00:02Z",
            "attributes": {"gen_ai.agent.handoff.source.name": "agent-b"},
        },
    ]
    p = tmp_path / "test.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in spans))
    rc = main(["scan", f"otel:{p}", "--all"])
    assert rc == 0


# ---------------------------------------------------------------------------
# T9: missing file returns rc 2, clean error line, no traceback
# ---------------------------------------------------------------------------


def test_scan_otel_missing_file_returns_rc2_clean_error(capsys):
    """``scan otel:/no/such/file.json --all`` returns 2; stderr starts with 'error:';
    'Traceback' does not appear in stderr.
    """
    rc = main(["scan", "otel:/no/such/file.json", "--all"])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# T10: multi-scenario file with no #scenario returns rc 2, clean error
# ---------------------------------------------------------------------------


def test_scan_otel_multi_scenario_no_hash_returns_rc2(capsys):
    """``scan otel:<multi-scenario-file> --all`` (no #scenario) returns 2 with 'error:'."""
    rc = main(["scan", f"otel:{_FLAT_FIXTURE}", "--all"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err


# ---------------------------------------------------------------------------
# T11: scenario selection on .jsonl returns rc 2 (FIX 6)
# ---------------------------------------------------------------------------


def test_scan_otel_jsonl_with_scenario_returns_rc2(tmp_path, capsys):
    """``scan otel:<path>.jsonl#deadlock --all`` returns 2; scenario-on-jsonl rejected."""
    span = {
        "span_id": "t11-001",
        "start_time": "2024-06-01T00:00:01Z",
        "attributes": {"gen_ai.agent.handoff.source.name": "agent-x"},
    }
    p = tmp_path / "test.jsonl"
    p.write_text(json.dumps(span))
    rc = main(["scan", f"otel:{p}#deadlock", "--all"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err


# ---------------------------------------------------------------------------
# T12: non-handoff OTLP span is filtered, not crashed on (FIX 1 at CLI level)
# ---------------------------------------------------------------------------


def test_scan_otel_non_handoff_span_filtered_no_crash(tmp_path, capsys):
    """``scan otel:<mixed-otlp>.json --all`` returns 0; non-handoff span is filtered;
    'Traceback' does not appear in stdout or stderr.
    """
    # OTLP doc: one non-handoff 'chat' span (no source.name) + two deadlock handoff spans.
    doc = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            # Non-handoff span: no gen_ai.agent.handoff.source.name
                            {
                                "spanId": "nonhandoff001",
                                "startTimeUnixNano": "1717200001000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.operation.name",
                                        "value": {"stringValue": "chat"},
                                    },
                                    {
                                        "key": "gen_ai.request.model",
                                        "value": {"stringValue": "claude"},
                                    },
                                ],
                            },
                            # Deadlock handoff span A
                            {
                                "spanId": "0000000000000001",
                                "startTimeUnixNano": "1717200002000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.agent.handoff.source.name",
                                        "value": {"stringValue": "code-writer"},
                                    },
                                    {
                                        "key": "gen_ai.agent.handoff.target.name",
                                        "value": {"stringValue": "code-reviewer"},
                                    },
                                    {
                                        "key": "gen_ai.agent.handoff.state",
                                        "value": {"stringValue": "blocked"},
                                    },
                                ],
                            },
                            # Deadlock handoff span B
                            {
                                "spanId": "0000000000000002",
                                "startTimeUnixNano": "1717200003000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.agent.handoff.source.name",
                                        "value": {"stringValue": "code-reviewer"},
                                    },
                                    {
                                        "key": "gen_ai.agent.handoff.target.name",
                                        "value": {"stringValue": "code-writer"},
                                    },
                                    {
                                        "key": "gen_ai.agent.handoff.state",
                                        "value": {"stringValue": "blocked"},
                                    },
                                ],
                            },
                        ]
                    }
                ]
            }
        ]
    }
    p = tmp_path / "mixed.json"
    p.write_text(json.dumps(doc))
    rc = main(["scan", f"otel:{p}", "--all"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


# ---------------------------------------------------------------------------
# T13: flat-wrapper span missing start_time/span_id returns rc 2, clean error
# ---------------------------------------------------------------------------


def test_scan_otel_missing_start_time_returns_rc2_clean_error(tmp_path, capsys):
    """A flat-wrapper JSON with a span missing start_time/span_id returns 2 with 'error:',
    no traceback (FIX 2 at CLI level).
    """
    doc = {
        "spans": [
            {
                # Missing start_time and span_id — only has attributes.
                "attributes": {"gen_ai.agent.handoff.source.name": "agent-a"}
            }
        ]
    }
    p = tmp_path / "bad_span.json"
    p.write_text(json.dumps(doc))
    rc = main(["scan", f"otel:{p}", "--all"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err
