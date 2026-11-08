"""Phase 6 checks on the ChromaDB collection and the data/index/ snapshot (6.1, 6.3, 6.4, 6.6).

6.2 (rerunning store keeps count() unchanged) is checked by running
`python -m pipeline.store` twice; it is not repeated here because it rewrites data/chroma/.
"""

import json
import os

import numpy as np
import pytest

from app.config import PROJECT_ROOT, load_config

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

CONFIG = load_config()
CHUNKS = PROJECT_ROOT / CONFIG["paths"]["chunks_dir"] / "chunks.jsonl"
INDEX_DIR = PROJECT_ROOT / CONFIG["paths"]["index_dir"]
MAX_INDEX_MB = 100


@pytest.fixture(scope="module")
def chunks():
    return [json.loads(l) for l in CHUNKS.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture(scope="module")
def collection():
    from pipeline.store import get_client

    client = get_client(CONFIG)
    name = CONFIG["vector_store"]["collection"]
    if name not in [c.name for c in client.list_collections()]:
        pytest.skip("run python -m pipeline.store first")
    return client.get_collection(name, embedding_function=None)


def test_6_1_count(collection, chunks):
    assert collection.count() == len(chunks)
    assert (collection.metadata or {}).get("hnsw:space") == CONFIG["vector_store"]["distance"]


def test_6_1_ids_match_chunks(collection, chunks):
    assert sorted(collection.get(include=[])["ids"]) == sorted(c["chunk_id"] for c in chunks)


SMOKE = [s for s in CONFIG["vector_store"]["smoke_queries"] if not s.get("informational")]


@pytest.mark.parametrize("spec", SMOKE, ids=[s["query"] for s in SMOKE])
def test_6_3_smoke_query_top1(spec, collection, chunks):
    from pipeline.embed import encode, load_model
    from pipeline.store import smoke_hit_ok

    q = encode(load_model(CONFIG["embedding"]["model"]), [spec["query"]])
    meta = collection.query(query_embeddings=q.tolist(), n_results=1, include=["metadatas"])["metadatas"][0][0]
    assert smoke_hit_ok(spec, meta, {c["source_id"]: c["url"] for c in chunks}), meta


def test_6_4_index_contents(chunks):
    for name in ("vectors.npy", "records.json", "index_meta.json", "model/model.onnx", "model/tokenizer.json"):
        assert (INDEX_DIR / name).exists(), name
    vecs = np.load(INDEX_DIR / "vectors.npy")
    records = json.loads((INDEX_DIR / "records.json").read_text(encoding="utf-8"))
    meta = json.loads((INDEX_DIR / "index_meta.json").read_text(encoding="utf-8"))
    assert vecs.shape == (len(chunks), CONFIG["embedding"]["dim"]) == (len(records), meta["dim"])
    assert meta["count"] == len(chunks)
    assert meta["embedding_model"] == CONFIG["embedding"]["model"]
    assert meta["corpus_last_updated"] == max(c["fetched_on"] for c in chunks)
    by_id = {c["chunk_id"]: c for c in chunks}
    for r in records:
        c = by_id[r["chunk_id"]]
        assert r["text"] == c["text"] and r["url"] == c["url"] and r["scheme"] == c["scheme"]


def test_6_4_snapshot_vectors_match_phase5(chunks):
    emb_dir = PROJECT_ROOT / CONFIG["paths"]["embeddings_dir"]
    ids = json.loads((emb_dir / "ids.json").read_text(encoding="utf-8"))
    src = dict(zip(ids, np.load(emb_dir / "embeddings.npy")))
    vecs = np.load(INDEX_DIR / "vectors.npy")
    records = json.loads((INDEX_DIR / "records.json").read_text(encoding="utf-8"))
    for r, v in zip(records, vecs):
        assert np.allclose(v, src[r["chunk_id"]], atol=1e-6), r["chunk_id"]


def test_6_6_index_size():
    total = sum(p.stat().st_size for p in INDEX_DIR.rglob("*") if p.is_file())
    assert total / 1e6 < MAX_INDEX_MB, f"{total / 1e6:.1f} MB"
