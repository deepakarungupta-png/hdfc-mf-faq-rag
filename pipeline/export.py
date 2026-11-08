"""Component 6 — Exporter.

Reads every record back from ChromaDB and writes the read-only snapshot the
Vercel runtime uses, plus an ONNX copy of the embedding model.

Snapshot (rows sorted by chunk_id; vectors[i] belongs to records[i]):
    data/index/vectors.npy       N x dim float32, L2-normalised
    data/index/records.json      [{chunk_id, text, source_id, url, publisher,
                                   doc_type, scheme, fetched_on}, ...]
    data/index/index_meta.json   {embedding_model, built_on, corpus_last_updated,
                                   count, dim}

Model (embedding.onnx_dir):
    model.onnx        inputs input_ids / attention_mask / token_type_ids (int64),
                      output last_hidden_state; the runtime applies mean pooling
                      over attention_mask, then L2 normalisation
    tokenizer.json    with truncation at the model's max_seq_length (256)
    export_meta.json  {model, revision, max_seq_length, pooling, normalize, opset}

The model is exported with torch.onnx (eager attention), not optimum: the
pinned optimum 1.17 cannot import under transformers 5.x, and the newer
optimum-onnx would downgrade transformers. tests/test_parity.py checks the
result against sentence-transformers. The export is skipped when
export_meta.json already matches the configured model and its revision.

Usage:
    python -m pipeline.export [--force-model]
"""

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import warnings
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

import numpy as np  # noqa: E402

from app.config import PROJECT_ROOT, load_config  # noqa: E402
from pipeline.store import META_FIELDS, get_client  # noqa: E402

ONNX_OPSET = 17
ONNX_INPUTS = ["input_ids", "attention_mask", "token_type_ids"]


def read_collection(config: dict) -> tuple[list[str], np.ndarray, list[str], list[dict]]:
    client = get_client(config)
    col = client.get_collection(config["vector_store"]["collection"], embedding_function=None)
    ids, embs, docs, metas = [], [], [], []
    page = 500
    for offset in range(0, col.count(), page):
        got = col.get(limit=page, offset=offset, include=["embeddings", "documents", "metadatas"])
        ids += got["ids"]
        embs.append(np.asarray(got["embeddings"], dtype=np.float32))
        docs += got["documents"]
        metas += got["metadatas"]
    if not ids:
        raise SystemExit("Export failed: the ChromaDB collection is empty. Run python -m pipeline.store first.")
    order = sorted(range(len(ids)), key=ids.__getitem__)
    vecs = np.concatenate(embs)[order]
    return [ids[i] for i in order], vecs, [docs[i] for i in order], [metas[i] for i in order]


def write_snapshot(config: dict, index_dir: Path) -> dict:
    ids, vecs, docs, metas = read_collection(config)
    dim = config["embedding"]["dim"]
    norms = np.linalg.norm(vecs, axis=1)
    if vecs.shape != (len(ids), dim) or not np.isfinite(vecs).all() or np.abs(norms - 1).max() > 1e-3:
        raise SystemExit(f"Export failed: vectors read from ChromaDB look wrong (shape {vecs.shape}).")
    if len(set(ids)) != len(ids):
        raise SystemExit("Export failed: duplicate ids in the collection.")

    records = [{"chunk_id": i, "text": d, **{k: m[k] for k in META_FIELDS}} for i, d, m in zip(ids, docs, metas)]
    meta = {
        "embedding_model": config["embedding"]["model"],
        "built_on": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "corpus_last_updated": max(m["fetched_on"] for m in metas),
        "count": len(ids),
        "dim": dim,
    }

    index_dir.mkdir(parents=True, exist_ok=True)
    tmp = {name: index_dir / f".{name}.tmp" for name in ("vectors.npy", "records.json", "index_meta.json")}
    with tmp["vectors.npy"].open("wb") as f:
        np.save(f, vecs)
    tmp["records.json"].write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    tmp["index_meta.json"].write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    for name, path in tmp.items():
        os.replace(path, index_dir / name)
    return meta


def model_revision(name: str) -> str:
    """The Hugging Face commit hash of the model snapshot sentence-transformers uses."""
    from huggingface_hub import snapshot_download

    try:
        path = snapshot_download(name, local_files_only=True)
    except Exception:
        path = snapshot_download(name)
    return Path(path).name


def export_model(config: dict, model_dir: Path, force: bool) -> str:
    name = config["embedding"]["model"]
    revision = model_revision(name)
    meta_path = model_dir / "export_meta.json"
    if not force and meta_path.exists() and (model_dir / "model.onnx").exists() and (model_dir / "tokenizer.json").exists():
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        if old.get("model") == name and old.get("revision") == revision:
            return f"kept existing ONNX model ({name} @ {revision[:10]})"

    import torch
    from sentence_transformers import SentenceTransformer
    from transformers import AutoModel, AutoTokenizer

    max_len = SentenceTransformer(name, device="cpu").max_seq_length
    # Eager attention: the SDPA path traces its mask with fixed shapes and gives wrong outputs.
    bert = AutoModel.from_pretrained(name, revision=revision, attn_implementation="eager").eval()
    tok = AutoTokenizer.from_pretrained(name, revision=revision)

    class Encoder(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.m(input_ids=input_ids, attention_mask=attention_mask,
                          token_type_ids=token_type_ids).last_hidden_state

    staging = model_dir.with_name(model_dir.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    sample = tok(["a short sample", "a somewhat longer sample sentence for tracing"], padding=True, return_tensors="pt")
    axes = {k: {0: "batch", 1: "seq"} for k in ONNX_INPUTS + ["last_hidden_state"]}
    with warnings.catch_warnings(), torch.no_grad():
        warnings.simplefilter("ignore")
        torch.onnx.export(Encoder(bert), tuple(sample[k] for k in ONNX_INPUTS), str(staging / "model.onnx"),
                          input_names=ONNX_INPUTS, output_names=["last_hidden_state"], dynamic_axes=axes,
                          opset_version=ONNX_OPSET, dynamo=False)

    fast = tok.backend_tokenizer
    fast.enable_truncation(max_length=max_len)
    fast.no_padding()
    fast.save(str(staging / "tokenizer.json"))
    (staging / "export_meta.json").write_text(json.dumps({
        "model": name, "revision": revision, "max_seq_length": max_len,
        "pooling": "mean", "normalize": True, "opset": ONNX_OPSET, "exporter": "torch.onnx",
        "torch_version": torch.__version__,
    }, indent=2) + "\n", encoding="utf-8")

    shutil.rmtree(model_dir, ignore_errors=True)
    staging.rename(model_dir)
    size = (model_dir / "model.onnx").stat().st_size / 1e6
    return f"exported ONNX model ({name} @ {revision[:10]}, {size:.1f} MB)"


def export(force_model: bool) -> int:
    config = load_config()
    index_dir = PROJECT_ROOT / config["paths"]["index_dir"]
    meta = write_snapshot(config, index_dir)
    print(f"Snapshot: {meta['count']} records x {meta['dim']}-d, corpus last updated "
          f"{meta['corpus_last_updated']} -> {config['paths']['index_dir']}/")
    print("Model: " + export_model(config, PROJECT_ROOT / config["embedding"]["onnx_dir"], force_model))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force-model", action="store_true", help="re-export the ONNX model even if cached")
    args = parser.parse_args()
    sys.exit(export(args.force_model))


if __name__ == "__main__":
    main()
