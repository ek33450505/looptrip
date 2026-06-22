"""Tests for detector configuration and shared primitives.

This module tests the DetectionConfig class, resolve_config function, and the
_shared module helpers that underpin all Phase-2 detectors. Tests use direct
in-memory Event construction following the established pattern in test_detector.py.
"""

from __future__ import annotations

import dataclasses
import pytest
from dataclasses import FrozenInstanceError

from looptrip.detector import DetectionConfig, resolve_config
from looptrip.detectors._shared import (
    _canonical_cycle,
    _is_blocked,
    _is_exempt,
    _is_progress,
    _is_terminal,
    _state_key,
)
from looptrip.normalize import Event


def _event(
    raw_id: int,
    *,
    agent: str = "agent-a",
    tool: str = "dispatch",
    args_hash: str | None = None,
    ts: str | None = None,
    handoff_state: str | None = None,
    progress: bool = False,
    cost_usd: float = 1.0,
) -> Event:
    """Build a test Event."""
    return Event(
        agent=agent,
        tool=tool,
        args_hash=args_hash,
        ts=ts or f"2026-06-21T00:00:{raw_id:02d}Z",
        handoff_state=handoff_state,
        progress=progress,
        cost_usd=cost_usd,
        raw_id=raw_id,
    )


# ---------------------------------------------------------------------------
# DetectionConfig: defaults and immutability
# ---------------------------------------------------------------------------


def test_detection_config_has_legacy_defaults():
    """Default DetectionConfig matches Phase-1 duplicate-work defaults."""
    cfg = DetectionConfig()
    assert cfg.token_tolerance == 0.05
    assert cfg.threshold == 2
    assert cfg.idempotent_agents == frozenset()


def test_detection_config_is_frozen():
    """DetectionConfig instances are immutable (frozen+slots)."""
    cfg = DetectionConfig()
    with pytest.raises(FrozenInstanceError):
        cfg.threshold = 3


def test_detection_config_all_defaults():
    """All DetectionConfig fields have safe defaults."""
    cfg = DetectionConfig()
    assert cfg.idempotent_tools == frozenset()
    assert cfg.retry_allowed == frozenset()
    assert cfg.allowlist_agents == frozenset()
    assert cfg.allowlist_tools == frozenset()
    assert cfg.progress_markers == frozenset()
    assert cfg.terminal_states == frozenset()
    assert cfg.blocked_states == frozenset({"blocked", "waiting"})
    assert cfg.min_cycle_len == 2
    assert cfg.cycle_trip_count == 2
    assert cfg.use_handoff_edges is False
    assert cfg.window_size == 20
    assert cfg.plateau_ratio == 0.5
    assert cfg.plateau_unique_states is None
    assert cfg.state_key == "signature"


# ---------------------------------------------------------------------------
# DetectionConfig.__post_init__ validation
# ---------------------------------------------------------------------------


def test_post_init_rejects_window_size_zero():
    """window_size < 1 raises ValueError."""
    with pytest.raises(ValueError, match="window_size must be >= 1"):
        DetectionConfig(window_size=0)


def test_post_init_rejects_threshold_zero():
    """threshold < 1 raises ValueError."""
    with pytest.raises(ValueError, match="threshold must be >= 1"):
        DetectionConfig(threshold=0)


def test_post_init_rejects_cycle_trip_count_zero():
    """cycle_trip_count < 1 raises ValueError."""
    with pytest.raises(ValueError, match="cycle_trip_count must be >= 1"):
        DetectionConfig(cycle_trip_count=0)


def test_post_init_rejects_min_cycle_len_one():
    """min_cycle_len < 2 raises ValueError."""
    with pytest.raises(ValueError, match="min_cycle_len must be >= 2"):
        DetectionConfig(min_cycle_len=1)


def test_post_init_rejects_negative_token_tolerance():
    """token_tolerance < 0 raises ValueError."""
    with pytest.raises(ValueError, match="token_tolerance must be >= 0"):
        DetectionConfig(token_tolerance=-0.1)


