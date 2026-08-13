"""The entity and relation types a graph writer is allowed to use.

The prompt has always listed these, but nothing enforced them, and a prompt is a
request rather than a constraint. A run writing 24 entities put 15 of them under
types that do not exist: fourteen drugs typed CHEMICAL/DRUG, because the prompt
wrote "Type: CHEMICAL / DRUG" to mean "one of these" and the model read it as a
name, plus one PROCESS invented outright. Type strings are what filtering and
retrieval group on, so CHEMICAL and CHEMICAL/DRUG split one drug across two
buckets.

Normalization here is deliberately narrow. A slash-joined pair whose every half
is a real type is a misread of the prompt and resolves to the first half, which
cannot mean anything else. Everything else is refused rather than guessed:
REGULATES is not REGULATES_EXPRESSION, since LRRK2 regulating mitophagy says
nothing about transcript levels, and a wrong edge that reads as precise is worse
than one the model was asked to rewrite.

No imports on purpose: this runs inside the add_to_graph tool, which already
carries langchain and pydantic, and inside tests that must run without them.
"""

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

ENTITY_TYPES = frozenset({
    "GENE",
    "PROTEIN",
    "DISEASE",
    "PHENOTYPE",
    "CHEMICAL",
    "DRUG",
    "CELL_LINE",
    "TISSUE",
    "PATHWAY",
    "GENE_SET",
    "PAPER",
    "FINDING",
})

RELATION_TYPES = frozenset({
    "ACTIVATES",
    "INHIBITS",
    "BINDS",
    "PHOSPHORYLATES",
    "REGULATES_EXPRESSION",
    "TREATS",
    "TARGETS",
    "MEMBER_OF_PATHWAY",
    "HAS_GENESET_MEMBER",
    "EXPRESSED_IN",
    "ASSOCIATED_WITH",
    "CO_OCCURS",
    "SUPPORTS",
    "REFUTES",
    "INCONCLUSIVE_FOR",
    "CITES",
})

# Carries a payload (DERIVED_FROM_KG:reactome@v88), so it is matched by prefix
# and its suffix is left exactly as sent.
_PROVENANCE_PREFIX = "DERIVED_FROM_KG:"

_DRUG_TYPES = frozenset({"CHEMICAL", "DRUG"})
_DISEASE_TYPES = frozenset({"DISEASE", "PHENOTYPE"})
_PATHWAY_TYPES = frozenset({"PATHWAY", "GENE_SET"})


