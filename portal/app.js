/* NVB Digital Services portal (synthetic demo).
 *
 * Plain JavaScript, no build step and no third-party code: the files in portal/ are served as-is
 * by the gateway. Everything is same-origin:
 *   /api/auth/{login,logout,me}  -> core (session cookie nvb_session is HttpOnly; never read here)
 *   /x/statements/v1/...         -> statements-api
 *   /x/loans/v1/...              -> loans-api
 * The CSRF token comes from the core's login / "me" response, is kept in memory only and is sent
 * as X-CSRF-Token on every state-changing request. All text is inserted with textContent
 * (never innerHTML), so server data cannot inject markup. Money is integer minor units.
 */
"use strict";

const S = "/x/statements/v1";
const L = "/x/loans/v1";
const state = { user: null, csrf: null, subject: null, roles: { statements: [], loans: [] }, timer: null };

// ---------------------------------------------------------------- helpers
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "value") node.value = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function newKey() {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

async function api(method, path, body, headers) {
  const opts = { method, credentials: "same-origin", headers: { Accept: "application/json", ...(headers || {}) } };
  if (method !== "GET") {
    if (state.csrf) opts.headers["X-CSRF-Token"] = state.csrf;
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  }
  let resp;
  try { resp = await fetch(path, opts); } catch (e) { throw new ApiError(0, "network", "Network error. Please retry."); }
  let data = null;
  const type = resp.headers.get("content-type") || "";
  if (type.includes("application/json")) { try { data = await resp.json(); } catch (e) { data = null; } }
  if (!resp.ok) {
    const err = data && data.error ? data.error : null;
    const detail = data && data.detail;
    const message = err ? err.message : (typeof detail === "string" ? detail
      : (detail && detail.message) || (resp.status === 422 ? "Please check the values you entered." : `Request failed (${resp.status}).`));
    if (resp.status === 401 && state.user && path !== "/api/auth/login") { signedOut("Your session has ended. Please sign in again."); }
    throw new ApiError(resp.status, err ? err.code : String(resp.status), message);
  }
  return data;
}

function toast(message) {
  const t = document.getElementById("toast");
  t.textContent = message; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, 5000);
}

