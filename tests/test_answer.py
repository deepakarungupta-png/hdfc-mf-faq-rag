"""Phase 7 offline tests: retrieval rules, citation mapping, formatting, error
handling and the HTTP layer, with Gemini mocked (no key or network needed)."""

import json
import logging

from fastapi.testclient import TestClient
from google.genai import errors
import pytest

from app import formatter, generator, retriever, service
from app.sources import is_allowed_host

QUESTION = "What is the exit load of HDFC Flexi Cap Fund?"


def fake_model(payload: dict | str):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return lambda prompt: raw


# ---------- retriever ----------

@pytest.mark.parametrize("scheme", ["HDFC Large Cap", "HDFC Flexi Cap", "HDFC ELSS Tax Saver", "HDFC Mid Cap"])
def test_scheme_filter_keeps_only_that_scheme_and_general(scheme):
    hits = retriever.retrieve("expense ratio exit load minimum SIP", scheme, top_k=50)
    assert hits and {h.record["scheme"] for h in hits} <= {scheme, "general"}


@pytest.mark.parametrize("q,scheme,chunk", [
    ("Expense ratio of HDFC Large Cap Fund?", "HDFC Large Cap", "s01-001"),
    ("What is the expense ratio of HDFC Flexi Cap Fund?", "HDFC Flexi Cap", "s02-001"),
    ("TER of HDFC ELSS Tax Saver", "HDFC ELSS Tax Saver", "s03-001"),
    ("What is the TER of HDFC Mid Cap fund", "HDFC Mid Cap", "s04-001"),
])
def test_ter_prefers_scheme_page(q, scheme, chunk):
    assert retriever.retrieve(q, scheme)[0].record["chunk_id"] == chunk


def test_off_topic_returns_nothing():
    assert retriever.retrieve("What is the capital of France?") == []


def test_performance_chunks_never_retrieved():
    hits = retriever.retrieve("HDFC Flexi Cap returns last 5 years CAGR since inception", "HDFC Flexi Cap", top_k=991)
    ids = {h.record["chunk_id"] for h in hits}
    assert "s05-015" not in ids and "s05-013" not in ids  # factsheet returns tables
    assert "s05-002" not in ids                            # AUM figure


# ---------- service with mocked Gemini ----------

def test_answer_uses_cited_chunk_url_and_date(monkeypatch):
    monkeypatch.setattr(generator, "_call", fake_model(
        {"answerable": True, "answer": "Exit load is 1% within 1 year. See https://evil.example.", "cited_chunk": "C1", "is_advice": False}))
    resp, rec = service.ask(QUESTION)
    hit = retriever.retrieve(QUESTION, "HDFC Flexi Cap")[0].record
    assert resp["type"] == "answer"
    assert resp["source_url"] == hit["url"] and is_allowed_host(resp["source_url"])
    assert resp["last_updated"] == hit["fetched_on"]
    assert rec["cited_source_id"] == hit["source_id"] and rec["scheme"] == "HDFC Flexi Cap"


@pytest.mark.parametrize("cited", ["", "C9", "C0", "chunk 2", None])
def test_missing_or_unknown_citation_is_not_found(monkeypatch, cited):
    monkeypatch.setattr(generator, "_call", fake_model(
        {"answerable": True, "answer": "Something.", "cited_chunk": cited, "is_advice": False}))
    assert service.ask(QUESTION)[0]["type"] == "not_found"


def test_unanswerable_is_not_found(monkeypatch):
    monkeypatch.setattr(generator, "_call", fake_model(
        {"answerable": False, "answer": "", "cited_chunk": "", "is_advice": False}))
    assert service.ask(QUESTION)[0]["type"] == "not_found"


def test_bad_json_is_not_found(monkeypatch):
    monkeypatch.setattr(generator, "_call", fake_model('{"answerable": tru'))
    assert service.ask(QUESTION)[0]["type"] == "not_found"


def test_is_advice_becomes_refusal(monkeypatch):
    monkeypatch.setattr(generator, "_call", fake_model(
        {"answerable": False, "answer": "", "cited_chunk": "", "is_advice": True}))
    assert service.ask("Should I buy HDFC Flexi Cap?")[0]["type"] == "refusal"


