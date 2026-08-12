"""
Build a knowledge graph beside an agent that was not written to build one.

Most agents in this repo reproduce a published method, and their published
method does not include a knowledge graph. Handing them graph-writing tools
would change what the paper's agent does; watching their database calls does
not. The observer sits on the tool-call path, reads structure out of responses
that were already structured, and writes it to the same store the DeepEvidence
agent uses — so a graph appears without a single token of the agent's reasoning
being spent on it.

Nothing here is allowed to fail a run. Every write is guarded, and failures are
counted and logged rather than raised: an agent that answered its question
correctly has not failed just because a side effect did.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .tool import add_observations, create_entities, create_relations, load_graph_data
from .tool_graph_extractor import (
    ExtractedGraph,
    build_gene_set_graph,
    extract_from_tool_result,
    link_terms_to_set,
)

logger = logging.getLogger(__name__)

# Tools whose enrichment terms describe the query set as a whole rather than any
# single member, so their nodes stay unattached until the set has a name.
_SET_LEVEL_TOOLS = ("get_enrichment_for_gene_set",)


class ToolGraphObserver:
    """Turns tool responses into graph writes.

    Args:
        context: Graph name to write into, shared with the other agents so one
            view can render everything.
        cache_dir: Graph store location. None uses the process default.
        session_id: Identifier used in the graph event log.
        known_genes: Symbols known to belong to the query, used to attribute
            literature without guessing.
    """

    def __init__(
        self,
        context: str = "evidence_graph",
        cache_dir: Optional[str] = None,
        session_id: Optional[str] = None,
        known_genes: Optional[Iterable[str]] = None,
    ):
        self.context = context
        self.cache_dir = cache_dir
        self.session_id = session_id
        self.known_genes: Set[str] = {
            str(g).strip().upper() for g in (known_genes or ()) if str(g).strip()
        }

        self.entities_written = 0
        self.relations_written = 0
        self.observations_written = 0
        self.failures = 0

        self._step = 0
        self._started = time.time()
        self._set_level_terms: List[str] = []

    # -- observation ------------------------------------------------------

    def observe(self, tool_name: str, tool_args: Any, tool_result: Any) -> ExtractedGraph:
        """Extract structure from one tool response and persist it.

        Returns the fragment that was written, mostly so callers can log or
        stream it; the return value can be ignored safely.
        """
        self._step += 1
        args = tool_args if isinstance(tool_args, dict) else {}
        graph = extract_from_tool_result(
            tool_name, args, tool_result, known_genes=self.known_genes
        )
        if not graph:
            return graph

        if tool_name in _SET_LEVEL_TOOLS:
            self._remember_set_level_terms(graph)

        self.write(graph, source=tool_name)
        return graph

    def _remember_set_level_terms(self, graph: ExtractedGraph) -> None:
        for entity in graph.entities:
            name = entity.get("name")
            if name and name not in self._set_level_terms:
                self._set_level_terms.append(name)

    # -- writing ----------------------------------------------------------

    def write(self, graph: ExtractedGraph, source: str = "") -> None:
        """Persist a fragment. Never raises."""
        if not graph:
            return
        entities, observations = _split_observations(graph)
        written_entities: List[Dict[str, Any]] = []
        written_relations: List[Dict[str, str]] = []

        if entities:
            written_entities = self._guarded(
                create_entities, entities, what="entities"
            ) or []
        if graph.relations:
            # Endpoints missing from the store are created automatically, so
            # relations are safe to write even when their nodes were skipped.
            written_relations = self._guarded(
                create_relations, graph.relations, what="relations"
            ) or []
        if observations:
            # Separate from entity creation because the store skips entities
            # that already exist, which would silently drop their observations.
            written = self._guarded(
                add_observations, observations, what="observations"
            )
            if written:
                self.observations_written += len(written)

        self.entities_written += len(written_entities)
        self.relations_written += len(written_relations)
        _log_write(
            self.session_id, self._step, source, written_entities, written_relations
        )

    def _guarded(self, func, payload, what: str):
        try:
            return func(payload, self.context, self.cache_dir)
        except Exception:
            self.failures += 1
            logger.warning(
                "Could not write %s to the %s graph", what, self.context, exc_info=True
            )
            return None

    # -- conclusions ------------------------------------------------------

    def record_gene_set(
        self,
        set_name: str,
        genes: Sequence[str],
        observations: Optional[Sequence[str]] = None,
    ) -> None:
        """Write the agent's own conclusion: the set, its name, its members.

        This needs no extraction — the members are the input and the name is the
        answer — and it is what turns a scatter of database facts into a graph
        with a centre.
        """
        graph = build_gene_set_graph(set_name, genes, observations)
        if not graph:
            return
        self.write(graph, source="gene_set")
        if self._set_level_terms:
            self.write(
                link_terms_to_set(set_name, self._set_level_terms),
                source="enrichment_link",
            )

    # -- reporting --------------------------------------------------------

    def snapshot(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Read back the whole graph. Returns empty lists if it cannot be read."""
        try:
            data = load_graph_data(self.context, self.cache_dir)
        except Exception:
            logger.warning("Could not read back the %s graph", self.context, exc_info=True)
            return [], []
        if not isinstance(data, dict):
            return [], []
        return data.get("entities") or [], data.get("relations") or []

    def log_run_summary(self, query: str = "") -> Tuple[int, int]:
        """Record end-of-run totals in the graph event log."""
        entities, relations = self.snapshot()
        try:
            from biodsa.narrative.graph_log import log_run_summary

            log_run_summary(
                self.session_id,
                entities,
                relations,
                time.time() - self._started,
                query,
            )
        except Exception:
            logger.warning("Could not log the run summary", exc_info=True)
        return len(entities), len(relations)


def _split_observations(
    graph: ExtractedGraph,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Separate node creation from observation attachment.

    The store creates entities only when they are new, so observations carried
    on an entity that already exists would be dropped. Sending every observation
    down the add_observations path instead makes a repeat mention additive.
    """
    entities = [
        {"name": e["name"], "entity_type": e.get("entity_type", ""), "observations": []}
        for e in graph.entities
        if e.get("name")
    ]

    merged: Dict[str, List[str]] = {}
    for entity in graph.entities:
        name = entity.get("name")
        contents = entity.get("observations") or []
        if name and contents:
            merged.setdefault(name, []).extend(contents)
    for record in graph.observations:
        name = record.get("entityName")
        contents = record.get("contents") or []
        if name and contents:
            merged.setdefault(name, []).extend(contents)

    observations = []
    for name, contents in merged.items():
        unique: List[str] = []
        for content in contents:
            if content not in unique:
                unique.append(content)
        observations.append({"entityName": name, "contents": unique})
    return entities, observations


def _log_write(
    session_id: Optional[str],
    step: int,
    source: str,
    entities: Sequence[Dict[str, Any]],
    relations: Sequence[Dict[str, Any]],
) -> None:
    if not entities and not relations:
        return
    try:
        from biodsa.narrative.graph_log import log_graph_write

        log_graph_write(session_id, entities, relations, source=source, step=step)
    except Exception:
        logger.warning("Could not log a graph write", exc_info=True)
