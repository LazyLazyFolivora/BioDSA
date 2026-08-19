from dataclasses import dataclass
from typing import List, Dict
import json
import hashlib


def normalize_observations(value: object) -> List[str]:
    """Coerce an observations field into a list of non-empty strings.

    Entity is a dataclass, so its List[str] annotation carries no runtime check.
    Models do send this field as a single string, and left alone that string
    reaches the graph file verbatim, where everything that counts or iterates
    observations then sees characters instead of observations.
    """
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = [value]

    texts: List[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        text = (candidate if isinstance(candidate, str) else str(candidate)).strip()
        if text:
            texts.append(text)
    return texts


@dataclass
class Entity:
    """Represents an entity in the knowledge graph."""
    name: str
    entity_type: str
    observations: List[str]

    def __post_init__(self) -> None:
        self.observations = normalize_observations(self.observations)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "entityType": self.entity_type,
            "observations": self.observations
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'Entity':
        return cls(
            name=data["name"],
            entity_type=data.get("entityType", ""),
            observations=data.get("observations", [])
        )


def normalize_strength(value: object) -> float:
    """Coerce a relation strength into a float clamped to [0, 1].

    The dataclass annotation carries no runtime check, and models send this field
    as a string, omit it, or emit None. A missing or unparsable value falls back
    to a neutral 0.5 so the edge keeps its default thickness.
    """
    if value is None:
        return 0.5
    try:
        strength = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(1.0, max(0.0, strength))


@dataclass
class Relation:
    """Represents a relation between entities in the knowledge graph."""
    from_entity: str
    to_entity: str
    relation_type: str
    strength: float = 0.5

    def __post_init__(self) -> None:
        self.strength = normalize_strength(self.strength)

    def to_dict(self) -> Dict:
        return {
            "from": self.from_entity,
            "to": self.to_entity,
            "relationType": self.relation_type,
            "strength": self.strength,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'Relation':
        return cls(
            from_entity=data["from"],
            to_entity=data["to"],
            relation_type=data["relationType"],
            strength=data.get("strength", 0.5),
        )


@dataclass
class KnowledgeGraph:
    """Represents the complete knowledge graph."""
    entities: List[Entity]
    relations: List[Relation]

    def to_dict(self) -> Dict:
        return {
            "entities": [entity.to_dict() for entity in self.entities],
            "relations": [relation.to_dict() for relation in self.relations]
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'KnowledgeGraph':
        entities = [Entity.from_dict(e) for e in data.get("entities", [])]
        relations = [Relation.from_dict(r) for r in data.get("relations", [])]
        return cls(entities=entities, relations=relations)



def calculate_entities_hash(entities: List[Entity]) -> str:
    """
    Calculate a hash of entities to detect changes.
    Optimized to avoid expensive sorting and JSON serialization.
    """
    if not entities:
        return hashlib.md5(b"").hexdigest()
    
    # Fast hash: just count entities and use first/last few names
    # This is a lightweight check - doesn't need to be cryptographically perfect
    hash_input = f"{len(entities)}"
    
    # Use a subset of entity names for speed (first 10, last 10)
    if len(entities) <= 20:
        names = [e.name for e in entities]
    else:
        names = [e.name for e in entities[:10]] + [e.name for e in entities[-10:]]
    
    hash_input += "|".join(sorted(names))
    
    return hashlib.md5(hash_input.encode()).hexdigest()