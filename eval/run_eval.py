"""Phase 9 eval: runs eval/test_questions.yaml against a running API (local or deployed).

    python eval/run_eval.py --base-url http://localhost:8000 [--delay 7] [--only id1,id2]

Per case it checks:
  status     HTTP status (200, or 400 for invalid input)
  type       response type is one of `expect`
  link       exactly one source_url that is listed in sources.csv (on the host allow-list) for
             answer / refusal / performance / out_of_scope / not_found; none for
             pii_warning / need_scheme / invalid
  sentences  text has at most answer.max_sentences sentences
  date       last_updated is YYYY-MM-DD when there is a link (and equals the cited source's
             fetched_on for answers); null when there is no link
  figures    no NAV / returns / AUM figures (answer.blocked_patterns); template responses
             (refusal, performance, out_of_scope, not_found) contain no digits or "%" at all.
             Answers may contain "%" for facts such as TER or exit load.
  content    answer text matches `expect_text`, if given

Writes eval/results.md (pass/fail table), eval/results.json and eval/sample_qa.md.

Gemini free tier: requests that may reach Gemini (cases expecting answer/not_found) are
spaced at least --delay seconds apart. Rule-handled cases (PII, advice, performance,
scope, need-scheme, invalid) never call Gemini and are not delayed. A case that returns
"error" (e.g. HTTP 429 upstream) is retried once after --error-wait seconds; the retry is
recorded in the results.
"""

import argparse
import csv
from datetime import date
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app.config import load_config  # noqa: E402
from app.formatter import has_blocked_figures, split_sentences  # noqa: E402
from app.sources import is_allowed_host  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
LINKED = {"answer", "refusal", "performance", "out_of_scope", "not_found"}
UNLINKED = {"pii_warning", "need_scheme", "invalid", "error"}
TEMPLATES = {"refusal", "performance", "out_of_scope", "not_found"}
MAY_CALL_GEMINI = {"answer", "not_found"}
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_sources() -> tuple[set[str], dict[str, str]]:
    with (ROOT / "sources.csv").open(newline="", encoding="utf-8") as f:
        urls = {row["url"] for row in csv.DictReader(f)}
    fetched = {}
    manifest = ROOT / "data" / "raw" / "manifest.json"
    if manifest.exists():
        for m in json.loads(manifest.read_text(encoding="utf-8")):
            fetched[m["url"]] = m.get("fetched_on")
    return urls, fetched


