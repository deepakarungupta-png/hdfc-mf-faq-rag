// Tiny client for POST /api/ask. All server text is rendered with textContent.
// No localStorage, no cookies, no analytics.
(() => {
  "use strict";

  const MAX = 500;
  const TIMEOUT_MS = 30000;
  const form = document.getElementById("ask-form");
  const input = document.getElementById("question");
  const submit = document.getElementById("submit");
  const count = document.getElementById("count");
  const examples = Array.from(document.querySelectorAll(".example"));
  const card = document.getElementById("answer");
  const label = document.getElementById("answer-label");
  const text = document.getElementById("answer-text");
  const sourceRow = document.getElementById("answer-source");
  const link = document.getElementById("answer-link");
  const updated = document.getElementById("answer-updated");

  // type -> [card label, style class]
  const STYLES = {
    answer: ["Answer", ""],
    not_found: ["Not found in sources", "is-refusal"],
    refusal: ["Facts only", "is-refusal"],
    performance: ["Facts only", "is-refusal"],
    out_of_scope: ["Out of scope", "is-refusal"],
    pii_warning: ["Personal information", "is-pii"],
    need_scheme: ["Which fund?", "is-info"],
    invalid: ["Check your question", "is-error"],
    error: ["Something went wrong", "is-error"],
  };

  const ALLOWED_HOSTS = ["hdfcfund.com", "sebi.gov.in", "amfiindia.com"];
  function safeUrl(raw) {
    try {
      const u = new URL(raw);
      const host = u.hostname.toLowerCase();
      const ok = u.protocol === "https:" && ALLOWED_HOSTS.some((d) => host === d || host.endsWith("." + d));
      return ok ? u.href : null;
    } catch {
      return null;
    }
  }

  function updateCount() {
    count.textContent = `${input.value.length} / ${MAX}`;
  }

  function setBusy(busy) {
    submit.disabled = busy;
    examples.forEach((b) => { b.disabled = busy; });
    submit.textContent = busy ? "Asking…" : "Ask";
    form.setAttribute("aria-busy", String(busy));
  }

  function show(data) {
    const type = data && typeof data.type === "string" && STYLES[data.type] ? data.type : "error";
    const [title, cls] = STYLES[type];
    card.className = "card" + (cls ? " " + cls : "");
    label.textContent = title;
    text.textContent = (data && typeof data.text === "string" && data.text) ||
      "The service is busy, please retry in a moment.";

    const url = type === "pii_warning" ? null : safeUrl(data && data.source_url);
    if (url) {
      link.href = url;
      link.textContent = new URL(url).hostname + " — open source";
      link.title = url;
      sourceRow.hidden = false;
    } else {
      link.removeAttribute("href");
      link.textContent = "";
      sourceRow.hidden = true;
    }

    const date = data && typeof data.last_updated === "string" && /^\d{4}-\d{2}-\d{2}$/.test(data.last_updated)
      ? data.last_updated : null;
    updated.textContent = date ? `Last updated from sources: ${date}` : "";
    updated.hidden = !date || type === "pii_warning";
    card.hidden = false;
  }

  async function ask(question) {
    setBusy(true);
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question }),
        signal: ctrl.signal,
        credentials: "omit",
        cache: "no-store",
      });
      let data = null;
      try { data = await res.json(); } catch { /* non-JSON body */ }
      if (!data) data = { type: "error", text: "The service is unavailable right now. Please try again shortly." };
      show(data);
    } catch {
      show({ type: "error", text: "Couldn't reach the service. Check your connection and try again." });
    } finally {
      clearTimeout(timer);
      setBusy(false);
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q || q.length > MAX) {
      show({ type: "invalid", text: "Please enter a question of 1 to 500 characters." });
      return;
    }
    if (!submit.disabled) ask(q);
  });

  input.addEventListener("input", updateCount);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  examples.forEach((btn) => {
    btn.addEventListener("click", () => {
      input.value = btn.textContent;
      updateCount();
      form.requestSubmit();
    });
  });

  updateCount();
})();
