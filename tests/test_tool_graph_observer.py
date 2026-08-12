"""
Tests that the observer actually persists a graph, and persists it correctly.

These write to a real store in a temporary directory rather than to a mock: the
failure this module most needs to catch is the store silently dropping part of a
write, which a mock would happily accept.
"""

import json

import pytest

from biodsa.memory.memory_graph import ToolGraphObserver, clear_manager_cache
from biodsa.memory.memory_graph.tool import load_graph_data
from biodsa.memory.memory_graph.tool_graph_extractor import ExtractedGraph

CONTEXT = "test_graph"

DISEASE_RESPONSE = "Disease associations for PINK1:\n" + json.dumps([
    {"gene_name": "PINK1", "disease_id": "MESH:D010300",
     "disease_name": "Parkinson Disease", "count": 42},
    {"gene_name": "PINK1", "disease_id": "DOID:4", "disease_name": "Disease", "count": 1},
])

PATHWAY_RESPONSE = "Pathway analysis results for PINK1,PRKN:\n" + json.dumps([
    {"term": "Mitophagy - animal", "overlapping genes": "PINK1,PRKN",
     "database": "KEGG_2021_Human"},
])

GENE_SUMMARY_RESPONSE = (
    "Gene summary for PINK1 (Homo):\n"
    + str({
        "name": "PINK1",
        "nomenclaturesymbol": "PINK1",
        "nomenclaturename": "PTEN induced kinase 1",
        "summary": "Encodes a serine/threonine protein kinase that localizes to "
                   "mitochondria.",
        "maplocation": "1p36.12",
    })
)

ENRICHMENT_RESPONSE = "Enrichment analysis results for PINK1,PRKN:\n" + json.dumps([
    {"native": "GO:0000422", "name": "mitophagy", "source": "GO:BP",
     "p_value": 1.2e-8, "intersection_size": 2},
])


@pytest.fixture
def observer(tmpdir):
    clear_manager_cache()
    yield ToolGraphObserver(
        context=CONTEXT,
        cache_dir=str(tmpdir),
        session_id="test-session",
        known_genes=["PINK1", "PRKN"],
    )
    clear_manager_cache()


def _graph(tmpdir):
    data = load_graph_data(CONTEXT, str(tmpdir))
    entities = {e["name"]: e for e in data.get("entities", [])}
    relations = {
        (r["from"], r["relationType"], r["to"]) for r in data.get("relations", [])
    }
    return entities, relations


