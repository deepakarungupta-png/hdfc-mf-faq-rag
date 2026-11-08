"""Component 1 — Collector.

Downloads exactly the URLs listed in sources.csv, one GET each, and nothing
else: no link parsing, no embedded resources, no crawling. HTTP redirects are
followed by hand so every hop can be checked against the domain rule.

Some sites (www.hdfcfund.com) block all automated requests. For those rows
only, a page saved by hand from a browser can be placed in data/manual/ as
<id>.html or <id>.pdf. It is used only when that row's download fails, and
is rejected if it names a different page or is an "Access Denied" page.

Output: data/raw/<id>.<html|pdf> and data/raw/manifest.json.

Usage:
    python -m pipeline.collect [--sources PATH]
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import re
from urllib.parse import urlparse

import requests

from app.config import PROJECT_ROOT, load_config
from app.sources import is_allowed_host

REQUIRED_COLUMNS = ["id", "publisher", "scheme", "doc_type", "url"]
MIN_SOURCES = 15
MAX_REDIRECTS = 5


class ValidationError(Exception):
    pass


def read_sources(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != REQUIRED_COLUMNS:
            raise ValidationError(
                f"{path.name}: header must be {','.join(REQUIRED_COLUMNS)}, "
                f"got {','.join(reader.fieldnames or [])}"
            )
        return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def validate(rows: list[dict], config: dict) -> None:
    """Raise ValidationError naming every invalid row. Makes no network calls."""
    errors = []
    seen = set()
    allowed = {
        "publisher": config["allowed_publishers"],
        "doc_type": config["allowed_doc_types"],
        "scheme": config["allowed_schemes"],
    }
    for line_no, row in enumerate(rows, start=2):  # line 1 is the header
        label = f"line {line_no} (id={row.get('id') or '?'})"
        if None in row:
            errors.append(f"{label}: too many columns (is a URL with a comma unquoted?)")
            continue
        if not row["id"]:
            errors.append(f"{label}: empty id")
        elif row["id"] in seen:
            errors.append(f"{label}: duplicate id")
        seen.add(row["id"])
        if not is_allowed_host(row["url"], config["source_allowlist"]):
            errors.append(f"{label}: url not allowed (must be https on {config['source_allowlist']}): {row['url']}")
        for column, values in allowed.items():
            if row[column] not in values:
                errors.append(f"{label}: {column} {row[column]!r} not in {values}")
    if not rows:
        errors.append("no rows")
    if errors:
        raise ValidationError("\n".join(errors))


def detect_type(content_type: str, body: bytes) -> str | None:
    ct = content_type.lower()
    if "pdf" in ct or body[:5] == b"%PDF-":
        return "pdf"
    if "html" in ct or body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    return None


def fetch(session: requests.Session, url: str, timeout: float, allowlist: list[str]) -> dict:
    """GET one URL, following redirects only while every hop passes the domain rule."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        resp = session.get(current, timeout=timeout, allow_redirects=False)
        if resp.is_redirect or resp.is_permanent_redirect:
            nxt = urljoin(current, resp.headers.get("Location", ""))
            if not is_allowed_host(nxt, allowlist):
                return {"final_url": nxt, "http_status": resp.status_code,
                        "error": f"redirect to non-allowed host: {nxt}"}
            current = nxt
            continue
        return {"final_url": current, "http_status": resp.status_code,
                "content_type": resp.headers.get("Content-Type", ""), "body": resp.content}
    return {"final_url": current, "http_status": None, "error": "too many redirects"}


SAVED_FROM = re.compile(rb"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->", re.I)


