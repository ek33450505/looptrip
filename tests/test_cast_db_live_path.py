"""Hermetic coverage for the cast.db live loader and ``cast-db:<id>`` CLI scheme.

Every other cast.db test injects a fake ``db_query`` callable, so the real
loader branch — :meth:`CastDbAdapter._default_db_query`, which loads CAST's
``cast_db.py`` from disk — and the CLI's ``cast-db:<id>`` source scheme have no
direct execution. This file closes that gap WITHOUT ever touching the real
``~/.claude/cast.db`` or ``~/.claude/scripts``:

* A temp directory holds a synthetic ``cast_db.py`` that exposes a real
  ``db_query(sql, params)`` backed by a temp SQLite ``agent_runs`` table.
* ``_CAST_DB_SCRIPTS_DIR`` is monkeypatched to that temp directory so the
  importlib file-path loader resolves the fake instead of anything real.

It also pins the two remediated behaviours:

* **B3 (portability):** ``_CAST_DB_SCRIPTS_DIR`` resolves from the
  ``CAST_DB_SCRIPTS_DIR`` env var with a portable ``~/.claude/scripts`` default
  — no machine-specific home path baked into the package.
* **S3 (safe loading):** the loader does NOT leave a lasting ``sys.path`` entry
  and does NOT register the loaded module under ``cast_db`` in ``sys.modules``.

stdlib + pytest only.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys

import pytest

from looptrip import cli
from looptrip.adapters import cast_db as cdb
from looptrip.adapters.cast_db import CastDbAdapter

SESSION = "sess-live-c3"

# Internal module name the loader assigns to the file-loaded cast_db (it must
# NOT be registered in sys.modules — exec_module never auto-registers it).
_LOADER_MODULE_NAME = "looptrip._cast_db_live"


def _build_fake_scripts_dir(tmp_path, session_id):
    """Create a temp scripts dir whose ``cast_db.py`` is backed by a temp DB.

    Writes a real SQLite ``agent_runs`` table with two same-agent rows (token
    counts within the detector's default 5% tolerance so duplicate-work trips
    at occurrence #2) deliberately stored out of (started_at, id) order to also
    prove the adapter's ordering. Returns the scripts dir as a ``str``.
    """
    db_path = tmp_path / "fake_cast.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE agent_runs (id INTEGER, session_id TEXT, agent TEXT, "
            "model TEXT, started_at TEXT, ended_at TEXT, input_tokens INTEGER, "
            "output_tokens INTEGER, cost_usd REAL, status TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (2, session_id, "workflow-subagent", "claude-sonnet",
                 "2026-06-21T00:00:02Z", "2026-06-21T00:00:03Z", 1200, 50, 2.5, "DONE"),
                (1, session_id, "workflow-subagent", "claude-sonnet",
                 "2026-06-21T00:00:01Z", "2026-06-21T00:00:02Z", 1190, 40, 1.5, "DONE"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    # A self-contained fake cast_db.py: stdlib-only, opens the temp DB by an
    # embedded absolute path and returns sqlite3.Row rows (cast_db's real shape).
    (scripts_dir / "cast_db.py").write_text(
        "import sqlite3\n"
        f"_DB_PATH = {str(db_path)!r}\n"
        "\n"
        "def db_query(sql, params):\n"
        "    conn = sqlite3.connect(_DB_PATH)\n"
        "    conn.row_factory = sqlite3.Row\n"
        "    try:\n"
        "        return conn.execute(sql, params).fetchall()\n"
        "    finally:\n"
        "        conn.close()\n",
        encoding="utf-8",
    )
    return str(scripts_dir)


# ---------------------------------------------------------------------------
# Live loader — _default_db_query actually loads the fake cast_db.py from disk
# ---------------------------------------------------------------------------

def test_live_mode_loads_fake_cast_db_from_disk_and_returns_events(monkeypatch, tmp_path):
    """Live mode (no injected db_query) drives the real importlib file loader.

    With ``_CAST_DB_SCRIPTS_DIR`` pointed at a temp dir holding a fake
    ``cast_db.py``, ``CastDbAdapter(session)`` resolves ``db_query`` via
    :meth:`CastDbAdapter._default_db_query`, queries the temp SQLite DB, and
    yields normalized, ordered events.
    """
    scripts_dir = _build_fake_scripts_dir(tmp_path, SESSION)
    monkeypatch.setattr(cdb, "_CAST_DB_SCRIPTS_DIR", scripts_dir)

    adapter = CastDbAdapter(SESSION)  # live mode — no rows, no db_query
    events = list(adapter.events())

    assert [e.raw_id for e in events] == [1, 2]  # ordered by (started_at, id)
    assert [e.cost_usd for e in events] == [1.5, 2.5]
    assert all(e.tool == "dispatch" and e.args_hash is None for e in events)
    assert all(e.agent == "workflow-subagent" for e in events)


def test_default_db_query_returns_a_callable_querying_the_fake(monkeypatch, tmp_path):
    """The loader hands back the fake's ``db_query`` bound to the parameterized
    query — the session id is passed as a bound ``?`` param, not interpolated."""
    scripts_dir = _build_fake_scripts_dir(tmp_path, SESSION)
    monkeypatch.setattr(cdb, "_CAST_DB_SCRIPTS_DIR", scripts_dir)

    query = CastDbAdapter._default_db_query()
    assert callable(query)
    rows = query(cdb._AGENT_RUNS_SQL, (SESSION,))
    assert {r["id"] for r in rows} == {1, 2}
    # Wrong session id -> empty, proving the bound param actually filters.
    assert query(cdb._AGENT_RUNS_SQL, ("no-such-session",)) == []


def test_live_loader_leaves_sys_path_and_sys_modules_unpolluted(monkeypatch, tmp_path):
    """S3: loading cast_db by file path adds no lasting sys.path entry and does
    NOT register the loaded module under any importable name in sys.modules."""
    scripts_dir = _build_fake_scripts_dir(tmp_path, SESSION)
    monkeypatch.setattr(cdb, "_CAST_DB_SCRIPTS_DIR", scripts_dir)

    path_before = list(sys.path)
    list(CastDbAdapter(SESSION).events())  # force the loader to run

    assert scripts_dir not in sys.path
    assert sys.path == path_before
    # The file-loaded module is never registered (neither as cast_db nor the
    # internal loader name), so it cannot shadow stdlib/site-packages elsewhere.
    assert _LOADER_MODULE_NAME not in sys.modules
    assert "cast_db" not in sys.modules


# ---------------------------------------------------------------------------
# CLI — the cast-db:<id> source scheme exercises the live loader end to end
# ---------------------------------------------------------------------------

def test_cli_scan_cast_db_scheme_drives_live_loader(monkeypatch, tmp_path, capsys):
    """``looptrip scan cast-db:<id>`` resolves the live adapter, runs the
    duplicate-work detector, and prints a non-empty report — exit 0, no error."""
    scripts_dir = _build_fake_scripts_dir(tmp_path, SESSION)
    monkeypatch.setattr(cdb, "_CAST_DB_SCRIPTS_DIR", scripts_dir)

    rc = cli.main(["scan", f"cast-db:{SESSION}"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.err == ""
    # Duplicate-work tripped on the workflow-subagent repeat: a populated table,
    # not the empty-stream message.
    assert "no duplicate-work pathologies detected" not in captured.out
    assert "workflow-subagent" in captured.out


# ---------------------------------------------------------------------------
# Missing cast_db.py — a clean ModuleNotFoundError -> CLI exit 2
# ---------------------------------------------------------------------------

def test_missing_cast_db_file_raises_module_not_found(monkeypatch, tmp_path):
    """When the scripts dir has no cast_db.py the loader raises a clear
    ModuleNotFoundError (never silently reads anything real)."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setattr(cdb, "_CAST_DB_SCRIPTS_DIR", str(empty_dir))

    with pytest.raises(ModuleNotFoundError) as excinfo:
        CastDbAdapter._default_db_query()
    assert "cast_db.py not found" in str(excinfo.value)


def test_cli_scan_cast_db_missing_loader_exits_two(monkeypatch, tmp_path, capsys):
    """A missing cast_db.py surfaces through the CLI as a clean exit-2 error,
    not a traceback (the caught-tuple includes ModuleNotFoundError)."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setattr(cdb, "_CAST_DB_SCRIPTS_DIR", str(empty_dir))

    rc = cli.main(["scan", f"cast-db:{SESSION}"])
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.err.startswith("error:")


# ---------------------------------------------------------------------------
# B3 — scripts dir resolves from CAST_DB_SCRIPTS_DIR with a portable default
# ---------------------------------------------------------------------------

def test_scripts_dir_resolves_from_env_var(monkeypatch, tmp_path):
    """``CAST_DB_SCRIPTS_DIR`` overrides the scripts dir; the default is the
    portable ``~/.claude/scripts`` (no hardcoded personal home path).

    Reloads the module under a controlled env then restores it so no other test
    sees a mutated constant.
    """
    override = str(tmp_path / "custom-scripts")
    monkeypatch.setenv("CAST_DB_SCRIPTS_DIR", override)
    try:
        importlib.reload(cdb)
        assert cdb._CAST_DB_SCRIPTS_DIR == override
    finally:
        monkeypatch.delenv("CAST_DB_SCRIPTS_DIR", raising=False)
        importlib.reload(cdb)

    # Default (env var unset): the portable home-relative path, never a literal
    # personal home path shipped in the package.
    assert cdb._CAST_DB_SCRIPTS_DIR.endswith("/.claude/scripts")
    # Regression guard for B3: the personal home path must not be hardcoded as a
    # literal in the shipped source. (The runtime-expanded value legitimately
    # contains the real home dir, e.g. /Users/<name>/.claude/scripts, so assert
    # against the module source — not the resolved value — to stay machine-independent.)
    import inspect

    assert "/Users/" not in inspect.getsource(cdb)
