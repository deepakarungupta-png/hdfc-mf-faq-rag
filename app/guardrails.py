"""Component 7 — Guardrails (architecture §5).

check(question) -> {"type": ..., "scheme": str | None}

Checks run in a fixed order and the first match wins:
    PII -> advice -> performance -> out of scope -> need scheme -> factual

- pii_warning:  PAN, Aadhaar, email, Indian mobile, OTP, folio/account numbers.
                The question never reaches Gemini and is never logged.
- refusal:      advice or opinion (should I, better, best, recommend, buy/sell/switch, ...).
                One exception: a "better/best" comparison of returns ("which gave better
                returns?") is a performance question (PRD §10 test 8 expects the factsheet).
- performance:  returns, CAGR, NAV, performance, AUM -> factsheet link, no figures.
- out_of_scope: another AMC, or an HDFC scheme other than the four in scope.
- need_scheme:  asks for a fund-specific fact (TER, exit load, minimum SIP, ...) but names
                no in-scope fund, or several. We ask the user to name one instead of guessing.
                Definitions ("what is an expense ratio?") are still answered.
- factual:      everything else, with the detected scheme (or None).
"""

import re

from app.schemes import detect_schemes

_I = re.IGNORECASE


def _rx(*patterns: str) -> list[re.Pattern]:
    return [re.compile(p, _I) for p in patterns]


def _any(patterns: list[re.Pattern], text: str) -> bool:
    return any(p.search(text) for p in patterns)


# ---------------- PII ----------------
PII = _rx(
    r"\b[a-z]{5}\s?\d{4}\s?[a-z]\b",                                  # PAN (any case)
    r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)",                       # Aadhaar, 12 digits
    r"[\w.+-]+@[\w-]+(\.[\w-]+)+",                                     # email
    r"(?<!\d)(?:\+?\s?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)",        # Indian mobile
    r"\b(otp|one[\s-]time[\s-]password|pin)\b\D{0,20}\d{4,8}(?!\d)",   # OTP 482913
    r"(?<!\d)\d{4,8}\D{0,20}\b(otp|one[\s-]time[\s-]password)\b",      # 482913 is my OTP
    r"\b(folio|account|a/c|acct|ac)\b\.?\s*(no|number|num|#)?\.?\D{0,15}\d{5,}",  # folio no 12345678
)

# ---------------- advice / opinion ----------------
ADVICE = _rx(
    r"\b(should|shall)\s+(i|we)\b(?!\s+(download|get|find|check|see|view|request|know))",
    r"\bis it (wise|safe|smart|right|a good)\b",
    r"\b(better|best|worst|top[\s-]?rated)\b",
    r"\b(recommend\w*|suggest\w*|advi[cs]e\w*|advisable)\b",
    r"\bgood (for me|fund|investment|choice|option|time)\b",
    r"\bwhich (fund|scheme|one)\b",
    r"\bworth (it|investing|buying)\b",
    r"\b(ignore|forget|disregard)\b.{0,30}\b(rules?|instructions?|prompt)\b",
    r"\b(will|would)\b.{0,40}\b(go up|go down|rise|fall|grow|beat)\b",
    r"\b(predict|forecast|outlook|target price)\b",
)
# buy / sell / switch / invest are advice, unless the question is procedural
# ("how do I switch?", "exit load if I sell within a year").
_TRADE = re.compile(r"\b(buy|sell|switch|invest|redeem)\w*\b", _I)
_PROCEDURAL = _rx(
    r"\bhow (do|can|to|does|should)\b",
    r"\b(exit load|entry load|charges?|fees?|process|procedure|steps?|tax(ed|ation)?|lock[\s-]?in|minimum)\b",
    r"\bwhat (is|are|happens)\b",
)

# ---------------- performance ----------------
_BENCHMARK_NAME = re.compile(r"total returns? index|\btri\b", _I)
PERFORMANCE = _rx(
    r"\breturns?\b",
    r"\bCAGR\b",
    r"\b(current|latest|today'?s|historical|daily)\s+nav\b",
    r"\bnav\b.{0,20}\b(of|for|today|now|growth|history|on|chart|trend)\b",
    r"\bperform\w*\b",
    r"\d+(\.\d+)?\s*%\s*(return|gain|growth)",
    r"\b(fund size|aum|assets under management|corpus size)\b",
    r"\bhow much (did|has|does)\b.{0,40}\b(grow|gain|earn|make|return)\b",
    r"\b(xirr|alpha|sharpe|track record|historical (nav|data))\b",
)

