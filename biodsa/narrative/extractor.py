"""
Event extractor — converts LangGraph AIMessage tool_calls into NarrativeEvent list.

No regex. All extraction comes from structured tool_calls args.
"""

import json
from typing import Dict, List, Optional, Tuple

from biodsa.narrative.events import (
    NarrativeEvent,
    EntitySearching,
    LiteratureSearching,
    EntityConfirmed,
    RelationFound,
    PhaseChange,
    _normalize_entity_type,
)

# ── tool_name → (entity_type, arg_key, source_kb) ──────────────────────
# For tools that target a specific named entity (gene name, drug name, etc.).

ENTITY_SEARCH_TOOLS: Dict[str, Tuple[str, str, str]] = {
    # gene
    "unified_gene_search":      ("gene",    "search_term",  "gene"),
    "fetch_gene_details":       ("gene",    "gene_id",      "gene"),
    # drug
    "unified_drug_search":      ("drug",    "search_term",  "drug"),
    "fetch_drug_details":       ("drug",    "drug_id",      "drug"),
    # disease
    "unified_disease_search":   ("disease", "search_term",  "disease"),
    "fetch_disease_details":    ("disease", "disease_id",   "disease"),
    # variant
    "search_variants":          ("variant", "search_term",  "variant"),
    "fetch_variant_details":    ("variant", "variant_id",   "variant"),
    # target
    "unified_target_search":    ("target",  "search_term",  "target"),
    "fetch_target_details":     ("target",  "target_id",    "target"),
    # compound
    "unified_compound_search":  ("compound","search_term",  "compound"),
    "fetch_compound_details":   ("compound","compound_id",  "compound"),
    # pathway
    "unified_pathway_search":   ("pathway", "search_term",  "pathway"),
    "fetch_pathway_details":    ("pathway", "pathway_id",   "pathway"),
}

# ── tools that search broadly (free text queries, not a named entity) ──

LITERATURE_SEARCH_TOOLS: Dict[str, str] = {
    "search_papers":            "pubmed_papers",
    "find_entities":            "pubmed_papers",
    "find_related_entities":    "pubmed_papers",
    "fetch_paper_content":      "pubmed_papers",
    "get_paper_references":     "pubmed_papers",
    "fetch_paper_annotations":  "pubmed_papers",
    "search_trials":            "clinical_trials",
    "fetch_trial_details":      "clinical_trials",
    "web_search":               "web_search",
}


def _extract_text_arg(args: dict, keys: List[str]) -> Optional[str]:
    """Return the first non-empty string value from any of the given keys."""
    for key in keys:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _as_dict_list(value) -> List[dict]:
    """Normalise a tool argument that should be a list of objects.

    Models routinely pass such arguments as a JSON string, or as a single object
    instead of a one-item list. Iterating a string yields characters, so without
    this the entities would be dropped without a trace.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def extract_events(message, step_num: int = 0) -> List[NarrativeEvent]:
    """
    Convert a LangGraph AIMessage (with optional tool_calls) into narrative events.

    Call this once per stream chunk from agent_graph.stream().
    """
    if not hasattr(message, "tool_calls") or not message.tool_calls:
        return []

    events: list[NarrativeEvent] = []

    for tc in message.tool_calls:
        name: str = tc.get("name", "")
        args: dict = tc.get("args", {}) or {}

        # ── entity search tools ──────────────────────────────────
        if name in ENTITY_SEARCH_TOOLS:
            entity_type, arg_key, source_kb = ENTITY_SEARCH_TOOLS[name]
            entity_name = _extract_text_arg(args, [arg_key])
            if entity_name:
                events.append(EntitySearching(
                    entity_name=entity_name,
                    entity_type=entity_type,
                    source_kb=source_kb,
                ))

        # ── literature / free-text search tools ──────────────────
        elif name in LITERATURE_SEARCH_TOOLS:
            source_kb = LITERATURE_SEARCH_TOOLS[name]
            query = _extract_text_arg(args, [
                "query", "boolean_query_text", "condition",
                "search_term", "task_name",
            ])
            events.append(LiteratureSearching(
                query=query or "(free-text search)",
                source_kb=source_kb,
            ))

        # add_to_graph is deliberately absent: its arguments say what the model
        # intended to write, and the write can still be skipped or rejected after
        # that. Those events come from the result instead, via
        # extract_result_events.

        # ── phase change (BFS / DFS) ─────────────────────────────
        elif name == "go_breadth_first_search":
            events.append(PhaseChange(
                phase="broad_search",
                search_target=args.get("search_target", ""),
                knowledge_bases=args.get("knowledge_bases", []) or [],
            ))

        elif name == "go_depth_first_search":
            events.append(PhaseChange(
                phase="deep_dive",
                search_target=args.get("search_target", ""),
                knowledge_bases=args.get("knowledge_bases", []) or [],
            ))

    return events


def _result_payload(message) -> dict:
    """Read a tool result message as a JSON object, or {} if it is not one."""
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        return {}
    try:
        payload = json.loads(content)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def extract_result_events(message, step_num: int = 0) -> List[NarrativeEvent]:
    """Convert an add_to_graph result into events for what it actually wrote.

    Reading the request instead reported entities and relations that never
    reached the graph: a batch can be partly skipped, and the store drops
    duplicates of what it already holds. The result carries only what was
    accepted, so counts here match the graph the client ends up seeing.
    """
    if getattr(message, "name", None) != "add_to_graph":
        return []

    results = _result_payload(message).get("results")
    if not isinstance(results, dict):
        return []

    events: list[NarrativeEvent] = []

    created_entities = results.get("entities_created")
    if isinstance(created_entities, dict):
        for ent in _as_dict_list(created_entities.get("entities")):
            name = ent.get("name")
            if not name:
                continue
            events.append(EntityConfirmed(
                entity_name=name,
                # The store writes camelCase; the tool arguments were snake_case.
                entity_type=_normalize_entity_type(
                    ent.get("entityType") or ent.get("entity_type") or ""
                ),
                observations=ent.get("observations") or [],
            ))

    created_relations = results.get("relations_created")
    if isinstance(created_relations, dict):
        for rel in _as_dict_list(created_relations.get("relations")):
            source = rel.get("from") or rel.get("from_entity") or ""
            target = rel.get("to") or rel.get("to_entity") or ""
            if not source or not target:
                continue
            events.append(RelationFound(
                source_entity=source,
                target_entity=target,
                relation_type=rel.get("relationType") or rel.get("relation_type") or "",
            ))

    # Adding an observation to an unknown entity creates it, and that is the only
    # place such a node is reported.
    for obs in _as_dict_list(results.get("observations_added")):
        if not obs.get("entity_created"):
            continue
        name = obs.get("entityName") or obs.get("name")
        if not name:
            continue
        events.append(EntityConfirmed(
            entity_name=name,
            entity_type=_normalize_entity_type(""),
            observations=obs.get("addedObservations") or [],
        ))

    return events
