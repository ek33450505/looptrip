"""cast_db.py — the cast.db ``agent_runs`` adapter for looptrip.

Translates rows of CAST's ``agent_runs`` table into the normalized
:class:`~looptrip.normalize.Event` stream the detectors consume.

The ``agent_runs`` table has no per-dispatch tool/args columns, so every event
is emitted with ``tool="dispatch"`` and ``args_hash=None``. Duplicate-work
detection therefore relies on the ``(agent, ts)`` repeat signal plus
input-token variance — exactly the real constraint that the worst runaways
("workflow-subagent") are STATUS_CONTRACT_EXEMPT and emit no ``## Handoff``
block. ``handoff_state`` enriches but is never required for detection, so this
adapter leaves it ``None``.

Three sourcing modes, in precedence order:

* **Injected rows** — ``CastDbAdapter(session_id, rows=[...])``: used by
  :meth:`CastDbAdapter.from_fixture` and by tests. No DB access ever happens.
* **Fixture** — :meth:`CastDbAdapter.from_fixture`: loads the packaged hermetic
  fixture and selects one session's rows. The Phase 1 proof substrate.
* **Live** — ``CastDbAdapter(session_id)`` with no rows: lazily resolves a
  ``db_query`` callable (an injected one, or the default cast_db loader) and
  queries the real database. The import of cast_db is deferred to call time so
  the package imports cleanly in CI where cast_db is absent.

This module is stdlib-only and defines no global mutable state.
"""

from __future__ import annotations

import importlib.util
import json
import os
from importlib.resources import files
from typing import Any, Callable, Iterator, List, Optional

from looptrip.normalize import Adapter, Event

# Parameterized query — the session id is always bound as a ``?`` placeholder,
# never string-interpolated. Column order mirrors the agent_runs schema.
_AGENT_RUNS_SQL = (
    "SELECT id, session_id, agent, model, started_at, ended_at, "
    "input_tokens, output_tokens, cost_usd, status "
    "FROM agent_runs WHERE session_id = ? ORDER BY started_at, id"
)

# Location of the cast_db helper used by the default (live) loader. Resolved
# from the ``CAST_DB_SCRIPTS_DIR`` env var with a portable ``~/.claude/scripts``
# default so the package carries no machine-specific home path. The cast_db
# module itself is loaded lazily — never at module import — so CI without
# cast_db still loads looptrip.
_CAST_DB_SCRIPTS_DIR = os.environ.get(
    "CAST_DB_SCRIPTS_DIR", os.path.expanduser("~/.claude/scripts")
)

# Type alias for a parameterized query callable, e.g. cast_db.db_query.
DbQuery = Callable[[str, tuple], List[Any]]


