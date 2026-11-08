"""Phase 6 parity checks (6.5).

1. The exported ONNX model, with mean pooling and L2 normalisation, embeds
   questions the same way as sentence-transformers (cosine > 0.999).
2. Exact search over the data/index/ snapshot, using ONNX query vectors,
   returns the same top-5 chunk IDs as ChromaDB queried with
   sentence-transformers vectors.

This mirrors what the Vercel runtime does, using only onnxruntime,
tokenizers and numpy on the snapshot side.
"""

import json
import os

import numpy as np
import pytest

from app.config import PROJECT_ROOT, load_config

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

CONFIG = load_config()
INDEX_DIR = PROJECT_ROOT / CONFIG["paths"]["index_dir"]
MODEL_DIR = PROJECT_ROOT / CONFIG["embedding"]["onnx_dir"]

QUESTIONS = [
    "What is the expense ratio of HDFC Flexi Cap Fund direct plan?",
    "Exit load for HDFC Mid Cap Fund",
    "What is the lock-in period of HDFC ELSS Tax Saver?",
    "Minimum SIP amount for HDFC Large Cap Fund",
    "How do I download my capital gains statement?",
    "What is the benchmark of HDFC Mid Cap Fund?",
    "riskometer level of HDFC Flexi Cap",
    "Who is the fund manager of HDFC Large Cap Fund?",
    "How can I get a consolidated account statement?",
    "What is an ELSS fund?",
    "What is a direct plan versus a regular plan?",
    "How is NAV calculated?",
    "Can I redeem ELSS units before three years?",
    "What is the minimum lump sum investment in HDFC Mid Cap Fund?",
    "Is there an entry load on mutual funds?",
    "What does total expense ratio mean?",
    "How do I update my KYC details?",
    "Where can I file a complaint about a mutual fund?",
    "Should I invest in HDFC Mid Cap Fund now?",   # advice-style, still must embed identically
    "x",                                             # degenerate one-token query
]
SEARCH_QUERIES = QUESTIONS[:10]
TOP_K = 5


@pytest.fixture(scope="module")
def onnx_embed():
    if not (MODEL_DIR / "model.onnx").exists():
        pytest.skip("run python -m pipeline.export first")
    import onnxruntime as ort
    from tokenizers import Tokenizer

    session = ort.InferenceSession(str(MODEL_DIR / "model.onnx"), providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))

    def embed(texts: list[str]) -> np.ndarray:
        out = []
        for text in texts:  # one query at a time, as the runtime does
            enc = tok.encode(text)
            feed = {
                "input_ids": np.array([enc.ids], dtype=np.int64),
                "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
                "token_type_ids": np.array([enc.type_ids], dtype=np.int64),
            }
            hidden = session.run(None, feed)[0]
            mask = feed["attention_mask"][..., None].astype(np.float32)
            pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
            out.append(pooled[0] / np.linalg.norm(pooled[0]))
        return np.asarray(out, dtype=np.float32)

    return embed


@pytest.fixture(scope="module")
def st_embed():
    from pipeline.embed import encode, load_model

    model = load_model(CONFIG["embedding"]["model"])
    return lambda texts: encode(model, texts)


def test_onnx_matches_sentence_transformers(onnx_embed, st_embed):
    a, b = onnx_embed(QUESTIONS), st_embed(QUESTIONS)
    cos = (a * b).sum(axis=1)
    worst = int(np.argmin(cos))
    assert cos.min() > 0.999, f"cosine {cos[worst]:.6f} for {QUESTIONS[worst]!r}"


def test_onnx_truncates_like_sentence_transformers(onnx_embed, st_embed):
    long_text = " ".join(["exit load and expense ratio of the scheme"] * 80)  # > 256 word pieces
    cos = float(onnx_embed([long_text])[0] @ st_embed([long_text])[0])
    assert cos > 0.999


def test_snapshot_top5_matches_chromadb(onnx_embed, st_embed):
    from pipeline.store import get_client

    vecs = np.load(INDEX_DIR / "vectors.npy")
    records = json.loads((INDEX_DIR / "records.json").read_text(encoding="utf-8"))
    col = get_client(CONFIG).get_collection(CONFIG["vector_store"]["collection"], embedding_function=None)

    snap_q = onnx_embed(SEARCH_QUERIES)
    chroma = col.query(query_embeddings=st_embed(SEARCH_QUERIES).tolist(), n_results=TOP_K, include=[])
    for q, sq, chroma_ids in zip(SEARCH_QUERIES, snap_q, chroma["ids"]):
        snap_ids = [records[i]["chunk_id"] for i in np.argsort(-(vecs @ sq))[:TOP_K]]
        assert snap_ids == chroma_ids, f"{q!r}: snapshot {snap_ids} vs chroma {chroma_ids}"
