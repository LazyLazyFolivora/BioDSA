"""Canonical gene symbols, so that one gene is one node.

Two writers spell the same gene differently. Databases return current HGNC
symbols, while a user's gene list, older literature, and some tool payloads
carry retired symbols, locus names, and protein names. Without a shared
spelling the graph grows one node per spelling, and a reader sees GBA and GBA1,
or PRKN and PARKIN, as two separate genes with the evidence split between them.

The table is small and hand-checked on purpose. It covers renames and aliases
that show up in practice rather than mirroring all of HGNC, because a wrong
entry silently merges two distinct genes -- GBA2 and GBA3 are not GBA1 -- which
is worse than leaving an alias alone. Lookup is exact, so a near-miss symbol is
left untouched.
"""

from typing import Dict

# Retired Parkinson disease locus names and protein names. The PARK series is
# the main offender: reviews and gene lists cite the locus while databases
# return the gene. Loci that never resolved to a single gene (PARK3, PARK10,
# PARK12, PARK16) and the still-disputed PARK21 are deliberately absent.
_PARKINSON_ALIASES = {
    "PARK1": "SNCA",
    "PARK4": "SNCA",
    "PARK2": "PRKN",
    "PARKIN": "PRKN",
    "PARK5": "UCHL1",
    "PARK6": "PINK1",
    "DJ-1": "PARK7",
    "DJ1": "PARK7",
    "PARK8": "LRRK2",
    "DARDARIN": "LRRK2",
    "PARK9": "ATP13A2",
    "PARK11": "GIGYF2",
    "PARK13": "HTRA2",
    "PARK14": "PLA2G6",
    "PARK15": "FBXO7",
    "PARK17": "VPS35",
    "PARK18": "EIF4G1",
    "PARK19": "DNAJC6",
    "PARK20": "SYNJ1",
    "PARK22": "CHCHD2",
    "PARK23": "VPS13C",
    # HGNC renamed GBA to GBA1 in 2019; both spellings remain in circulation.
    "GBA": "GBA1",
    "NURR1": "NR4A2",
    "TAU": "MAPT",
    "IT15": "HTT",
}

# Hyphenated and colloquial forms of widely cited genes. These turn up whenever
# a symbol is written from memory instead of copied out of a database field.
_COMMON_ALIASES = {
    "P53": "TP53",
    "C-MYC": "MYC",
    "K-RAS": "KRAS",
    "N-RAS": "NRAS",
    "H-RAS": "HRAS",
    "HER2": "ERBB2",
    "BCL-2": "BCL2",
    "BCL-XL": "BCL2L1",
    "PD-1": "PDCD1",
    "PD-L1": "CD274",
    "CTLA-4": "CTLA4",
    "C9ORF72": "C9orf72",
}

_ALIASES: Dict[str, str] = {}
_ALIASES.update(_PARKINSON_ALIASES)
_ALIASES.update(_COMMON_ALIASES)


def canonical_gene_symbol(name: str) -> str:
    """Return the HGNC symbol for a gene alias, or the name unchanged.

    An unrecognized name is returned as given rather than upper-cased:
    approved symbols are not uniformly upper case (C9orf72), so normalizing
    case would corrupt the ones that are already right.
    """
    text = " ".join(str(name or "").split())
    if not text:
        return ""
    return _ALIASES.get(text.upper(), text)
