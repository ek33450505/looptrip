"""Tests for the normalized event contract (src/looptrip/normalize.py)."""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from looptrip.normalize import Adapter, Event, args_hash_from, split_handoff_state


# ---------------------------------------------------------------------------
# Event — construction & defaults
# ---------------------------------------------------------------------------

def test_event_minimal_construction_and_defaults():
    """An Event built with only the required fields gets the documented defaults."""
    ev = Event(agent="workflow-subagent", tool="dispatch", args_hash=None, ts="2026-06-21T00:00:00Z")
    assert ev.agent == "workflow-subagent"
    assert ev.tool == "dispatch"
    assert ev.args_hash is None
    assert ev.ts == "2026-06-21T00:00:00Z"
    assert ev.handoff_state is None
    assert ev.to_agent is None
    assert ev.input_tokens is None
    assert ev.cost_usd is None
    assert ev.progress is False
    assert ev.raw_id is None


def test_event_full_construction_preserves_fields():
    """All enrichment fields round-trip as supplied."""
    ev = Event(
        agent="coder",
        tool="dispatch",
        args_hash="deadbeef",
        ts="2026-06-21T01:02:03Z",
        handoff_state="blocked",
        to_agent="code-reviewer",
        input_tokens=1234,
        cost_usd=10.98,
        progress=True,
        raw_id=554,
    )
    assert ev.handoff_state == "blocked"
    assert ev.to_agent == "code-reviewer"
    assert ev.input_tokens == 1234
    assert ev.cost_usd == 10.98
    assert ev.progress is True
    assert ev.raw_id == 554


# ---------------------------------------------------------------------------
# Event — frozen / immutability
# ---------------------------------------------------------------------------

def test_event_is_frozen():
    """Assigning to any field of a frozen Event raises FrozenInstanceError."""
    ev = Event(agent="a", tool="dispatch", args_hash=None, ts="2026-06-21T00:00:00Z")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.agent = "b"  # type: ignore[misc]


def test_event_frozen_blocks_enrichment_fields_too():
    """Immutability covers defaulted fields, not just the required ones."""
    ev = Event(agent="a", tool="dispatch", args_hash=None, ts="2026-06-21T00:00:00Z")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.cost_usd = 1.0  # type: ignore[misc]


def test_event_uses_slots_no_dict():
    """slots=True means instances have no __dict__ and reject novel attributes."""
    ev = Event(agent="a", tool="dispatch", args_hash=None, ts="2026-06-21T00:00:00Z")
    assert not hasattr(ev, "__dict__")
    # On CPython 3.10/3.11 @dataclass(frozen=True, slots=True) has a known bug
    # where assigning a non-field attribute raises TypeError instead of
    # AttributeError/FrozenInstanceError (fixed in 3.12+). Accept all three so
    # the test passes on all CI targets without weakening the __dict__ assertion.
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        ev.surprise = 1  # type: ignore[attr-defined]


def test_event_is_hashable():
    """Frozen events are hashable and usable as set/dict keys."""
    ev = Event(agent="a", tool="dispatch", args_hash=None, ts="2026-06-21T00:00:00Z")
    assert ev in {ev}


# ---------------------------------------------------------------------------
# Event — signature()
# ---------------------------------------------------------------------------

def test_signature_returns_agent_tool_args_hash():
    """signature() is exactly the (agent, tool, args_hash) triple."""
    ev = Event(
        agent="workflow-subagent",
        tool="dispatch",
        args_hash="abc123",
        ts="2026-06-21T00:00:00Z",
        input_tokens=999,
        cost_usd=5.0,
    )
    assert ev.signature() == ("workflow-subagent", "dispatch", "abc123")


def test_signature_ignores_non_signature_fields():
    """Two events differing only in non-signature fields share a signature."""
    a = Event(agent="x", tool="dispatch", args_hash=None, ts="2026-06-21T00:00:00Z",
              input_tokens=100, cost_usd=10.0, progress=False, raw_id=1)
    b = Event(agent="x", tool="dispatch", args_hash=None, ts="2026-06-21T00:00:09Z",
              input_tokens=101, cost_usd=10.0, progress=False, raw_id=2)
    assert a.signature() == b.signature()


