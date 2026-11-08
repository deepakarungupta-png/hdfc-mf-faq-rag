"""Phase 4 checks on data/chunks/chunks.jsonl, plus the factsheet page matcher."""

import json
from collections import Counter

import pytest

from app.config import PROJECT_ROOT, load_config
from pipeline.chunk import page_scheme

SCHEMES = ["HDFC Large Cap", "HDFC Flexi Cap", "HDFC ELSS Tax Saver", "HDFC Mid Cap"]
CHUNKS = PROJECT_ROOT / load_config()["paths"]["chunks_dir"] / "chunks.jsonl"
DOCS = PROJECT_ROOT / load_config()["paths"]["loaded_dir"] / "documents.jsonl"


@pytest.mark.parametrize("title, expected", [
    ("HDFC Flexi Cap Fun", "HDFC Flexi Cap"),            # truncated by the column split
    ("HDFC Flexi Cap Fund", "HDFC Flexi Cap"),
    ("HDFC Large Cap Fu", "HDFC Large Cap"),
    ("HDFC ELSS Tax Save", "HDFC ELSS Tax Saver"),
    ("HDFC Mid Cap Fund ....Contd from previous page", "HDFC Mid Cap"),
    ("HDFC Large and Mid", None),                         # a different fund
    ("HDFC Large an", None),
    ("HDFC Large", None),                                 # ambiguous fragment
    ("HD", None),
    ("HDFC Small Cap Fund", None),
    ("GLOSSARY", None),
    ("FUND DETAILS ANNEX", None),
])
def test_page_scheme(title, expected):
    assert page_scheme(title, SCHEMES) == expected


@pytest.fixture(scope="module")
def chunks():
    if not CHUNKS.exists():
        pytest.skip("run python -m pipeline.chunk first")
    return [json.loads(l) for l in CHUNKS.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_4_1_max_tokens(chunks):
    assert max(c["token_count"] for c in chunks) <= 256


def test_4_2_every_source_has_a_chunk(chunks):
    ids = {json.loads(l)["id"] for l in DOCS.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert ids <= {c["source_id"] for c in chunks}


def test_4_3_unique_ids(chunks):
    assert not [k for k, n in Counter(c["chunk_id"] for c in chunks).items() if n > 1]


def test_4_4_required_fields(chunks):
    assert all(c["url"] and c["scheme"] and c["fetched_on"] for c in chunks)


def test_factsheet_only_our_schemes(chunks):
    fs = [c for c in chunks if c["doc_type"] == "factsheet"]
    assert {c["scheme"] for c in fs} == set(SCHEMES)
    assert all(c["text"].startswith(f"{c['scheme']} Fund — Factsheet:") for c in fs)


# ---------- scheme pages: one chunk per labelled fact ----------

FACTS = load_config()["chunking"]["fact_chunks"]["facts"]
SAMPLE_PAGE = "\n".join([
    "HDFC Mid Cap Fund — Direct Plan (scheme page)",
    "Total Expense Ratio (TER), Direct Plan: 0.74%",
    "Exit load: 1.00% if redeemed within 1 year.",
    "Exit load: No Entry/Exit Load on bonus units",
    "Entry load: Not levied.",
    "Minimum SIP: ₹100",
    "Lock-in period: NA",
    "Benchmark: NIFTY Midcap 150 Index",
])


def test_fact_chunk_texts_one_per_fact():
    from pipeline.chunk import fact_chunk_texts

    assert fact_chunk_texts(SAMPLE_PAGE, FACTS) == [
        "HDFC Mid Cap Fund (Direct Plan) - Total Expense Ratio (TER): 0.74%",
        "HDFC Mid Cap Fund (Direct Plan) - Exit load: 1.00% if redeemed within 1 year. "
        "No Entry/Exit Load on bonus units.",   # repeated label -> one chunk, each line a sentence
        "HDFC Mid Cap Fund (Direct Plan) - Entry load: Not levied.",
        "HDFC Mid Cap Fund (Direct Plan) - Minimum SIP: ₹100",
        "HDFC Mid Cap Fund (Direct Plan) - Lock-in period: NA",
        "HDFC Mid Cap Fund (Direct Plan) - Benchmark: NIFTY Midcap 150 Index",  # optional riskometer absent
    ]


@pytest.mark.parametrize("text, message", [
    (SAMPLE_PAGE.replace("Minimum SIP: ₹100\n", ""), "missing required fact"),
    (SAMPLE_PAGE + "\nNAV: ₹123", "unknown fact line"),
    (SAMPLE_PAGE + "\nsome unlabelled text", "unknown fact line"),
    (SAMPLE_PAGE.replace(" (scheme page)", ""), "title line not recognised"),
])
def test_fact_chunk_texts_fails_loudly(text, message):
    from pipeline.chunk import ChunkError, fact_chunk_texts

    with pytest.raises(ChunkError, match=message):
        fact_chunk_texts(text, FACTS)


def test_scheme_pages_split_per_fact(chunks):
    pages = [c for c in chunks if c["doc_type"] == "scheme_page"]
    assert {c["source_id"] for c in pages} == {"s01", "s02", "s03", "s04"}
    labels = [f["label"] for f in FACTS]
    for sid in ("s01", "s02", "s03", "s04"):
        mine = [c for c in pages if c["source_id"] == sid]
        prefix = f"{mine[0]['scheme']} Fund (Direct Plan) - "
        assert all(c["text"].startswith(prefix) for c in mine), sid
        got = [c["text"][len(prefix):].split(": ", 1)[0] for c in mine]
        assert got == [l for l in labels if l in got], sid          # config order, no repeats
        required = [f["label"] for f in FACTS if f.get("required")]
        assert set(required) <= set(got), sid
        assert all("\n" not in c["text"] for c in mine), sid


@pytest.mark.parametrize("text", [
    "HDFC ELSS Tax Saver Fund (Direct Plan) - Lock-in period: 3 years",
    "HDFC Large Cap Fund (Direct Plan) - Minimum SIP: ₹100",
])
def test_scheme_page_fact_chunk_present(chunks, text):
    assert text in {c["text"] for c in chunks}
