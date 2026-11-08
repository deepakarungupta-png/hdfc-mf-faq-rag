"""Component 10 — Response Formatter.

Every response has one shape:
    {"type": ..., "text": "<= 3 sentences", "source_url": str | None, "last_updated": "YYYY-MM-DD" | None}

- answer: the model's text (capped at answer.max_sentences) + the cited chunk's url/fetched_on.
- refusal / performance / not_found / out_of_scope: fixed templates with one link from
  sources.csv (config.yaml → templates).
- pii_warning / need_scheme / error: fixed text, no link.
"""

from functools import lru_cache
import re

from app.config import load_config
from app.sources import is_allowed_host

_CFG = load_config()
_T = _CFG["templates"]
_MAX_SENTENCES = _CFG["answer"]["max_sentences"]
_BLOCKED = [re.compile(p, re.IGNORECASE) for p in _CFG["answer"].get("blocked_patterns", [])]
_BENCHMARK_NAME = re.compile(r"total returns? index", re.IGNORECASE)

# Dots that end these words do not end a sentence.
_ABBREV = re.compile(r"(?:\b(?:rs|ltd|no|nos|mr|mrs|ms|dr|viz|etc|approx|vs|inc|co|st|pvt|i\.e|e\.g)|\b[a-z])\.$", re.IGNORECASE)


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    sentences: list[str] = []
    for part in parts:
        if sentences and _ABBREV.search(sentences[-1]):
            sentences[-1] = f"{sentences[-1]} {part}"
        elif part:
            sentences.append(part)
    return sentences


def cap_sentences(text: str, limit: int = _MAX_SENTENCES) -> str:
    return " ".join(split_sentences(text)[:limit])


def has_blocked_figures(text: str) -> bool:
    """True if the text quotes NAV, returns or AUM figures (answer.blocked_patterns)."""
    cleaned = _BENCHMARK_NAME.sub("", text)
    return any(rx.search(cleaned) for rx in _BLOCKED)


@lru_cache(maxsize=None)
def _fetched_on_for(url: str) -> str | None:
    from app.retriever import INDEX_META, RECORDS

    for rec in RECORDS:
        if rec["url"] == url:
            return rec["fetched_on"]
    return INDEX_META.get("corpus_last_updated")


def _linked(kind: str, url: str) -> dict:
    if not is_allowed_host(url):
        raise RuntimeError(f"templates link for {kind} is not on the source allow-list")
    return {"type": kind, "text": _T["texts"][kind], "source_url": url, "last_updated": _fetched_on_for(url)}


def answer(text: str, chunk: dict) -> dict:
    url = chunk["url"]
    if not is_allowed_host(url):  # every record comes from sources.csv, so this should never trip
        return not_found()
    return {"type": "answer", "text": cap_sentences(text), "source_url": url, "last_updated": chunk["fetched_on"]}


def refusal() -> dict:
    return _linked("refusal", _T["refusal_link"])


def performance(scheme: str | None = None) -> dict:
    links = _T["factsheet_links"]
    return _linked("performance", links.get(scheme or "general") or links["general"])


def out_of_scope() -> dict:
    return _linked("out_of_scope", _T["refusal_link"])


def not_found() -> dict:
    return _linked("not_found", _T["refusal_link"])


def pii_warning() -> dict:
    return {"type": "pii_warning", "text": _T["texts"]["pii_warning"], "source_url": None, "last_updated": None}


def need_scheme() -> dict:
    """A fund-specific question that names no in-scope fund (or several): ask, don't guess."""
    return {"type": "need_scheme", "text": _T["texts"]["need_scheme"], "source_url": None, "last_updated": None}


def error() -> dict:
    return {"type": "error", "text": _T["texts"]["error"], "source_url": None, "last_updated": None}


def for_type(kind: str, scheme: str | None = None) -> dict:
    """Template response for a guardrail decision."""
    if kind == "performance":
        return performance(scheme)
    builders = {
        "refusal": refusal,
        "out_of_scope": out_of_scope,
        "not_found": not_found,
        "pii_warning": pii_warning,
        "need_scheme": need_scheme,
        "error": error,
    }
    return builders[kind]()
