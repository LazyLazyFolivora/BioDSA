"""Tests that a bad entry in an add_to_graph batch costs only itself.

Local models drop a key mid-array. Rejecting the whole batch for that used to
lose every good entry sent alongside it, and those were gone for good whenever
the model moved on instead of retrying.
"""

import json

import pytest

# Imported through importorskip because the module needs typing.Annotated (3.9+).
graph_tools = pytest.importorskip("biodsa.memory.graph")
from biodsa.memory.memory_graph import clear_manager_cache, load_graph_data  # noqa: E402

CONTEXT = "test_partial_writes"
RELATION = {"from_entity": "SNCA", "to_entity": "Parkinson disease",
            "relation_type": "ASSOCIATED_WITH"}


@pytest.fixture
def tool(tmpdir):
    clear_manager_cache()
    yield graph_tools.AddToGraph(database_name=CONTEXT, cache_dir=str(tmpdir))
    clear_manager_cache()


def _relations_on_disk(tmpdir):
    data = load_graph_data(CONTEXT, str(tmpdir))
    return {(r["from"], r["to"]) for r in data.get("relations", [])}


def _entities_on_disk(tmpdir):
    data = load_graph_data(CONTEXT, str(tmpdir))
    return {e["name"] for e in data.get("entities", [])}


def test_entry_missing_a_field_does_not_take_the_batch_with_it(tool, tmpdir):
    result = json.loads(tool._run(relations=[
        dict(RELATION),
        {"from_entity": "CAT", "relation_type": "INHIBITS"},  # no to_entity
        {"from_entity": "PRKN", "to_entity": "Parkinson disease",
         "relation_type": "ASSOCIATED_WITH"},
    ]))
    assert result["success"] is True
    assert result["results"]["relations_created"]["count"] == 2
    assert _relations_on_disk(tmpdir) == {
        ("SNCA", "Parkinson disease"), ("PRKN", "Parkinson disease")}


def test_the_skipped_entry_is_reported_with_its_position_and_field(tool):
    result = json.loads(tool._run(relations=[
        dict(RELATION),
        {"from_entity": "CAT", "relation_type": "INHIBITS"},
    ]))
    skipped = " ".join(result["skipped"])
    assert "relation 2" in skipped
    assert "to_entity" in skipped


def test_malformed_json_keeps_the_entries_around_the_broken_one(tool, tmpdir):
    # A missing key name makes the whole argument undecodable as one array.
    payload = ('[{"from_entity": "SNCA", "relation_type": "ASSOCIATED_WITH", '
               '"to_entity": "Parkinson disease"}, '
               '{"from_entity": "LRRK2", "relation_type": "ASSOCIATED_WITH", '
               '"Parkinson disease"}, '
               '{"from_entity": "PRKN", "relation_type": "ASSOCIATED_WITH", '
               '"to_entity": "Parkinson disease"}]')
    result = json.loads(tool._run(relations=payload))
    assert result["success"] is True
    assert _relations_on_disk(tmpdir) == {
        ("SNCA", "Parkinson disease"), ("PRKN", "Parkinson disease")}
    assert result["skipped"]


def test_a_batch_with_nothing_usable_fails_and_says_why(tool):
    result = json.loads(tool._run(relations=[{"from_entity": "CAT"}]))
    assert result["success"] is False
    assert "relation 1" in " ".join(result["skipped"])


def test_a_clean_batch_reports_no_skips(tool, tmpdir):
    result = json.loads(tool._run(
        entities=[{"name": "SNCA", "entity_type": "GENE", "observations": ["x"]}],
        relations=[dict(RELATION)],
    ))
    assert result["success"] is True
    assert "skipped" not in result
    assert "SNCA" in _entities_on_disk(tmpdir)


def test_an_entity_missing_its_type_is_skipped_alone(tool, tmpdir):
    result = json.loads(tool._run(entities=[
        {"name": "SNCA", "entity_type": "GENE", "observations": []},
        {"name": "MAPT", "observations": []},
    ]))
    assert result["results"]["entities_created"]["count"] == 1
    assert "entity 2" in " ".join(result["skipped"])
    assert _entities_on_disk(tmpdir) == {"SNCA"}


def test_empty_call_is_still_an_error(tool):
    result = json.loads(tool._run())
    assert result["success"] is False
    assert "No data provided" in result["error"]


def test_entities_survive_a_relations_argument_that_is_unreadable(tool, tmpdir):
    """The two arguments are independent; one being junk should not hide the other."""
    result = json.loads(tool._run(
        entities=[{"name": "SNCA", "entity_type": "GENE", "observations": []}],
        relations="not json at all",
    ))
    assert result["success"] is True
    assert _entities_on_disk(tmpdir) == {"SNCA"}
    assert result["skipped"]