// Money: decimal STRING -> integer minor units. No floating point anywhere.
function parseMoney(text) {
  const m = /^\s*(\d{1,12})(?:[.,](\d{1,2}))?\s*$/.exec(text || "");
  if (!m) return null;
  return parseInt(m[1], 10) * 100 + parseInt(((m[2] || "") + "00").slice(0, 2), 10);
}
function fmtMoney(minor, currency) {
  if (!Number.isInteger(minor)) return "-";
  const neg = minor < 0, v = Math.abs(minor);
  const whole = Math.trunc(v / 100).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${neg ? "-" : ""}${whole}.${String(v % 100).padStart(2, "0")}${currency ? " " + currency : ""}`;
}
function fmtTime(iso) { return iso ? new Date(iso).toLocaleString() : "-"; }

function pill(status) {
  const kind = { completed: "ok", approved: "ok", running: "info", queued: "info", submitted: "info", under_review: "info",
    draft: "warn", freezing: "warn", releasing: "warn", completed_with_failures: "warn",
    failed: "bad", rejected: "bad", cancelled: "bad" }[status] || "info";
  return el("span", { class: `pill ${kind}` }, String(status).replace(/_/g, " "));
}

function card(title, ...children) { return el("section", { class: "card" }, el("h2", null, title), ...children); }
function table(headers, rows, empty) {
  if (!rows.length) return el("p", { class: "muted" }, empty || "Nothing to show yet.");
  return el("div", { class: "table-wrap" }, el("table", null,
    el("thead", null, el("tr", null, headers.map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", null, rows)));
}
function field(label, input) { return el("label", null, label, input); }
function errorBox() { return el("p", { class: "error", role: "alert", hidden: true }); }
function showError(box, e) { box.textContent = e.message || String(e); box.hidden = false; }

async function guarded(button, box, fn) {
  if (box) box.hidden = true;
  if (button) button.disabled = true;
  try { await fn(); } catch (e) { if (box) showError(box, e); else toast(e.message); } finally { if (button) button.disabled = false; }
}

function hasRole(service, ...names) { return names.some((n) => state.roles[service].includes(n)); }
function isStaff() { return state.roles.statements.length > 0 || state.roles.loans.length > 0; }

// ---------------------------------------------------------------- session
async function loadSession() {
  try {
    const me = await api("GET", "/api/auth/me");
    state.user = me.user; state.csrf = me.csrf_token;
  } catch (e) { state.user = null; state.csrf = null; return; }
  const [s, l] = await Promise.allSettled([api("GET", `${S}/me`), api("GET", `${L}/me`)]);
  state.roles.statements = s.status === "fulfilled" ? s.value.extension_roles : [];
  state.roles.loans = l.status === "fulfilled" ? l.value.extension_roles : [];
  state.subject = s.status === "fulfilled" ? s.value.subject : (l.status === "fulfilled" ? l.value.subject : null);
}

function signedOut(message) {
  state.user = null; state.csrf = null; state.subject = null; state.roles = { statements: [], loans: [] };
  location.hash = "#/login";
  render();
  if (message) toast(message);
}

async function logout() {
  try { await api("POST", "/api/auth/logout"); } catch (e) { /* the session may already be gone */ }
  signedOut("Signed out.");
}

// ---------------------------------------------------------------- views
function viewLogin() {
  const user = el("input", { name: "username", autocomplete: "username", required: true, maxlength: 64 });
  const pass = el("input", { name: "password", type: "password", autocomplete: "current-password", required: true, maxlength: 256 });
  const box = errorBox();
  const btn = el("button", { type: "submit" }, "Sign in");
  const form = el("form", { onsubmit: (ev) => {
    ev.preventDefault();
    guarded(btn, box, async () => {
      const r = await api("POST", "/api/auth/login", { username: user.value, password: pass.value });
      state.user = r.user; state.csrf = r.csrf_token;
      pass.value = "";
      await loadSession();
      location.hash = state.user && state.user.role === "customer" ? "#/statements" : "#/identity";
      render();
    });
  } }, field("Username", user), field("Password", pass), box, el("div", { class: "actions" }, btn));
  return card("Sign in", el("p", { class: "muted" },
    "Use your existing NVB (core) username and password. Sign-in is handled by the core banking application; this portal never stores your password."), form);
}

function viewIdentity() {
  const kv = el("dl", { class: "kv" },
    el("dt", null, "Username"), el("dd", null, state.user.username),
    el("dt", null, "Core role"), el("dd", null, state.user.role),
    el("dt", null, "Stable subject"), el("dd", { class: "mono", id: "subject" }, state.subject || "-"),
    el("dt", null, "Statements roles"), el("dd", null, state.roles.statements.join(", ") || "none"),
    el("dt", null, "Loans roles"), el("dd", null, state.roles.loans.join(", ") || "none"));
  return card("My identity", kv, el("p", { class: "notice" },
    "Staff capabilities are explicit extension role assignments bound to the stable subject above. A core role (customer / admin / demo) grants none of them. Give this subject to an extension administrator to be assigned a role."));
}

// ---- customer: statements
async function viewStatements() {
  const root = card("Account statements (PDF)");
  const accounts = (await api("GET", `${S}/accounts`)).items;
  const acct = el("select", { name: "account" }, accounts.map((a) =>
    el("option", { value: a.account_id }, `${a.masked_number} — ${a.nickname} (${a.currency})`)));
  const now = new Date();
  const months = [];
  for (let i = 1; i <= 12; i++) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - i, 1));
    months.push(`${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`);
  }
  const period = el("select", { name: "period" }, months.map((m) => el("option", { value: m }, m)));
  const box = errorBox();
  const list = el("div", { id: "statement-list" });
  const btn = el("button", { type: "submit" }, "Request statement");
  const form = el("form", { class: "row", onsubmit: (ev) => {
    ev.preventDefault();
    guarded(btn, box, async () => {
      await api("POST", `${S}/requests`, { account_id: parseInt(acct.value, 10), period: period.value }, { "Idempotency-Key": newKey() });
      toast("Statement requested. It is generated in the background.");
      await refresh();
    });
  } }, field("Account", acct), field("Month (Africa/Cairo calendar month)", period), btn);

  async function refresh() {
    const items = (await api("GET", `${S}/requests`)).items;
    const rows = items.map((r) => el("tr", null,
      el("td", null, r.period.label), el("td", null, r.account.masked_number), el("td", null, pill(r.status)),
      el("td", null, fmtTime(r.created_at)),
      el("td", null,
        r.document_url ? el("a", { href: r.document_url, download: "" }, "Download PDF") : null,
        r.status === "queued" ? el("button", { class: "secondary", onclick: (ev) => guarded(ev.target, null, async () => {
          await api("POST", `${S}/requests/${r.id}/cancel`); await refresh(); }) }, "Cancel") : null,
        r.error ? el("span", { class: "muted" }, r.error.message) : null)));
    list.replaceChildren(table(["Period", "Account", "Status", "Requested", ""], rows, "No statement requests yet."));
    return items.some((r) => r.status === "queued" || r.status === "running");
  }
  root.append(form, box, el("h3", null, "My requests"), list,
    el("p", { class: "muted" }, "Statements cover a closed calendar month. Downloads are checked against your current account ownership every time."));
  poll(refresh);
  return root;
}

// ---- customer: loans
async function viewLoans() {
  const root = card("Loan applications");
  const accounts = (await api("GET", `${S}/accounts`)).items;
  const acct = el("select", null, accounts.map((a) => el("option", { value: a.account_id }, `${a.masked_number} — ${a.nickname} (${a.currency})`)));
  const amount = el("input", { inputmode: "decimal", placeholder: "e.g. 120000.00", required: true });
  const term = el("input", { type: "number", min: 1, max: 480, value: 24, required: true });
  const income = el("input", { inputmode: "decimal", placeholder: "e.g. 15000.00", required: true });
  const purpose = el("textarea", { maxlength: 200 });
  const box = errorBox();
  const list = el("div", { id: "loan-list" });
  const btn = el("button", { type: "submit" }, "Save draft");
  const form = el("form", { onsubmit: (ev) => {
    ev.preventDefault();
    guarded(btn, box, async () => {
      const a = parseMoney(amount.value), inc = parseMoney(income.value);
      if (a === null || a <= 0 || inc === null) throw new Error("Enter amounts like 120000.00 (at most two decimals).");
      await api("POST", `${L}/applications`, { account_id: parseInt(acct.value, 10), amount_minor: a,
        term_months: parseInt(term.value, 10), monthly_income_minor: inc, purpose: purpose.value });
      amount.value = ""; purpose.value = "";
      toast("Draft saved. Submit it when you are ready.");
      await refresh();
    });
  } }, el("div", { class: "row" }, field("Account", acct), field("Amount requested", amount), field("Term (months)", term),
    field("Declared monthly income", income)), field("Purpose", purpose), box, el("div", { class: "actions" }, btn));

  async function refresh() {
    const items = (await api("GET", `${L}/applications`)).items;
    const rows = items.map((a) => el("tr", null,
      el("td", null, fmtTime(a.created_at)), el("td", null, a.account.masked_number),
      el("td", { class: "num" }, fmtMoney(a.amount_minor, a.currency)), el("td", { class: "num" }, a.term_months),
      el("td", null, pill(a.status)), el("td", null, a.decision_reason || ""),
      el("td", null, a.status === "draft" ? el("button", { onclick: (ev) => guarded(ev.target, null, async () => {
        await api("POST", `${L}/applications/${a.id}/submit`, { version: a.version });
        toast("Application submitted for review."); await refresh(); }) }, "Submit") : null)));
    list.replaceChildren(table(["Created", "Account", "Amount", "Months", "Status", "Decision reason", ""], rows, "No applications yet."));
    return items.some((a) => a.status === "submitted" || a.status === "under_review");
  }
  root.append(el("p", { class: "notice" }, "Demo workflow only: an approval is a decision on the application. It does not disburse money and creates no loan in the core."),
    form, el("h3", null, "My applications"), list);
  poll(refresh);
  return root;
}

// ---- staff: loan review
async function viewReview() {
  const root = card("Loan review");
  const list = el("div", { id: "review-list" });
  const detail = el("div", { id: "review-detail" });
  const canAssign = hasRole("loans", "approver", "extension_admin");
  let reviewers = [];
  if (canAssign) { try { reviewers = (await api("GET", `${L}/review/reviewers`)).items; } catch (e) { reviewers = []; } }

  async function open(id) {
    const a = await api("GET", `${L}/review/applications/${id}`);
    const box = errorBox();
    const s = a.summary;
    const summary = !s ? el("p", { class: "muted" }, "No summary yet.")
      : s.status !== "completed" ? el("p", { class: "muted" }, `Summary ${s.status}${s.error_code ? " (" + s.error_code + ")" : ""}.`)
      : el("div", null, el("p", { class: "notice" }, s.label + ". Advisory only; the human decision below is what counts."),
          el("dl", { class: "kv" },
            el("dt", null, "Indicative band"), el("dd", null, s.result.band.replace(/_/g, " ")),
            el("dt", null, "Monthly instalment (no interest)"), el("dd", null, fmtMoney(s.result.installment_minor, a.currency)),
            el("dt", null, "Average monthly credits"), el("dd", null, fmtMoney(s.result.average_monthly_credits_minor, a.currency)),
            el("dt", null, "Average monthly debits"), el("dd", null, fmtMoney(s.result.average_monthly_debits_minor, a.currency)),
            el("dt", null, "Latest closing balance"), el("dd", null, fmtMoney(s.result.latest_closing_balance_minor, a.currency)),
            el("dt", null, "Months reviewed"), el("dd", null, s.result.months.map((m) => m.period).join(", "))));
    const act = async (path, body, msg) => { await api("POST", `${L}/review/applications/${a.id}/${path}`, { ...body, version: a.version }); toast(msg); await refresh(); await open(a.id); };
    const forms = [];
    if (canAssign && (a.status === "submitted" || (a.status === "under_review" && !a.recommendation))) {
      const sel = el("select", null, reviewers.map((r) => el("option", { value: r.subject }, r.label ? `${r.label} (${r.subject})` : r.subject)));
      forms.push(el("div", null, el("h3", null, "Assign reviewer"), el("div", { class: "row" }, field("Reviewer", sel),
        el("button", { disabled: reviewers.length === 0, onclick: (ev) => guarded(ev.target, box, () => act("assign", { reviewer_subject: sel.value }, "Reviewer assigned.")) }, "Assign"))));
    }
    if (hasRole("loans", "reviewer") && a.status === "under_review" && a.reviewer_subject === state.subject && !a.recommendation) {
      const rec = el("select", null, el("option", { value: "approve" }, "Recommend approval"), el("option", { value: "reject" }, "Recommend rejection"));
      const why = el("textarea", { minlength: 10, maxlength: 500, placeholder: "Reason (required, at least 10 characters)" });
      forms.push(el("div", null, el("h3", null, "My recommendation"), field("Recommendation", rec), field("Reason", why),
        el("button", { onclick: (ev) => guarded(ev.target, box, () => act("recommendation", { recommendation: rec.value, reason: why.value }, "Recommendation recorded.")) }, "Record recommendation")));
    }
    if (hasRole("loans", "approver") && a.status === "under_review" && a.recommendation) {
      const why = el("textarea", { minlength: 10, maxlength: 500, placeholder: "Decision reason (required, at least 10 characters)" });
      const self = a.reviewer_subject === state.subject;
      forms.push(el("div", null, el("h3", null, "Final decision"),
        self ? el("p", { class: "notice" }, "You reviewed this application, so a different approver must decide.") : null,
        field("Reason", why), el("div", { class: "actions" },
          el("button", { disabled: self, onclick: (ev) => guarded(ev.target, box, () => act("decision", { decision: "approved", reason: why.value }, "Application approved (workflow decision only).")) }, "Approve"),
          el("button", { class: "danger", disabled: self, onclick: (ev) => guarded(ev.target, box, () => act("decision", { decision: "rejected", reason: why.value }, "Application rejected.")) }, "Reject"))));
    }
    detail.replaceChildren(el("section", { class: "card" }, el("h2", null, "Application ", el("span", { class: "mono" }, a.id.slice(0, 8))),
      el("dl", { class: "kv" },
        el("dt", null, "Status"), el("dd", null, pill(a.status)),
        el("dt", null, "Applicant"), el("dd", null, `${a.owner_label} `, el("span", { class: "mono" }, a.owner_subject)),
        el("dt", null, "Account"), el("dd", null, a.account.masked_number),
        el("dt", null, "Amount / term"), el("dd", null, `${fmtMoney(a.amount_minor, a.currency)} over ${a.term_months} months`),
        el("dt", null, "Declared income"), el("dd", null, fmtMoney(a.monthly_income_minor, a.currency)),
        el("dt", null, "Purpose"), el("dd", null, a.purpose || "-"),
        el("dt", null, "Reviewer"), el("dd", { class: "mono" }, a.reviewer_subject || "unassigned"),
        el("dt", null, "Recommendation"), el("dd", null, a.recommendation ? `${a.recommendation} — ${a.recommendation_reason}` : "-"),
        el("dt", null, "Decision"), el("dd", null, a.decided_by ? `${a.status} by ${a.decided_by}: ${a.decision_reason}` : "-")),
      el("h3", null, "Background summary"), summary, box, ...forms,
      el("h3", null, "History"), table(["When", "Event", "Actor", "From", "To"], a.events.map((e) => el("tr", null,
        el("td", null, fmtTime(e.at)), el("td", null, e.type), el("td", { class: "mono" }, e.actor), el("td", null, e.from || ""), el("td", null, e.to || ""))))));
  }

  async function refresh() {
    const items = (await api("GET", `${L}/review/applications`)).items;
    const rows = items.map((a) => el("tr", null,
      el("td", null, fmtTime(a.submitted_at)), el("td", null, a.owner_label),
      el("td", { class: "num" }, fmtMoney(a.amount_minor, a.currency)), el("td", null, pill(a.status)),
      el("td", null, a.recommendation || "-"),
      el("td", null, el("button", { class: "secondary", onclick: (ev) => guarded(ev.target, null, () => open(a.id)) }, "Open"))));
    list.replaceChildren(table(["Submitted", "Applicant", "Amount", "Status", "Recommendation", ""], rows,
      hasRole("loans", "approver", "extension_admin") ? "No applications to review." : "No applications are assigned to you."));
    return false;
  }
  await refresh();
  root.append(el("p", { class: "muted" }, "Reviewers see only applications assigned to them. The reviewer and the approver must be different people."), list);
  return el("div", null, root, detail);
}

// ---- staff: batches
async function viewBatches() {
  const wrap = el("div");
  const sections = [];
  if (hasRole("statements", "batch_operator")) {
    const now = new Date();
    const prev = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
    const period = el("input", { value: `${prev.getUTCFullYear()}-${String(prev.getUTCMonth() + 1).padStart(2, "0")}`, pattern: "\\d{4}-\\d{2}", required: true });
    const box = errorBox();
    const list = el("div", { id: "statement-batches" });
    const btn = el("button", { type: "submit" }, "Create statement batch");
    const form = el("form", { class: "row", onsubmit: (ev) => { ev.preventDefault(); guarded(btn, box, async () => {
      await api("POST", `${S}/batches`, { period: period.value }, { "Idempotency-Key": newKey() });
      toast("Batch created. Members are processed by bounded workers."); await refresh(); }); } },
    field("Month (YYYY-MM)", period), btn);
    const refresh = batchList(list, `${S}/batches`, true);
    sections.push({ node: card("Statement batches", el("p", { class: "muted" }, "One statement per eligible account for a closed month. The member list is frozen from the core's authorised account discovery before anything is processed."), form, box, list), refresh });
  }
  if (hasRole("loans", "batch_operator")) {
    const box = errorBox();
    const list = el("div", { id: "loan-batches" });
    const btn = el("button", null, "Refresh summaries for open applications");
    btn.addEventListener("click", () => guarded(btn, box, async () => {
      await api("POST", `${L}/batches`, undefined, { "Idempotency-Key": newKey() }); toast("Summary batch created."); await refresh(); }));
    const refresh = batchList(list, `${L}/batches`, false);
    sections.push({ node: card("Loan summary batches", el("div", { class: "actions" }, btn), box, list), refresh });
  }
  if (!sections.length) return card("Batches", el("p", { class: "muted" }, "You do not hold the batch_operator role."));
  for (const s of sections) wrap.append(s.node);
  poll(async () => { const r = await Promise.all(sections.map((s) => s.refresh())); return r.some(Boolean); });
  return wrap;
}

function batchList(container, base, canCancel) {
  return async function refresh() {
    const items = (await api("GET", base)).items;
    const rows = items.map((b) => {
      const p = b.progress, done = p.completed + p.failed + (p.cancelled || 0);
      const active = !["completed", "completed_with_failures", "cancelled", "failed"].includes(b.status);
      const act = (path, msg) => (ev) => guarded(ev.target, null, async () => { await api("POST", `${base}/${b.id}/${path}`); toast(msg); await refresh(); });
      return el("tr", null,
        el("td", null, fmtTime(b.created_at)), el("td", null, b.period ? b.period.label : "open applications"),
        el("td", null, b.trigger), el("td", null, pill(b.status), b.error_code ? ` ${b.error_code}` : ""),
        el("td", null, el("progress", { max: Math.max(p.total, 1), value: done }), ` ${done}/${p.total}`,
          el("div", { class: "muted" }, `queued ${p.queued} · running ${p.running} · completed ${p.completed} · failed ${p.failed}`)),
        el("td", null,
          p.failed > 0 ? el("button", { class: "secondary", onclick: act("retry-failed", "Failed members re-queued.") }, `Retry failed (round ${b.retry_rounds + 1})`) : null,
          canCancel && (b.status === "freezing" || b.status === "releasing") ? el("button", { class: "secondary", onclick: act("resume", "Batch resumed.") }, "Resume") : null,
          canCancel && active ? el("button", { class: "danger", onclick: act("cancel", "Batch cancelled.") }, "Cancel") : null,
          el("button", { class: "secondary", onclick: (ev) => guarded(ev.target, null, async () => {
            const d = await api("GET", `${base}/${b.id}`);
            const f = d.failures || [];
            toast(f.length ? "Failures: " + f.map((x) => `${x.account_masked || x.application_id.slice(0, 8)}: ${x.error_code}`).join("; ") : "No failures recorded.");
          }) }, "Failures")));
    });
    container.replaceChildren(table(["Created", "Scope", "Trigger", "Status", "Progress", ""], rows, "No batches yet."));
    return items.some((b) => !["completed", "completed_with_failures", "cancelled", "failed"].includes(b.status));
  };
}

// ---- staff: role administration
async function viewRoles() {
  const wrap = el("div");
  for (const [name, base, service] of [["Statements", S, "statements"], ["Loans", L, "loans"]]) {
    if (!hasRole(service, "extension_admin")) continue;
    const list = el("div");
    const box = errorBox();
    const data = await api("GET", `${base}/admin/roles`);
    const subject = el("input", { placeholder: "nvb-user:<id>:<created>", pattern: "nvb-user:\\d+:\\d+", required: true });
    const role = el("select", null, data.known_roles.map((r) => el("option", { value: r }, r)));
    const label = el("input", { placeholder: "username (display only)", maxlength: 80 });
    const reason = el("input", { placeholder: "reason", maxlength: 200 });
    const draw = (items) => list.replaceChildren(table(["Subject", "Label", "Role", "Changed by", ""], items.map((i) => el("tr", null,
      el("td", { class: "mono" }, i.subject), el("td", null, i.label), el("td", null, i.role), el("td", { class: "mono" }, i.updated_by),
      el("td", null, el("button", { class: "danger", onclick: (ev) => guarded(ev.target, box, async () => {
        draw((await api("POST", `${base}/admin/roles/revoke`, { subject: i.subject, role: i.role, reason: "revoked in portal" })).items); }) }, "Revoke")))), "No role assignments."));
    draw(data.items);
    const btn = el("button", { type: "submit" }, "Grant role");
    const form = el("form", { class: "row", onsubmit: (ev) => { ev.preventDefault(); guarded(btn, box, async () => {
      draw((await api("POST", `${base}/admin/roles/grant`, { subject: subject.value.trim(), role: role.value, label: label.value, reason: reason.value })).items);
      toast("Role granted."); }); } }, field("Subject", subject), field("Role", role), field("Label", label), field("Reason", reason), btn);
    wrap.append(card(`${name}: extension roles`, form, box, list));
  }
  if (!wrap.children.length) return card("Roles", el("p", { class: "muted" }, "You do not hold the extension_admin role."));
  return wrap;
}

// ---------------------------------------------------------------- router
function poll(refresh) {
  clearInterval(state.timer);
  const token = location.hash;
  const run = async () => {
    if (location.hash !== token || !state.user) { clearInterval(state.timer); return; }
    try { await refresh(); } catch (e) { /* shown on the next user action */ }
  };
  run();
  state.timer = setInterval(run, 4000);
}

const ROUTES = [
  { hash: "#/statements", label: "Statements", show: () => state.user.role === "customer", view: viewStatements },
  { hash: "#/loans", label: "Loans", show: () => state.user.role === "customer", view: viewLoans },
  { hash: "#/staff/review", label: "Loan review", show: () => hasRole("loans", "reviewer", "approver", "extension_admin"), view: viewReview },
  { hash: "#/staff/batches", label: "Batches", show: () => hasRole("statements", "batch_operator") || hasRole("loans", "batch_operator"), view: viewBatches },
  { hash: "#/staff/roles", label: "Roles", show: () => hasRole("statements", "extension_admin") || hasRole("loans", "extension_admin"), view: viewRoles },
  { hash: "#/identity", label: "My identity", show: () => true, view: async () => viewIdentity() },
];

async function render() {
  clearInterval(state.timer);
  const view = document.getElementById("view"), nav = document.getElementById("nav"), who = document.getElementById("who");
  if (!state.user) {
    nav.replaceChildren(); who.replaceChildren();
    view.replaceChildren(viewLogin());
    return;
  }
  const routes = ROUTES.filter((r) => r.show());
  let current = routes.find((r) => r.hash === location.hash);
  if (!current) { current = routes[0]; history.replaceState(null, "", current.hash); }
  nav.replaceChildren(...routes.map((r) => el("a", { href: r.hash, "aria-current": r === current ? "page" : null }, r.label)));
  who.replaceChildren(el("span", null, `${state.user.display_name} (${state.user.role}${isStaff() ? ", staff" : ""})`),
    el("button", { class: "secondary", id: "logout", onclick: logout }, "Sign out"));
  view.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  try { view.replaceChildren(await current.view()); }
  catch (e) { if (state.user) view.replaceChildren(card("Something went wrong", el("p", { class: "error" }, e.message))); }
}

window.addEventListener("hashchange", render);
loadSession().then(render);
