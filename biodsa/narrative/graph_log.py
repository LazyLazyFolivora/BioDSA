"""
Append-only, on-disk log of knowledge-graph events in the order they happen.

Separate from the application log on purpose: a run emits hundreds of graph
events, and mixing them into the server log makes both hard to read. This one
rotates daily and is meant to be read directly (or grepped) to answer "what
nodes and relations did this run produce".

Both stages of a node's life are recorded: SEARCHING when the agent looks an
entity up in a knowledge base, and CONFIRMED when it writes the entity into the
evidence graph.
"""

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Dedicated logger; kept off the root handlers so graph events do not flood the
# server log and vice versa.
_graph_logger = logging.getLogger("biodsa.graph_events")
_graph_logger.setLevel(logging.INFO)
_graph_logger.propagate = False

_configured_path: Optional[Path] = None
_configure_attempted = False

BACKUP_DAYS = 30

# Marks our handler on the logger. The logger is a process-wide singleton while
# this module's globals are not, so the handler itself is the only reliable
# record of whether setup already happened.
_HANDLER_TAG = "_biodsa_graph_log"


def _existing_handler() -> Optional[logging.Handler]:
    for handler in _graph_logger.handlers:
        if getattr(handler, _HANDLER_TAG, False):
            return handler
    return None


def _default_log_dir() -> Path:
    """Sit next to the graph cache so both follow REPO_BASE_DIR."""
    repo_base = os.environ.get("REPO_BASE_DIR")
    base = Path(repo_base) if repo_base else Path.home()
    return base / ".biodsa_memory" / "graph_events"


def configure_graph_log(log_dir: Optional[str] = None) -> Optional[Path]:
    """Attach a daily-rotating file handler. Idempotent.

    Returns the log file path, or None if logging to disk could not be set up
    (in which case graph events are simply not persisted; callers must keep
    working).
    """
    global _configured_path, _configure_attempted
    existing = _existing_handler()
    if existing is not None:
        _configure_attempted = True
        _configured_path = Path(getattr(existing, "baseFilename", "")) or None
        return _configured_path
    if _configure_attempted:
        # An earlier attempt failed; retrying on every event would just repeat
        # the same error for the whole run.
        return None
    _configure_attempted = True

    target_dir = Path(log_dir) if log_dir else _default_log_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "graph_events.log"
        handler = logging.handlers.TimedRotatingFileHandler(
            path, when="midnight", backupCount=BACKUP_DAYS, encoding="utf-8",
        )
        # Rotated files get a .YYYY-MM-DD suffix.
        handler.suffix = "%Y-%m-%d"
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        setattr(handler, _HANDLER_TAG, True)
        _graph_logger.addHandler(handler)
        _configured_path = path
        logger.info("Graph event log: %s", path)
        return path
    except Exception:
        logger.warning("Could not open graph event log in %s", target_dir, exc_info=True)
        return None


def _ensure_configured() -> None:
    """Fall back to the default location so that runs which never went through
    the MCP server entry point still leave a trail."""
    if not _configure_attempted:
        configure_graph_log(None)