def test_off_topic_never_calls_gemini(monkeypatch):
    def boom(prompt):
        raise AssertionError("Gemini called")
    monkeypatch.setattr(generator, "_call", boom)
    assert service.ask("What is the capital of France?")[0]["type"] == "not_found"


@pytest.mark.parametrize("text", [
    "The NAV is ₹1,234.56 as on June 30.",
    "It delivered 17.67% returns over 5 years.",
    "Returns were 15.2% since launch.",
    "The 5-year CAGR is 12%.",
    "AUM is ₹106,495.63 Cr.",
    "Assets under management were 1,06,495 crore.",
])
def test_figures_in_answer_become_performance(monkeypatch, text):
    monkeypatch.setattr(generator, "_call", fake_model(
        {"answerable": True, "answer": text, "cited_chunk": "C1", "is_advice": False}))
    resp = service.ask(QUESTION)[0]
    assert resp["type"] == "performance" and "%" not in resp["text"]


@pytest.mark.parametrize("text", [
    "The benchmark is the NIFTY 500 Total Returns Index.",
    "The TER (Direct Plan) is 0.75%.",
    "An exit load of 1.00% applies if units are redeemed within 1 year.",
    "The closing NAV of the day applies to applications received before 3 p.m.",
    "Minimum SIP is ₹100.",
])
def test_ordinary_facts_not_blocked(text):
    assert not formatter.has_blocked_figures(text)


def test_answer_capped_at_three_sentences():
    text = "Rs. 100 is the minimum. It is set by the AMC. Units are allotted at NAV. A fourth one. A fifth."
    out = formatter.cap_sentences(text)
    assert formatter.split_sentences(out) == formatter.split_sentences(text)[:3]
    assert out.startswith("Rs. 100 is the minimum.") and "fourth" not in out


# ---------- Gemini errors ----------

class _Models:
    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def generate_content(self, **kw):
        self.calls += 1
        raise self.exc


class _Client:
    def __init__(self, exc):
        self.models = _Models(exc)


@pytest.mark.parametrize("code,expected_calls", [(400, 1), (429, 2), (503, 2)])
def test_gemini_errors_become_error_type(monkeypatch, code, expected_calls):
    client = _Client(errors.APIError(code, {"error": {"message": "x", "status": "X"}}))
    monkeypatch.setattr(generator, "_get_client", lambda: client)
    monkeypatch.setattr(generator.time, "sleep", lambda s: None)
    resp = service.ask(QUESTION)[0]
    assert resp["type"] == "error" and "busy" in resp["text"]
    assert client.models.calls == expected_calls


def test_missing_key_is_error(monkeypatch):
    from app.config import ConfigError

    def no_key():
        raise ConfigError("GEMINI_API_KEY is not set")
    monkeypatch.setattr(generator, "_get_client", no_key)
    assert service.ask(QUESTION)[0]["type"] == "error"


# ---------- HTTP layer ----------

@pytest.fixture()
def client():
    from api.ask import app
    return TestClient(app)


@pytest.mark.parametrize("body", [{"question": ""}, {"question": "   "}, {"question": "x" * 501}, {}, {"question": 5}, []])
def test_rejects_bad_questions(client, body):
    r = client.post("/api/ask", json=body)
    assert r.status_code == 400 and r.json()["type"] == "invalid"


def test_500_chars_accepted_and_logs_have_no_question(client, monkeypatch, caplog):
    monkeypatch.setattr(generator, "_call", fake_model(
        {"answerable": True, "answer": "Exit load is 1%.", "cited_chunk": "C1", "is_advice": False}))
    secret = "zebra-quokka-marker"
    q = f"Exit load HDFC Flexi Cap {secret} ".ljust(500, "?")
    with caplog.at_level(logging.INFO, logger="hdfc_mf_faq"):
        r = client.post("/api/ask", json={"question": q})
    assert r.status_code == 200 and r.json()["type"] == "answer"
    assert secret not in caplog.text
    logged = json.loads(caplog.records[-1].getMessage())
    assert set(logged) == {"type", "scheme", "top_score", "cited_source_id", "latency_ms"}
