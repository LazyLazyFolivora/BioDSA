"""Tests for the graph store one conversation shares across its tool calls.

MCP clients identify each tool call separately (WeKnora sends
"<conversation_id>:<tool_call_id>"), which used to give every call its own empty
graph directory and leave a trail of small disconnected graphs.
"""

import threading

import pytest

from biodsa.memory.memory_graph import (
    clear_manager_cache,
    create_relations,
    graph_scope_id,
    load_graph_data,
    resolve_graph_cache_dir,
)

CONTEXT = "test_session_graph"


@pytest.fixture
def clean_managers():
    clear_manager_cache()
    yield
    clear_manager_cache()


def test_composite_id_reduces_to_the_conversation():
    assert graph_scope_id("e9a1b2c3:call_abc123") == "e9a1b2c3"


def test_plain_id_is_left_alone():
    assert graph_scope_id("plain-session") == "plain-session"


def test_id_that_is_only_a_separator_falls_back_to_the_whole_id():
    # Splitting would yield an empty scope, which would collapse every such
    # session into one shared directory.
    assert graph_scope_id(":call_abc") == ":call_abc"


def test_only_the_first_separator_starts_the_tool_call_part():
    assert graph_scope_id("conv:a:b") == "conv"


def test_tool_calls_of_one_conversation_share_a_directory(tmpdir, monkeypatch):
    monkeypatch.setattr(
        "biodsa.memory.memory_graph.graph.get_default_memory_graph_cache_dir",
        lambda: tmpdir,
    )
    first, first_scoped = resolve_graph_cache_dir(None, "convA:call_1")
    second, second_scoped = resolve_graph_cache_dir(None, "convA:call_2")
    assert first == second
    assert first_scoped and second_scoped


def test_separate_conversations_stay_separate(tmpdir, monkeypatch):
    monkeypatch.setattr(
        "biodsa.memory.memory_graph.graph.get_default_memory_graph_cache_dir",
        lambda: tmpdir,
    )
    assert (resolve_graph_cache_dir(None, "convA:call_1")[0]
            != resolve_graph_cache_dir(None, "convB:call_1")[0])


def test_explicit_directory_is_not_session_scoped():
    path, scoped = resolve_graph_cache_dir("/tmp/explicit", "convA:call_1")
    assert path == "/tmp/explicit"
    # The caller owns it, so nothing here may assume it is safe to clear.
    assert not scoped


def test_absent_session_id_is_not_session_scoped():
    assert not resolve_graph_cache_dir(None, None)[1]


def test_session_id_cannot_escape_the_sessions_directory(tmpdir, monkeypatch):
    monkeypatch.setattr(
        "biodsa.memory.memory_graph.graph.get_default_memory_graph_cache_dir",
        lambda: tmpdir,
    )
    path = resolve_graph_cache_dir(None, "../../etc/passwd:call_1")[0]
    assert "sessions" in path
    assert ".." not in path


def test_a_later_call_sees_what_an_earlier_call_wrote(tmpdir, clean_managers, monkeypatch):
    monkeypatch.setattr(
        "biodsa.memory.memory_graph.graph.get_default_memory_graph_cache_dir",
        lambda: tmpdir,
    )
    first = resolve_graph_cache_dir(None, "convC:call_1")[0]
    create_relations([{"from_entity": "SNCA", "to_entity": "Parkinson disease",
                       "relation_type": "ASSOCIATED_WITH"}],
                     context=CONTEXT, cache_dir=first)
    second = resolve_graph_cache_dir(None, "convC:call_2")[0]
    create_relations([{"from_entity": "LRRK2", "to_entity": "Parkinson disease",
                       "relation_type": "ASSOCIATED_WITH"}],
                     context=CONTEXT, cache_dir=second)

    sources = {r["from"] for r in load_graph_data(CONTEXT, second).get("relations", [])}
    assert sources == {"SNCA", "LRRK2"}


def test_concurrent_writes_to_a_shared_directory_are_not_lost(tmpdir, clean_managers):
    """Writing a relation is load-mutate-overwrite, and one conversation's runs
    now share a directory. Without serialisation the majority of these writes is
    silently dropped rather than reported as an error."""
    shared = str(tmpdir)
    per_thread, thread_count = 25, 4

    def writer(tag):
        for i in range(per_thread):
            create_relations([{"from_entity": "%s-%d" % (tag, i), "to_entity": "PD",
                               "relation_type": "ASSOCIATED_WITH"}],
                             context=CONTEXT, cache_dir=shared)

    threads = [threading.Thread(target=writer, args=("T%d" % t,))
               for t in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    written = {r["from"] for r in load_graph_data(CONTEXT, shared).get("relations", [])}
    expected = {"T%d-%d" % (t, i) for t in range(thread_count) for i in range(per_thread)}
    assert written == expected
