"""Phase 5 checks on data/embeddings/."""

import json

import numpy as np
import pytest

from app.config import PROJECT_ROOT, load_config

CONFIG = load_config()
EMB_DIR = PROJECT_ROOT / CONFIG["paths"]["embeddings_dir"]
CHUNKS = PROJECT_ROOT / CONFIG["paths"]["chunks_dir"] / "chunks.jsonl"


@pytest.fixture(scope="module")
def data():
    if not (EMB_DIR / "embeddings.npy").exists():
        pytest.skip("run python -m pipeline.embed first")
    chunks = [json.loads(l) for l in CHUNKS.read_text(encoding="utf-8").splitlines() if l.strip()]
    vecs = np.load(EMB_DIR / "embeddings.npy")
    ids = json.loads((EMB_DIR / "ids.json").read_text(encoding="utf-8"))
    meta = json.loads((EMB_DIR / "meta.json").read_text(encoding="utf-8"))
    return chunks, vecs, ids, meta


def test_5_1_shape(data):
    chunks, vecs, _, meta = data
    assert vecs.shape == (len(chunks), CONFIG["embedding"]["dim"])
    assert vecs.dtype == np.float32
    assert meta["count"] == len(chunks) and meta["dim"] == vecs.shape[1]
    assert meta["model"] == CONFIG["embedding"]["model"]


def test_5_2_ids_match_chunks_in_order(data):
    chunks, vecs, ids, _ = data
    assert len(ids) == vecs.shape[0]
    assert ids == [c["chunk_id"] for c in chunks]


def test_5_3_norms(data):
    norms = np.linalg.norm(data[1], axis=1)
    assert np.all(np.abs(norms - 1) <= 1e-3)


def test_5_4_finite(data):
    assert np.isfinite(data[1]).all()


def test_5_5_elss_lock_in_search(data):
    """Top-1 is the ELSS chunk that states the lock-in; every top-3 hit is an ELSS chunk
    or a lock-in fact (another scheme's "Lock-in period: NA" may rank 2nd or 3rd; phase 7's
    scheme filter removes those at query time)."""
    from pipeline.embed import encode, load_model

    chunks, vecs, _, _ = data
    q = encode(load_model(CONFIG["embedding"]["model"]), ["ELSS lock-in period"])[0]
    top = [chunks[i] for i in np.argsort(-(vecs @ q))[:3]]
    first = top[0]
    assert first["scheme"] == "HDFC ELSS Tax Saver", first["chunk_id"]
    assert "lock-in" in first["text"].lower() and "3 years" in first["text"], first["chunk_id"]
    for c in top:
        is_elss = c["scheme"] == "HDFC ELSS Tax Saver" or "elss" in c["text"].lower()
        assert is_elss or "lock-in" in c["text"].lower(), c["chunk_id"]
