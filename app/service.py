"""The request path shared by api/ask.py and scripts/ask.py:
guardrails → retriever → generator → formatter.

ask() returns (response, log_record). The log record holds only
{type, scheme, top_score, cited_source_id, latency_ms}, never the question.
"""

import time

from app import formatter, guardrails, retriever
from app.generator import GenerationError, generate


def ask(question: str) -> tuple[dict, dict]:
    start = time.perf_counter()
    rec = {"type": None, "scheme": None, "top_score": None, "cited_source_id": None, "latency_ms": None}

    def done(resp: dict) -> tuple[dict, dict]:
        rec["type"] = resp["type"]
        rec["latency_ms"] = round((time.perf_counter() - start) * 1000)
        return resp, rec

    decision = guardrails.check(question)
    rec["scheme"] = decision.get("scheme")
    if decision["type"] != "factual":
        return done(formatter.for_type(decision["type"], decision.get("scheme")))

    hits = retriever.retrieve(question, decision.get("scheme"))
    if not hits:
        return done(formatter.not_found())
    rec["top_score"] = round(hits[0].score, 4)

    try:
        gen = generate(question, hits)
    except GenerationError:
        return done(formatter.error())

    if gen.is_advice:
        return done(formatter.refusal())
    if not gen.answerable or not gen.answer or gen.cited is None:
        return done(formatter.not_found())
    if formatter.has_blocked_figures(gen.answer):
        return done(formatter.performance(decision.get("scheme")))

    rec["cited_source_id"] = gen.cited.record["source_id"]
    return done(formatter.answer(gen.answer, gen.cited.record))
