"""Component 5 — Vector Store (ChromaDB).

Upserts the precomputed phase 5 vectors (not ChromaDB's own embedding
function), the chunk texts and their metadata into a persistent ChromaDB
collection with cosine distance.

Idempotent: records are keyed by chunk_id, so a rerun replaces them, and
records whose chunk_id is no longer in chunks.jsonl are deleted. If the
existing collection uses a different distance, it is dropped and rebuilt.

Ends with the smoke queries from vector_store.smoke_queries in config.yaml.

Input:  data/chunks/chunks.jsonl, data/embeddings/
Output: data/chroma/  (persistent client directory; not deployed)

Usage:
    python -m pipeline.store [--no-smoke]
"""

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

import chromadb  # noqa: E402
import numpy as np  # noqa: E402
from chromadb.config import Settings  # noqa: E402

from app.config import PROJECT_ROOT, load_config  # noqa: E402

META_FIELDS = ("source_id", "url", "publisher", "doc_type", "scheme", "fetched_on")


def get_client(config: dict) -> chromadb.ClientAPI:
    path = PROJECT_ROOT / config["vector_store"]["persist_dir"]
    return chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))


def get_collection(client: chromadb.ClientAPI, config: dict):
    vcfg = config["vector_store"]
    name, space = vcfg["collection"], vcfg["distance"]
    if name in [c.name for c in client.list_collections()]:
        existing = client.get_collection(name, embedding_function=None)
        if (existing.metadata or {}).get("hnsw:space") != space:
            print(f"Collection {name!r} uses a different distance; rebuilding it with {space}.")
            client.delete_collection(name)
    return client.get_or_create_collection(name, metadata={"hnsw:space": space}, embedding_function=None)


def load_inputs(config: dict) -> tuple[list[dict], np.ndarray]:
    paths = config["paths"]
    chunks_path = PROJECT_ROOT / paths["chunks_dir"] / "chunks.jsonl"
    emb_dir = PROJECT_ROOT / paths["embeddings_dir"]
    chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    vecs = np.load(emb_dir / "embeddings.npy")
    ids = json.loads((emb_dir / "ids.json").read_text(encoding="utf-8"))
    meta = json.loads((emb_dir / "meta.json").read_text(encoding="utf-8"))
    if ids != [c["chunk_id"] for c in chunks] or len(ids) != len(vecs):
        raise SystemExit("Store failed: data/embeddings/ is out of date with chunks.jsonl. "
                         "Run python -m pipeline.embed first.")
    if meta["model"] != config["embedding"]["model"]:
        raise SystemExit(f"Store failed: embeddings were made with {meta['model']}, "
                         f"config says {config['embedding']['model']}. Rerun pipeline.embed.")
    return chunks, vecs


def smoke_hit_ok(spec: dict, meta: dict, source_urls: dict[str, str]) -> bool:
    if "scheme" in spec and meta["scheme"] != spec["scheme"]:
        return False
    if "doc_type" in spec and meta["doc_type"] != spec["doc_type"]:
        return False
    return bool(meta["url"]) and meta["url"] == source_urls.get(meta["source_id"])


def smoke(collection, config: dict, chunks: list[dict]) -> int:
    from pipeline.embed import encode, load_model

    specs = config["vector_store"].get("smoke_queries", [])
    if not specs:
        return 0
    source_urls = {c["source_id"]: c["url"] for c in chunks}
    qvecs = encode(load_model(config["embedding"]["model"]), [s["query"] for s in specs])
    res = collection.query(query_embeddings=qvecs.tolist(), n_results=3,
                           include=["metadatas", "distances"])
    misses = 0
    print("\nSmoke queries (top 3, cosine similarity):")
    for spec, ids, metas, dists in zip(specs, res["ids"], res["metadatas"], res["distances"]):
        ok = smoke_hit_ok(spec, metas[0], source_urls)
        misses += not ok
        want = spec.get("scheme") or spec.get("doc_type")
        print(f"  [{'OK ' if ok else 'MISS'}] {spec['query']!r}  (want {want})")
        for cid, m, d in zip(ids, metas, dists):
            print(f"         {1 - d:.3f}  {cid:<9} {m['scheme']:<20} {m['doc_type']:<16} {m['url']}")
    return misses


def store(run_smoke: bool) -> int:
    config = load_config()
    chunks, vecs = load_inputs(config)
    client = get_client(config)
    col = get_collection(client, config)

    new_ids = [c["chunk_id"] for c in chunks]
    stale = sorted(set(col.get(include=[])["ids"]) - set(new_ids))
    if stale:
        col.delete(ids=stale)

    batch = client.get_max_batch_size()
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        col.upsert(
            ids=[c["chunk_id"] for c in part],
            embeddings=vecs[i:i + batch],
            documents=[c["text"] for c in part],
            metadatas=[{k: c[k] for k in META_FIELDS} for c in part],
        )

    count = col.count()
    print(f"Upserted {len(chunks)} records into {col.name!r} "
          f"({config['vector_store']['distance']}); deleted {len(stale)} stale; count() = {count}")
    if count != len(chunks):
        print(f"Store failed: count() {count} != {len(chunks)} chunks", file=sys.stderr)
        return 1
    print(f"-> {config['vector_store']['persist_dir']}/")

    if run_smoke:
        misses = smoke(col, config, chunks)
        if misses:
            print(f"\n{misses} smoke quer{'y' if misses == 1 else 'ies'} missed (see check 6.3). "
                  "The collection was still written.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-smoke", action="store_true", help="skip the smoke queries")
    args = parser.parse_args()
    sys.exit(store(not args.no_smoke))


if __name__ == "__main__":
    main()
