"""Vercel function: POST /api/ask with body {"question": str}.

Runs guardrails → retriever → generator → formatter (app/service.py).
Logs only {type, scheme, top_score, cited_source_id, latency_ms}; never the question.

Local:  uvicorn api.ask:app --port 8000
        DEV_STATIC=1 uvicorn api.ask:app --port 8000   (also serves public/ at /)
"""

import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.config import load_config  # noqa: E402
from app.service import ask as run_ask  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("hdfc_mf_faq")
for _noisy in ("httpx", "httpx2", "google_genai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_T = load_config()["templates"]
MAX_CHARS = _T["max_question_chars"]

app = FastAPI(title="HDFC MF FAQ", docs_url=None, redoc_url=None, openapi_url=None)


def _invalid() -> JSONResponse:
    body = {"type": "invalid", "text": _T["texts"]["invalid"], "source_url": None, "last_updated": None}
    return JSONResponse(body, status_code=400)


@app.exception_handler(RequestValidationError)
async def _bad_body(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default 422 body echoes the input; return a plain 400 instead.
    return _invalid()


@app.post("/api/ask")
async def ask(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON
        return _invalid()
    question = body.get("question") if isinstance(body, dict) else None
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_CHARS:
        return _invalid()

    try:
        resp, record = run_ask(question.strip())
    except Exception as exc:  # noqa: BLE001 - never crash, never leak details
        log.error(json.dumps({"event": "unhandled", "error": type(exc).__name__}))
        from app.formatter import error

        return JSONResponse(error(), status_code=200)
    log.info(json.dumps(record))
    return JSONResponse(resp, headers={"Cache-Control": "no-store"})


if os.environ.get("DEV_STATIC") == "1":
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=ROOT / "public", html=True), name="public")