class CastDbAdapter(Adapter):
    """Adapter from CAST ``agent_runs`` rows to normalized events.

    Args:
        session_id: The session whose dispatches to stream.
        rows:       Pre-supplied rows (dicts or ``sqlite3.Row``). When given,
                    no DB access ever occurs. ``None`` selects live mode.
        db_query:   Optional parameterized-query callable used in live mode.
                    When ``None`` and rows are needed, the default cast_db
                    loader is resolved lazily at query time.
    """

    def __init__(
        self,
        session_id: str,
        *,
        rows: Optional[List[Any]] = None,
        db_query: Optional[DbQuery] = None,
    ) -> None:
        self._session_id = session_id
        self._rows = rows
        self._db_query = db_query

    @classmethod
    def from_fixture(cls, session_id: str, path: Optional[str] = None) -> "CastDbAdapter":
        """Build an adapter from the packaged runaway fixture (or ``path``).

        Loads ``_data/cast_db_runaways.json`` via :mod:`importlib.resources`
        when ``path`` is ``None``, then selects ``sessions[session_id]``. An
        unknown ``session_id`` yields an empty event stream rather than raising.

        Args:
            session_id: Key into the fixture's ``sessions`` map.
            path:       Optional filesystem path to an alternative fixture JSON.

        Returns:
            A :class:`CastDbAdapter` carrying that session's rows.
        """
        if path is None:
            resource = files("looptrip").joinpath("_data/cast_db_runaways.json")
            data = json.loads(resource.read_text(encoding="utf-8"))
        else:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        rows = data.get("sessions", {}).get(session_id, [])
        return cls(session_id, rows=list(rows))

    def events(self) -> Iterator[Event]:
        """Yield normalized events ordered by ``(started_at, id)``.

        Ordering is enforced here for every source, so injected and fixture
        rows are deterministic even if not pre-sorted; the live query also
        orders server-side. Rows with ``started_at=None`` sort before any
        string timestamp (null-first, matching SQLite NULL ordering). Each row
        becomes one ``tool="dispatch"`` event with ``args_hash=None``,
        ``handoff_state=None``, and ``to_agent=None`` — see the module
        docstring.
        """
        for row in self._ordered_rows():
            yield Event(
                agent=row["agent"],
                tool="dispatch",
                args_hash=None,
                ts=row["started_at"],
                handoff_state=None,
                to_agent=None,
                input_tokens=row["input_tokens"],
                cost_usd=row["cost_usd"],
                progress=False,
                raw_id=row["id"],
            )

    def _ordered_rows(self) -> List[Any]:
        """Return this session's rows sorted by ``(started_at, id)``.

        Resolves and caches live rows on first access so repeated
        :meth:`events` iteration triggers at most one DB query.

        The sort key is null-safe: a ``None`` ``started_at`` is coerced to an
        empty string, placing it before any real timestamp (matching SQLite's
        NULL ordering convention). This prevents a :class:`TypeError` when rows
        with a ``None`` timestamp are mixed with string-timestamped rows.
        """
        if self._rows is None:
            self._rows = list(self._fetch_live_rows())
        return sorted(self._rows, key=lambda r: (r["started_at"] or "", r["id"]))

    def _fetch_live_rows(self) -> List[Any]:
        """Query the live database for this session's agent_runs rows.

        Uses the injected ``db_query`` when present, else the default cast_db
        loader. The session id is bound as a ``?`` parameter — never
        interpolated into the SQL text.
        """
        query = self._db_query if self._db_query is not None else self._default_db_query()
        return list(query(_AGENT_RUNS_SQL, (self._session_id,)))

    @staticmethod
    def _default_db_query() -> DbQuery:
        """Load ``cast_db.db_query`` from the CAST scripts directory by file path.

        The cast_db module is loaded via
        :func:`importlib.util.spec_from_file_location` against the explicit
        ``<scripts_dir>/cast_db.py`` file, so the scripts directory never
        becomes a lasting ``sys.path`` entry able to shadow stdlib or
        site-packages for the rest of the process. The loaded module is not
        registered in ``sys.modules`` either. The load is deferred to call time
        so importing looptrip never depends on cast_db being present — keeping
        CI (where it is absent) green.

        Raises:
            ModuleNotFoundError: when ``cast_db.py`` is absent from the scripts
                directory (so the CLI surfaces a clean exit-2 error).
            ImportError: when the file exists but exposes no ``db_query``
                callable or cannot be loaded.
        """
        module_path = os.path.join(_CAST_DB_SCRIPTS_DIR, "cast_db.py")
        if not os.path.isfile(module_path):
            raise ModuleNotFoundError(
                f"cast_db.py not found at {module_path!r}; set CAST_DB_SCRIPTS_DIR "
                "to the directory holding CAST's cast_db.py to use cast-db mode"
            )
        spec = importlib.util.spec_from_file_location("looptrip._cast_db_live", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load a module spec for {module_path!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            return module.db_query  # type: ignore[no-any-return]
        except AttributeError as exc:
            raise ImportError(
                f"{module_path!r} defines no 'db_query' callable"
            ) from exc