def post(base_url: str, question: str, timeout: float = 60) -> tuple[int, dict | None]:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/ask",
        data=json.dumps({"question": question}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except json.JSONDecodeError:
            return e.code, None


def evaluate(case: dict, status: int, resp: dict | None, urls: set[str], fetched: dict, max_sentences: int) -> dict:
    expect = case["expect"] if isinstance(case["expect"], list) else [case["expect"]]
    checks: dict[str, bool] = {}
    resp = resp or {}
    kind = resp.get("type")
    text = resp.get("text") or ""
    url = resp.get("source_url")
    updated = resp.get("last_updated")

    checks["status"] = status == case.get("status", 200)
    checks["type"] = kind in expect
    if kind in LINKED:
        checks["link"] = isinstance(url, str) and url in urls and is_allowed_host(url)
        ok_date = isinstance(updated, str) and bool(DATE_RX.match(updated))
        if ok_date and kind == "answer" and url in fetched:
            ok_date = updated == fetched[url]
        checks["date"] = ok_date
    else:
        checks["link"] = url is None
        checks["date"] = updated is None
    checks["sentences"] = len(split_sentences(text)) <= max_sentences
    figures_ok = not has_blocked_figures(text)
    if kind in TEMPLATES:
        figures_ok = figures_ok and not re.search(r"[\d%]", text)
    checks["figures"] = figures_ok
    if case.get("expect_text") and kind == "answer":
        checks["content"] = bool(re.search(case["expect_text"], text, re.IGNORECASE))
    return {"checks": checks, "passed": all(checks.values())}


def md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def write_results(rows: list[dict], base_url: str) -> None:
    passed = sum(r["passed"] for r in rows)
    lines = [
        "# Eval results",
        "",
        f"Run on {date.today().isoformat()} against `{base_url}`: **{passed}/{len(rows)} passed**.",
        "",
        "| id | question | expected | got | status | link | date | sentences | figures | content | result |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        c = r["checks"]
        mark = lambda k: "—" if k not in c else ("✓" if c[k] else "✗")  # noqa: E731
        exp = r["expect"] if isinstance(r["expect"], str) else " / ".join(r["expect"])
        q = r["question"] if len(r["question"]) <= 70 else r["question"][:67] + "..."
        got = (r["response"] or {}).get("type")
        if r.get("retried"):
            got = f"{got} (retried)"
        lines.append(
            f"| {r['id']} | {md_escape(q)} | {exp} | {got} | {r['http_status']} | {mark('link')} | {mark('date')} "
            f"| {mark('sentences')} | {mark('figures')} | {mark('content')} | {'PASS' if r['passed'] else 'FAIL'} |"
        )
    (EVAL_DIR / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (EVAL_DIR / "results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_sample_qa(rows: list[dict], disclaimer: str) -> int:
    picked = [r for r in rows if r.get("sample") and r["passed"]]
    out = [
        "# Sample Q&A",
        "",
        f"Real responses from `eval/run_eval.py` ({date.today().isoformat()}). {disclaimer}",
        "",
    ]
    for i, r in enumerate(picked, 1):
        resp = r["response"]
        out += [f"## {i}. {r['question']}", "", f"**Type:** `{resp['type']}`", "", resp["text"], ""]
        if resp.get("source_url"):
            out += [f"Source: <{resp['source_url']}>", ""]
        if resp.get("last_updated"):
            out += [f"Last updated from sources: {resp['last_updated']}", ""]
    (EVAL_DIR / "sample_qa.md").write_text("\n".join(out), encoding="utf-8")
    return len(picked)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--delay", type=float, default=7.0, help="min seconds between requests that may reach Gemini")
    ap.add_argument("--error-wait", type=float, default=60.0, help="wait before retrying a case that returned 'error'")
    ap.add_argument("--only", default="", help="comma-separated case ids")
    args = ap.parse_args()

    cfg = load_config()
    max_sentences = cfg["answer"]["max_sentences"]
    cases = yaml.safe_load((EVAL_DIR / "test_questions.yaml").read_text(encoding="utf-8"))["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    urls, fetched = load_sources()

    rows = []
    last_gemini = 0.0
    for case in cases:
        question = case["question"] * case.get("question_repeat", 1)
        expect = case["expect"] if isinstance(case["expect"], list) else [case["expect"]]
        may_call = bool(MAY_CALL_GEMINI & set(expect))

        def run() -> tuple[int, dict | None]:
            nonlocal last_gemini
            if may_call:
                wait = args.delay - (time.monotonic() - last_gemini)
                if wait > 0:
                    time.sleep(wait)
            result = post(args.base_url, question)
            if may_call:
                last_gemini = time.monotonic()
            return result

        status, resp = run()
        retried = False
        if (resp or {}).get("type") == "error":
            print(f"  {case['id']}: got 'error', retrying in {args.error_wait:.0f}s", flush=True)
            time.sleep(args.error_wait)
            status, resp = run()
            retried = True

        result = evaluate(case, status, resp, urls, fetched, max_sentences)
        row = {"id": case["id"], "question": question if len(question) <= 200 else f"<{len(question)} chars>",
               "expect": case["expect"], "sample": case.get("sample", False), "http_status": status,
               "response": resp, "retried": retried, **result}
        rows.append(row)
        flag = "PASS" if result["passed"] else "FAIL"
        failed = [k for k, v in result["checks"].items() if not v]
        print(f"{flag}  {case['id']:<22} {(resp or {}).get('type')!s:<13} {'' if not failed else 'failed: ' + ','.join(failed)}", flush=True)

    write_results(rows, args.base_url)
    n = write_sample_qa(rows, cfg["answer"]["disclaimer"]) if not args.only else 0
    passed = sum(r["passed"] for r in rows)
    print(f"\n{passed}/{len(rows)} passed. Wrote eval/results.md" + (f" and eval/sample_qa.md ({n} Q&As)" if n else ""))
    sys.exit(0 if passed == len(rows) else 1)


if __name__ == "__main__":
    main()