def test_post_init_rejects_plateau_ratio_too_high():
    """plateau_ratio > 1.0 raises ValueError."""
    with pytest.raises(ValueError, match="plateau_ratio must be in \\[0.0, 1.0\\]"):
        DetectionConfig(plateau_ratio=1.5)


def test_post_init_rejects_plateau_ratio_negative():
    """plateau_ratio < 0.0 raises ValueError."""
    with pytest.raises(ValueError, match="plateau_ratio must be in \\[0.0, 1.0\\]"):
        DetectionConfig(plateau_ratio=-0.1)


def test_post_init_rejects_plateau_unique_states_zero():
    """plateau_unique_states < 1 when set raises ValueError."""
    with pytest.raises(ValueError, match="plateau_unique_states must be >= 1"):
        DetectionConfig(plateau_unique_states=0)


def test_post_init_rejects_invalid_state_key():
    """state_key not in {'signature','agent','handoff_state'} raises ValueError."""
    with pytest.raises(ValueError, match="state_key must be one of"):
        DetectionConfig(state_key="bogus")


def test_post_init_allows_valid_state_keys():
    """All three valid state_key values are accepted."""
    for key in ["signature", "agent", "handoff_state"]:
        cfg = DetectionConfig(state_key=key)
        assert cfg.state_key == key


def test_post_init_allows_boundary_values():
    """Boundary values that pass validation are accepted."""
    cfg = DetectionConfig(
        window_size=1,
        threshold=1,
        cycle_trip_count=1,
        min_cycle_len=2,
        token_tolerance=0.0,
        plateau_ratio=0.0,
        plateau_unique_states=1,
    )
    assert cfg.window_size == 1
    assert cfg.threshold == 1
    assert cfg.cycle_trip_count == 1
    assert cfg.plateau_ratio == 0.0
    assert cfg.plateau_unique_states == 1


# ---------------------------------------------------------------------------
# resolve_config
# ---------------------------------------------------------------------------


def test_resolve_config_none_config_returns_default():
    """resolve_config(None, {}) returns a default DetectionConfig."""
    cfg = resolve_config(None, {})
    assert cfg.token_tolerance == 0.05
    assert cfg.threshold == 2


def test_resolve_config_merges_legacy_knobs():
    """resolve_config applies threshold and token_tolerance overrides."""
    cfg = resolve_config(None, {"threshold": 3, "token_tolerance": 0.1})
    assert cfg.threshold == 3
    assert cfg.token_tolerance == 0.1
    assert cfg.idempotent_agents == frozenset()


def test_resolve_config_merges_idempotent_agents():
    """resolve_config applies idempotent_agents override."""
    cfg = resolve_config(None, {"idempotent_agents": frozenset({"agent-x"})})
    assert cfg.idempotent_agents == frozenset({"agent-x"})


def test_resolve_config_merges_new_detector_knobs():
    """resolve_config applies new-detector sensitivity knob overrides."""
    cfg = resolve_config(
        None,
        {
            "window_size": 10,
            "cycle_trip_count": 3,
            "use_handoff_edges": True,
        },
    )
    assert cfg.window_size == 10
    assert cfg.cycle_trip_count == 3
    assert cfg.use_handoff_edges is True


def test_resolve_config_rejects_unknown_knob():
    """resolve_config raises TypeError on unknown knob name."""
    with pytest.raises(TypeError, match="unexpected configuration knob"):
        resolve_config(None, {"nope": 1})


def test_resolve_config_rejects_multiple_unknown_knobs():
    """resolve_config reports all unknown knobs."""
    with pytest.raises(TypeError, match="unexpected configuration knob"):
        resolve_config(None, {"nope": 1, "invalid": 2})


def test_resolve_config_with_base_config():
    """resolve_config merges knobs onto a provided base config."""
    base = DetectionConfig(threshold=5, window_size=30)
    cfg = resolve_config(base, {"threshold": 3})
    assert cfg.threshold == 3
    assert cfg.window_size == 30


def test_resolve_config_empty_knobs_returns_base():
    """resolve_config({}) on a config returns that config unchanged."""
    base = DetectionConfig(threshold=5)
    cfg = resolve_config(base, {})
    assert cfg is base


