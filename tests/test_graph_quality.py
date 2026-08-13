"""Tests for the filters that keep a shared graph readable.

Once a conversation accumulates into one graph, three kinds of damage that were
invisible in per-call graphs become visible in it: the same gene under two
spellings, single-abstract co-mentions presented as disease associations, and
control genes dragged in from an agent's verification queries. A fourth is
plain corruption -- an observations field sent as a string, which used to be
stored verbatim and then counted one observation per character.
"""

import pytest

from biodsa.memory.memory_graph import (
    add_observations,
    clear_manager_cache,
    load_graph_data,
)
from biodsa.memory.memory_graph.gene_symbols import canonical_gene_symbol
from biodsa.memory.memory_graph.observer import ToolGraphObserver
from biodsa.memory.memory_graph.schema import Entity, normalize_observations
from biodsa.memory.memory_graph.tool_graph_extractor import (
    ExtractedGraph,
    extract_from_tool_result,
)

CONTEXT = "test_graph_quality"


@pytest.fixture
def clean_managers():
    clear_manager_cache()
    yield
    clear_manager_cache()


# -- observations that arrive as a string ----------------------------------


def test_a_string_of_observations_becomes_one_observation():
    # Verbatim from a production write, which then logged obs=136.
    entity = Entity(
        name="LRRK2 kinase inhibitor",
        entity_type="CHEMICAL",
        observations=(
            "Therapeutic target for LRRK2 G2019S and R1441 mutations. Inhibits "
            "hyperactive kinase activity. Clinical trials ongoing for PD treatment."
        ),
    )
    assert len(entity.observations) == 1
    assert entity.observations[0].startswith("Therapeutic target for LRRK2")


def test_a_graph_file_already_holding_a_string_is_repaired_on_read():
    entity = Entity.from_dict(
        {
            "name": "oxidative stress pathway",
            "entityType": "PATHWAY",
            "observations": "Converging mechanism across PD genes",
        }
    )
    assert entity.observations == ["Converging mechanism across PD genes"]


def test_normalization_keeps_order_and_drops_empties():
    assert normalize_observations(["second", "", None, "  first  "]) == [
        "second",
        "first",
    ]
    assert normalize_observations(None) == []
    assert normalize_observations([]) == []


def test_observations_survive_the_add_path_intact(tmpdir, clean_managers):
    # add_observations builds its list before Entity does, so a string there
    # used to be spread one character per observation by list()/extend().
    add_observations(
        [{"entityName": "PRKN", "contents": "E3 ligase recruited by PINK1"}],
        CONTEXT,
        str(tmpdir),
    )
    entities = load_graph_data(CONTEXT, str(tmpdir))["entities"]
    stored = next(e for e in entities if e["name"] == "PRKN")
    assert stored["observations"] == ["E3 ligase recruited by PINK1"]


# -- one gene, one spelling -------------------------------------------------


def test_retired_symbols_map_onto_current_ones():
    assert canonical_gene_symbol("GBA") == "GBA1"
    assert canonical_gene_symbol("PARKIN") == "PRKN"
    assert canonical_gene_symbol("PARK2") == "PRKN"
    assert canonical_gene_symbol("DJ-1") == "PARK7"
    assert canonical_gene_symbol("park8") == "LRRK2"


def test_neighbouring_genes_are_not_merged():
    # A wrong entry here would silently fuse two real genes.
    assert canonical_gene_symbol("GBA2") == "GBA2"
    assert canonical_gene_symbol("GBA3") == "GBA3"
    assert canonical_gene_symbol("PARK7") == "PARK7"
    assert canonical_gene_symbol("SNCAIP") == "SNCAIP"


def test_unrecognized_symbols_keep_their_own_case():
    # Approved symbols are not uniformly upper case.
    assert canonical_gene_symbol("C9orf72") == "C9orf72"
    assert canonical_gene_symbol("SCHIP1") == "SCHIP1"
    assert canonical_gene_symbol("") == ""


def test_extracted_edges_use_the_canonical_symbol():
    rows = [
        {
            "gene_name": "PARKIN",
            "disease_id": "MESH:D010300",
            "disease_name": "Parkinson Disease",
            "count": 412,
        }
    ]
    graph = extract_from_tool_result("get_disease_for_single_gene", {}, rows)
    assert [e["name"] for e in graph.entities if e["entity_type"] == "GENE"] == ["PRKN"]
    assert graph.relations[0]["from_entity"] == "PRKN"


# -- disease associations worth showing ------------------------------------


