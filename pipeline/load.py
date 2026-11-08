"""Component 2 — Loader.

Turns each file in data/raw/ into clean text with its source metadata.
No network access.

- Scheme pages (doc_type in loader.next_data.doc_types): fund facts are read
  from the page's __NEXT_DATA__ JSON and written as labelled lines. A missing
  JSON block or required field is an error; there is no silent fallback.
- Other HTML: trafilatura, falling back to <main>/<article> text.
- PDFs: pdfplumber page by page with duplicate characters removed, pypdf as a
  per-page fallback. Multi-column layouts (loader.pdf_column_doc_types) are
  split at column gutters so columns are read one after another.

Output: data/loaded/<id>.txt and data/loaded/documents.jsonl.

Usage:
    python -m pipeline.load [--raw-dir DIR] [--out-dir DIR]
"""

import argparse
import html as html_lib
import json
import logging
import re
import shutil
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import pdfplumber
import pypdf
import trafilatura
from lxml import html as lxml_html

from app.config import PROJECT_ROOT, load_config

logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pypdf").setLevel(logging.ERROR)

NEXT_DATA = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class LoadError(Exception):
    pass


# ---------- text helpers ----------

def normalise(text: str) -> str:
    """NFKC (fixes ligatures), collapse spaces within lines, at most one blank line."""
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_lines(fragment: str) -> list[str]:
    """Strip HTML from a CMS field, keeping list items / <br> / bullets as separate lines."""
    fragment = re.sub(r"(?i)<\s*(br|/p|/li|/div)\s*/?>", "\n", fragment)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment))
    parts = re.split(r"\n|●|•", text)
    return [re.sub(r"\s+", " ", p).strip(" -") for p in parts if re.sub(r"\s+", " ", p).strip(" -")]


# ---------- scheme pages: __NEXT_DATA__ ----------

def _walk(obj, path: str):
    for key in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(key)]
        elif isinstance(obj, dict):
            obj = obj[key]
        else:
            raise KeyError(key)
    return obj