def _normalize(value: Any) -> str:
    """Upper-case with spaces and hyphens folded to underscores."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return text.upper().replace("-", "_").replace(" ", "_")


def canonical_entity_type(value: Any) -> Optional[str]:
    """Return the approved entity type, or None if it cannot be resolved safely."""
    raw = str(value or "").strip()
    if not raw:
        return None

    direct = _normalize(raw)
    if direct in ENTITY_TYPES:
        return direct

    if "/" in raw:
        halves = [_normalize(part) for part in raw.split("/")]
        halves = [half for half in halves if half]
        # Every half being a real type is the signature of a misread prompt line.
        # A pair like CHEMICAL/NONSENSE is something else and is left refused.
        if halves and all(half in ENTITY_TYPES for half in halves):
            return halves[0]

    return None


def canonical_relation_type(value: Any) -> Optional[str]:
    """Return the approved relation type, or None if it is not one of them."""
    raw = str(value or "").strip()
    if not raw:
        return None

    if raw.upper().startswith(_PROVENANCE_PREFIX):
        suffix = raw[len(_PROVENANCE_PREFIX):].strip()
        return _PROVENANCE_PREFIX + suffix if suffix else None

    direct = _normalize(raw)
    return direct if direct in RELATION_TYPES else None


def _listed(names) -> str:
    return ", ".join(sorted(names))


def _as_type_set(value: Any) -> FrozenSet[str]:
    """The canonical types held under one name, from a string or a collection.

    A name can carry more than one type at once: Parkinson disease is both the
    DISEASE and the KEGG PATHWAY of that name, and both nodes exist. Types that
    do not resolve, such as the auto_created placeholder the store assigns to an
    entity invented by a relation, drop out and leave the name looking unknown,
    which is what they are.
    """
    if value is None:
        return frozenset()
    candidates = [value] if isinstance(value, str) else list(value)
    resolved = set()
    for candidate in candidates:
        kind = canonical_entity_type(candidate)
        if kind:
            resolved.add(kind)
    return frozenset(resolved)


def screen_entities(
    objects: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Split entities into those with a usable type, and complaints.

    Entries that survive carry the canonical spelling of their type, so a caller
    writes CHEMICAL even when CHEMICAL/DRUG was sent. Positions are 1-based over
    the batch as sent, matching how missing fields are already reported.
    """
    usable: List[Dict[str, Any]] = []
    problems: List[str] = []
    refused = False

    for position, obj in enumerate(objects, 1):
        resolved = canonical_entity_type(obj.get("entity_type"))
        if resolved is None:
            refused = True
            problems.append(
                "entity %d skipped, %s is not an entity type (%s)"
                % (position, obj.get("entity_type"), obj.get("name"))
            )
            continue
        entry = dict(obj)
        entry["entity_type"] = resolved
        usable.append(entry)

    if refused:
        problems.append("entity types are: %s" % _listed(ENTITY_TYPES))
    return usable, problems


def screen_relations(
    objects: List[Dict[str, Any]],
    type_of: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Split relations into those that can be written, and complaints.

    type_of maps entity name to its type, or to several of them, and may be
    partial: relations are allowed to name entities that do not exist yet, which
    the store creates. The two pairing rules apply only where both ends are
    known, so an unknown end means the predicate is checked but the pairing is
    not.

    Where a name holds more than one type, the pairing rules give way. Both of
    them exist to refuse, and a refusal a model cannot act on is worse than an
    edge that is merely suspect: told that Parkinson disease is a DISEASE, it has
    no way to see that the pathway of the same name was the intended object.
    """
    known = {
        str(name): _as_type_set(kinds) for name, kinds in (type_of or {}).items()
    }

    usable: List[Dict[str, Any]] = []
    problems: List[str] = []
    refused_type = False

    for position, obj in enumerate(objects, 1):
        source = obj.get("from_entity")
        target = obj.get("to_entity")
        where = "%s -> %s" % (source, target)

        resolved = canonical_relation_type(obj.get("relation_type"))
        if resolved is None:
            refused_type = True
            problems.append(
                "relation %d skipped, %s is not a relation type (%s)"
                % (position, obj.get("relation_type"), where)
            )
            continue

        from_types = known.get(str(source), frozenset())
        to_types = known.get(str(target), frozenset())

        # A drug does not inhibit or activate a disease, it treats it. The model
        # reaches for a mechanistic verb here when it has no target gene to point
        # at, which reads as a mechanism nobody measured.
        if (
            from_types
            and to_types
            and from_types <= _DRUG_TYPES
            and to_types <= _DISEASE_TYPES
            and resolved != "TREATS"
        ):
            problems.append(
                "relation %d skipped, use TREATS for a drug and a disease, not %s (%s)"
                % (position, resolved, where)
            )
            continue

        if (
            resolved == "MEMBER_OF_PATHWAY"
            and to_types
            and not (to_types & _PATHWAY_TYPES)
        ):
            problems.append(
                "relation %d skipped, MEMBER_OF_PATHWAY needs a pathway as its object,"
                " and %s is a %s (%s)"
                % (position, target, _listed(to_types), where)
            )
            continue

        entry = dict(obj)
        entry["relation_type"] = resolved
        usable.append(entry)

    if refused_type:
        problems.append("relation types are: %s" % _listed(RELATION_TYPES))
    return usable, problems
