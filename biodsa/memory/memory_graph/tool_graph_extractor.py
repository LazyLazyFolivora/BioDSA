"""
Derive knowledge-graph structure from biomedical database tool output.

Agents built to answer a question rather than to build a graph still call the
same curated databases — Enrichr, g:Profiler, PubTator, NCBI Gene — and those
responses are already structured triples. Reading the graph out of the tool
response, rather than asking the model to write it, has two properties we want:
the agent's reasoning is left byte-for-byte unchanged, and every edge is
traceable to the database that produced it instead of to a model's paraphrase.

Everything here is a pure function: no network, no LLM, no disk. Unexpected or
malformed payloads yield nothing rather than raising — a graph is never worth
failing an agent run over.

Entity and relation vocabularies match the DeepEvidence memory-graph protocol
(see biodsa/agents/deepevidence/prompt.py) so that graphs from different agents
can be rendered in one view.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from .gene_symbols import canonical_gene_symbol

logger = logging.getLogger(__name__)


# Entity types, from the shared protocol vocabulary.
GENE = "GENE"
PROTEIN = "PROTEIN"
DISEASE = "DISEASE"
PATHWAY = "PATHWAY"
GENE_SET = "GENE_SET"

# Relation types, from the shared protocol vocabulary.
ASSOCIATED_WITH = "ASSOCIATED_WITH"
BINDS = "BINDS"
MEMBER_OF_PATHWAY = "MEMBER_OF_PATHWAY"
HAS_GENESET_MEMBER = "HAS_GENESET_MEMBER"

# Per-call caps. The databases return long tails ordered by co-occurrence count;
# past the first handful the edges are noise that would swamp the display.
MAX_DISEASES_PER_GENE = 10
MAX_INTERACTIONS = 20
MAX_COMPLEXES = 10
MAX_DOMAINS_PER_GENE = 5
MAX_PATHWAYS = 5
MAX_ENRICHMENT_TERMS = 5
MAX_PAPERS = 5

# Co-occurrence floor for gene-disease edges. PubTator indexes any co-mention,
# so every gene picks up a tail of diseases it shares a single abstract with:
# PRKN and the common cold, LRRK2 and anodontia. Requiring a few independent
# publications drops those while leaving the documented associations, which run
# to hundreds of papers, untouched.
MIN_DISEASE_COOCCURRENCE = 3

# Upper ontology nodes carry no information: every gene is "associated with"
# DOID:4 (disease). Dropping them by identifier is more reliable than by name,
# but PubTator mixes vocabularies, so both are checked.
_UNINFORMATIVE_DISEASE_IDS = {
    "DOID:4",           # disease
    "DOID:7",           # disease of anatomical entity
    "DOID:225",         # syndrome
    "DOID:630",         # genetic disease
    "DOID:150",         # disease of mental health
    "DOID:14566",       # disease of cellular proliferation
    "DOID:0014667",     # disease of metabolism
    "DOID:0080015",     # physical disorder
    "DOID:0080014",     # chromosomal disease
    "DOID:0050177",     # monogenic disease
}

_UNINFORMATIVE_DISEASE_NAMES = {
    "disease",
    "diseases",
    "disorder",
    "disorders",
    "syndrome",
    "disease of anatomical entity",
    "disease of metabolism",
    "disease of mental health",
    "disease of cellular proliferation",
    "genetic disease",
    "monogenic disease",
    "chromosomal disease",
    "physical disorder",
    "neoplasm",
    "neoplasms",
}

# ICD-10 residual buckets ("Disorder of central nervous system, unspecified")
# name a filing category rather than a disease.
_RESIDUAL_CATEGORY = re.compile(r"\bunspecified\b|\bnot elsewhere classified\b", re.I)

# Gene symbols as they appear in free text: HGNC-style, long enough not to match
# ordinary words. Used only to attribute literature to genes already known to
# belong to the query set, so a loose pattern is safe.
_SYMBOL_IN_TEXT = re.compile(r"\b[A-Z][A-Z0-9]{1,9}\b")


@dataclass
class ExtractedGraph:
    """Graph fragments ready for `create_entities` / `create_relations` /
    `add_observations`, in that module's key spelling."""

    entities: List[Dict[str, Any]] = field(default_factory=list)
    relations: List[Dict[str, str]] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.entities or self.relations or self.observations)

    def extend(self, other: "ExtractedGraph") -> None:
        self.entities.extend(other.entities)
        self.relations.extend(other.relations)
        self.observations.extend(other.observations)

    def deduplicate(self) -> "ExtractedGraph":
        """Collapse repeats within this fragment.

        The graph store deduplicates on write, but a single tool response can
        name the same gene a dozen times, and shrinking here keeps the write and
        the log proportional to what was actually learned.
        """
        entities: Dict[str, Dict[str, Any]] = {}
        for entity in self.entities:
            name = entity.get("name")
            if not name:
                continue
            existing = entities.get(name)
            if existing is None:
                entities[name] = {
                    "name": name,
                    "entity_type": entity.get("entity_type", ""),
                    "observations": list(entity.get("observations") or []),
                }
                continue
            # First type wins; later mentions only contribute observations.
            for observation in entity.get("observations") or []:
                if observation not in existing["observations"]:
                    existing["observations"].append(observation)

        relations: Dict[tuple, Dict[str, str]] = {}
        for relation in self.relations:
            key = (
                relation.get("from_entity"),
                relation.get("to_entity"),
                relation.get("relation_type"),
            )
            if all(key) and key not in relations:
                relations[key] = relation

        observations: Dict[str, Dict[str, Any]] = {}
        for record in self.observations:
            name = record.get("entityName")
            if not name:
                continue
            bucket = observations.setdefault(name, {"entityName": name, "contents": []})
            for content in record.get("contents") or []:
                if content not in bucket["contents"]:
                    bucket["contents"].append(content)

        return ExtractedGraph(
            entities=list(entities.values()),
            relations=list(relations.values()),
            observations=[o for o in observations.values() if o["contents"]],
        )