# ---------------- out of scope ----------------
OTHER_AMCS = (
    "sbi", "axis", "icici", "kotak", "nippon", "reliance mutual", "aditya birla", "absl", "birla sun life",
    "uti", "dsp", "tata", "mirae", "parag parikh", "ppfas", "franklin", "templeton", "motilal", "quant",
    "edelweiss", "invesco", "bandhan", "idfc", "pgim", "sundaram", "lic mf", "lic mutual", "hsbc",
    "canara robeco", "baroda bnp", "bnp paribas", "groww", "navi", "zerodha", "whiteoak", "white oak",
    "jm financial", "mahindra manulife", "union mutual", "bank of india mutual", "shriram", "quantum",
    "iti mutual", "samco", "helios", "old bridge", "360 one", "trust mf", "taurus", "l&t mutual",
)
OTHER_SCHEMES = (
    "small cap", "smallcap", "large and mid cap", "large & mid cap", "large mid cap", "multi cap", "multicap",
    "balanced advantage", "hybrid fund", "hybrid equity", "hybrid debt", "liquid fund", "overnight fund", "gilt", "index fund", "etf",
    "focused fund", "focused 30", "value fund", "dividend yield", "arbitrage", "infrastructure fund", "banking and financial",
    "banking & financial", "pharma", "healthcare", "technology fund", "retirement savings", "retirement fund", "children's gift", "childrens gift", "children's fund", "children fund",
    "gold fund", "gold etf", "silver etf", "silver fund", "corporate bond", "money market", "short term debt", "credit risk",
    "multi asset", "equity savings", "business cycle", "manufacturing fund", "defence fund", "transportation",
    "housing opportunities", "mnc fund", "non cyclical", "dynamic debt", "ultra short", "low duration",
    "nifty 50 index", "sensex index", "nifty next 50", "capital builder", "growth opportunities",
)


def _phrase_rx(phrases: tuple[str, ...]) -> re.Pattern:
    alts = "|".join(re.escape(p).replace(r"\ ", r"[\s-]+") for p in phrases)
    return re.compile(rf"(?<![a-z0-9])({alts})(?![a-z0-9])", _I)


_OTHER_AMC_RX = _phrase_rx(OTHER_AMCS)
_OTHER_SCHEME_RX = _phrase_rx(OTHER_SCHEMES)

# ---------------- need scheme ----------------
# Facts that differ by fund, so a question about them must name one fund.
FUND_FACTS = _rx(
    r"\b(expense ratio|ter|total expense ratio)\b",
    r"\bexit load\b",
    r"\bentry load\b",
    r"\bmin(imum)?\.?\s+(sip|investment|amount|lumpsum|purchase)\b",
    r"\bsip (amount|minimum)\b",
    r"\block[\s-]?in\b",
    r"\briskometer\b|\brisk (level|category|grade)\b",
    r"\bbenchmark\b",
    r"\bfund manager\b",
)
# Definitions are general: "what is an expense ratio?", "what does exit load mean?"
DEFINITION = _rx(
    r"\bwhat (is|are)\s+(an?\s+)?(expense ratio|ter|total expense ratio|exit load|entry load|lock[\s-]?in( period)?|riskometer|benchmark|sip|fund manager)s?\b\s*\??\s*$",
    r"\b(meaning of|define|definition of|explain)\b",
    r"\bwhat does\b.{0,40}\bmean\b",
    r"\bhow (is|are|does)\b.{0,40}\b(calculated|charged|work|works|decided|set)\b",
    r"\bin general\b|\bgenerally\b|\bmutual funds\b",
)


def _is_advice(q: str) -> bool:
    if _any(ADVICE, q):
        return True
    return bool(_TRADE.search(q)) and not _any(_PROCEDURAL, q)


def _is_performance(q: str) -> bool:
    return _any(PERFORMANCE, _BENCHMARK_NAME.sub(" ", q))


def _comparative_returns_only(q: str) -> bool:
    """'Which gave better returns?': the only advice cue is better/best/which, about returns."""
    cleaned = re.sub(r"\b(better|best|worst|which (fund|scheme|one))\b", " ", q, flags=_I)
    return _is_performance(q) and not _is_advice(cleaned)


def is_pii(q: str) -> bool:
    return _any(PII, q)


def is_out_of_scope(q: str) -> bool:
    return bool(_OTHER_AMC_RX.search(q) or _OTHER_SCHEME_RX.search(q))


def needs_scheme(q: str, schemes: list[str]) -> bool:
    if len(schemes) == 1 or not _any(FUND_FACTS, q):
        return False
    if len(schemes) > 1:
        return True
    return not _any(DEFINITION, q)


def check(question: str) -> dict:
    q = question.strip()
    schemes = detect_schemes(q)
    scheme = schemes[0] if len(schemes) == 1 else None

    if is_pii(q):
        return {"type": "pii_warning", "scheme": None}
    if _is_advice(q):
        if _comparative_returns_only(q):
            return {"type": "performance", "scheme": scheme}
        return {"type": "refusal", "scheme": scheme}
    if _is_performance(q):
        return {"type": "performance", "scheme": scheme}
    if is_out_of_scope(q):
        return {"type": "out_of_scope", "scheme": None}
    if needs_scheme(q, schemes):
        return {"type": "need_scheme", "scheme": None}
    return {"type": "factual", "scheme": scheme}