# ---------------------------------------------------------------------------
# _is_progress
# ---------------------------------------------------------------------------


def test_is_progress_explicit_flag():
    """_is_progress returns True when event.progress is True."""
    event = _event(1, progress=True)
    cfg = DetectionConfig()
    assert _is_progress(event, cfg) is True


def test_is_progress_no_flag_no_marker():
    """_is_progress returns False when progress=False and handoff_state not in markers."""
    event = _event(1, progress=False, handoff_state="DONE")
    cfg = DetectionConfig()
    assert _is_progress(event, cfg) is False


def test_is_progress_handoff_state_in_markers():
    """_is_progress returns True when handoff_state is in progress_markers."""
    event = _event(1, progress=False, handoff_state="PROGRESS")
    cfg = DetectionConfig(progress_markers=frozenset({"PROGRESS"}))
    assert _is_progress(event, cfg) is True


def test_is_progress_handoff_state_not_in_markers():
    """_is_progress returns False when handoff_state is not in markers."""
    event = _event(1, progress=False, handoff_state="OTHER")
    cfg = DetectionConfig(progress_markers=frozenset({"PROGRESS"}))
    assert _is_progress(event, cfg) is False


def test_is_progress_handoff_state_none():
    """_is_progress returns False when handoff_state is None."""
    event = _event(1, progress=False, handoff_state=None)
    cfg = DetectionConfig(progress_markers=frozenset({"PROGRESS"}))
    assert _is_progress(event, cfg) is False


# ---------------------------------------------------------------------------
# _is_terminal
# ---------------------------------------------------------------------------


def test_is_terminal_in_terminal_states():
    """_is_terminal returns True when handoff_state is in terminal_states."""
    event = _event(1, handoff_state="DONE")
    cfg = DetectionConfig(terminal_states=frozenset({"DONE"}))
    assert _is_terminal(event, cfg) is True


def test_is_terminal_not_in_terminal_states():
    """_is_terminal returns False when handoff_state is not in terminal_states."""
    event = _event(1, handoff_state="BLOCKED")
    cfg = DetectionConfig(terminal_states=frozenset({"DONE"}))
    assert _is_terminal(event, cfg) is False


def test_is_terminal_handoff_state_none():
    """_is_terminal returns False when handoff_state is None."""
    event = _event(1, handoff_state=None)
    cfg = DetectionConfig(terminal_states=frozenset({"DONE"}))
    assert _is_terminal(event, cfg) is False


def test_is_terminal_empty_terminal_states():
    """_is_terminal returns False when terminal_states is empty (default)."""
    event = _event(1, handoff_state="DONE")
    cfg = DetectionConfig(terminal_states=frozenset())
    assert _is_terminal(event, cfg) is False


# ---------------------------------------------------------------------------
# _is_exempt
# ---------------------------------------------------------------------------


def test_is_exempt_idempotent_agent():
    """_is_exempt returns True when agent is in idempotent_agents."""
    event = _event(1, agent="safe-agent")
    cfg = DetectionConfig(idempotent_agents=frozenset({"safe-agent"}))
    assert _is_exempt(event, cfg) is True


def test_is_exempt_retry_allowed_agent():
    """_is_exempt returns True when agent is in retry_allowed."""
    event = _event(1, agent="retryable")
    cfg = DetectionConfig(retry_allowed=frozenset({"retryable"}))
    assert _is_exempt(event, cfg) is True


def test_is_exempt_allowlist_agent():
    """_is_exempt returns True when agent is in allowlist_agents."""
    event = _event(1, agent="whitelisted")
    cfg = DetectionConfig(allowlist_agents=frozenset({"whitelisted"}))
    assert _is_exempt(event, cfg) is True


def test_is_exempt_idempotent_tool():
    """_is_exempt returns True when tool is in idempotent_tools."""
    event = _event(1, tool="safe-tool")
    cfg = DetectionConfig(idempotent_tools=frozenset({"safe-tool"}))
    assert _is_exempt(event, cfg) is True