def test_single_abstract_co_mentions_are_dropped():
    rows = [
        {
            "gene_name": "PRKN",
            "disease_id": "MESH:D010300",
            "disease_name": "Parkinson Disease",
            "count": 412,
        },
        {
            "gene_name": "PRKN",
            "disease_id": "MESH:D000163",
            "disease_name": "Acute nasopharyngitis [common cold]",
            "count": 1,
        },
        {
            "gene_name": "PRKN",
            "disease_id": "MESH:D002971",
            "disease_name": "Cleft Palate",
            "count": 2,
        },
    ]
    graph = extract_from_tool_result("get_disease_for_single_gene", {}, rows)
    diseases = [e["name"] for e in graph.entities if e["entity_type"] == "DISEASE"]
    assert diseases == ["Parkinson Disease"]


def test_a_missing_count_does_not_pass_the_floor():
    rows = [{"gene_name": "PRKN", "disease_name": "Anodontia"}]
    graph = extract_from_tool_result("get_disease_for_single_gene", {}, rows)
    assert not graph.relations


# -- the genes actually under study ---------------------------------------


def _observer(tmpdir, known_genes):
    return ToolGraphObserver(
        context=CONTEXT, cache_dir=str(tmpdir), known_genes=known_genes
    )


def test_a_control_gene_and_its_pathways_are_left_out(tmpdir, clean_managers):
    observer = _observer(tmpdir, ["SNCA", "LRRK2"])
    graph = ExtractedGraph(
        entities=[
            {"name": "SNCA", "entity_type": "GENE", "observations": []},
            {"name": "MYC", "entity_type": "GENE", "observations": []},
            {
                "name": "Pathways of neurodegeneration",
                "entity_type": "PATHWAY",
                "observations": [],
            },
            {
                "name": "Telomerase hTERT transcriptional regulation",
                "entity_type": "PATHWAY",
                "observations": [],
            },
        ],
        relations=[
            {
                "from_entity": "SNCA",
                "to_entity": "Pathways of neurodegeneration",
                "relation_type": "MEMBER_OF_PATHWAY",
            },
            {
                "from_entity": "MYC",
                "to_entity": "Telomerase hTERT transcriptional regulation",
                "relation_type": "MEMBER_OF_PATHWAY",
            },
        ],
    )
    kept = observer._within_scope(graph)
    assert {e["name"] for e in kept.entities} == {
        "SNCA",
        "Pathways of neurodegeneration",
    }
    assert [r["from_entity"] for r in kept.relations] == ["SNCA"]


def test_an_interaction_partner_is_kept_as_an_endpoint(tmpdir, clean_managers):
    # The partner is not itself under study, but the edge to it is the finding.
    observer = _observer(tmpdir, ["SNCA"])
    graph = ExtractedGraph(
        entities=[
            {"name": "SNCA", "entity_type": "GENE", "observations": []},
            {"name": "VAMP2", "entity_type": "GENE", "observations": []},
        ],
        relations=[
            {"from_entity": "SNCA", "to_entity": "VAMP2", "relation_type": "BINDS"}
        ],
    )
    kept = observer._within_scope(graph)
    assert {e["name"] for e in kept.entities} == {"SNCA", "VAMP2"}


def test_a_query_written_with_an_alias_still_matches(tmpdir, clean_managers):
    # known_genes says PARKIN, the database answers PRKN.
    observer = _observer(tmpdir, ["PARKIN", "DJ-1"])
    rows = [
        {
            "gene_name": "PRKN",
            "disease_id": "MESH:D010300",
            "disease_name": "Parkinson Disease",
            "count": 412,
        }
    ]
    written = observer.observe("get_disease_for_single_gene", {}, rows)
    assert [r["to_entity"] for r in written.relations] == ["Parkinson Disease"]


def test_a_run_without_a_declared_gene_set_is_not_filtered(tmpdir, clean_managers):
    observer = _observer(tmpdir, [])
    graph = ExtractedGraph(
        entities=[{"name": "MYC", "entity_type": "GENE", "observations": []}],
        relations=[
            {
                "from_entity": "MYC",
                "to_entity": "Telomerase hTERT transcriptional regulation",
                "relation_type": "MEMBER_OF_PATHWAY",
            }
        ],
    )
    kept = observer._within_scope(graph)
    assert len(kept.entities) == 1
    assert len(kept.relations) == 1


def test_set_level_enrichment_is_not_filtered_out(tmpdir, clean_managers):
    # These terms describe the query set as a whole and carry no edges, so a
    # scope test on endpoints would discard all of them.
    observer = _observer(tmpdir, ["SNCA", "LRRK2"])
    rows = [
        {
            "name": "regulation of dopamine secretion",
            "native": "GO:0014059",
            "source": "GO:BP",
            "p_value": 1e-8,
            "intersection_size": 4,
        }
    ]
    written = observer.observe("get_enrichment_for_gene_set", {}, rows)
    assert [e["name"] for e in written.entities] == [
        "regulation of dopamine secretion"
    ]
