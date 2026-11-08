# HDFC MF FAQ (facts-only RAG assistant)

> **Facts-only. No investment advice.**

A small retrieval-augmented question-answering app for retail investors. It answers factual questions about four HDFC Mutual Fund schemes. Answers use only 16 official documents from HDFC MF, SEBI and AMFI. Every answer is at most 3 sentences, cites **one** official source link, and shows the date that source was last fetched.

It refuses investment advice and won't quote returns, NAV or fund size. It never accepts personal data. When a question about a fund-specific fact (expense ratio, exit load, minimum SIP, and so on) doesn't name a fund, it asks you to name one instead of guessing.

How it works: an offline pipeline (collect → load → chunk → embed → store → export) builds a read-only index snapshot in `data/index/`. At request time, `api/ask.py` runs guardrails → retriever (ONNX MiniLM embeddings and numpy cosine search) → Gemini (JSON answer citing one chunk) → formatter. The citation URL always comes from the cited chunk's metadata, never from text the model writes. For more detail, see [architecture.md](architecture.md), [PRD.md](PRD.md) and [implementation.md](implementation.md).

## Scope

| In scope | |
|---|---|
| AMC | HDFC Mutual Fund |
| Schemes | HDFC Large Cap Fund, HDFC Flexi Cap Fund, HDFC ELSS Tax Saver Fund, HDFC Mid Cap Fund (Direct Plan facts) |
| Questions | Expense ratio (TER), exit and entry load, minimum SIP, lock-in, riskometer, benchmark, statements (CAS, capital-gains), and general definitions from SEBI and AMFI material |

| Not answered | Response type |
|---|---|
| Advice or opinions ("should I buy…", "which is better…") | `refusal` with an AMFI investor-education link |
| Returns, CAGR, NAV, AUM, performance comparisons | `performance` with the official factsheet link and no figures |
| Other AMCs or other HDFC schemes (e.g. SBI Bluechip, HDFC Small Cap) | `out_of_scope` |
| Fund-specific fact with no fund named ("What is the expense ratio?") | `need_scheme`: "Please name the fund you're asking about…" |
| PAN, Aadhaar, email, phone, OTP, folio or account numbers | `pii_warning`. The question never reaches Gemini and is never logged. |
| Anything not found in the sources | `not_found` |

## Setup (macOS)

```bash
# 1. Python 3.12 (Homebrew)
brew install python@3.12

# 2. Virtual environment
cd hdfc-mf-faq-rag
python3.12 -m venv .venv
source .venv/bin/activate

# 3. Dependencies
pip install --upgrade pip
pip install -r requirements.txt            # runtime: API + retriever + Gemini client
pip install -r requirements-pipeline.txt   # only if you rebuild the corpus or run tests

# 4. Gemini API key
cp .env.example .env
# then open .env in an editor and set GEMINI_API_KEY=<your key from Google AI Studio>
```

`.env` is git-ignored, and the app never prints or logs the key. On a hosted deployment, set `GEMINI_API_KEY` as an environment variable instead.

## Rebuilding the corpus (offline pipeline)

The committed `data/index/` snapshot is all the app needs. Rebuild it only to refresh the sources.

```bash
python -m pipeline.run_all                 # collect → load → chunk → embed → store → export
python -m pipeline.run_all --from chunk    # rebuild from already-loaded documents (no network)
```

`www.hdfcfund.com` blocks automated downloads (HTTP 403), so 8 of its pages (s01–s04 and s10–s13) are used from copies saved in a browser. Before running `collect`, open each URL in Chrome and use **Save As → "Webpage, Complete"**. Name the file `<id>.html` (e.g. `s01.html`) and save it in `data/manual/`. A saved copy is used only when that row's download fails. Its `fetched_on` date is the file's save date.

## Running the app

```bash
# API + UI on one port (dev-only static mount)
DEV_STATIC=1 uvicorn api.ask:app --port 8000
# open http://localhost:8000

# API only
uvicorn api.ask:app --port 8000
curl -s localhost:8000/api/ask -H 'content-type: application/json' \
  -d '{"question":"What is the exit load of HDFC Flexi Cap Fund?"}'

# CLI, no HTTP layer
python scripts/ask.py "ELSS lock-in?"
```

Every response has the same shape: `{"type", "text", "source_url", "last_updated"}`.

## Tests and evaluation

```bash
pytest tests/ -v                                        # offline; Gemini is mocked
python eval/run_eval.py --base-url http://localhost:8000  # needs the server running and a valid key
```

`eval/run_eval.py` runs the 10 PRD acceptance tests and 22 edge cases in `eval/test_questions.yaml`. For each case it checks the response type, the link (exactly one URL from `sources.csv`, or none where expected), the 3-sentence cap, the last-updated date, and that the text has no return, NAV or AUM figures. It spaces requests that may reach Gemini at least 7 seconds apart (`--delay`). It writes `eval/results.md`, `eval/results.json` and [`eval/sample_qa.md`](eval/sample_qa.md).

## Deploy (Vercel)