def _present(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(re.sub(r"<[^>]+>|\s", "", value))
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def load_scheme_page(raw: str, entry: dict, cfg: dict) -> str:
    m = NEXT_DATA.search(raw)
    if not m:
        raise LoadError("no __NEXT_DATA__ JSON in the page")
    try:
        data = _walk(json.loads(m.group(1)), cfg["path"])
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LoadError(f"__NEXT_DATA__ has no {cfg['path']} ({type(e).__name__}: {e})") from e
    missing = [f for f in cfg["required"] if not _present(data.get(f))]
    if missing:
        raise LoadError(f"__NEXT_DATA__ is missing required field(s): {', '.join(missing)}")

    lines = [f"{entry['scheme']} Fund — Direct Plan (scheme page)"]
    lines.append(f"Total Expense Ratio (TER), Direct Plan: {data['terDirecct'].strip()}%")
    lines += [f"Exit load: {x}" for x in html_to_lines(data["exitLoad"])]
    lines.append("Entry load: " + " ".join(html_to_lines(data["entryLoad"])))
    lines.append(f"Minimum SIP: ₹{data['minimumSip'].strip()}")
    if "lockInValue" in cfg["optional"]:
        lines.append(f"Lock-in period: {data.get('lockInValue') or 'NA'}")
    if "risk" in cfg["optional"] and _present(data.get("risk")):
        names = [r.get("name", "").strip() for r in data["risk"] if isinstance(r, dict) and r.get("name")]
        if names:
            lines.append(f"Riskometer: {', '.join(names)}")
    if "benchmark" in cfg["optional"] and _present(data.get("benchmark")):
        lines.append(f"Benchmark: {', '.join(b.strip() for b in data['benchmark'])}")
    return "\n".join(lines)


# ---------- other HTML ----------

def _find_faq_lists(obj):
    """Yield every list stored under a "faqs" key whose items have a title and a body."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if (k == "faqs" and isinstance(v, list) and v
                    and all(isinstance(i, dict) and "title" in i and "body" in i for i in v)):
                yield v
            yield from _find_faq_lists(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_faq_lists(v)


def faq_pairs(raw: str) -> list[tuple[str, str]]:
    """FAQ question/answer pairs from __NEXT_DATA__, de-duplicated by question.

    trafilatura drops the questions on these pages (they sit in accordion
    headers) but keeps the answers, so the pairs are rebuilt from the JSON.
    """
    m = NEXT_DATA.search(raw)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    pairs, seen = [], set()
    for faqs in _find_faq_lists(data):
        for item in faqs:
            q = re.sub(r"^\s*\d+[.)]\s*", "", " ".join(html_to_lines(item["title"] or "")))
            a = " ".join(html_to_lines(item["body"] or ""))
            if q and a and q.lower() not in seen:
                seen.add(q.lower())
                pairs.append((q, a))
    return pairs


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def merge_faqs(text: str, pairs: list[tuple[str, str]]) -> str:
    """Remove the orphaned answers trafilatura kept, then append the paired FAQ."""
    answers = _squash(" ".join(a for _, a in pairs))
    kept = []
    for line in text.split("\n"):
        core = _squash(line.lstrip("-•● "))
        if len(core) >= 15 and core in answers:
            continue  # this line is (part of) an FAQ answer; it is re-added below with its question
        if core in ("faqs", "faq"):
            continue
        kept.append(line)
    faq = ["FAQs"] + [f"Q: {q}\nA: {a}" for q, a in pairs]
    return "\n".join(kept).rstrip() + "\n\n" + "\n\n".join(faq)


def load_html(raw: str, strip_phrases: list[str], min_chars: int) -> str:
    text = trafilatura.extract(raw, favor_recall=True, include_tables=True,
                               include_comments=False, deduplicate=False) or ""
    if len(text) < min_chars:
        tree = lxml_html.fromstring(raw)
        for bad in tree.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        nodes = tree.xpath("//main") or tree.xpath("//article")
        if nodes:
            text = "\n".join(n.text_content() for n in nodes)
    for phrase in strip_phrases:
        text = text.replace(phrase, "")
    return text


# ---------- PDFs ----------

def _gutters(page, words, min_gap: int = 8) -> list[float]:
    """x positions of vertical whitespace gutters (column boundaries)."""
    width = int(page.width)
    cover = [0] * (width + 1)
    for w in words:
        for x in range(max(0, int(w["x0"])), min(width, int(w["x1"])) + 1):
            cover[x] += 1
    limit = max(1, len(words) * 0.01)
    gaps, start = [], None
    for x in range(width + 1):
        if cover[x] <= limit:
            start = x if start is None else start
        else:
            if start is not None and x - start >= min_gap and start > 20 and x < width - 20:
                gaps.append((start + x) / 2)
            start = None
    return gaps


def _page_text(page, columns: bool) -> str:
    page = page.dedupe_chars()
    if not columns:
        return page.extract_text() or ""
    edges = [0, *_gutters(page, page.extract_words()), page.width]
    parts = [page.crop((a, 0, b, page.height)).extract_text() or "" for a, b in zip(edges, edges[1:])]
    return "\n".join(p for p in parts if p.strip())


def _strip_running_lines(pages: list[list[str]]) -> list[list[str]]:
    """Drop page numbers and header/footer lines repeated on at least half the pages."""
    def key(line: str) -> str:
        return re.sub(r"\d+", "#", line.lower())

    if len(pages) >= 4:
        edge = Counter(k for lines in pages for k in {key(l) for l in lines[:2] + lines[-2:]})
        running = {k for k, n in edge.items() if n >= len(pages) / 2}
    else:
        running = set()
    out = []
    for lines in pages:
        kept = []
        for i, line in enumerate(lines):
            at_edge = i < 2 or i >= len(lines) - 2
            if re.fullmatch(r"(page\s*)?\d{1,3}(\s*of\s*\d{1,3})?", line.strip(), re.I):
                continue
            if at_edge and key(line) in running:
                continue
            kept.append(line)
        out.append(kept)
    return out


def load_pdf(path: Path, columns: bool) -> str:
    pages = []
    with pdfplumber.open(path) as pdf:
        fallback = None
        for n, page in enumerate(pdf.pages, start=1):
            text = _page_text(page, columns)
            if not text.strip():
                fallback = fallback or pypdf.PdfReader(path)
                text = fallback.pages[n - 1].extract_text() or ""
            pages.append(normalise(text).split("\n"))
    pages = _strip_running_lines(pages)
    return "\n\n".join(f"[page {n}]\n" + "\n".join(lines) for n, lines in enumerate(pages, start=1))


# ---------- main ----------

def load(raw_dir: Path, out_dir: Path) -> int:
    config = load_config()
    lcfg = config["loader"]
    manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))

    staging = out_dir.with_name(out_dir.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    docs, errors, warnings = [], [], []
    for entry in manifest:
        if not entry.get("file"):
            errors.append(f"{entry['id']}: no raw file in the manifest (run pipeline.collect)")
            continue
        path = raw_dir / entry["file"]
        try:
            if path.suffix == ".pdf":
                columns = entry["doc_type"] in lcfg["pdf_column_doc_types"]
                text = load_pdf(path, columns)
                method = "pdfplumber+columns" if columns else "pdfplumber"
            else:
                raw = path.read_text(encoding="utf-8", errors="replace")
                if entry["doc_type"] in lcfg["next_data"]["doc_types"]:
                    text, method = load_scheme_page(raw, entry, lcfg["next_data"]), "next_data"
                else:
                    text, method = load_html(raw, lcfg["strip_phrases"], lcfg["min_chars"]), "trafilatura"
                    pairs = faq_pairs(raw)
                    if pairs:
                        text, method = merge_faqs(normalise(text), pairs), f"trafilatura+faq_json({len(pairs)})"
        except LoadError as e:
            errors.append(f"{entry['id']}: {e}")
            continue
        text = normalise(text)
        if len(text) < lcfg["min_chars"] and method != "next_data":
            warnings.append(f"{entry['id']}: only {len(text)} characters — likely extraction failure")
        (staging / f"{entry['id']}.txt").write_text(text + "\n", encoding="utf-8")
        docs.append({k: entry[k] for k in ("id", "url", "publisher", "doc_type", "scheme", "fetched_on")}
                    | {"method": method, "char_count": len(text), "text": text})
        print(f"ok  {entry['id']}  {method:<18} {len(text):>9,} chars")

    if errors:
        shutil.rmtree(staging, ignore_errors=True)
        print("\nLoad failed. data/loaded/ was not changed.", file=sys.stderr)
        for e in errors:
            print(f"  ERROR {e}", file=sys.stderr)
        return 1

    with (staging / "documents.jsonl").open("w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    (staging / ".gitkeep").touch()
    shutil.rmtree(out_dir, ignore_errors=True)
    staging.rename(out_dir)

    for w in warnings:
        print(f"WARNING {w}")
    rel = out_dir.relative_to(PROJECT_ROOT) if out_dir.is_relative_to(PROJECT_ROOT) else out_dir
    print(f"\n{len(docs)} documents -> {rel}/")
    return 0


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-dir", type=Path, default=PROJECT_ROOT / config["paths"]["raw_dir"])
    parser.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / config["paths"]["loaded_dir"])
    args = parser.parse_args()
    sys.exit(load(args.raw_dir.resolve(), args.out_dir.resolve()))


if __name__ == "__main__":
    main()
