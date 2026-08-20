"""Entity-name comparison keys, so one concept lands on one node.

The graph store deduplicates by exact name, so two spellings of the same
concept split into two nodes. A gene is already canonicalized by
``canonical_gene_symbol``; every other entity -- a disease, drug, pathway or
gene set -- carries a name straight from a database field or a model, and those
vary in case ("Alzheimer disease" / "alzheimer disease"), possessives
("Parkinson's disease" / "Parkinson disease"), and number ("diseases" /
"disease").

``entity_name_key`` reduces a name to a comparison key that is equal across
those variants. It is deliberately conservative: it never rewrites the display
name (which stays first-write-wins), and it refuses to de-pluralize anything it
cannot fold safely. A false merge -- two distinct concepts collapsed into one --
is worse than a missed merge, so the plural fold is guarded by a stoplist, by
suffix rules, and by treating acronyms and identifiers (all-caps or
digit-bearing, like "KRAS" or "C9orf72") as immune to folding.

No imports on purpose: this runs inside the graph store and inside tests that
must not drag in langchain or pydantic.
"""

from typing import FrozenSet

# Singular nouns that happen to end in "s" and must never be de-pluralized. The
# Greek/Latin -is/-us/-ss forms are caught by the suffix rules below, but these
# do not share a single suffix.
_PLURAL_STOPLIST: FrozenSet[str] = frozenset({
    "aids",
    "diabetes",
    "herpes",
    "rabies",
    "measles",
    "mumps",
    "series",
    "species",
    "shingles",
})


def _is_symbol(word: str) -> bool:
    """True for an acronym or identifier, which plural folding must not touch.

    Gene symbols and identifiers are all-caps or carry digits ("KRAS", "TP53",
    "C9orf72"). Their trailing letters are not English plurals, and folding them
    would corrupt the key ("KRAS" -> "kra") or collide a symbol with a common
    noun ("CARS" with "CAR").
    """
    return word.isupper() or any(ch.isdigit() for ch in word)


def _fold_plural(word: str) -> str:
    """Best-effort singular of one lower-cased, non-symbol word."""
    if len(word) <= 3:
        return word
    if word in _PLURAL_STOPLIST:
        return word
    # Greek/Latin singulars: -ss (illness), -us (lupus, virus), -is (sclerosis,
    # fibrosis, arthritis, analysis, tuberculosis). They end in "s" but are
    # singular, so stripping it would corrupt them.
    if word.endswith(("ss", "us", "is")):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"  # therapies -> therapy
    if word.endswith("es"):
        return word[:-1]  # diseases -> disease
    if word.endswith("s"):
        return word[:-1]  # disorders -> disorder
    return word


def entity_name_key(name: object) -> str:
    """Reduce a name to a comparison key equal across spelling variants.

    Folds whitespace, case, and possessives on every word, then folds the plural
    of the tail word only -- and only when the tail word is a natural-language
    word, never a symbol or identifier.
    """
    text = " ".join(str(name or "").split())
    if not text:
        return ""

    originals = text.split(" ")
    lowered = [word.lower() for word in originals]

    # Possessives fold on every word: "Alzheimer's disease" -> "alzheimer disease".
    for i, word in enumerate(lowered):
        if word.endswith("'s"):
            lowered[i] = word[:-2]
        elif word.endswith("s'"):
            lowered[i] = word[:-1]

    # Plural folds on the tail word only, and only for natural-language words.
    if lowered and not _is_symbol(originals[-1]):
        lowered[-1] = _fold_plural(lowered[-1])

    return " ".join(lowered)