def test_is_exempt_allowlist_tool():
    """_is_exempt returns True when tool is in allowlist_tools."""
    event = _event(1, tool="whitelisted-tool")
    cfg = DetectionConfig(allowlist_tools=frozenset({"whitelisted-tool"}))
    assert _is_exempt(event, cfg) is True


def test_is_exempt_union_of_agent_sets():
    """_is_exempt checks the union of all three agent exemption sets."""
    event = _event(1, agent="exempt")
    cfg = DetectionConfig(
        idempotent_agents=frozenset({"a"}),
        retry_allowed=frozenset({"b"}),
        allowlist_agents=frozenset({"exempt"}),
    )
    assert _is_exempt(event, cfg) is True


def test_is_exempt_union_of_tool_sets():
    """_is_exempt checks the union of both tool exemption sets."""
    event = _event(1, tool="exempt-tool")
    cfg = DetectionConfig(
        idempotent_tools=frozenset({"a"}),
        allowlist_tools=frozenset({"exempt-tool"}),
    )
    assert _is_exempt(event, cfg) is True


def test_is_exempt_not_exempt():
    """_is_exempt returns False when agent and tool are not in any exemption set."""
    event = _event(1, agent="normal", tool="normal-tool")
    cfg = DetectionConfig()
    assert _is_exempt(event, cfg) is False


# ---------------------------------------------------------------------------
# _state_key
# ---------------------------------------------------------------------------


def test_state_key_signature_default():
    """_state_key('signature') returns event.signature()."""
    event = _event(1, agent="a", tool="t", args_hash="h")
    cfg = DetectionConfig(state_key="signature")
    assert _state_key(event, cfg) == ("a", "t", "h")


def test_state_key_signature_none_args_hash():
    """_state_key('signature') works with args_hash=None."""
    event = _event(1, agent="a", tool="t", args_hash=None)
    cfg = DetectionConfig(state_key="signature")
    assert _state_key(event, cfg) == ("a", "t", None)


def test_state_key_agent():
    """_state_key('agent') returns event.agent."""
    event = _event(1, agent="my-agent", tool="t", args_hash="h")
    cfg = DetectionConfig(state_key="agent")
    assert _state_key(event, cfg) == "my-agent"


def test_state_key_handoff_state_with_value():
    """_state_key('handoff_state') returns event.handoff_state when not None."""
    event = _event(1, handoff_state="DONE")
    cfg = DetectionConfig(state_key="handoff_state")
    assert _state_key(event, cfg) == "DONE"


def test_state_key_handoff_state_none():
    """_state_key('handoff_state') returns None when event.handoff_state is None."""
    event = _event(1, handoff_state=None)
    cfg = DetectionConfig(state_key="handoff_state")
    assert _state_key(event, cfg) is None


# ---------------------------------------------------------------------------
# _canonical_cycle
# ---------------------------------------------------------------------------


def test_canonical_cycle_single_element():
    """_canonical_cycle on a single element returns a 1-tuple."""
    result = _canonical_cycle(["A"])
    assert result == ("A",)


def test_canonical_cycle_two_elements_sorted():
    """_canonical_cycle on [A,B] returns (A,B)."""
    result = _canonical_cycle(["A", "B"])
    assert result == ("A", "B")


def test_canonical_cycle_rotation_invariance():
    """_canonical_cycle returns the same canonical form regardless of rotation."""
    result_abc = _canonical_cycle(["A", "B", "C"])
    result_bca = _canonical_cycle(["B", "C", "A"])
    result_cab = _canonical_cycle(["C", "A", "B"])
    assert result_abc == result_bca == result_cab == ("A", "B", "C")


def test_canonical_cycle_direction_distinct():
    """_canonical_cycle preserves direction: A→B→C ≠ A→C→B."""
    result_abc = _canonical_cycle(["A", "B", "C"])
    result_acb = _canonical_cycle(["A", "C", "B"])
    assert result_abc != result_acb
    assert result_abc == ("A", "B", "C")
    assert result_acb == ("A", "C", "B")


