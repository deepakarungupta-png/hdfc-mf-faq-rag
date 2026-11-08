"""Component 3 — Chunker.

Splits each loaded document into chunks short enough for all-MiniLM-L6-v2
(256 word pieces), each carrying its source metadata.

- Tokens are counted with the embedding model's own tokenizer, [CLS]/[SEP]
  included, so counts match what the model sees.
- Text is split into sections at page markers and headings, and into units
  at line boundaries. Hard-wrapped PDF prose is re-joined into one unit, so a
  chunk never starts or ends mid-sentence; a table row (one line) is never
  split. Units are packed into chunks of up to chunk_size_tokens, with
  chunk_overlap_tokens of whole-unit overlap when a section has to be split.
- A unit longer than a chunk is split at sentence ends, then at line ends,
  and only as a last resort at word boundaries.
- Multi-scheme documents (chunking.scheme_page_filter_doc_types, i.e. the
  factsheet) keep only the pages whose title names an in-scope scheme; each
  kept chunk is tagged with that scheme. All other pages are dropped.
- Each chunk starts with a short context header, e.g.
  "HDFC Mid Cap Fund — Factsheet:".
- Scheme pages (chunking.fact_chunks.doc_types, i.e. s01–s04) are not packed:
  each labelled fact becomes its own chunk, prefixed with the scheme name and
  plan, e.g. "HDFC ELSS Tax Saver Fund (Direct Plan) - Lock-in period: 3 years".
  An unknown label or a missing required fact stops the chunker.

Input:  data/loaded/documents.jsonl
Output: data/chunks/chunks.jsonl

Usage:
    python -m pipeline.chunk [--in-dir DIR] [--out-dir DIR]
"""

import argparse
import json
import os
import re
import shutil
import statistics
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from transformers import AutoTokenizer  # noqa: E402
from transformers.utils import logging as hf_logging  # noqa: E402

from app.config import PROJECT_ROOT, load_config  # noqa: E402

hf_logging.set_verbosity_error()  # silence "sequence longer than 512" when measuring whole sections

PAGE_MARKER = re.compile(r"^\[page (\d+)\]$")
CONTD = re.compile(r"\s*\.{2,}\s*contd\.? from previous page.*$", re.I)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9₹•●-])")
TERMINAL = re.compile(r"[.!?:;]$")
# Words that cannot end a sentence: a line ending in one continues on the next line.
CONNECTOR = re.compile(r"\b(and|or|of|the|a|an|to|in|on|for|by|with|from|at|as|is|are|be|"
                       r"that|which|under|per|any|such|its|their|this|these|not|than)$", re.I)


class Tokens:
    def __init__(self, model: str):
        self.tok = AutoTokenizer.from_pretrained(model)

    def count(self, text: str) -> int:
        """Word pieces the model sees, including [CLS] and [SEP]."""
        return len(self.tok(text, add_special_tokens=True, truncation=False)["input_ids"])

    def inner(self, text: str) -> int:
        """Word pieces without the special tokens (for packing)."""
        return len(self.tok(text, add_special_tokens=False, truncation=False)["input_ids"])


# ---------- scheme tagging for multi-scheme documents ----------

def page_scheme(title: str, schemes: list[str]) -> str | None:
    """Scheme named by a factsheet page's title line, or None.

    The factsheet's column split can truncate the title ("HDFC Large Cap Fu",
    "HDFC ELSS Tax Save"). A title matches a scheme when it is a prefix of
    "<scheme> Fund" (or that name followed by more text) and covers every word
    of the scheme name except, at most, part of the last one — so fragments such
    as "HD" or "HDFC Large" (which could be HDFC Large and Mid Cap) never match.
    """
    t = re.sub(r"\s+", " ", CONTD.sub("", title)).strip().lower()
    for scheme in schemes:
        full = f"{scheme} fund".lower()
        must_cover = len(scheme.lower().rsplit(" ", 1)[0]) + 2  # all words before the last + 1 letter
        if len(t) >= must_cover and (full.startswith(t) or t.startswith(full)):
            return scheme
    return None


def split_pages(text: str) -> list[tuple[int | None, str]]:
    """[(page_number, page_text)]; a single (None, text) for documents without page markers."""
    pages, current, lines = [], None, []
    for line in text.split("\n"):
        m = PAGE_MARKER.match(line.strip())
        if m:
            if lines or current is not None:
                pages.append((current, "\n".join(lines).strip()))
            current, lines = int(m.group(1)), []
        else:
            lines.append(line)
    pages.append((current, "\n".join(lines).strip()))
    return [(n, t) for n, t in pages if t]


