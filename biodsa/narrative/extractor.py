"""
Event extractor — converts LangGraph AIMessage tool_calls into NarrativeEvent list.

No regex. All extraction comes from structured tool_calls args.
"""

import json
from typing import List, Optional

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

ENTITY_SEARCH_TOOLS: dict[str, tuple[str, str, str]] = {
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

LITERATURE_SEARCH_TOOLS: dict[str, str] = {
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

        # ── add_to_graph ─────────────────────────────────────────
        elif name == "add_to_graph":
            for ent in _as_dict_list(args.get("entities")):
                if "name" in ent:
                    events.append(EntityConfirmed(
                        entity_name=ent["name"],
                        entity_type=_normalize_entity_type(
                            ent.get("entity_type", "")
                        ),
                        observations=ent.get("observations", []) or [],
                    ))
            for rel in _as_dict_list(args.get("relations")):
                source = rel.get("from_entity", "")
                target = rel.get("to_entity", "")
                # An edge missing either end cannot be drawn; emitting it would
                # only add a blank row to the graph stream.
                if not source or not target:
                    continue
                events.append(RelationFound(
                    source_entity=source,
                    target_entity=target,
                    relation_type=rel.get("relation_type", ""),
                ))

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
