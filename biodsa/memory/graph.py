"""
Tools that leverage the memory graph to manage the memory graph for the agent.

This module provides two simple tools:
1. AddToGraph - Add entities, relations, and observations to the memory graph
2. RetrieveFromGraph - Search and retrieve information from the memory graph
"""
from typing import Optional, List, Dict, Any, Tuple, Type
from langchain_core.tools import BaseTool, InjectedToolArg
from pydantic import BaseModel, Field
import json

from biodsa.memory.memory_graph import (
    create_entities, 
    create_relations, 
    add_observations, 
    search_nodes, 
    open_nodes, 
    get_graph_text_overview, 
    load_graph_data,
)
from biodsa.memory.memory_graph.vocabulary import screen_entities, screen_relations

class Entity(BaseModel):
    name: str
    entity_type: str
    observations: List[str]

class Relation(BaseModel):
    from_entity: str
    to_entity: str
    relation_type: str

def _as_object(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a single tool argument into a plain dict, or None if impossible."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, BaseModel):
        return value.model_dump() if hasattr(value, "model_dump") else value.dict()
    return value if isinstance(value, dict) else None


def _brief(value: Any, limit: int = 120) -> str:
    """Render a value short enough to sit inside an error message."""
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _end_of_object(text: str, start: int) -> int:
    """Return the index just past the object beginning at *start*.

    Lets a scan resume after an object that could not be decoded, instead of
    re-reading it forever. Counts braces while respecting strings, so an
    observation containing "{" does not end the object early.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _salvage_object_list(text: str) -> Tuple[List[Dict[str, Any]], int]:
    """Decode the objects of a malformed JSON array one at a time.

    Local models drop a key mid-array -- {"from_entity": "X", "relation_type":
    "Y", "Parkinson disease"} -- which leaves the whole argument undecodable even
    though the entries around it are well formed. Decoding per object keeps those
    rather than losing the batch to one bad entry.

    Returns the objects recovered and how many fragments stayed unreadable.
    """
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    broken = 0
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            broken += 1
            index = _end_of_object(text, start)
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        else:
            broken += 1
        index = end
    return objects, broken


def _as_object_list(value: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Coerce a batch argument into objects, describing what could not be read.

    Models routinely send these arguments as a JSON string, or as a bare object
    instead of a one-item list. Iterating a JSON string yields its characters,
    which is what produced "expected dict, got str. Entity: [".

    Unreadable input is reported in the second return value rather than failing
    the batch. The readable entries are still worth writing, and the description
    is what lets the model resend only the entry it got wrong.
    """
    problems: List[str] = []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            recovered, broken = _salvage_object_list(value)
            if not recovered:
                return [], ["could not be read as JSON: %s" % _brief(value)]
            if broken:
                problems.append(
                    "%d malformed entr%s dropped while reading the batch; "
                    "check for a missing key name" % (broken, "y" if broken == 1 else "ies")
                )
            return recovered, problems
    if isinstance(value, (dict, BaseModel)):
        single = _as_object(value)
        if single is None:
            return [], ["could not be read as an object: %s" % _brief(value)]
        return [single], problems
    if not isinstance(value, list):
        return [], [
            "expected a list of objects, got %s: %s" % (type(value).__name__, _brief(value))
        ]
    items = []
    for item in value:
        obj = _as_object(item)
        if obj is None:
            problems.append("not an object: %s" % _brief(item))
            continue
        items.append(obj)
    return items, problems


def _require_fields(
    objects: List[Dict[str, Any]], required: Tuple[str, ...], kind: str
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Split objects into those carrying every required field, and complaints.

    Positions are 1-based and counted over the batch as sent, so the model can
    tell which entry to fix.
    """
    usable: List[Dict[str, Any]] = []
    problems: List[str] = []
    for position, obj in enumerate(objects, 1):
        missing = [key for key in required if not obj.get(key)]
        if missing:
            problems.append(
                "%s %d skipped, missing %s: %s"
                % (kind, position, " and ".join(missing), _brief(obj))
            )
            continue
        usable.append(obj)
    return usable, problems


def _as_int(value: Any, default: Optional[int]) -> Optional[int]:
    """Coerce a numeric tool argument that models routinely send as a string.

    The agent tool nodes call _run(**tool_call["args"]) directly and so bypass
    args_schema validation: "10" arrives where 10 was declared, and any slice
    taken with it raises "slice indices must be integers".
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    """Coerce a flag that may arrive as the string "true"/"false"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _as_name_list(value: Any) -> List[str]:
    """Read entity_names, declared as a JSON string but often sent as a list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]  # a single bare name rather than a JSON list
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


class AddToGraphInput(BaseModel):
    entities: Optional[List[Entity]] = Field(None, description="List of entities to create")
    relations: Optional[List[Relation]] = Field(None, description="List of relations to create between entities")
    observations: Optional[Entity] = Field(None, description="Entity with observations to add")

class AddToGraph(BaseTool):
    name: str = "add_to_graph"
    description: str = """Add information to the graph knowledge graph.
    
    Use this tool to store and organize research findings by:
    - Creating entities
    - Defining relationships between entities
    - Adding observations/notes to existing entities
    
    This helps build a structured knowledge base during the research process."""
    args_schema: Type[BaseModel] = AddToGraphInput
    database_name: str = "memory_graph"
    cache_dir: str = None

    def __init__(self, database_name: str = "memory_graph", cache_dir: str = None):
        super().__init__()
        self.database_name = database_name
        self.cache_dir = cache_dir

    def _entity_types(self, context: str) -> Dict[str, str]:
        """Name to type for what the graph already holds, for the pairing rules.

        Best effort by design. A relation is allowed to name an entity that does
        not exist yet, and the store creates it, so an unreadable graph should
        cost a check and not a write.
        """
        try:
            data = load_graph_data(context, cache_dir=self.cache_dir) or {}
        except Exception:
            return {}

        types: Dict[str, str] = {}
        for entity in data.get("entities") or []:
            if isinstance(entity, dict) and entity.get("name"):
                types[str(entity["name"])] = str(entity.get("entityType") or "")
        return types

    def _run(
        self, 
        entities: Optional[List[Entity]] = None,
        relations: Optional[List[Relation]] = None,
        observations: Optional[Entity] = None,
    ) -> str:
        """
        Add entities, relations, or observations to the graph.
        
        Args:
            entities: list of Entity objects
                
            relations: list of Relation objects
                
            observations: list of observations to add to an existing entity (creates entities if they don't exist).
        
        Returns:
            JSON string with operation results
        """
        try:
            context = self.database_name
            results = {}
            # A batch is filtered rather than rejected: one entry with a missing
            # key used to discard every good entry sent with it, and those were
            # lost for good whenever the model moved on instead of retrying.
            skipped: List[str] = []
            # Types from this batch, which the pairing rules trust over the graph:
            # a relation usually arrives alongside the entities it names.
            batch_types: Dict[str, str] = {}

            # Process entities
            if entities:
                objects, problems = _as_object_list(entities)
                skipped.extend(problems)
                usable, problems = _require_fields(objects, ("name", "entity_type"), "entity")
                skipped.extend(problems)
                usable, problems = screen_entities(usable)
                skipped.extend(problems)
                batch_types.update(
                    {str(e["name"]): e["entity_type"] for e in usable if e.get("name")}
                )
                if usable:
                    created = create_entities(usable, context=context, cache_dir=self.cache_dir)
                    results["entities_created"] = {
                        "count": len(created),
                        "entities": created
                    }

            # Process relations
            if relations:
                objects, problems = _as_object_list(relations)
                skipped.extend(problems)
                usable, problems = _require_fields(
                    objects, ("from_entity", "to_entity", "relation_type"), "relation"
                )
                skipped.extend(problems)
                type_of = self._entity_types(context)
                type_of.update(batch_types)
                usable, problems = screen_relations(usable, type_of)
                skipped.extend(problems)
                if usable:
                    created = create_relations(usable, context=context, cache_dir=self.cache_dir)
                    results["relations_created"] = {
                        "count": len(created),
                        "relations": created
                    }

            # Process observations
            if observations:
                # The schema asks for one entity, but models often batch several,
                # and the underlying add_observations takes a list either way.
                objects, problems = _as_object_list(observations)
                skipped.extend(problems)
                usable, problems = _require_fields(objects, ("name", "observations"), "observation")
                skipped.extend(problems)
                if usable:
                    obs_dicts = [
                        {"entityName": obs["name"], "contents": obs["observations"]}
                        for obs in usable
                    ]
                    results["observations_added"] = add_observations(
                        obs_dicts, context=context, cache_dir=self.cache_dir
                    )

            if not results:
                if skipped:
                    return json.dumps({
                        "success": False,
                        "error": "Nothing could be written. Fix the entries listed under "
                                 "'skipped' and send only those again.",
                        "skipped": skipped,
                    })
                return json.dumps({
                    "success": False,
                    "error": "No data provided. Please provide at least one of: entities, relations, or observations"
                })

            payload = {"success": True, "results": results}
            if skipped:
                # Reported next to the successful writes so the model resends just
                # the entries it got wrong instead of repeating the whole batch.
                payload["skipped"] = skipped
            return json.dumps(payload)
                
        except json.JSONDecodeError as e:
            return json.dumps({
                "success": False,
                "error": f"Invalid JSON format: {str(e)}"
            })
        except Exception as e:
            return json.dumps({
                "success": False,
                "error": f"Error adding to graph: {str(e)}"
            })


class RetrieveFromGraphInput(BaseModel):
    query: Optional[str] = Field(None, description="Natural language search query to find relevant entities and relations")
    entity_names: Optional[str] = Field(None, description="JSON string list of exact entity names to retrieve with their relations")
    get_full_map: bool = Field(False, description="If True, returns a full text representation of the entire graph")
    top_k: int = Field(10, description="Maximum number of search results to return (only used with query)")
    max_entities: Optional[int] = Field(None, description="Maximum number of entities to include in full map (None = all, only used with get_full_map=True)")
    max_observations_per_entity: int = Field(5, description="Maximum observations to show per entity in full map (only used with get_full_map=True)")

class RetrieveFromGraph(BaseTool):
    name: str = "retrieve_from_graph"
    description: str = """Retrieve information from the graph knowledge graph.
    
    Use this tool to:
    - Get the full text representation of the entire graph (use get_full_map=True)
    - Search for entities and relations using natural language queries
    - Get specific entities by their exact names along with their connections
    
    This helps you find and review information stored in the graph."""
    args_schema: Type[BaseModel] = RetrieveFromGraphInput
    database_name: str = "memory_graph"
    cache_dir: str = None
    
    def __init__(self, database_name: str = "memory_graph", cache_dir: str = None):
        super().__init__()
        self.database_name = database_name
        self.cache_dir = cache_dir
    def _run(
        self, 
        query: Optional[str] = None,
        entity_names: Optional[str] = None,
        get_full_map: bool = False,
        top_k: int = 10,
        max_entities: Optional[int] = None,
        max_observations_per_entity: int = 5,
    ) -> str:
        """
        Search or retrieve information from the graph.
        
        Args:
            get_full_map: If True, returns a full text representation of the entire graph.
                This is useful to get an overview of all entities and relations in a readable format.
                Example: get_full_map=True
                
            query: Natural language search query to find relevant entities and relations.
                Example: "genes related to breast cancer", "datasets about mutations"
                
            entity_names: JSON string list of exact entity names to retrieve with their relations.
                Format: '["Entity1", "Entity2"]'
                Example: '["BRCA1", "Breast Cancer"]'
                
            top_k: Maximum number of search results to return (default: 10, only used with query)
            
            max_entities: Maximum number of entities to include in full graph (None = all, only used with get_full_map=True)
            
            max_observations_per_entity: Maximum observations to show per entity in full graph (default: 5, only used with get_full_map=True)
        
        Returns:
            JSON string with retrieved entities and relations, or text representation if get_full_graph=True
        """
        try:
            context = self.database_name

            get_full_map = _as_bool(get_full_map)
            top_k = _as_int(top_k, 10) or 10
            max_entities = _as_int(max_entities, None)
            max_observations_per_entity = _as_int(max_observations_per_entity, 5) or 5

            # Get full map as text
            if get_full_map:
                text_repr = get_graph_text_overview(
                    context=context,
                    max_entities=max_entities,
                    max_observations_per_entity=max_observations_per_entity,
                    group_by_type=True,
                    include_statistics=True,
                    cache_dir=self.cache_dir
                )
                return text_repr
            
            # Search by query
            elif query:
                result = search_nodes(query, context=context, top_k=top_k, cache_dir=self.cache_dir)
                return json.dumps({
                    "success": True,
                    "search_query": query,
                    "results": result
                })
            
            # Retrieve specific entities
            elif entity_names:
                entity_names_list = _as_name_list(entity_names)
                result = open_nodes(entity_names_list, context=context, cache_dir=self.cache_dir)
                return json.dumps({
                    "success": True,
                    "requested_entities": entity_names_list,
                    "results": result
                })
            
            else:
                return json.dumps({
                    "error": "Please provide one of: 'get_full_map=True' for full map view, 'query' for searching, or 'entity_names' for retrieving specific entities"
                })
                
        except json.JSONDecodeError as e:
            return json.dumps({
                "success": False,
                "error": f"Invalid JSON format: {str(e)}"
            })
        except Exception as e:
            return json.dumps({
                "success": False,
                "error": f"Error retrieving from graph: {str(e)}"
            })