def test_canonical_cycle_never_reverses():
    """_canonical_cycle never reverses: A→B→C ≠ C→B→A (in reverse)."""
    result_abc = _canonical_cycle(["A", "B", "C"])
    result_cba = _canonical_cycle(["C", "B", "A"])
    # The reverse of A→B→C is C→B→A, which canonicalizes to ("A", "C", "B").
    assert result_abc == ("A", "B", "C")
    assert result_cba == ("A", "C", "B")
    assert result_abc != result_cba


def test_canonical_cycle_long_sequence():
    """_canonical_cycle handles longer cycles correctly."""
    result = _canonical_cycle(["D", "A", "B", "C"])
    # Rotations: D,A,B,C → A,B,C,D → B,C,D,A → C,D,A,B
    # Minimum is A,B,C,D
    assert result == ("A", "B", "C", "D")


def test_canonical_cycle_returns_tuple():
    """_canonical_cycle returns a tuple, not a list."""
    result = _canonical_cycle(["A", "B"])
    assert isinstance(result, tuple)


# ---------------------------------------------------------------------------
# _is_blocked — bare-token blocked-state predicate (explicit-to_agent contract)
# ---------------------------------------------------------------------------
# Under the explicit-to_agent contract, handoff_state carries ONLY the bare
# state token; the wait-for target lives in event.to_agent.  _is_blocked does
# NO delimiter scanning — it is a case-insensitive membership predicate.


def test_is_blocked_default_blocked_token():
    """_is_blocked returns True for the default 'blocked' token."""
    cfg = DetectionConfig()
    assert _is_blocked("blocked", cfg) is True


def test_is_blocked_default_waiting_token():
    """_is_blocked returns True for the default 'waiting' token."""
    cfg = DetectionConfig()
    assert _is_blocked("waiting", cfg) is True


def test_is_blocked_non_blocked_token():
    """_is_blocked returns False for a token not in blocked_states (e.g. 'DONE')."""
    cfg = DetectionConfig()
    assert _is_blocked("DONE", cfg) is False


def test_is_blocked_none_input():
    """_is_blocked returns False when handoff_state is None."""
    cfg = DetectionConfig()
    assert _is_blocked(None, cfg) is False


def test_is_blocked_empty_string():
    """_is_blocked returns False when handoff_state is empty."""
    cfg = DetectionConfig()
    assert _is_blocked("", cfg) is False


def test_is_blocked_case_insensitive_uppercase_input():
    """_is_blocked matches 'BLOCKED' against the default lowercase blocked_states."""
    cfg = DetectionConfig()
    assert _is_blocked("BLOCKED", cfg) is True


def test_is_blocked_case_insensitive_mixed_case_input():
    """_is_blocked matches 'Blocked' (mixed case) case-insensitively."""
    cfg = DetectionConfig()
    assert _is_blocked("Blocked", cfg) is True


def test_is_blocked_strips_whitespace():
    """_is_blocked strips surrounding whitespace before membership testing."""
    cfg = DetectionConfig()
    assert _is_blocked("  blocked  ", cfg) is True


def test_is_blocked_custom_blocked_states_membership():
    """_is_blocked respects custom blocked_states membership."""
    cfg = DetectionConfig(blocked_states=frozenset({"stuck", "halted"}))
    assert _is_blocked("stuck", cfg) is True
    assert _is_blocked("halted", cfg) is True
    assert _is_blocked("blocked", cfg) is False


def test_is_blocked_custom_blocked_states_case_insensitive():
    """_is_blocked matches custom blocked_states case-insensitively on both sides."""
    cfg = DetectionConfig(blocked_states=frozenset({"STALLED"}))
    # Lowercase input matches an uppercase configured state.
    assert _is_blocked("stalled", cfg) is True
    # And vice versa.
    cfg_lower = DetectionConfig(blocked_states=frozenset({"stalled"}))
    assert _is_blocked("STALLED", cfg_lower) is True


def test_is_blocked_empty_blocked_states():
    """_is_blocked returns False when blocked_states is empty."""
    cfg = DetectionConfig(blocked_states=frozenset())
    assert _is_blocked("blocked", cfg) is False