The deployment is a static frontend (`public/`) plus one Python function (`api/ask.py`, served at `/api/ask`). Two files configure it:

- **`vercel.json`**: serves `public/` as static files, and bundles `data/index/**` (the vector snapshot and ONNX model) and `config.yaml` with the function. It also gives the function 1024 MB of memory and a 30-second time limit.
- **`.vercelignore`**: keeps everything else out of the upload, including the offline pipeline data (`data/raw`, `loaded`, `chunks`, `embeddings`, `chroma`, `manual`), `.venv`, `tests`, `eval`, `pipeline`, `scripts`, `requirements-pipeline.txt` and `.env`.

The function installs only `requirements.txt` (onnxruntime, tokenizers, numpy, google-genai, fastapi, pyyaml, python-dotenv), with no torch or chromadb. Measured locally with Linux x86_64 wheels, the dependencies plus `data/index` come to about 274 MB unzipped. Vercel's limit for Python bundles is 500 MB.

```bash
npm i -g vercel
vercel link                      # create or link the project
vercel env add GEMINI_API_KEY    # for Production and Preview; never commit the key
vercel                           # preview deployment
python eval/run_eval.py --base-url https://<preview-url>   # should match the local pass rate
vercel --prod                    # promote once the eval passes
```

Status: the config files are ready, but nothing has been deployed yet. There is no live URL or measured cold-start time yet.

## Known limits

- **Facts are only as current as the last pipeline run.** Every answer shows its source's fetch date. All sources here were fetched on 2026-11-08.
- **Large Cap TER: 1.01% vs 1.03%.** The HDFC Large Cap scheme page (s01, fetched 2026-11-08) gives a Direct Plan TER of **1.01%**. The June 2026 factsheet (s05) gives **1.03%** "as on June 30, 2026". Both are official, with different as-of dates. The retriever puts the scheme-page value first for TER questions, so the app answers 1.01% and cites s01.
- **s12 is a blog post.** The capital-gains statement guide (s12) is on HDFC MF's own site, `hdfcfund.com/learn/blog/…`. It is an official HDFC MF source, but it is a blog article, not a scheme document or regulatory FAQ. Answers about downloading a capital-gains statement cite it.
- **Sources were saved from a browser.** `hdfcfund.com` blocks automated downloads, so its 8 pages are copies saved by hand (see above). A refresh means saving them again.
- **Gemini free-tier rate limits.** The free tier allows only a few requests per minute (about 10/min in testing, then HTTP 429). Several users at once may get "The service is busy, please retry in a moment." The eval runner waits 7 seconds between requests that may reach Gemini.
- **Gemini free-tier data use.** On the free tier, Google may use prompts and responses to improve its products. Users must never enter personal data. The PII guard blocks common formats before anything is sent, but it is regex-based and can't catch everything.
- **Rule-based guardrails can miss unusual wording.** Gemini's `is_advice` flag is a second check for advice questions. Out-of-scope detection uses a fixed list of other AMCs and HDFC scheme names.
- **Retrieval threshold.** `retrieval.min_score` is 0.30. Very short in-scope queries ("elss lockin") score about 0.32–0.34, while off-topic probes scored at most 0.28, so the margin is small. Gemini's `answerable=false` is the backstop.
- **Non-English questions** usually return `not_found` (the embedding model is English-only). They are never answered from outside the sources.
- **PDF tables** (exit-load slabs, TER tables) may extract imperfectly. Performance tables and AUM figures are excluded from retrieval on purpose.

## Sources

All 16 sources, from [`sources.csv`](sources.csv) (publishers: HDFC MF, SEBI, AMFI only):