# ---------- sections and units ----------

def is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 80 or s.startswith(("Q:", "A:")):
        return False
    if re.match(r"^(\d{1,2}|[IVX]{1,4}|[A-Z])[.)]\s+\S", s) and not TERMINAL.search(s[-1:]) and len(s.split()) <= 12:
        return True  # "5. Investment Objective", "4) What is NAV of a scheme?" is handled below
    if re.match(r"^\d{1,2}[.)]\s+.*\?$", s) and len(s.split()) <= 20:
        return True  # numbered FAQ question
    letters = re.sub(r"[^A-Za-z]", "", s)
    return len(letters) >= 4 and letters.isupper() and len(s.split()) <= 10


def sections(text: str) -> list[list[str]]:
    """Blank lines and headings start a new section; a heading stays with what follows it."""
    out, cur = [], []
    for line in text.split("\n"):
        if not line.strip():
            if cur:
                out.append(cur)
            cur = []
        elif is_heading(line) and cur and not is_heading(cur[-1]):
            out.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        out.append(cur)
    return out


def units(lines: list[str], wrap_width: int) -> list[str]:
    """Group lines into units. A hard-wrapped prose line is joined with the next one,
    so units end at sentence ends (or at short lines such as table rows)."""
    out, cur = [], []
    for i, line in enumerate(lines):
        cur.append(line)
        s = line.rstrip()
        nxt = lines[i + 1].lstrip() if i + 1 < len(lines) else ""
        continues = bool(nxt) and not is_heading(nxt) and not nxt.startswith(("Q:", "A:", "- ", "• ", "● ")) and (
            nxt[:1].islower()
            or s.endswith((",", "-", "/", "(", "&", "–"))
            or bool(CONNECTOR.search(s))
            or (len(s) >= wrap_width and not TERMINAL.search(s))
        )
        if s.startswith("Q:") and nxt.startswith("A:"):
            continues = True  # keep an FAQ question with its answer
        if not continues:
            out.append("\n".join(cur))
            cur = []
    if cur:
        out.append("\n".join(cur))
    return out


def wrap_width(text: str) -> int:
    """Line length at which a document's prose is hard-wrapped (80th percentile of long lines)."""
    lengths = sorted(len(l) for l in text.split("\n") if len(l) > 30)
    if len(lengths) < 20:
        return 10_000  # HTML/JSON text is not hard-wrapped
    return int(lengths[int(len(lengths) * 0.8)] * 0.75)


def split_oversized(unit: str, budget: int, tk: Tokens) -> list[str]:
    """Split a unit longer than the budget: sentences, then lines, then words."""
    for pieces in (SENTENCE_END.split(unit), unit.split("\n")):
        if len(pieces) > 1:
            out, cur = [], ""
            for p in pieces:
                cand = f"{cur} {p}".strip() if cur else p
                if cur and tk.inner(cand) > budget:
                    out.append(cur)
                    cur = p
                else:
                    cur = cand
            if cur:
                out.append(cur)
            if all(tk.inner(o) <= budget for o in out):
                return out
            return [x for o in out for x in (split_oversized(o, budget, tk) if tk.inner(o) > budget else [o])]
    words, out, cur = unit.split(" "), [], []
    for w in words:
        if cur and tk.inner(" ".join(cur + [w])) > budget:
            out.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        out.append(" ".join(cur))
    return out


# ---------- packing ----------

def pack(secs: list[list[str]], size: int, overlap: int, tk: Tokens) -> list[str]:
    """Pack units into chunk bodies of at most `size` word pieces.

    A section boundary starts a new chunk once the current one holds at least half
    a chunk (no overlap across sections). A size-forced break carries the last
    whole units, up to `overlap` word pieces, into the next chunk.
    """
    bodies, cur, cur_n = [], [], 0

    def flush():
        nonlocal cur, cur_n
        if cur:
            bodies.append("\n".join(cur))
        cur, cur_n = [], 0

    for sec_units in secs:
        if cur_n >= size // 2:
            flush()
        for u in sec_units:
            if cur and tk.inner("\n".join(cur + [u])) > size:
                tail, tail_n = [], 0
                for prev in reversed(cur):
                    pn = tk.inner(prev)
                    if tail_n + pn > overlap:
                        break
                    tail.insert(0, prev)
                    tail_n += pn
                flush()
                if tail and tk.inner("\n".join(tail + [u])) <= size:
                    cur, cur_n = tail, tail_n
            cur.append(u)
            cur_n = tk.inner("\n".join(cur))
    flush()
    return bodies


