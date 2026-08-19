"""
Narrative event model for agent reasoning visualization.

Events capture the agent's research progress as structured signals
suitable for real-time knowledge graph rendering.
"""

import uuid
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List

# typing generics rather than PEP 585: these annotations are evaluated at import
# time, so dict[str, str] here makes the whole module unimportable below 3.9.
ENTITY_TYPE_NORMALIZE: Dict[str, str] = {
    "GENE": "gene", "PROTEIN": "gene",
    "VARIANT": "variant",
    "DRUG": "drug", "CHEMICAL": "compound",
    "DISEASE": "disease", "PHENOTYPE": "disease",
    "PATHWAY": "pathway", "GENE_SET": "pathway",
    "PAPER": "literature",
    "FINDING": "finding",
    "CELL_LINE": "cell_line", "TISSUE": "tissue",
}


def _normalize_entity_type(raw: str) -> str:
    """Normalize entity type strings from AddToGraph to a consistent vocabulary."""
    return ENTITY_TYPE_NORMALIZE.get(raw.upper(), raw.lower())


@dataclass(frozen=True)
class NarrativeEvent:
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_type"] = self.__class__.__name__
        return d


@dataclass(frozen=True)
class EntitySearching(NarrativeEvent):
    """Agent is about to search for a specific entity from a knowledge base."""
    entity_name: str = ""
    entity_type: str = ""   # gene | drug | disease | variant | pathway | compound | target
    source_kb: str = ""     # e.g. "gene", "drug", "pubmed_papers"
    search_term: str = ""   # raw search_term from tool args (may differ from entity_name for literature)


@dataclass(frozen=True)
class LiteratureSearching(NarrativeEvent):
    """Agent is searching the literature for a conceptual query (not a specific entity)."""
    query: str = ""
    source_kb: str = "pubmed_papers"


@dataclass(frozen=True)
class EntityConfirmed(NarrativeEvent):
    """Agent confirmed an entity as important and wrote it to the evidence graph."""
    entity_name: str = ""
    entity_type: str = ""   # normalized
    observations: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RelationFound(NarrativeEvent):
    """Agent discovered a relationship between two entities."""
    source_entity: str = ""
    target_entity: str = ""
    relation_type: str = ""
    strength: float = 0.5  # 0..1, LLM-scored: a thicker rendered edge = stronger relation


@dataclass(frozen=True)
class PhaseChange(NarrativeEvent):
    """Agent switched research phase (BFS → DFS or vice versa)."""
    phase: str = ""              # broad_search | deep_dive
    search_target: str = ""      # what the orchestrator asked the sub-agent to research
    knowledge_bases: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class EntityPlanned(NarrativeEvent):
    """Orchestrator declared an entity it intends to search, before confirmation."""
    entity_name: str = ""   # canonical name/ID, e.g. "EGFR", "PMID:12345"
    entity_type: str = ""   # normalized: gene|drug|disease|variant|target|compound|pathway|cell_line|tissue|finding|literature
    search_target: str = "" # provenance: the parent search_target this plan came from
    confidence: float = 0.5  # 0..1, LLM-scored: higher when the entity is named in the question


@dataclass(frozen=True)
class Progress(NarrativeEvent):
    """Periodic progress snapshot emitted every N steps."""
    step: int = 0
    total_steps_estimate: int = 30
    entities_found: int = 0
    relations_found: int = 0
    current_phase: str = ""


@dataclass(frozen=True)
class RunComplete(NarrativeEvent):
    """Agent finished. Carries the final graph snapshot for persistence."""
    entities: List[dict] = field(default_factory=list)
    relations: List[dict] = field(default_factory=list)
    total_steps: int = 0
    duration_seconds: float = 0.0
    final_response_preview: str = ""