def _same_page(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return ((pa.hostname or "").lower() == (pb.hostname or "").lower()
            and pa.path.rstrip("/") == pb.path.rstrip("/"))


def check_manual_dir(manual_dir: Path, ids: set[str]) -> list[str]:
    """Return errors for files in data/manual/ that aren't <listed id>.html|pdf."""
    errors = []
    if not manual_dir.exists():
        return errors
    for p in manual_dir.iterdir():
        if p.name == ".gitkeep" or p.is_dir():  # browsers also save an <id>_files/ folder
            continue
        if p.suffix.lower() not in (".html", ".htm", ".pdf") or p.stem not in ids:
            errors.append(f"data/manual/{p.name}: not named <id>.html or <id>.pdf for an id in sources.csv")
    return errors


def load_manual(manual_dir: Path, row: dict) -> dict | None:
    """Return a saved copy of this row's page, or None if there isn't one.

    Returns {"error": ...} if a file exists but can't be trusted.
    """
    candidates = [manual_dir / f"{row['id']}{ext}" for ext in (".html", ".htm", ".pdf")]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return None
    body = path.read_bytes()
    label = f"data/manual/{path.name}"
    ext = detect_type("application/pdf" if path.suffix == ".pdf" else "text/html", body)
    if not body or ext is None:
        return {"error": f"{label}: empty or not HTML/PDF"}
    if ext == "html":
        if b"Access Denied" in body[:5000] and len(body) < 5000:
            return {"error": f"{label}: is an 'Access Denied' page, not the real page"}
        m = SAVED_FROM.search(body[:2000])
        if m and not _same_page(m.group(1).decode("ascii", "replace"), row["url"]):
            return {"error": f"{label}: saved from {m.group(1).decode()}, not {row['url']}"}
    saved_on = dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
    return {"body": body, "ext": ext, "saved_on": saved_on, "file": label}


def collect(sources_path: Path) -> int:
    config = load_config()
    raw_dir = PROJECT_ROOT / config["paths"]["raw_dir"]
    manual_dir = PROJECT_ROOT / config["paths"]["manual_dir"]
    opts = config["collect"]
    allowlist = config["source_allowlist"]

    try:
        rows = read_sources(sources_path)
        validate(rows, config)
        manual_errors = check_manual_dir(manual_dir, {r["id"] for r in rows})
        if manual_errors:
            raise ValidationError("\n".join(manual_errors))
    except ValidationError as e:
        print(f"Validation failed for {sources_path}. Nothing was downloaded.\n{e}", file=sys.stderr)
        return 2

    # Download into a staging folder; data/raw is only replaced at the end,
    # so an interrupted run never leaves a half-written data/raw.
    staging = raw_dir.with_name(raw_dir.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    session = requests.Session()
    session.headers["User-Agent"] = opts["user_agent"]
    today = dt.date.today().isoformat()
    manifest = []

    for i, row in enumerate(rows):
        if i:
            time.sleep(opts["delay_between_requests_s"])
        entry = {**{k: row[k] for k in ("id", "url", "publisher", "doc_type", "scheme")},
                 "method": "download", "final_url": None, "fetched_on": today, "http_status": None,
                 "content_type": None, "bytes": 0, "sha256": None, "file": None, "error": None}
        try:
            result = fetch(session, row["url"], opts["timeout_s"], allowlist)
        except requests.RequestException as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        entry["final_url"] = result.get("final_url")
        entry["http_status"] = result.get("http_status")
        entry["content_type"] = result.get("content_type")
        entry["error"] = result.get("error")

        body = result.get("body")
        if body is not None and entry["error"] is None:
            ext = detect_type(entry["content_type"] or "", body)
            if entry["http_status"] != 200:
                entry["error"] = f"HTTP {entry['http_status']}"
            elif not body:
                entry["error"] = "empty response"
            elif ext is None:
                entry["error"] = f"unexpected content type: {entry['content_type']}"
            else:
                name = f"{row['id']}.{ext}"
                (staging / name).write_bytes(body)
                entry.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), file=name)

        if not entry["file"]:
            manual = load_manual(manual_dir, row)
            if manual is None:
                entry["error"] += f"; no saved copy at data/manual/{row['id']}.html"
            elif "error" in manual:
                entry["error"] += f"; saved copy rejected: {manual['error']}"
            else:
                name = f"{row['id']}.{manual['ext']}"
                (staging / name).write_bytes(manual["body"])
                entry.update(method="manual", download_error=entry["error"], error=None,
                             final_url=row["url"], fetched_on=manual["saved_on"],
                             content_type=f"saved copy ({manual['file']})", bytes=len(manual["body"]),
                             sha256=hashlib.sha256(manual["body"]).hexdigest(), file=name)

        status = ("ok  " if entry["method"] == "download" else "SAVED") if entry["file"] else "FAIL"
        detail = f"{entry['bytes']:>9,} B  {entry['file']}" if entry["file"] else entry["error"]
        print(f"{status} {row['id']}  {entry['http_status']}  {detail}")
        manifest.append(entry)

    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    (staging / ".gitkeep").touch()
    shutil.rmtree(raw_dir, ignore_errors=True)
    staging.rename(raw_dir)

    ok = sum(1 for e in manifest if e["file"])
    print(f"\n{ok}/{len(manifest)} sources fetched -> {raw_dir.relative_to(PROJECT_ROOT)}/")
    if ok < MIN_SOURCES:
        print(f"Fewer than {MIN_SOURCES} sources fetched successfully.", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", type=Path, default=PROJECT_ROOT / config["paths"]["sources_csv"],
                        help="sources CSV to collect (default: sources.csv)")
    args = parser.parse_args()
    sys.exit(collect(args.sources))


if __name__ == "__main__":
    main()