def _payload(text: Any) -> Any:
    """Pull the structured body out of a tool response.

    The tool wrappers prepend a human-readable sentence to the database payload,
    and one of them interpolates a Python dict, which yields single-quoted repr
    rather than JSON. Both are handled; anything else returns None.
    """
    if isinstance(text, (list, dict)):
        return text
    if not isinstance(text, str):
        return None
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        candidate = text[index:]
        try:
            value, _ = json.JSONDecoder().raw_decode(candidate)
            return value
        except ValueError:
            pass
        try:
            return ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            continue
    return None


def _rows(payload: Any) -> List[Dict[str, Any]]:
    """Normalize a PubTator-style `results` body to a list of records."""
    if isinstance(payload, dict):
        # Tolerate callers that pass the full response rather than `results`.
        inner = payload.get("results", payload)
        if isinstance(inner, list):
            return [row for row in inner if isinstance(row, dict)]
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _count(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _by_count(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Highest co-occurrence count first, original order breaking ties."""
    return sorted(rows, key=_count, reverse=True)[:limit]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _symbol(value: Any) -> str:
    """A gene name in its canonical spelling.

    Applied where the name is read rather than only where the node is built, so
    that the node and the edges naming it agree.
    """
    return canonical_gene_symbol(_clean(value))


def _is_uninformative_disease(name: str, disease_id: str) -> bool:
    if disease_id and disease_id.strip() in _UNINFORMATIVE_DISEASE_IDS:
        return True
    if name.strip().lower() in _UNINFORMATIVE_DISEASE_NAMES:
        return True
    return bool(_RESIDUAL_CATEGORY.search(name))


def _gene(name: str, observations: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    return {
        "name": name,
        "entity_type": GENE,
        "observations": list(observations or []),
    }


# ---------------------------------------------------------------------------
# Per-tool extractors
# ---------------------------------------------------------------------------


def _extract_disease(args: Dict[str, Any], result: Any, **_) -> ExtractedGraph:
    """PubTator gene-disease co-occurrence.

    Rows: {gene_name, disease_id, disease_name, count}
    """
    graph = ExtractedGraph()
    for row in _by_count(_rows(_payload(result)), MAX_DISEASES_PER_GENE):
        gene = _symbol(row.get("gene_name"))
        disease = _clean(row.get("disease_name"))
        disease_id = _clean(row.get("disease_id"))
        if not gene or not disease:
            continue
        if _is_uninformative_disease(disease, disease_id):
            continue
        if _count(row) < MIN_DISEASE_COOCCURRENCE:
            continue
        note = f"Co-occurs with {gene} in {_count(row)} indexed publications"
        if disease_id:
            note += f"; {disease_id}"
        graph.entities.append(_gene(gene))
        graph.entities.append({
            "name": disease,
            "entity_type": DISEASE,
            "observations": [f"{note} (source: PubTator)"],
        })
        graph.relations.append({
            "from_entity": gene,
            "to_entity": disease,
            "relation_type": ASSOCIATED_WITH,
        })
    return graph


def _extract_interactions(args: Dict[str, Any], result: Any, **_) -> ExtractedGraph:
    """PubTator protein-protein interactions.

    Rows: {gene1_name, gene2_name, count}

    Self-interactions are dropped, and because binding is symmetric the pair is
    ordered so that A-B and B-A collapse to one edge.
    """
    graph = ExtractedGraph()
    for row in _by_count(_rows(_payload(result)), MAX_INTERACTIONS):
        left = _symbol(row.get("gene1_name"))
        right = _symbol(row.get("gene2_name"))
        if not left or not right or left == right:
            continue
        source, target = sorted((left, right))
        graph.entities.append(_gene(source))
        graph.entities.append(_gene(target))
        graph.relations.append({
            "from_entity": source,
            "to_entity": target,
            "relation_type": BINDS,
        })
    return graph


def _extract_complex(args: Dict[str, Any], result: Any, **_) -> ExtractedGraph:
    """PubTator protein complexes.

    Field names are probed rather than assumed: this endpoint returns an empty
    result for many gene sets, so its exact schema is rarely observable.
    """
    graph = ExtractedGraph()
    for row in _by_count(_rows(_payload(result)), MAX_COMPLEXES):
        gene = _symbol(row.get("gene_name") or row.get("gene1_name"))
        complex_name = _clean(
            row.get("complex_name") or row.get("name") or row.get("complex")
        )
        complex_id = _clean(row.get("complex_id") or row.get("id"))
        if not gene or not complex_name:
            continue
        note = f"Complex containing {gene}"
        if complex_id:
            note += f"; {complex_id}"
        graph.entities.append(_gene(gene))
        graph.entities.append({
            "name": complex_name,
            "entity_type": PROTEIN,
            "observations": [f"{note} (source: PubTator)"],
        })
        graph.relations.append({
            "from_entity": gene,
            "to_entity": complex_name,
            "relation_type": BINDS,
        })
    return graph


def _extract_domain(args: Dict[str, Any], result: Any, **_) -> ExtractedGraph:
    """NCBI conserved domains.

    Rows: {gene_name, domain_id, domain_name, count}

    A domain is a property of its gene rather than a concept other findings
    attach to, and its name usually restates the gene ("serine/threonine-protein
    kinase PINK1"), so this records observations instead of nodes.
    """
    graph = ExtractedGraph()
    for row in _by_count(_rows(_payload(result)), MAX_DOMAINS_PER_GENE):
        gene = _symbol(row.get("gene_name"))
        domain = _clean(row.get("domain_name"))
        domain_id = _clean(row.get("domain_id"))
        if not gene or not domain:
            continue
        note = f"Conserved domain: {domain}"
        if domain_id:
            note += f" ({domain_id})"
        graph.entities.append(_gene(gene))
        graph.observations.append({
            "entityName": gene,
            "contents": [f"{note} (source: NCBI CDD)"],
        })
    return graph


def _extract_pathway(args: Dict[str, Any], result: Any, **_) -> ExtractedGraph:
    """Enrichr pathway enrichment.

    Rows: {term, "overlapping genes": "A,B,C", database}

    The most valuable of these extractors: the overlapping genes are named, so
    the gene-to-pathway edges are exact rather than inferred from set-level
    enrichment.
    """
    graph = ExtractedGraph()
    payload = _payload(result)
    rows = payload if isinstance(payload, list) else _rows(payload)
    for row in rows[:MAX_PATHWAYS]:
        if not isinstance(row, dict):
            continue
        term = _clean(row.get("term"))
        if not term:
            continue
        database = _clean(row.get("database"))
        raw_genes = row.get("overlapping genes") or row.get("overlapping_genes") or ""
        genes = [_symbol(g) for g in str(raw_genes).split(",") if _clean(g)]
        note = f"Enriched in the query gene set ({len(genes)} overlapping genes)"
        graph.entities.append({
            "name": term,
            "entity_type": PATHWAY,
            "observations": [f"{note} (source: {database or 'Enrichr'})"],
        })
        for gene in genes:
            graph.entities.append(_gene(gene))
            graph.relations.append({
                "from_entity": gene,
                "to_entity": term,
                "relation_type": MEMBER_OF_PATHWAY,
            })
    return graph


def _extract_enrichment(args: Dict[str, Any], result: Any, **_) -> ExtractedGraph:
    """g:Profiler functional enrichment.

    Rows: {native, name, source, p_value, intersection_size, ...}

    No edges are produced. g:Profiler is called without evidence lists, so which
    genes belong to a term is unknown; attaching every query gene to every term
    would invent memberships the data does not support. Callers that have a
    gene-set node can link these terms at set level via `link_terms_to_set`.
    """
    graph = ExtractedGraph()
    payload = _payload(result)
    rows = payload if isinstance(payload, list) else _rows(payload)
    for row in rows[:MAX_ENRICHMENT_TERMS]:
        if not isinstance(row, dict):
            continue
        name = _clean(row.get("name"))
        if not name:
            continue
        native = _clean(row.get("native"))
        source = _clean(row.get("source"))
        note = "Enriched in the query gene set"
        try:
            note += f", p={float(row['p_value']):.2e}"
        except (KeyError, TypeError, ValueError):
            pass
        size = row.get("intersection_size")
        if isinstance(size, int):
            note += f", {size} genes"
        if native:
            note += f"; {native}"
        graph.entities.append({
            "name": name,
            "entity_type": PATHWAY,
            "observations": [f"{note} (source: g:Profiler {source})".rstrip()],
        })
    return graph


def _extract_gene_summary(args: Dict[str, Any], result: Any, **_) -> ExtractedGraph:
    """NCBI Gene summary.

    The payload is an esummary record interpolated into the response as a Python
    dict. Its prose fields are the authoritative description of what the gene
    does, which is exactly what makes a node worth clicking on.
    """
    graph = ExtractedGraph()
    payload = _payload(result)
    if not isinstance(payload, dict):
        return graph
    record = payload.get("result", payload)
    if not isinstance(record, dict):
        return graph

    symbol = _symbol(
        record.get("nomenclaturesymbol")
        or record.get("name")
        or args.get("gene_name")
    )
    if not symbol:
        return graph

    notes: List[str] = []
    full_name = _clean(record.get("nomenclaturename") or record.get("description"))
    if full_name:
        notes.append(f"{full_name} (source: NCBI Gene)")
    summary = _clean(record.get("summary"))
    if summary:
        # Long enough to be worth truncating; the point is to characterize the
        # gene, not to mirror the whole record.
        notes.append(f"{summary[:400]} (source: NCBI Gene)")
    location = _clean(record.get("maplocation") or record.get("chromosome"))
    if location:
        notes.append(f"Chromosomal location {location} (source: NCBI Gene)")
    aliases = _clean(record.get("otheraliases"))
    if aliases:
        notes.append(f"Also known as {aliases} (source: NCBI Gene)")

    graph.entities.append(_gene(symbol))
    if notes:
        graph.observations.append({"entityName": symbol, "contents": notes})
    return graph


def _extract_pubmed(
    args: Dict[str, Any],
    result: Any,
    known_genes: Optional[Set[str]] = None,
    **_,
) -> ExtractedGraph:
    """PubMed search results.

    Records citations as observations on the genes named in the query rather
    than as paper nodes: which claim a paper supports is a judgement the model
    makes in prose, and a mechanically attached edge would assert more than the
    search result does. Attribution is limited to genes already known to belong
    to the query set, so an incidental capitalized word cannot pull in an edge.
    """
    graph = ExtractedGraph()
    if not known_genes:
        return graph
    text = result if isinstance(result, str) else str(result)
    pmids = re.findall(r"PMID:\s*(\d+)", text)
    if not pmids:
        return graph

    term = str(args.get("term") or "")
    mentioned = set()
    for raw in _SYMBOL_IN_TEXT.findall(term.upper()):
        canonical = canonical_gene_symbol(raw)
        if canonical in known_genes:
            mentioned.add(canonical)
    if not mentioned:
        return graph

    citation = ", ".join(f"PMID:{pmid}" for pmid in pmids[:MAX_PAPERS])
    note = f"Literature retrieved for '{_clean(term)[:80]}': {citation} (source: PubMed)"
    for symbol in sorted(mentioned):
        graph.entities.append(_gene(symbol))
        graph.observations.append({"entityName": symbol, "contents": [note]})
    return graph


_EXTRACTORS = {
    "get_disease_for_single_gene": _extract_disease,
    "get_interactions_for_gene_set": _extract_interactions,
    "get_complex_for_gene_set": _extract_complex,
    "get_domain_for_single_gene": _extract_domain,
    "get_pathway_for_gene_set": _extract_pathway,
    "get_enrichment_for_gene_set": _extract_enrichment,
    "get_gene_summary_for_single_gene": _extract_gene_summary,
    "get_pubmed_articles": _extract_pubmed,
}


def supported_tools() -> Set[str]:
    return set(_EXTRACTORS)


def extract_from_tool_result(
    tool_name: str,
    tool_args: Optional[Dict[str, Any]],
    tool_result: Any,
    known_genes: Optional[Set[str]] = None,
) -> ExtractedGraph:
    """Read graph structure out of one tool response.

    Args:
        tool_name: Name of the tool that produced the response.
        tool_args: Arguments it was called with.
        tool_result: Its return value, usually the wrapper's decorated string.
        known_genes: Symbols known to belong to the query, used to attribute
            literature without guessing.

    Returns:
        The fragment, deduplicated. Empty for unsupported tools, error strings,
        and anything that does not parse.
    """
    extractor = _EXTRACTORS.get(tool_name)
    if extractor is None:
        return ExtractedGraph()
    try:
        graph = extractor(
            tool_args or {},
            tool_result,
            known_genes=known_genes,
        )
        return graph.deduplicate()
    except Exception:
        # A malformed payload must not surface as an agent failure.
        logger.warning("Graph extraction failed for %s", tool_name, exc_info=True)
        return ExtractedGraph()


def build_gene_set_graph(
    set_name: str,
    genes: Sequence[str],
    observations: Optional[Sequence[str]] = None,
) -> ExtractedGraph:
    """Build the set node and its membership edges.

    This is the conclusion a gene-set agent exists to produce, and it needs no
    parsing: the members are the input and the name is the answer.
    """
    graph = ExtractedGraph()
    name = _clean(set_name)
    if not name:
        return graph
    graph.entities.append({
        "name": name,
        "entity_type": GENE_SET,
        "observations": list(observations or []),
    })
    for gene in genes:
        symbol = _clean(gene)
        if not symbol:
            continue
        graph.entities.append(_gene(symbol))
        graph.relations.append({
            "from_entity": name,
            "to_entity": symbol,
            "relation_type": HAS_GENESET_MEMBER,
        })
    return graph.deduplicate()


def link_terms_to_set(set_name: str, term_names: Sequence[str]) -> ExtractedGraph:
    """Attach set-level enrichment terms to the gene-set node.

    Enrichment is a property of the set, so the edge belongs to the set node.
    Kept separate from `_extract_enrichment` because the set's name is only
    settled once the agent has finished revising it.
    """
    graph = ExtractedGraph()
    name = _clean(set_name)
    if not name:
        return graph
    for term in term_names:
        target = _clean(term)
        if not target or target == name:
            continue
        graph.relations.append({
            "from_entity": name,
            "to_entity": target,
            "relation_type": ASSOCIATED_WITH,
        })
    return graph.deduplicate()