| id | Publisher | Scheme | Type | URL |
|---|---|---|---|---|
| s01 | HDFC MF | HDFC Large Cap | scheme_page | https://www.hdfcfund.com/explore/mutual-funds/hdfc-large-cap-fund/direct |
| s02 | HDFC MF | HDFC Flexi Cap | scheme_page | https://www.hdfcfund.com/explore/mutual-funds/hdfc-flexi-cap-fund/direct |
| s03 | HDFC MF | HDFC ELSS Tax Saver | scheme_page | https://www.hdfcfund.com/explore/mutual-funds/hdfc-elss-tax-saver-fund/direct |
| s04 | HDFC MF | HDFC Mid Cap | scheme_page | https://www.hdfcfund.com/explore/mutual-funds/hdfc-mid-cap-fund/direct |
| s05 | HDFC MF | general | factsheet | https://files.hdfcfund.com/s3fs-public/2026-07/HDFC%20MF%20Factsheet%20-%20June%202026.pdf |
| s06 | HDFC MF | HDFC Large Cap | kim | https://files.hdfcfund.com/s3fs-public/KIM/2025-11/KIM%20-%20HDFC%20Large%20Cap%20Fund%20dated%20November%2021%2C%202025_0.pdf?VersionId=XuOtUPPEfOmbQpuzxBMGCAdqsTmowMh4 |
| s07 | HDFC MF | HDFC Mid Cap | kim | https://files.hdfcfund.com/s3fs-public/KIM/2025-11/KIM%20-%20HDFC%20Mid%20Cap%20Fund%20dated%20November%2021,%202025_1.pdf |
| s08 | HDFC MF | HDFC ELSS Tax Saver | sid | https://files.hdfcfund.com/s3fs-public/SID/2024-11/SID%20-%20HDFC%20ELSS%20Tax%20Saver%20Fund%20dated%20November%2021,%202024.pdf |
| s09 | HDFC MF | HDFC Flexi Cap | sid | https://files.hdfcfund.com/s3fs-public/SID/2025-05/SID%20-%20HDFC%20Flexi%20Cap%20Fund%20dated%20May%2030,%202025.pdf |
| s10 | HDFC MF | general | learning_page | https://www.hdfcfund.com/learners-corner/elss |
| s11 | HDFC MF | general | statement_guide | https://www.hdfcfund.com/services/consolidated-account-statement |
| s12 | HDFC MF | general | statement_guide | https://www.hdfcfund.com/learn/blog/how-get-capital-gain-statement-mutual-fund-schemes-india |
| s13 | HDFC MF | general | statement_guide | https://www.hdfcfund.com/services/additional-info/request-statement |
| s14 | SEBI | general | investor_guide | https://investor.sebi.gov.in/riskometer.html |
| s15 | SEBI | general | regulator_faq | https://www.sebi.gov.in/sebi_data/faqfiles/sep-2024/1727242783639.pdf |
| s16 | AMFI | general | investor_guide | https://www.amfiindia.com/uploads/AMFI_Understanding_Mutual_Funds_IAP_Booklet_Final_Website_version_860a5b77aa.pdf |

## Sample Q&A

These are real responses from the eval run on 2026-11-08 (`eval/run_eval.py`, 32/32 passed). The full set is in [`eval/sample_qa.md`](eval/sample_qa.md).

**1. Expense ratio of HDFC Large Cap Fund?** (`answer`)
HDFC Large Cap Fund (Direct Plan) has a Total Expense Ratio (TER) of 1.01%.
Source: https://www.hdfcfund.com/explore/mutual-funds/hdfc-large-cap-fund/direct · Last updated from sources: 2026-11-08

**2. ELSS lock-in?** (`answer`)
HDFC ELSS Tax Saver Fund (Direct Plan) has a lock-in period of 3 years.
Source: https://www.hdfcfund.com/explore/mutual-funds/hdfc-elss-tax-saver-fund/direct · Last updated from sources: 2026-11-08

**3. Minimum SIP for HDFC Mid Cap?** (`answer`)
HDFC Mid Cap Fund (Direct Plan) has a minimum SIP of ₹100.
Source: https://www.hdfcfund.com/explore/mutual-funds/hdfc-mid-cap-fund/direct · Last updated from sources: 2026-11-08

**4. Exit load for HDFC Flexi Cap?** (`answer`)
An Exit Load of 1.00% is payable if Units are redeemed / switched-out within 1 year from the date of allotment. No Exit Load is payable if Units are redeemed / switched-out after 1 year from the date of allotment.
Source: https://www.hdfcfund.com/explore/mutual-funds/hdfc-flexi-cap-fund/direct · Last updated from sources: 2026-11-08

**5. Riskometer / benchmark of HDFC Mid Cap?** (`answer`)
The riskometer for HDFC Mid Cap Fund (Direct Plan) is Very High. The benchmark is NIFTY Midcap 150 Index (Total Returns Index).
Source: https://www.hdfcfund.com/explore/mutual-funds/hdfc-mid-cap-fund/direct · Last updated from sources: 2026-11-08

**6. How to download capital-gains statement?** (`answer`)
You can visit the websites of CAMS, KFintech, or other mutual fund investment platforms and use your PAN and registered email ID to download the statement. Alternatively, you can log in to investment platforms or AMC websites, or request a consolidated account statement in capital gains format via CAMS or NSDL.
Source: https://www.hdfcfund.com/learn/blog/how-get-capital-gain-statement-mutual-fund-schemes-india · Last updated from sources: 2026-11-08

**7. Should I buy HDFC Flexi Cap?** (`refusal`)
I can only share facts from official sources and can't give investment advice or opinions. For help understanding mutual funds, see this investor-education guide.
Source: https://www.amfiindia.com/uploads/AMFI_Understanding_Mutual_Funds_IAP_Booklet_Final_Website_version_860a5b77aa.pdf · Last updated from sources: 2026-11-08

**8. Which gave better returns, Large Cap or Mid Cap?** (`performance`)
I don't quote or compare NAV, returns or fund size. Please see the scheme's official factsheet for its performance data.
Source: https://files.hdfcfund.com/s3fs-public/2026-07/HDFC%20MF%20Factsheet%20-%20June%202026.pdf · Last updated from sources: 2026-11-08

## Disclaimer

```
Facts-only. No investment advice.
```

This prototype is for education only. It isn't affiliated with HDFC Mutual Fund, SEBI or AMFI. Always check the official scheme documents before you invest.
