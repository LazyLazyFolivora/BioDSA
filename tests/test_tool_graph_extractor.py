"""
Tests for reading knowledge-graph structure out of database tool output.

The payloads below are verbatim responses from the live endpoints, wrapped the
way biodsa/agents/geneagent/tools.py wraps them. That fidelity is the point: the
extractors parse strings assembled by someone else's f-string, so a hand-tidied
fixture would test the wrong thing.
"""

import json

import pytest

from biodsa.memory.memory_graph.tool_graph_extractor import (
    ASSOCIATED_WITH,
    BINDS,
    HAS_GENESET_MEMBER,
    MEMBER_OF_PATHWAY,
    build_gene_set_graph,
    extract_from_tool_result,
    link_terms_to_set,
)


def _edges(graph):
    return {
        (r["from_entity"], r["relation_type"], r["to_entity"])
        for r in graph.relations
    }


def _entity(graph, name):
    for entity in graph.entities:
        if entity["name"] == name:
            return entity
    raise AssertionError(f"{name!r} not among {[e['name'] for e in graph.entities]}")


# ---------------------------------------------------------------------------
# Gene-disease associations (PubTator)
# ---------------------------------------------------------------------------

DISEASE_RESPONSE = "Disease associations for PINK1:\n" + json.dumps([
    {"gene_name": "PINK1", "disease_id": "DOID:331",
     "disease_name": "Central nervous system disease", "count": 1},
    {"gene_name": "PINK1", "disease_id": "ICD10:G96",
     "disease_name": "Cerebrospinal fluid leak  unspecified", "count": 1},
    {"gene_name": "PINK1", "disease_id": "DOID:4", "disease_name": "Disease", "count": 1},
    {"gene_name": "PINK1", "disease_id": "DOID:7",
     "disease_name": "Disease of anatomical entity", "count": 1},
    {"gene_name": "PINK1", "disease_id": "MESH:D010300",
     "disease_name": "Parkinson Disease", "count": 42},
])


def test_disease_edges_are_extracted():
    graph = extract_from_tool_result(
        "get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE
    )
    assert ("PINK1", ASSOCIATED_WITH, "Parkinson Disease") in _edges(graph)
    assert _entity(graph, "PINK1")["entity_type"] == "GENE"
    assert _entity(graph, "Parkinson Disease")["entity_type"] == "DISEASE"


def test_upper_ontology_diseases_are_dropped():
    """Every gene is "associated with" DOID:4; such a node informs nobody."""
    graph = extract_from_tool_result(
        "get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE
    )
    names = {e["name"] for e in graph.entities}
    assert "Disease" not in names
    assert "Disease of anatomical entity" not in names


def test_icd10_residual_categories_are_dropped():
    graph = extract_from_tool_result(
        "get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE
    )
    assert not [e for e in graph.entities if "unspecified" in e["name"].lower()]


def test_disease_observation_records_provenance_and_support():
    graph = extract_from_tool_result(
        "get_disease_for_single_gene", {"gene_name": "PINK1"}, DISEASE_RESPONSE
    )
    observations = _entity(graph, "Parkinson Disease")["observations"]
    assert any("42" in o and "PubTator" in o for o in observations)
    assert any("MESH:D010300" in o for o in observations)


def test_diseases_are_ordered_by_support():
    """The endpoint returns a long alphabetical tail; the cap has to keep the
    best-supported rows, not the alphabetically luckiest ones."""
    rows = [
        {"gene_name": "PINK1", "disease_id": f"DOID:{i}",
         "disease_name": f"Aardvark disease {i}", "count": 1}
        for i in range(30)
    ]
    rows.append({"gene_name": "PINK1", "disease_id": "MESH:D010300",
                 "disease_name": "Parkinson Disease", "count": 99})
    graph = extract_from_tool_result(
        "get_disease_for_single_gene", {"gene_name": "PINK1"}, json.dumps(rows)
    )
    assert "Parkinson Disease" in {e["name"] for e in graph.entities}


# ---------------------------------------------------------------------------
# Protein-protein interactions (PubTator)
# ---------------------------------------------------------------------------

PPI_RESPONSE = "Protein-protein interactions for PINK1:\n" + json.dumps([
    {"gene1_name": "PINK1", "gene2_name": "PINK1", "count": 4},
    {"gene1_name": "PINK1", "gene2_name": "SIAH3", "count": 4},
    {"gene1_name": "SIAH3", "gene2_name": "PINK1", "count": 3},
    {"gene1_name": "PINK1", "gene2_name": "SIRT1", "count": 2},
])


def test_interactions_become_binds_edges():
    graph = extract_from_tool_result(
        "get_interactions_for_gene_set", {"gene_set": "PINK1"}, PPI_RESPONSE
    )
    assert ("PINK1", BINDS, "SIRT1") in _edges(graph)


def test_self_interaction_is_dropped():
    """PINK1-PINK1 draws a loop on the node and states nothing."""
    graph = extract_from_tool_result(
        "get_interactions_for_gene_set", {"gene_set": "PINK1"}, PPI_RESPONSE
    )
    assert not [
        r for r in graph.relations if r["from_entity"] == r["to_entity"]
    ]