def merge_small(bodies: list[str], min_tokens: int, hard: int, tk: Tokens) -> list[str]:
    """Fold chunks under min_tokens (stray headings, table fragments) into a neighbour,
    as long as the result stays within the hard cap."""
    out = list(bodies)
    i = 0
    while i < len(out):
        if len(out) > 1 and tk.inner(out[i]) < min_tokens:
            if i > 0 and tk.inner(out[i - 1] + "\n" + out[i]) <= hard:
                out[i - 1] += "\n" + out.pop(i)
                continue
            if i + 1 < len(out) and tk.inner(out[i] + "\n" + out[i + 1]) <= hard:
                out[i + 1] = out.pop(i) + "\n" + out[i]
                continue
        i += 1
    return out


# ---------- scheme pages: one chunk per labelled fact ----------

SCHEME_PAGE_TITLE = re.compile(r"^(?P<name>.+?) — (?P<plan>.+? Plan) \(scheme page\)$")


class ChunkError(ValueError):
    pass


def fact_chunk_texts(text: str, facts: list[dict]) -> list[str]:
    """Split a scheme page's labelled fact list into one text per fact.

    The first line is the title written by pipeline.load ("HDFC Large Cap Fund — Direct
    Plan (scheme page)"); every other line is "<label>: <value>". Each output text is
    "<name> (<plan>) - <label>: <value>", in the order of `facts`. Lines that share a
    label (exit load bullets) are joined into one fact, each ending as a sentence.
    Raises ChunkError on an unexpected title, an unknown label or a missing required fact.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    m = SCHEME_PAGE_TITLE.match(lines[0]) if lines else None
    if not m:
        raise ChunkError(f"scheme page title line not recognised: {lines[0] if lines else ''!r}")
    prefix = f"{m['name']} ({m['plan']})"
    by_line = {f["line"]: f for f in facts}
    values: dict[str, list[str]] = {}
    for line in lines[1:]:
        label, sep, value = line.partition(": ")
        if not sep or label not in by_line:
            raise ChunkError(f"unlabelled or unknown fact line: {line!r}")
        values.setdefault(label, []).append(value.strip())
    missing = [f["line"] for f in facts if f.get("required") and not values.get(f["line"])]
    if missing:
        raise ChunkError(f"missing required fact(s): {', '.join(missing)}")
    out = []
    for f in facts:
        vals = values.get(f["line"])
        if not vals:
            continue
        if len(vals) > 1:
            vals = [v if TERMINAL.search(v) else v + "." for v in vals]
        out.append(f"{prefix} - {f['label']}: {' '.join(vals)}")
    return out


# ---------- main ----------

def header(doc: dict, scheme: str, labels: dict) -> str:
    label = labels.get(doc["doc_type"], doc["doc_type"])
    who = f"{scheme} Fund" if scheme != "general" else doc["publisher"]
    return f"{who} — {label}:"


def chunk_document(doc: dict, ccfg: dict, schemes: list[str], tk: Tokens) -> tuple[list[dict], dict]:
    size, overlap = ccfg["chunk_size_tokens"], ccfg["chunk_overlap_tokens"]
    cap = ccfg.get("max_total_tokens", 256)
    labels = ccfg.get("doc_type_labels", {})
    filter_pages = doc["doc_type"] in ccfg.get("scheme_page_filter_doc_types", [])
    fcfg = ccfg.get("fact_chunks") or {}

    def record(n: int, scheme: str, text_out: str) -> dict:
        return {
            "chunk_id": f"{doc['id']}-{n:03d}",
            "source_id": doc["id"],
            "url": doc["url"],
            "publisher": doc["publisher"],
            "doc_type": doc["doc_type"],
            "scheme": scheme,
            "fetched_on": doc["fetched_on"],
            "token_count": tk.count(text_out),
            "text": text_out,
        }

    if doc["doc_type"] in fcfg.get("doc_types", []):
        texts = fact_chunk_texts(doc["text"], fcfg["facts"])
        return [record(i, doc["scheme"], t) for i, t in enumerate(texts, 1)], {}

    # Group the text into runs that share one scheme tag.
    runs: list[tuple[str, str]] = []
    kept_pages: dict[str, list[int]] = {}
    dropped = 0
    for page_no, page_text in split_pages(doc["text"]):
        if filter_pages:
            scheme = page_scheme(page_text.split("\n", 1)[0], schemes)
            if scheme is None:
                dropped += 1
                continue
            kept_pages.setdefault(scheme, []).append(page_no)
        else:
            scheme = doc["scheme"]
        if runs and runs[-1][0] == scheme:
            # No blank line at a page break: prose often runs on to the next page,
            # and units() decides from the text whether the line continues.
            runs[-1] = (scheme, runs[-1][1] + "\n" + page_text)
        else:
            runs.append((scheme, page_text))

    width = wrap_width(doc["text"])
    chunks = []
    for scheme, text in runs:
        head = header(doc, scheme, labels)
        budget = size
        secs = []
        for sec in sections(text):
            us = []
            for u in units(sec, width):
                us += split_oversized(u, budget, tk) if tk.inner(u) > budget else [u]
            secs.append(us)
        hard = cap - tk.count(head + "\n")  # room left for the body under the hard cap
        if tk.inner(text) <= hard:
            bodies = [text]  # a short document (e.g. a scheme page's fact list) stays whole
        else:
            bodies = merge_small(pack(secs, budget, overlap, tk), ccfg.get("min_chunk_tokens", 0), hard, tk)
        for body in bodies:
            chunks.append(record(len(chunks) + 1, scheme, f"{head}\n{body}"))
    info = {"kept_pages": kept_pages, "dropped_pages": dropped} if filter_pages else {}
    return chunks, info


def chunk(in_dir: Path, out_dir: Path) -> int:
    config = load_config()
    ccfg = config["chunking"]
    schemes = [s for s in config["allowed_schemes"] if s != "general"]
    cap = ccfg.get("max_total_tokens", 256)
    tk = Tokens(config["embedding"]["model"])

    docs = [json.loads(l) for l in (in_dir / "documents.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    all_chunks, errors = [], []
    for doc in docs:
        try:
            chunks, info = chunk_document(doc, ccfg, schemes, tk)
        except ChunkError as e:
            errors.append(f"{doc['id']}: {e}")
            continue
        if not chunks:
            errors.append(f"{doc['id']}: produced no chunks")
        if info:
            for s in schemes:
                if s not in info["kept_pages"]:
                    errors.append(f"{doc['id']}: no page found for scheme {s!r}")
            kept = ", ".join(f"{s} p{'/'.join(map(str, p))}" for s, p in info["kept_pages"].items())
            print(f"     {doc['id']} scheme pages kept: {kept}; {info['dropped_pages']} other pages dropped")
        all_chunks += chunks

    over = [c["chunk_id"] for c in all_chunks if c["token_count"] > cap]
    if over:
        errors.append(f"{len(over)} chunk(s) over {cap} tokens: {', '.join(over[:10])}")
    for c in all_chunks:
        for field in ("url", "scheme", "fetched_on", "text"):
            if not c.get(field):
                errors.append(f"{c['chunk_id']}: empty {field}")
    dupes = [k for k, n in Counter(c["chunk_id"] for c in all_chunks).items() if n > 1]
    if dupes:
        errors.append(f"duplicate chunk_id(s): {', '.join(dupes)}")

    if errors:
        print("\nChunk failed. data/chunks/ was not changed.", file=sys.stderr)
        for e in errors:
            print(f"  ERROR {e}", file=sys.stderr)
        return 1

    staging = out_dir.with_name(out_dir.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with (staging / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    (staging / ".gitkeep").touch()
    shutil.rmtree(out_dir, ignore_errors=True)
    staging.rename(out_dir)

    per_source = Counter(c["source_id"] for c in all_chunks)
    counts = [c["token_count"] for c in all_chunks]
    print("\nChunks per source:")
    for doc in docs:
        sub = [c["token_count"] for c in all_chunks if c["source_id"] == doc["id"]]
        print(f"  {doc['id']}  {doc['doc_type']:<16} {per_source[doc['id']]:>4}   "
              f"tokens min/mean/max {min(sub)}/{statistics.mean(sub):.0f}/{max(sub)}")
    per_scheme = Counter(c["scheme"] for c in all_chunks)
    print("Chunks per scheme: " + ", ".join(f"{s}: {n}" for s, n in sorted(per_scheme.items())))
    print(f"Total chunks: {len(all_chunks)}   tokens min/mean/max "
          f"{min(counts)}/{statistics.mean(counts):.0f}/{max(counts)}")
    rel = out_dir.relative_to(PROJECT_ROOT) if out_dir.is_relative_to(PROJECT_ROOT) else out_dir
    print(f"-> {rel}/chunks.jsonl")
    return 0


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in-dir", type=Path, default=PROJECT_ROOT / config["paths"]["loaded_dir"])
    parser.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / config["paths"]["chunks_dir"])
    args = parser.parse_args()
    sys.exit(chunk(args.in_dir.resolve(), args.out_dir.resolve()))


if __name__ == "__main__":
    main()
