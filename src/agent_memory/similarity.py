"""Text normalisation and Jaccard similarity for duplicates/conflicts (research A6). Pure."""

from __future__ import annotations

import re

DUP_THRESHOLD = 0.8
STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was "
    "were will with we our you your must should how what which when where who why do does "
    "did can could would should shall may might new again any all some get set use using into "
    "about there their them they than then also just only very more most other".split()
)
_SUFFIXES = ("ations", "ation", "ings", "ing", "ies", "ed", "es", "s")


def stem(word: str) -> str:
    """A tiny suffix stripper so 'migrations' finds 'migration' (prefix match does the rest)."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word


_WORD = re.compile(r"[a-z0-9_.]+")


def words(text: str) -> set[str]:
    return {w.strip(".") for w in _WORD.findall(text.lower()) if w.strip(".")} - STOPWORDS


def jaccard(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    if not wa and not wb:
        return 1.0
    return len(wa & wb) / len(wa | wb)