def test_symmetric_interaction_collapses_to_one_edge():
    """Binding is symmetric, so A-B and B-A are the same fact."""
    graph = extract_from_tool_result(
        "get_interactions_for_gene_set", {"gene_set": "PINK1"}, PPI_RESPONSE
    )
    pink_siah = [
        r for r in graph.relations
        if {r["from_entity"], r["to_entity"]} == {"PINK1", "SIAH3"}
    ]
    assert len(pink_siah) == 1


# ---------------------------------------------------------------------------
# Pathway enrichment (Enrichr)
# ---------------------------------------------------------------------------

PATHWAY_RESPONSE = "Pathway analysis results for PINK1,PRKN,SNCA:\n" + json.dumps([
    {"term": "Parkinson disease", "overlapping genes": "PINK1,PRKN,SNCA",
     "database": "KEGG_2021_Human"},
    {"term": "Mitophagy - animal", "overlapping genes": "PINK1,PRKN",
     "database": "KEGG_2021_Human"},
])


def test_pathway_membership_uses_named_overlapping_genes():
    """Enrichr names the overlap, so these edges are exact rather than inferred
    from set-level enrichment."""
    graph = extract_from_tool_result(
        "get_pathway_for_gene_set", {"gene_set": "PINK1,PRKN,SNCA"}, PATHWAY_RESPONSE
    )
    edges = _edges(graph)
    assert ("PINK1", MEMBER_OF_PATHWAY, "Mitophagy - animal") in edges
    assert ("PRKN", MEMBER_OF_PATHWAY, "Mitophagy - animal") in edges
    assert ("SNCA", MEMBER_OF_PATHWAY, "Mitophagy - animal") not in edges


def test_pathway_node_records_source_database():
    graph = extract_from_tool_result(
        "get_pathway_for_gene_set", {"gene_set": "PINK1,PRKN,SNCA"}, PATHWAY_RESPONSE
    )
    observations = _entity(graph, "Parkinson disease")["observations"]
    assert any("KEGG_2021_Human" in o for o in observations)


def test_pathway_disease_name_is_typed_as_pathway():
    """KEGG names a pathway "Parkinson disease". It is still a pathway, and
    typing it as a disease would produce a gene MEMBER_OF_PATHWAY disease edge."""
    graph = extract_from_tool_result(
        "get_pathway_for_gene_set", {"gene_set": "PINK1,PRKN,SNCA"}, PATHWAY_RESPONSE
    )
    assert _entity(graph, "Parkinson disease")["entity_type"] == "PATHWAY"


# ---------------------------------------------------------------------------
# Conserved domains (NCBI CDD)
# ---------------------------------------------------------------------------

DOMAIN_RESPONSE = "Protein domains for PINK1:\n" + json.dumps([
    {"gene_name": "PINK1", "domain_id": "cd14018",
     "domain_name": "serine/threonine-protein kinase PINK1", "count": 1},
])


def test_domain_is_recorded_as_observation_not_node():
    """The domain name restates the gene, so a node would duplicate it."""
    graph = extract_from_tool_result(
        "get_domain_for_single_gene", {"gene_name": "PINK1"}, DOMAIN_RESPONSE
    )
    assert graph.relations == []
    assert {e["name"] for e in graph.entities} == {"PINK1"}
    assert any(
        "cd14018" in content
        for record in graph.observations
        for content in record["contents"]
    )


# ---------------------------------------------------------------------------
# NCBI Gene summary — a Python dict interpolated into a string
# ---------------------------------------------------------------------------

GENE_SUMMARY_RESPONSE = (
    "Gene summary for PINK1 (Homo):\n"
    + str({
        "uid": "65018",
        "name": "PINK1",
        "description": "PTEN induced kinase 1",
        "nomenclaturesymbol": "PINK1",
        "nomenclaturename": "PTEN induced kinase 1",
        "summary": "This gene encodes a serine/threonine protein kinase that "
                   "localizes to mitochondria. Mutations in this gene cause one "
                   "form of autosomal recessive early-onset Parkinson disease.",
        "otheraliases": "BRPK, PARK6, PTENIK",
        "chromosome": "1",
        "maplocation": "1p36.12",
    })
)


def test_gene_summary_parses_python_repr_not_json():
    """The wrapper interpolates a dict, so the payload is single-quoted repr and
    json.loads cannot read it."""
    graph = extract_from_tool_result(
        "get_gene_summary_for_single_gene",
        {"gene_name": "PINK1", "specie": "Homo"},
        GENE_SUMMARY_RESPONSE,
    )
    contents = [c for r in graph.observations for c in r["contents"]]
    assert any("serine/threonine protein kinase" in c for c in contents)
    assert any("1p36.12" in c for c in contents)
    assert any("PARK6" in c for c in contents)


def test_gene_summary_attaches_to_the_official_symbol():
    graph = extract_from_tool_result(
        "get_gene_summary_for_single_gene",
        {"gene_name": "PINK1", "specie": "Homo"},
        GENE_SUMMARY_RESPONSE,
    )
    assert {r["entityName"] for r in graph.observations} == {"PINK1"}