def test_observation_reaches_disk(observer, tmpdir):
    observer.observe("get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE)
    entities, relations = _graph(tmpdir)
    assert "PINK1" in entities
    assert ("PINK1", "ASSOCIATED_WITH", "Parkinson Disease") in relations


def test_entity_types_survive_the_round_trip(observer, tmpdir):
    observer.observe("get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE)
    entities, _ = _graph(tmpdir)
    assert entities["PINK1"]["entityType"] == "GENE"
    assert entities["Parkinson Disease"]["entityType"] == "DISEASE"


def test_observations_are_kept_for_an_entity_that_already_exists(observer, tmpdir):
    """The store creates entities only when they are new. If observations rode
    along with entity creation, everything learned about a gene after its first
    mention would be silently dropped."""
    observer.observe("get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE)
    entities, _ = _graph(tmpdir)
    assert entities["PINK1"]["observations"] == []

    observer.observe(
        "get_gene_summary_for_single_gene",
        {"gene_name": "PINK1", "specie": "Homo"},
        GENE_SUMMARY_RESPONSE,
    )
    entities, _ = _graph(tmpdir)
    observations = entities["PINK1"]["observations"]
    assert any("serine/threonine protein kinase" in o for o in observations)
    assert any("1p36.12" in o for o in observations)


def test_repeated_observation_does_not_duplicate(observer, tmpdir):
    for _ in range(3):
        observer.observe(
            "get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE
        )
    entities, relations = _graph(tmpdir)
    assert len([n for n in entities if n == "PINK1"]) == 1
    assert len(relations) == 1
    assert len(entities["Parkinson Disease"]["observations"]) == 1


def test_gene_set_conclusion_creates_the_centre_of_the_graph(observer, tmpdir):
    observer.observe("get_pathway_for_gene_set", {"gene_set": "PINK1,PRKN"}, PATHWAY_RESPONSE)
    observer.record_gene_set(
        "Mitochondrial quality control", ["PINK1", "PRKN"], ["Verified by GeneAgent"]
    )
    entities, relations = _graph(tmpdir)
    assert entities["Mitochondrial quality control"]["entityType"] == "GENE_SET"
    assert ("Mitochondrial quality control", "HAS_GENESET_MEMBER", "PINK1") in relations
    assert ("PINK1", "MEMBER_OF_PATHWAY", "Mitophagy - animal") in relations


def test_set_level_enrichment_terms_attach_once_the_set_is_named(observer, tmpdir):
    """g:Profiler reports enrichment for the set, not per gene, so its terms stay
    unattached until there is a set node to hang them on."""
    observer.observe(
        "get_enrichment_for_gene_set", {"gene_set": "PINK1,PRKN"}, ENRICHMENT_RESPONSE
    )
    _, relations = _graph(tmpdir)
    assert relations == set()

    observer.record_gene_set("Mitochondrial quality control", ["PINK1", "PRKN"])
    _, relations = _graph(tmpdir)
    assert ("Mitochondrial quality control", "ASSOCIATED_WITH", "mitophagy") in relations


def test_counters_track_what_was_written(observer):
    observer.observe("get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE)
    assert observer.entities_written > 0
    assert observer.relations_written > 0
    assert observer.failures == 0


def test_snapshot_reads_the_whole_graph(observer):
    observer.observe("get_pathway_for_gene_set", {"gene_set": "PINK1,PRKN"}, PATHWAY_RESPONSE)
    entities, relations = observer.snapshot()
    assert {e["name"] for e in entities} >= {"PINK1", "PRKN", "Mitophagy - animal"}
    assert len(relations) == 2


def test_unsupported_tool_writes_nothing(observer, tmpdir):
    observer.observe("execute_python_code", {"code": "print(1)"}, "1")
    entities, relations = _graph(tmpdir)
    assert not entities and not relations


def test_error_response_writes_nothing(observer, tmpdir):
    observer.observe(
        "get_disease_for_single_gene", {"gene_name": "PINK1"}, "Error: Unable to fetch data"
    )
    entities, relations = _graph(tmpdir)
    assert not entities and not relations


def test_a_broken_store_is_counted_not_raised(observer, monkeypatch):
    """An agent that answered its question has not failed because a side effect
    did."""
    def explode(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(
        "biodsa.memory.memory_graph.observer.create_entities", explode
    )
    observer.observe("get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE)
    assert observer.failures > 0


def test_unreadable_store_yields_an_empty_snapshot(observer, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(
        "biodsa.memory.memory_graph.observer.load_graph_data", explode
    )
    assert observer.snapshot() == ([], [])


def test_writing_an_empty_fragment_is_a_no_op(observer, tmpdir):
    observer.write(ExtractedGraph())
    entities, relations = _graph(tmpdir)
    assert not entities and not relations


def test_unnamed_gene_set_is_skipped(observer, tmpdir):
    observer.record_gene_set("", ["PINK1"])
    entities, _ = _graph(tmpdir)
    assert not entities


def test_known_genes_are_normalized_to_upper_case(tmpdir):
    clear_manager_cache()
    observer = ToolGraphObserver(
        context=CONTEXT, cache_dir=str(tmpdir), known_genes=["pink1", " prkn "]
    )
    assert observer.known_genes == {"PINK1", "PRKN"}
    clear_manager_cache()
