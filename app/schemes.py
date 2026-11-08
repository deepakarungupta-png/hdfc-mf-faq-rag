"""Scheme detection: maps the aliases in config.yaml (schemes.aliases) to a scheme name.

Used by the phase 7 guardrails stub so the Retriever can filter to the asked-about
fund plus general chunks. Phase 9's guardrails reuse it.
"""

from functools import lru_cache
import re

from app.config import load_config


def _normalise(text: str) -> str:
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return f" {re.sub(r'\s+', ' ', text).strip()} "


@lru_cache(maxsize=1)
def _alias_table() -> list[tuple[str, str]]:
    table = []
    for scheme, aliases in load_config()["schemes"]["aliases"].items():
        for alias in aliases:
            table.append((_normalise(alias), scheme))
    return table


def detect_schemes(question: str) -> list[str]:
    """Every in-scope scheme the question names, in config order, without duplicates."""
    q = _normalise(question)
    found: list[str] = []
    for alias, scheme in _alias_table():
        if alias in q and scheme not in found:
            found.append(scheme)
    return found


def detect_scheme(question: str) -> str | None:
    """The single scheme the question names, or None if it names none or several."""
    found = detect_schemes(question)
    return found[0] if len(found) == 1 else None
