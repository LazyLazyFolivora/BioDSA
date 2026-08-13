"""Tests that graph events report what was written, not what was requested.

Events used to be read from the add_to_graph arguments, so entities and
relations that the store skipped or deduplicated were still logged and streamed
to the client as if they existed.
"""

import json

import pytest

# Imported through importorskip because the module needs PEP 585 generics (3.9+).
extractor = pytest.importorskip("biodsa.narrative.extractor")


class ToolResult:
    """Stands in for the ToolMessage a graph tool returns."""

    def __init__(self, payload, name="add_to_graph"):
        self.content = payload if isinstance(payload, str) else json.dumps(payload)
        self.name = name


class Request:
    """Stands in for the AIMessage that asks for a tool call."""

    def __init__(self, args):
        self.tool_calls = [{"name": "add_to_graph", "args": args}]
        self.content = ""
        self.name = None


def _kinds(events):
    return [type(e).__name__ for e in events]


def test_written_entities_and_relations_become_events():
    events = extractor.extract_result_events(ToolResult({"success": True, "results": {
        "entities_created": {"count": 1, "entities": [
            {"name": "SNCA", "entityType": "GENE", "observations": ["encodes alpha-synuclein"]}]},
        "relations_created": {"count": 1, "relations": [
            {"from": "SNCA", "to": "Parkinson disease", "relationType": "ASSOCIATED_WITH"}]},
    }}))
    assert _kinds(events) == ["EntityConfirmed", "RelationFound"]
    assert events[0].entity_name == "SNCA"
    assert events[1].source_entity == "SNCA"
    assert events[1].target_entity == "Parkinson disease"
    assert events[1].relation_type == "ASSOCIATED_WITH"


def test_a_request_no_longer_produces_graph_events():
    request = Request({
        "entities": [{"name": "GHOST", "entity_type": "GENE", "observations": []}],
        "relations": [{"from_entity": "GHOST", "to_entity": "X", "relation_type": "TREATS"}],
    })
    assert extractor.extract_result_events(request) == []
    # extract_events handles searches and phase changes, never graph writes.
    assert extractor.extract_events(request) == []


def test_deduplicated_writes_are_not_reported_twice():
    """The store returns only what it accepted, so a repeat write reports nothing."""
    events = extractor.extract_result_events(ToolResult({"success": True, "results": {
        "relations_created": {"count": 0, "relations": []}}}))
    assert events == []


def test_a_rejected_write_produces_no_events():
    events = extractor.extract_result_events(ToolResult({
        "success": False, "error": "Nothing could be written.",
        "skipped": ["relation 1 skipped, missing to_entity: {...}"]}))
    assert events == []


def test_skipped_entries_produce_no_events():
    events = extractor.extract_result_events(ToolResult({
        "success": True,
        "results": {"relations_created": {"count": 1, "relations": [
            {"from": "A", "to": "B", "relationType": "TREATS"}]}},
        "skipped": ["relation 2 skipped, missing to_entity: {...}"]}))
    assert len(events) == 1


def test_an_entity_created_by_an_observation_is_reported():
    events = extractor.extract_result_events(ToolResult({"success": True, "results": {
        "observations_added": [
            {"entityName": "MAPT", "addedObservations": ["tau"], "entity_created": True},
            {"entityName": "SNCA", "addedObservations": ["more"], "entity_created": False},
        ]}}))
    assert [e.entity_name for e in events] == ["MAPT"]


def test_other_tools_are_ignored():
    assert extractor.extract_result_events(
        ToolResult({"success": True}, name="retrieve_from_graph")) == []


@pytest.mark.parametrize("content", [
    '{"success": true, "results": {"entities_crea',  # truncated
    "", "None", None, 12,
])
def test_unreadable_content_is_ignored_rather_than_raising(content):
    assert extractor.extract_result_events(ToolResult(content)) == []


def test_an_edge_missing_an_end_is_dropped():
    events = extractor.extract_result_events(ToolResult({"success": True, "results": {
        "relations_created": {"count": 1, "relations": [{"from": "A", "relationType": "TREATS"}]}}}))
    assert events == []
