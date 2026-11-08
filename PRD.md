# PRD — Mutual Fund FAQ Assistant (Facts-Only Q&A)

| | |
|---|---|
| **Product context** | Groww (used across all milestones) |
| **AMC in scope** | HDFC Mutual Fund |
| **Schemes in scope** | HDFC Large Cap Fund, HDFC Flexi Cap Fund, HDFC ELSS Tax Saver, HDFC Mid Cap Fund |
| **Type** | Working prototype — Retrieval-Augmented Generation (RAG) over a small, official corpus |
| **Status** | Draft v1 |

---

## 1. Problem

Retail investors comparing mutual fund schemes repeatedly ask the same factual questions — expense ratio, exit load, minimum SIP, ELSS lock-in, riskometer, benchmark, how to download statements. Answers are scattered across factsheets, KIM/SID documents and AMC/regulator pages. Support and content teams spend time answering these repetitive questions, and generic chatbots risk giving unsourced answers or drifting into investment advice.

## 2. Goal

Build a small FAQ assistant that answers **factual** questions about the four in-scope HDFC schemes using **only official public pages** (HDFC MF, AMFI, SEBI). Every answer cites **exactly one source link**. The assistant **never gives advice**.

### Success criteria
- Every factual answer is grounded in the corpus and carries one valid citation URL.
- Every opinion/portfolio question is refused politely with an educational link.
- No PII is accepted or stored.
- No return computations or performance comparisons.
- Answers are ≤ 3 sentences and include "Last updated from sources: <date>".

## 3. Target users

| User | Need |
|---|---|
| Retail investors comparing schemes | Quick, trustworthy facts with a link to verify |
| Support / content teams | Consistent, sourced answers to repetitive MF questions |

## 4. Scope

### In scope
- **One AMC:** HDFC Mutual Fund.
- **Four schemes:** HDFC Large Cap, HDFC Flexi Cap, HDFC ELSS Tax Saver, HDFC Mid Cap.
- **Corpus:** 15–25 public pages from HDFC MF, AMFI and SEBI — factsheets, KIM/SID, scheme FAQs, fee/charges pages, riskometer/benchmark notes, statement/tax-document guides.
- **Question types:** expense ratio, exit load, minimum SIP / lump sum, ELSS lock-in, riskometer, benchmark, fund manager / scheme category facts, how to download account/capital-gains statements.

### Out of scope
- Investment advice, recommendations, buy/sell/hold opinions, portfolio construction.
- Computing, comparing or projecting returns (link to the official factsheet instead).
- Any scheme or AMC outside the scope above.
- Third-party blogs, aggregators, or app back-end screenshots as sources.
- User accounts, transactions, or any personal data handling.

## 5. Functional requirements

### FR1 — Factual answering
- Identify the exact fact asked and the scheme it refers to.
- Retrieve relevant chunks from the vector store and answer **only** from retrieved content.
- Answer in **≤ 3 sentences**, plain language.
- If the corpus does not contain the answer, say so and point to the most relevant official page — do not guess.

### FR2 — Citation
- Every answer shows **exactly one** clear source link (the URL of the chunk the answer is drawn from).
- Every answer ends with: `Last updated from sources: <date>` (date the corpus was fetched).

### FR3 — Refusal of advice / opinion
- Detect opinionated or portfolio questions (e.g., "Should I buy/sell?", "Which fund is better?", "Will this give 15% returns?").
- Respond with a polite, facts-only refusal plus one relevant educational link (e.g., AMFI investor education or SEBI investor page).

### FR4 — Performance questions
- Do not compute, compare, or state returns. Provide the link to the official scheme factsheet.

### FR5 — PII guard
- Do not accept or store PAN, Aadhaar, account/folio numbers, OTPs, emails, or phone numbers.
- Detect such patterns in input; respond with a warning not to share personal data, and do not log the raw input.

### FR6 — UI (minimal)
- Welcome line.
- Three example questions (clickable), e.g.:
  1. "What is the expense ratio of HDFC Flexi Cap Fund?"
  2. "What is the lock-in period for HDFC ELSS Tax Saver?"
  3. "How do I download my capital-gains statement?"
- Persistent note: **"Facts-only. No investment advice."**

## 6. Answer & refusal format

**Factual answer**
```
<Answer in ≤3 sentences.>
Source: <one official URL>
Last updated from sources: <YYYY-MM-DD>
```

**Refusal**
```
I can only share factual information about mutual fund schemes and can't offer investment advice.
For guidance on choosing funds, see: <AMFI/SEBI educational URL>
```

## 7. Technical design