# ---------------------------------------------------------------------------
# Literature (PubMed)
# ---------------------------------------------------------------------------

PUBMED_RESPONSE = (
    "PMID: 36503124\nTitle: PINK1/Parkin-mediated mitophagy\nAbstract: ...\n"
    "PMID: 21317550\nTitle: DJ-1 regulation of mitochondrial function\nAbstract: ...\n"
)


def test_literature_is_attributed_only_to_known_query_genes():
    graph = extract_from_tool_result(
        "get_pubmed_articles",
        {"term": "PINK1 mitophagy"},
        PUBMED_RESPONSE,
        known_genes={"PINK1", "PRKN"},
    )
    assert {r["entityName"] for r in graph.observations} == {"PINK1"}
    contents = [c for r in graph.observations for c in r["contents"]]
    assert any("PMID:36503124" in c for c in contents)


def test_literature_ignores_capitalized_words_that_are_not_query_genes():
    """A loose symbol pattern must not turn "AND" or "OR" into a gene."""
    graph = extract_from_tool_result(
        "get_pubmed_articles",
        {"term": "MITOPHAGY AND AUTOPHAGY"},
        PUBMED_RESPONSE,
        known_genes={"PINK1"},
    )
    assert graph.observations == []


def test_literature_without_known_genes_yields_nothing():
    graph = extract_from_tool_result(
        "get_pubmed_articles", {"term": "PINK1"}, PUBMED_RESPONSE, known_genes=None
    )
    assert not graph


# ---------------------------------------------------------------------------
# Gene-set conclusion
# ---------------------------------------------------------------------------

def test_gene_set_membership_edges():
    graph = build_gene_set_graph(
        "Mitochondrial quality control",
        ["PINK1", "PRKN", "PARK7"],
        observations=["Annotated by GeneAgent after verification"],
    )
    edges = _edges(graph)
    assert ("Mitochondrial quality control", HAS_GENESET_MEMBER, "PINK1") in edges
    assert len(edges) == 3
    assert _entity(graph, "Mitochondrial quality control")["entity_type"] == "GENE_SET"


def test_gene_set_ignores_blank_members():
    graph = build_gene_set_graph("Some process", ["PINK1", "", "  "])
    assert len(graph.relations) == 1


def test_unnamed_gene_set_yields_nothing():
    assert not build_gene_set_graph("", ["PINK1"])


def test_enrichment_terms_link_at_set_level():
    graph = link_terms_to_set("Mitochondrial quality control", ["mitophagy", "autophagy"])
    assert ("Mitochondrial quality control", ASSOCIATED_WITH, "mitophagy") in _edges(graph)


def test_set_is_not_linked_to_itself():
    graph = link_terms_to_set("mitophagy", ["mitophagy"])
    assert graph.relations == []


# ---------------------------------------------------------------------------
# Robustness: a graph is never worth failing a run over
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "",
    "Error: Unable to fetch data",
    "Disease associations for PINK1:\n[]",
    "Disease associations for PINK1:\n{}",
    "not json at all {{{",
    None,
    123,
])
def test_unparseable_payloads_yield_empty_graphs(payload):
    for tool in (
        "get_disease_for_single_gene",
        "get_interactions_for_gene_set",
        "get_pathway_for_gene_set",
        "get_enrichment_for_gene_set",
        "get_gene_summary_for_single_gene",
        "get_domain_for_single_gene",
        "get_complex_for_gene_set",
    ):
        assert not extract_from_tool_result(tool, {}, payload)


def test_unsupported_tool_yields_empty_graph():
    assert not extract_from_tool_result("execute_python_code", {}, "whatever")


def test_rows_missing_expected_fields_are_skipped():
    payload = json.dumps([{"gene_name": "PINK1"}, {"disease_name": "Parkinson Disease"}])
    assert not extract_from_tool_result("get_disease_for_single_gene", {}, payload)


def test_full_response_body_is_tolerated():
    """Callers may pass the whole endpoint response rather than its results."""
    payload = json.dumps({
        "table": "disease",
        "query": "(gene_name IN ('PINK1'))",
        "results": [{"gene_name": "PINK1", "disease_id": "MESH:D010300",
                     "disease_name": "Parkinson Disease", "count": 7}],
    })
    graph = extract_from_tool_result("get_disease_for_single_gene", {}, payload)
    assert ("PINK1", ASSOCIATED_WITH, "Parkinson Disease") in _edges(graph)


def test_duplicate_mentions_collapse():
    payload = json.dumps([
        {"gene_name": "PINK1", "disease_id": "MESH:D010300",
         "disease_name": "Parkinson Disease", "count": 7},
        {"gene_name": "PINK1", "disease_id": "MESH:D010300",
         "disease_name": "Parkinson Disease", "count": 7},
    ])
    graph = extract_from_tool_result("get_disease_for_single_gene", {}, payload)
    assert len(graph.relations) == 1
    assert len([e for e in graph.entities if e["name"] == "PINK1"]) == 1