def _truncate(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def log_tool_calls(message, session_id: Optional[str] = None, step: int = 0) -> None:
    """Record the raw tool-call sequence for one step.

    Tools with no graph meaning (code execution, graph retrieval) produce no
    event of their own, which used to leave gaps in the step numbers and made it
    impossible to tell whether the agent ever called add_to_graph at all.
    """
    _ensure_configured()
    if _existing_handler() is None:
        return
    try:
        calls = getattr(message, "tool_calls", None) or []
        names = [c.get("name", "?") for c in calls if isinstance(c, dict)]
        if not names:
            return
        _graph_logger.info(
            "sid=%s | step=%3d | CALL       | %s",
            session_id or "-", step, ", ".join(names),
        )
    except Exception:
        logger.warning("Failed to log tool calls", exc_info=True)


GRAPH_TOOLS = ("add_to_graph", "retrieve_from_graph")


def log_tool_result(message, session_id: Optional[str] = None, step: int = 0) -> None:
    """Record what a graph tool actually returned.

    The CALL line only proves the model asked for the tool. These tools report
    failures as a JSON payload rather than raising, so without the result a
    rejected write is indistinguishable from a successful one.
    """
    _ensure_configured()
    if _existing_handler() is None:
        return
    try:
        name = getattr(message, "name", None)
        if name not in GRAPH_TOOLS:
            return
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = str(content)
        _graph_logger.info(
            "sid=%s | step=%3d | RESULT     | %-18s | %s",
            session_id or "-", step, name, _truncate(content, 300),
        )
    except Exception:
        logger.warning("Failed to log tool result", exc_info=True)


def log_graph_event(event, session_id: Optional[str] = None, step: int = 0) -> None:
    """Write one narrative event as a single line. Never raises."""
    _ensure_configured()
    if _existing_handler() is None:
        return
    try:
        kind = type(event).__name__
        sid = session_id or "-"
        head = "sid=%s | step=%3d" % (sid, step)

        if kind == "EntitySearching":
            line = "%s | SEARCHING  | %-10s | %s | kb=%s" % (
                head, getattr(event, "entity_type", ""),
                _truncate(getattr(event, "entity_name", "")),
                getattr(event, "source_kb", ""),
            )
        elif kind == "EntityConfirmed":
            observations = getattr(event, "observations", []) or []
            line = "%s | CONFIRMED  | %-10s | %s | obs=%d" % (
                head, getattr(event, "entity_type", ""),
                _truncate(getattr(event, "entity_name", "")),
                len(observations),
            )
        elif kind == "RelationFound":
            line = "%s | RELATION   | %s -[%s]-> %s" % (
                head,
                _truncate(getattr(event, "source_entity", ""), 60),
                getattr(event, "relation_type", ""),
                _truncate(getattr(event, "target_entity", ""), 60),
            )
        elif kind == "LiteratureSearching":
            line = "%s | LITERATURE | %-10s | %s" % (
                head, getattr(event, "source_kb", ""),
                _truncate(getattr(event, "query", "")),
            )
        elif kind == "PhaseChange":
            line = "%s | PHASE      | %s | target=%s" % (
                head, getattr(event, "phase", ""),
                _truncate(getattr(event, "search_target", ""), 60),
            )
        else:
            # Progress and anything new: keep it out of the way, the interesting
            # numbers are already derivable from the lines above.
            return
        _graph_logger.info(line)
    except Exception:
        logger.warning("Failed to log graph event", exc_info=True)


def log_run_summary(
    session_id: Optional[str],
    entities,
    relations,
    duration_seconds: float = 0.0,
    query: str = "",
) -> None:
    """Write the end-of-run totals plus the full node and relation list."""
    _ensure_configured()
    if _existing_handler() is None:
        return
    try:
        entities = entities or []
        relations = relations or []
        sid = session_id or "-"
        _graph_logger.info(
            "sid=%s | RUN DONE   | entities=%d relations=%d duration=%.0fs | q=%s",
            sid, len(entities), len(relations), duration_seconds, _truncate(query, 100),
        )
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            # The graph store writes camelCase; tolerate both spellings.
            name = ent.get("name", "")
            etype = ent.get("entityType") or ent.get("entity_type") or ""
            observations = ent.get("observations") or []
            _graph_logger.info(
                "sid=%s |   NODE     | %-10s | %s | obs=%d",
                sid, etype, _truncate(name), len(observations),
            )
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            src = rel.get("from") or rel.get("from_entity") or ""
            dst = rel.get("to") or rel.get("to_entity") or ""
            rtype = rel.get("relationType") or rel.get("relation_type") or ""
            _graph_logger.info(
                "sid=%s |   EDGE     | %s -[%s]-> %s",
                sid, _truncate(src, 60), rtype, _truncate(dst, 60),
            )
    except Exception:
        logger.warning("Failed to log run summary", exc_info=True)