def test_signature_carries_none_args_hash():
    """The cast.db case (args_hash=None) is reflected verbatim in the signature."""
    ev = Event(agent="x", tool="dispatch", args_hash=None, ts="2026-06-21T00:00:00Z")
    assert ev.signature() == ("x", "dispatch", None)


# ---------------------------------------------------------------------------
# args_hash_from
# ---------------------------------------------------------------------------

def test_args_hash_from_is_deterministic():
    """Same inputs always produce the same digest."""
    assert args_hash_from("a", "b", "c") == args_hash_from("a", "b", "c")


def test_args_hash_from_matches_sha1_of_pipe_join():
    """The digest is sha1 hex of the parts joined by '|'."""
    expected = hashlib.sha1("a|b|c".encode("utf-8")).hexdigest()
    assert args_hash_from("a", "b", "c") == expected


def test_args_hash_from_is_order_sensitive():
    """Reordering the parts changes the digest."""
    assert args_hash_from("a", "b") != args_hash_from("b", "a")


def test_args_hash_from_single_and_empty():
    """Single-part and zero-part calls hash the corresponding joined string."""
    assert args_hash_from("solo") == hashlib.sha1(b"solo").hexdigest()
    assert args_hash_from() == hashlib.sha1(b"").hexdigest()


def test_args_hash_from_returns_hex_string():
    """Result is a 40-char lowercase hex string (sha1)."""
    digest = args_hash_from("a", "b")
    assert isinstance(digest, str)
    assert len(digest) == 40
    assert all(ch in "0123456789abcdef" for ch in digest)


# ---------------------------------------------------------------------------
# Adapter — abstract base class
# ---------------------------------------------------------------------------

def test_adapter_cannot_be_instantiated_directly():
    """Adapter is abstract; instantiating it raises TypeError."""
    with pytest.raises(TypeError):
        Adapter()  # type: ignore[abstract]


def test_concrete_adapter_subclass_works():
    """A subclass implementing events() instantiates and yields Events."""

    class StubAdapter(Adapter):
        def events(self):
            yield Event(agent="a", tool="dispatch", args_hash=None, ts="2026-06-21T00:00:00Z")

    adapter = StubAdapter()
    out = list(adapter.events())
    assert len(out) == 1
    assert isinstance(out[0], Event)
    assert out[0].signature() == ("a", "dispatch", None)


def test_incomplete_adapter_subclass_still_abstract():
    """A subclass that does NOT implement events() remains un-instantiable."""

    class Incomplete(Adapter):
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# split_handoff_state — legacy packed-string splitter
# ---------------------------------------------------------------------------

def test_split_handoff_state_basic_on_delimiter():
    """A simple 'state on target' splits into (state, target)."""
    assert split_handoff_state("blocked on code-writer") == ("blocked", "code-writer")


def test_split_handoff_state_delimiter_is_case_insensitive():
    """The ' on ' delimiter matches regardless of case (' ON ', ' On ')."""
    assert split_handoff_state("blocked ON code-writer") == ("blocked", "code-writer")
    assert split_handoff_state("waiting On test-runner") == ("waiting", "test-runner")


def test_split_handoff_state_preserves_hyphenated_target():
    """A hyphenated target name is preserved verbatim (no further splitting)."""
    assert split_handoff_state("waiting on code-reviewer") == (
        "waiting",
        "code-reviewer",
    )


def test_split_handoff_state_no_delimiter_returns_state_and_none():
    """A bare token with no ' on ' delimiter returns (token, None)."""
    assert split_handoff_state("blocked") == ("blocked", None)
    assert split_handoff_state("in_progress") == ("in_progress", None)


def test_split_handoff_state_none_and_empty_return_none_pair():
    """None and the empty string both map to (None, None)."""
    assert split_handoff_state(None) == (None, None)
    assert split_handoff_state("") == (None, None)


def test_split_handoff_state_strips_surrounding_whitespace():
    """Both halves are stripped of surrounding whitespace."""
    assert split_handoff_state("  blocked on code-writer  ") == (
        "blocked",
        "code-writer",
    )
    # A whitespace-only input strips to empty -> (None, None).
    assert split_handoff_state("   ") == (None, None)