### Pipeline (build order — each stage persists its output)
| Stage | Description | Saved artifact |
|---|---|---|
| 1. Collect | Download only the 15–25 official URLs listed in `sources.csv` (HTML/PDF), with no link following; record URL, fetch date and HTTP status | `data/raw/` (original files + `manifest.json`) |
| 2. Load | Extract clean text from the downloaded files (no network access), keeping each source's metadata | `data/loaded/` (`<id>.txt` + `documents.jsonl`) |
| 3. Chunk | Split text into chunks with metadata (source URL, scheme, doc type, fetch date) | `data/chunks/` (JSON/JSONL) |
| 4. Embed | Embed chunks with `sentence-transformers/all-MiniLM-L6-v2` (free, local) | `data/embeddings/` |
| 5. Store | Persist vectors + metadata in ChromaDB | `data/chroma/` |
| 6. Retrieve & answer | Query classification (fact / advice / PII) → top-k retrieval → LLM answers from context with one citation | Logs of Q&A (PII-free) |

### Configuration
- `config` file (e.g., `config.yaml`) holds: embedding model name, LLM model name, chunk size, chunk overlap, top-k, collection name, corpus fetch date.
- `.env` holds `GEMINI_API_KEY`. `.env` is git-ignored; `.env.example` is provided with the variable name only (no real value).

### Prompting principles
- System prompt: answer only from provided context; ≤ 3 sentences; cite the single most relevant source URL; refuse advice; never compute returns.
- Refusal and PII handling run before retrieval where possible.

## 8. Constraints

- **Public sources only:** HDFC MF, AMFI, SEBI. No blogs, no app back-end screenshots.
- **No PII** accepted or stored.
- **No performance claims.**
- **Clarity & transparency:** ≤ 3 sentences + "Last updated from sources".
- **Free embedding model** (all-MiniLM-L6-v2); model and chunking choices configurable.

## 9. Deliverables

1. Working prototype link (app/notebook), or a ≤ 3-min demo video if hosting isn't possible.
2. Source list (CSV/MD) of the 15–25 URLs used.
3. README — setup steps, scope (AMC + schemes), known limits.
4. Sample Q&A file — 5–10 queries with the assistant's answers and links.
5. Disclaimer snippet used in the UI (facts-only, no advice).

## 10. Evaluation / acceptance tests

| # | Test | Expected |
|---|---|---|
| 1 | "Expense ratio of HDFC Large Cap Fund?" | Fact from corpus, 1 link, ≤ 3 sentences, last-updated line |
| 2 | "ELSS lock-in?" | 3-year lock-in stated, 1 official link |
| 3 | "Minimum SIP for HDFC Mid Cap?" | Amount from KIM/SID or scheme page, 1 link |
| 4 | "Exit load for HDFC Flexi Cap?" | Exit-load terms, 1 link |
| 5 | "Riskometer / benchmark of HDFC Mid Cap?" | Risk level and benchmark index, 1 link |
| 6 | "How to download capital-gains statement?" | Steps from official guide, 1 link |
| 7 | "Should I buy HDFC Flexi Cap?" | Polite refusal + educational link |
| 8 | "Which gave better returns, Large Cap or Mid Cap?" | No comparison; link to factsheet |
| 9 | "My PAN is ABCDE1234F, check my folio" | PII warning; input not stored |
| 10 | Question about an out-of-scope fund | States it's outside scope; no fabricated answer |

## 11. Skills demonstrated

- **W1 — Thinking Like a Model:** identify the exact fact asked; decide answer vs. refuse.
- **W2 — LLMs & Prompting:** instruction style, concise phrasing, polite safe refusals, citation wording.
- **W3 — RAG:** small-corpus retrieval with accurate citations from HDFC/AMFI/SEBI pages.

## 12. Risks & known limits

| Risk | Mitigation |
|---|---|
| Facts (e.g., expense ratio) change monthly | Show "Last updated from sources" date; re-run load stage to refresh |
| PDF extraction loses table structure | Inspect saved chunks; tune chunk size/overlap in config |
| Wrong scheme retrieved for similar names | Store scheme name in chunk metadata; filter by detected scheme |
| LLM adds unsupported detail | Strict context-only prompt; refuse when context is insufficient |
| Advice phrased subtly ("is it good for me?") | Classifier + prompt rules with refusal examples |

## 13. Decisions (formerly open questions)

| Question | Decision |
|---|---|
| Which LLM generates answers? | **Google Gemini (free tier).** Model name set in `config.yaml`; API key stored as `GEMINI_API_KEY` in `.env` (git-ignored). `.env.example` lists the variable with no real value. |
| Hosting target for the prototype? | **Web app deployed on Vercel** (static UI + Python serverless function). |
| Final list of 15–25 source URLs? | **To be added later** in the placeholder file [`sources.csv`](sources.csv) (HDFC MF, AMFI, SEBI only). |

See [architecture.md](architecture.md) for how these decisions are implemented.
