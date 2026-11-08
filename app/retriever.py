"""Component 8 — Retriever.

Loads the read-only snapshot in data/index/ and the ONNX embedding model once, at
module level, and runs exact cosine search over it with numpy. Uses only
onnxruntime, tokenizers and numpy, so it fits the Vercel runtime.

Retrieval rules (config.yaml → retrieval):
- With a scheme, only that scheme's chunks plus `scheme == general` chunks are searched.
- Chunks matching `exclude_patterns` (performance tables, AUM, NAV figures) are never
  returned, so the generator never sees them.
- `prefer_scheme_page`: when the question asks about one of these facts and a scheme is
  known, that scheme's scheme-page chunk for the fact is placed first (C1), ahead of
  factsheet/KIM/SID chunks that state the same fact.
- If the best score is below `min_score`, nothing is returned.
"""

from dataclasses import dataclass
import json
import re

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from app.config import PROJECT_ROOT, load_config

_CONFIG = load_config()
_RCFG = _CONFIG["retrieval"]
_INDEX_DIR = PROJECT_ROOT / _CONFIG["paths"]["index_dir"]
_MODEL_DIR = PROJECT_ROOT / _CONFIG["embedding"]["onnx_dir"]
_MAX_SEQ = 256  # all-MiniLM-L6-v2 max sequence length (data/index/model/export_meta.json)

# ---- loaded once per process (cold start) ----
_VECTORS: np.ndarray = np.load(_INDEX_DIR / "vectors.npy").astype(np.float32)
with (_INDEX_DIR / "records.json").open(encoding="utf-8") as _f:
    RECORDS: list[dict] = json.load(_f)
with (_INDEX_DIR / "index_meta.json").open(encoding="utf-8") as _f:
    INDEX_META: dict = json.load(_f)
if len(RECORDS) != _VECTORS.shape[0]:
    raise RuntimeError("data/index/ is inconsistent: records.json and vectors.npy differ in length")

_SESSION = ort.InferenceSession(str(_MODEL_DIR / "model.onnx"), providers=["CPUExecutionProvider"])
_INPUT_NAMES = {i.name for i in _SESSION.get_inputs()}
_TOKENIZER = Tokenizer.from_file(str(_MODEL_DIR / "tokenizer.json"))
_TOKENIZER.enable_truncation(max_length=_MAX_SEQ)
_TOKENIZER.no_padding()

_SCHEMES = np.array([r["scheme"] for r in RECORDS])
_EXCLUDE_RES = [re.compile(p, re.IGNORECASE) for p in _RCFG.get("exclude_patterns", [])]
_EXCLUDED = np.array([any(rx.search(r["text"]) for rx in _EXCLUDE_RES) for r in RECORDS])


@dataclass
class Hit:
    record: dict
    score: float


def embed_query(text: str) -> np.ndarray:
    """Mean-pooled, L2-normalised embedding (same as pipeline.embed and tests/test_parity.py)."""
    enc = _TOKENIZER.encode(text)
    feed = {
        "input_ids": np.array([enc.ids], dtype=np.int64),
        "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
        "token_type_ids": np.array([enc.type_ids], dtype=np.int64),
    }
    feed = {k: v for k, v in feed.items() if k in _INPUT_NAMES}
    hidden = _SESSION.run(None, feed)[0]
    mask = feed["attention_mask"][..., None].astype(np.float32)
    pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
    vec = pooled[0]
    return (vec / max(np.linalg.norm(vec), 1e-12)).astype(np.float32)


def _preferred_fact_label(question: str) -> str | None:
    q = question.lower()
    for rule in _RCFG.get("prefer_scheme_page", []):
        if any(re.search(rf"\b{re.escape(k.lower())}\b", q) for k in rule["keywords"]):
            return rule["label"]
    return None


def retrieve(question: str, scheme: str | None = None, top_k: int | None = None) -> list[Hit]:
    """Top-k chunks for the question, best first; [] if the best score is below min_score."""
    top_k = top_k or _RCFG["top_k"]
    allowed = ~_EXCLUDED
    if scheme:
        allowed &= (_SCHEMES == scheme) | (_SCHEMES == "general")
    idx = np.flatnonzero(allowed)
    if idx.size == 0:
        return []

    scores = _VECTORS[idx] @ embed_query(question)
    order = np.argsort(-scores)
    if float(scores[order[0]]) < _RCFG["min_score"]:
        return []
    hits = [Hit(RECORDS[idx[i]], float(scores[i])) for i in order[:top_k]]

    label = _preferred_fact_label(question) if scheme else None
    if label:
        marker = f" - {label}:"
        for i in order:
            rec = RECORDS[idx[i]]
            if rec["doc_type"] == "scheme_page" and rec["scheme"] == scheme and marker in rec["text"]:
                pinned = Hit(rec, float(scores[i]))
                hits = [pinned] + [h for h in hits if h.record["chunk_id"] != rec["chunk_id"]][: top_k - 1]
                break
    return hits
