"""Component 4 — Embedder.

Encodes every chunk with the configured sentence-transformers model
(all-MiniLM-L6-v2) into one L2-normalised vector per chunk.

Input:  data/chunks/chunks.jsonl
Output: data/embeddings/embeddings.npy  (N x dim, float32, row i = ids[i])
        data/embeddings/ids.json        (row order -> chunk_id)
        data/embeddings/meta.json       ({model, dim, count, created_on,
                                          sentence_transformers_version})

Usage:
    python -m pipeline.embed [--in-dir DIR] [--out-dir DIR] [--batch-size N]
"""

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np  # noqa: E402
import sentence_transformers  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

from app.config import PROJECT_ROOT, load_config  # noqa: E402


def load_model(name: str) -> SentenceTransformer:
    # CPU keeps vectors reproducible across machines and matches the ONNX runtime on Vercel.
    return SentenceTransformer(name, device="cpu")


def encode(model: SentenceTransformer, texts: list[str], batch_size: int = 64) -> np.ndarray:
    vecs = model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                        convert_to_numpy=True, show_progress_bar=False)
    return np.asarray(vecs, dtype=np.float32)


def embed(in_dir: Path, out_dir: Path, batch_size: int) -> int:
    config = load_config()
    ecfg = config["embedding"]
    chunks = [json.loads(l) for l in (in_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if not chunks:
        print("Embed failed: no chunks found.", file=sys.stderr)
        return 1

    model = load_model(ecfg["model"])
    vecs = encode(model, [c["text"] for c in chunks], batch_size)

    errors = []
    if vecs.shape != (len(chunks), ecfg["dim"]):
        errors.append(f"shape {vecs.shape}, expected ({len(chunks)}, {ecfg['dim']})")
    if not np.isfinite(vecs).all():
        errors.append("NaN or Inf in embeddings")
    norms = np.linalg.norm(vecs, axis=1)
    if np.abs(norms - 1).max() > 1e-3:
        errors.append(f"norms outside 1 ± 1e-3 (min {norms.min():.5f}, max {norms.max():.5f})")
    if errors:
        print("\nEmbed failed. data/embeddings/ was not changed.", file=sys.stderr)
        for e in errors:
            print(f"  ERROR {e}", file=sys.stderr)
        return 1

    meta = {
        "model": ecfg["model"],
        "dim": int(vecs.shape[1]),
        "count": int(vecs.shape[0]),
        "created_on": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sentence_transformers_version": sentence_transformers.__version__,
    }
    staging = out_dir.with_name(out_dir.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    np.save(staging / "embeddings.npy", vecs)
    (staging / "ids.json").write_text(json.dumps([c["chunk_id"] for c in chunks]), encoding="utf-8")
    (staging / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (staging / ".gitkeep").touch()
    shutil.rmtree(out_dir, ignore_errors=True)
    staging.rename(out_dir)

    rel = out_dir.relative_to(PROJECT_ROOT) if out_dir.is_relative_to(PROJECT_ROOT) else out_dir
    print(f"Embedded {meta['count']} chunks -> {meta['dim']}-d with {meta['model']} "
          f"(norms {norms.min():.5f}–{norms.max():.5f})")
    print(f"-> {rel}/embeddings.npy, ids.json, meta.json")
    return 0


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in-dir", type=Path, default=PROJECT_ROOT / config["paths"]["chunks_dir"])
    parser.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / config["paths"]["embeddings_dir"])
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()
    sys.exit(embed(args.in_dir.resolve(), args.out_dir.resolve(), args.batch_size))


if __name__ == "__main__":
    main()
