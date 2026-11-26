// People Lens web app: vanilla JS, no build step. All text is inserted as text nodes or escaped before rendering.
const API = (window.PEOPLELENS_API || "http://localhost:8000").replace(/\/$/, "");
const TYPE = { CL: "Casual", SL: "Sick", EL: "Earned", LOP: "Loss of pay" };

// ------------------------------------------------------------------ icons (24px stroke icons)
const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h14V9.5"/><path d="M10 20v-6h4v6"/>',
  leave: '<path d="M12 21V11"/><path d="M12 11C9 6 4 7 3 9c3-1 6 0 9 2Z"/><path d="M12 11c3-5 8-4 9-2-3-1-6 0-9 2Z"/><path d="M12 11c-1-4-5-7-8-6 3 1 6 3 8 6Z"/><path d="M12 11c1-4 5-7 8-6-3 1-6 3-8 6Z"/><path d="M7 21h10"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  checksq: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m8 12 3 3 5-6"/>',
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"/><circle cx="17" cy="9" r="2.6"/><path d="M16 14.6c2.8.2 5 2.2 5 5.4"/>',
  org: '<rect x="9" y="3" width="6" height="5" rx="1.2"/><rect x="3" y="16" width="6" height="5" rx="1.2"/><rect x="15" y="16" width="6" height="5" rx="1.2"/><path d="M12 8v4M6 16v-2h12v2"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  sparkle: '<path d="M12 3c.6 4.2 2.8 6.4 7 7-4.2.6-6.4 2.8-7 7-.6-4.2-2.8-6.4-7-7 4.2-.6 6.4-2.8 7-7Z"/><path d="M19 3v3M17.5 4.5h3"/>',
  send: '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  grid: '<rect x="4" y="4" width="6" height="6" rx="1.5"/><rect x="14" y="4" width="6" height="6" rx="1.5"/><rect x="4" y="14" width="6" height="6" rx="1.5"/><rect x="14" y="14" width="6" height="6" rx="1.5"/>',
  chat: '<path d="M4 5h16v11H9l-5 4Z"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
  bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7Z"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  cal: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  doc: '<path d="M14 3H6v18h12V7Z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>',
  book: '<path d="M4 4h6a3 3 0 0 1 3 3v13a2 2 0 0 0-2-2H4Z"/><path d="M20 4h-6a3 3 0 0 0-3 3"/><path d="M20 4v14h-7"/>',
  logout: '<path d="M15 4h4v16h-4"/><path d="M10 8l-4 4 4 4M6 12h10"/>',
  expand: '<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>',
  shrink: '<path d="M4 14h6v6M20 10h-6V4M10 14l-7 7M14 10l7-7"/>',
  collapse: '<path d="M10 6 4 12l6 6"/><path d="M20 6v12"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  home2: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  heart: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10Z"/>',
  inbox: '<path d="M3 13h5l2 3h4l2-3h5"/><path d="M5 5h14l2 8v6H3v-6Z"/>',
  lens: '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/>',
};
function icon(name, cls = "i") {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", cls); s.innerHTML = ICONS[name] || ICONS.doc;
  return s;
}

// ------------------------------------------------------------------ tiny DOM builder
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;            // only ever given escaped/sanitised markup
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const $ = (sel) => document.querySelector(sel);

// ------------------------------------------------------------------ helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x) => (x === null || x === undefined ? "—" : String(Number(x)));
const plural = (n, w) => `${num(n)} ${w}${Number(n) === 1 ? "" : "s"}`;
const initials = (name) => (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join("");
function fmtDate(iso, withYear = false) {
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(withYear ? { year: "numeric" } : {}) });
}
function fmtRange(a, b) { return a === b ? fmtDate(a, true) : `${fmtDate(a)} – ${fmtDate(b, true)}`; }
function ago(ts) {
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)} d ago`;
  return fmtDate(ts);
}
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "show" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = ""), 2800);
}
const pill = (status) => h("span", { class: `pill ${status}` }, status[0].toUpperCase() + status.slice(1));

// Safe markdown: escape everything first, then add a small set of formatting.
function md(src) {
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[P(\d+)\]/g, '<span class="ref">P$1</span>');
  const lines = String(src || "").split("\n");
  let out = "", i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:-]+\|/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      out += "<table><tr>" + cells(line).map((c) => `<th>${inline(c)}</th>`).join("") + "</tr>";
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { out += "<tr>" + cells(lines[i]).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>"; i++; }
      out += "</table>"; continue;
    }
    if (/^\s*([-*•])\s+/.test(line)) {
      out += "<ul>"; while (i < lines.length && /^\s*([-*•])\s+/.test(lines[i])) { out += `<li>${inline(lines[i].replace(/^\s*[-*•]\s+/, ""))}</li>`; i++; }
      out += "</ul>"; continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      out += "<ol>"; while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) { out += `<li>${inline(lines[i].replace(/^\s*\d+[.)]\s+/, ""))}</li>`; i++; }
      out += "</ol>"; continue;
    }
    if (/^#{1,4}\s+/.test(line)) { out += `<p><strong>${inline(line.replace(/^#+\s+/, ""))}</strong></p>`; i++; continue; }
    if (!line.trim()) { i++; continue; }
    let para = [line]; i++;
    while (i < lines.length && lines[i].trim() && !/^\s*([-*•]|\d+[.)]|\||#)/.test(lines[i])) { para.push(lines[i]); i++; }
    out += `<p>${para.map(inline).join("<br>")}</p>`;
  }
  return out;
}

// ------------------------------------------------------------------ state & API
const store = {
  get(k, d) { try { return JSON.parse(sessionStorage.getItem("pl." + k)) ?? d; } catch { return d; } },
  set(k, v) { try { sessionStorage.setItem("pl." + k, JSON.stringify(v)); } catch { /* private mode */ } },
  clear() { try { Object.keys(sessionStorage).filter((k) => k.startsWith("pl.")).forEach((k) => sessionStorage.removeItem(k)); } catch {} },
};
const S = {
  token: store.get("token", null), me: null, page: store.get("page", "home"), sessionId: store.get("session", null),
  chat: store.get("chat", []), tab: "suggestions", expanded: false, collapsed: store.get("collapsed", false),
  balances: [], requests: [], notes: { unread: 0, items: [] }, approvals: [], today: null, hr: null, busy: false,
};

async function api(method, path, body, { quiet = false } = {}) {
  let r;
  try {
    r = await fetch(API + path, { method, headers: { "Content-Type": "application/json", ...(S.token ? { Authorization: `Bearer ${S.token}` } : {}) },
                                   body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    if (!quiet) toast("The service is unavailable right now.", true);
    throw new Error("network");
  }
  if (r.status === 401 && S.token) { signOut(); throw new Error("auth"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const msg = typeof data.detail === "string" ? data.detail : "Something went wrong.";
    if (!quiet) toast(msg, true);
    const e = new Error(msg); e.status = r.status; throw e;
  }
  return data;
}
const isMgr = () => S.me && (S.me.role === "manager" || S.me.role === "hr_admin");
const isHR = () => S.me && S.me.role === "hr_admin";

async function loadCore() {
  const [bal, reqs, notes, today] = await Promise.all([
    api("GET", "/me/balances"), api("GET", "/me/leave-requests"), api("GET", "/notifications?limit=20"), api("GET", "/attendance/today")]);
  S.balances = bal.balances; S.requests = reqs; S.notes = notes; S.today = today;
  S.approvals = isMgr() ? await api("GET", "/approvals") : [];
  S.hr = isHR() ? await api("GET", "/hr/summary") : null;
}

// ------------------------------------------------------------------ sign in (people picker)
async function renderLogin() {
  const root = $("#root"); root.innerHTML = "";
  let people = [], personas = [], selected = null;
  try { [people, personas] = await Promise.all([api("GET", "/auth/directory"), api("GET", "/auth/identities")]); }
  catch { root.append(h("div", { class: "empty" }, "The service is unavailable right now. Please try again shortly.")); return; }

  const list = h("div", { class: "picker" });
  const go = h("button", { class: "btn primary block", disabled: true, onclick: () => selected && signIn(selected.employee_id) }, "Continue");
  const choose = (p) => { selected = p; go.disabled = false; go.textContent = `Continue as ${p.name.split(" ")[0]}`; draw(search.value); };
  const draw = (q) => {
    list.innerHTML = "";
    const ql = (q || "").toLowerCase();
    people.filter((p) => !ql || `${p.name} ${p.designation} ${p.department}`.toLowerCase().includes(ql)).slice(0, 60).forEach((p) =>
      list.append(h("button", { class: selected?.employee_id === p.employee_id ? "sel" : "", onclick: () => choose(p) },
        h("div", { class: "avatar sm soft" }, initials(p.name)),
        h("div", {}, h("div", { class: "n", style: "font-weight:600;color:var(--navy)" }, p.name), h("div", { class: "small muted" }, `${p.designation} · ${p.department}`)),
        h("span", { class: "role-tag" }, p.role.replace("_", " ")))));
  };
  const search = h("input", { class: "input", placeholder: "Search people…", style: "width:100%", oninput: (e) => draw(e.target.value) });
  const pick = { employee: personas.find((p) => p.role === "employee"), manager: personas.find((p) => p.role === "manager"), hr: personas.find((p) => p.role === "hr_admin") };

  root.append(h("div", { class: "login" },
    h("div", { class: "art" },
      h("div", { class: "brand" }, h("div", { class: "mark" }, icon("lens")), h("div", { class: "name" }, "People Lens")),
      h("div", {}, h("h2", {}, "Your HR, one conversation away."),
        h("p", {}, "Check in, apply for leave, approve requests and get policy answers with sources — all in one place.")),
      h("div", { class: "bubbles" }, h("div", {}, "“How many casual leaves do I have left?”"), h("div", {}, "“Apply for leave next Monday and Tuesday.”"),
        h("div", {}, "“Who in my team is away next week?”"))),
    h("div", { class: "panel" }, h("div", { class: "box" },
      h("h1", {}, "Sign in"), h("div", { class: "muted" }, "Choose who you are to continue."),
      h("div", { class: "quick" }, ...Object.entries(pick).filter(([, p]) => p).map(([label, p]) =>
        h("button", { class: "chip", onclick: () => choose(p) }, icon(label === "hr" ? "org" : label === "manager" ? "users" : "leave"),
          `${label === "hr" ? "HR" : label[0].toUpperCase() + label.slice(1)} · ${p.name.split(" ")[0]}`))),
      h("div", { class: "search" }, icon("search"), search), list, h("div", { style: "height:16px" }), go))));
  draw("");
}

async function signIn(employeeId) {
  try {
    const r = await api("POST", "/auth/login", { employee_id: employeeId });
    S.token = r.token; store.set("token", r.token);
    S.sessionId = (await api("POST", "/sessions")).session_id; store.set("session", S.sessionId);
    S.chat = []; store.set("chat", []); S.page = "home"; store.set("page", "home");
    await boot();
  } catch { /* toast already shown */ }
}
function signOut() { store.clear(); Object.assign(S, { token: null, me: null, chat: [], sessionId: null, page: "home" }); renderLogin(); }

// ------------------------------------------------------------------ shell
const NAV = () => [
  { section: null, items: [{ id: "home", label: "Home", icon: "home" }] },
  { section: "My space", items: [{ id: "leave", label: "My leave", icon: "leave" }] },
  ...(isMgr() ? [{ section: "My team", items: [{ id: "approvals", label: "Approvals", icon: "checksq", count: S.approvals.length }] }] : []),
  ...(isHR() ? [{ section: "Organisation", items: [{ id: "hr", label: "All leaves", icon: "org" }] }] : []),
];
const TITLES = { home: ["grid", "Home"], leave: ["leave", "My leave"], approvals: ["checksq", "Approvals"], hr: ["org", "All leaves"] };

function renderShell() {
  const root = $("#root"); root.innerHTML = "";
  const nav = h("nav", { class: "sidebar" }, h("div", { class: "accent" }),
    h("div", { class: "brand" }, h("div", { class: "mark" }, icon("lens")), h("div", { class: "name" }, "People Lens")),
    ...NAV().map((g) => [g.section ? h("div", { class: "nav-section" }, g.section) : null,
      ...g.items.map((it) => h("button", { class: `nav-item${S.page === it.id ? " active" : ""}`, title: it.label, onclick: () => go(it.id) },
        icon(it.icon), h("span", {}, it.label), it.count ? h("b", { class: "count" }, it.count) : null))]),
    h("div", { class: "spacer" }),
    h("button", { class: "nav-item", title: "Sign out", onclick: signOut }, icon("logout"), h("span", {}, "Sign out")));
  const wrap = h("div", { class: "sidebar-wrap" }, nav,
    h("button", { class: "collapse-btn", title: "Collapse menu", onclick: () => { S.collapsed = !S.collapsed; store.set("collapsed", S.collapsed); renderShell(); } }, icon("collapse")));
  const [ic, title] = TITLES[S.page] || TITLES.home;
  const main = h("div", { class: "main" }, renderTopbar(ic, title), h("div", { class: "content", id: "content" }));
  root.append(h("div", { class: `app${S.collapsed ? " collapsed" : ""}` }, wrap, main));
  renderPage();
}

function renderTopbar(ic, title) {
  const t = S.today || {};
  let checkin;
  if (t.on_leave) checkin = h("span", { class: "checkin-pill" }, h("i", { class: "dot" }), h("span", {}, "On leave today"));
  else if (t.working_day === false) checkin = null;
  else if (t.status === "not_checked_in") checkin = h("button", { class: "checkin-pill", onclick: () => doCheckIn("office") }, h("i", { class: "dot" }), h("span", {}, "Check in"));
  else if (!t.check_out) checkin = h("button", { class: "checkin-pill on", title: "Check out", onclick: doCheckOut }, h("i", { class: "dot" }),
    h("span", {}, `${t.status === "wfh" ? "WFH" : "In"} since ${t.check_in} · Check out`));
  else checkin = h("span", { class: "checkin-pill" }, h("i", { class: "dot" }), h("span", {}, `Done for today · ${t.check_in}–${t.check_out}`));

  const bellWrap = h("div", { class: "rel" },
    h("button", { class: "icon-btn", title: "Notifications", onclick: (e) => { e.stopPropagation(); toggleDropdown("notes", bellWrap); } }, icon("bell"),
      S.notes.unread ? h("span", { class: "badge" }, S.notes.unread > 9 ? "9+" : S.notes.unread) : null));
  const userWrap = h("div", { class: "rel" },
    h("button", { class: "user", onclick: (e) => { e.stopPropagation(); toggleDropdown("user", userWrap); } },
      h("span", { class: "uname" }, S.me.name.split(" ")[0]), h("div", { class: "avatar" }, initials(S.me.name))));
  return h("header", { class: "topbar" },
    h("div", { class: "title" }, icon(ic), title), h("div", { class: "grow" }),
    h("button", { class: "btn-ai", onclick: () => { go("home"); setTimeout(() => $("#composer")?.focus(), 30); } }, icon("sparkle"), h("span", {}, "Ask AI")),
    checkin, bellWrap, userWrap);
}

function toggleDropdown(kind, anchor) {
  const open = document.querySelector(".dropdown");
  const same = open && open.dataset.kind === kind;
  open?.remove();
  if (same) return;
  let dd;
  if (kind === "notes") {
    dd = h("div", { class: "dropdown", "data-kind": "notes", onclick: (e) => e.stopPropagation() },
      h("div", { class: "dh" }, h("b", {}, "Notifications"),
        S.notes.unread ? h("button", { class: "link-btn", onclick: async () => { await api("POST", "/notifications/read", {}); await refresh(); } }, "Mark all read") : null),
      h("div", { class: "items" }, S.notes.items.length ? S.notes.items.map(noteRow) : h("div", { class: "empty" }, "You're all caught up.")));
  } else {
    dd = h("div", { class: "dropdown menu", "data-kind": "user", onclick: (e) => e.stopPropagation() },
      h("div", { class: "dh" }, h("div", {}, h("b", {}, S.me.name), h("div", { class: "small muted" }, `${S.me.designation} · ${S.me.department}`))),
      h("button", { onclick: () => go("leave") }, icon("leave"), "My leave"),
      h("button", { onclick: signOut }, icon("logout"), "Sign out"));
  }
  anchor.append(dd);
}
document.addEventListener("click", () => document.querySelector(".dropdown")?.remove());

function noteRow(n) {
  return h("div", { class: `note${n.is_read ? "" : " unread"}`, style: "cursor:pointer", onclick: async () => {
      if (!n.is_read) await api("POST", "/notifications/read", { ids: [n.id] }, { quiet: true }).catch(() => {});
      document.querySelector(".dropdown")?.remove();
      await refresh(); go(n.kind === "approval_needed" && isMgr() ? "approvals" : "leave");
    } },
    h("i", { class: `dot${n.is_read ? " read" : ""}` }),
    h("div", {}, h("div", { class: "t" }, n.title), h("div", { class: "s" }, `${n.body || ""} · ${ago(n.created_at)}`)));
}

function go(page) { S.page = page; store.set("page", page); document.querySelector(".dropdown")?.remove(); renderShell(); }
async function refresh() { await loadCore(); renderShell(); }

async function doCheckIn(mode) {
  try { const r = await api("POST", "/attendance/check-in", { mode }); toast(`Checked in${mode === "wfh" ? " (WFH)" : ""} at ${r.check_in}`); await refresh(); } catch {}
}
async function doCheckOut() {
  try { const r = await api("POST", "/attendance/check-out"); toast(`Checked out at ${r.check_out}`); await refresh(); } catch {}
}

function renderPage() {
  const c = $("#content"); c.innerHTML = "";
  ({ home: pageHome, leave: pageLeave, approvals: pageApprovals, hr: pageHR }[S.page] || pageHome)(c);
}

// ------------------------------------------------------------------ home: agent + widgets
const PROMPTS = {
  suggestions: () => [
    ["leave", "How many leave days do I have available?"],
    ["plus", "I want to apply for leave"],
    ["book", "Can I carry forward earned leave?"],
    ["home2", "What is the work-from-home policy?"],
    ...(isMgr() ? [["users", "Who in my team is on leave next week?"]] : [["clock", "Who approves leave longer than five days?"]]),
  ],
  insights: () => isHR() ? [
    ["chart", "Which department has the most pending leave requests?"],
    ["bolt", "Are there any unusual attendance patterns or inconsistent records?"],
    ["users", "Who joined in the last three months?"],
    ["cal", "Forecast availability for the IT department next week"],
  ] : isMgr() ? [
    ["checksq", "Recommend which pending requests I should review first, considering coverage and policy"],
    ["users", "How many people in my team are on leave next week?"],
    ["cal", "Forecast my team's availability for next week"],
    ["chart", "Summarise my team's leave taken this quarter"],
  ] : [
    ["chart", "Summarise my leave usage this year"],
    ["cal", "Am I eligible for seven days of earned leave starting next month?"],
    ["clock", "What happens if my manager is unavailable to approve my leave?"],
    ["doc", "When do I need a medical certificate for sick leave?"],
  ],
};

function pageHome(c) {
  const grid = h("div", { class: `home-grid${S.expanded ? " wide" : ""}` });
  const center = h("div", { class: "stack" }, agentCard(), recentCard());
  grid.append(center);
  if (!S.expanded) grid.append(h("div", { class: "stack" }, leaveWidget(), todayWidget(), isMgr() ? approvalsWidget() : null, isHR() ? orgWidget() : null));
  c.append(grid);
  const th = $("#thread"); if (th) th.scrollTop = th.scrollHeight;
}

function agentCard() {
  const first = S.me.name.split(" ")[0];
  const ta = h("textarea", { id: "composer", rows: 1, placeholder: S.chat.length ? "Reply…" : "Ask about leave, attendance or any HR policy…",
    oninput: (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px"; sendBtn.disabled = !e.target.value.trim() || S.busy; },
    onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } } });
  const sendBtn = h("button", { class: "send", title: "Send", disabled: true, onclick: () => submit() }, icon("send"));
  const submit = () => { const v = ta.value.trim(); if (v && !S.busy) { ta.value = ""; ask(v); } };

  const head = h("div", { class: "card-h" }, h("h3", {}, "People Lens AI"),
    h("div", { class: "actions" },
      S.chat.length ? h("button", { class: "icon-btn", title: "New conversation", onclick: newChat }, icon("plus")) : null,
      h("button", { class: "icon-btn", title: S.expanded ? "Exit full width" : "Full width", onclick: () => { S.expanded = !S.expanded; renderPage(); } },
        icon(S.expanded ? "shrink" : "expand"))));
  const composer = h("div", { class: "composer" },
    h("div", { class: "row" }, icon("sparkle"), ta),
    h("div", { class: "bar" }, h("button", { class: "round", title: "Apply for leave", onclick: () => ask("I want to apply for leave") }, icon("plus")), sendBtn));

  const inner = h("div", { class: "inner" }, head);
  if (!S.chat.length) inner.append(h("div", { class: "greet" }, `Hi ${first}, where shall we start?`));
  else inner.append(threadView());
  inner.append(composer);
  if (!S.chat.length || S.expanded === false) inner.append(chipTabs());
  return h("div", { class: `agent${S.expanded ? " expanded" : ""}` }, inner);
}

function chipTabs() {
  const pendingItems = pendingList();
  const tabs = [["suggestions", "chat", "Suggestions"], ["pending", "checksq", "Pending", pendingItems.length], ["actions", "plus", "Actions"], ["insights", "chart", "Insights"]];
  const body = h("div", { class: "suggest" });
  const fill = () => {
    body.innerHTML = "";
    if (S.tab === "suggestions" || S.tab === "insights")
      PROMPTS[S.tab]().forEach(([ic, text]) => body.append(h("button", { onclick: () => ask(text) }, h("span", { class: "ic" }, icon(ic)), text)));
    else if (S.tab === "pending")
      (pendingItems.length ? pendingItems : [{ ic: "check", text: "Nothing pending — you're all set.", meta: "" }]).forEach((p) =>
        body.append(h("button", { onclick: p.act || null }, h("span", { class: "ic" }, icon(p.ic)), p.text, p.meta ? h("span", { class: "meta" }, p.meta) : null)));
    else actionList().forEach((a) => body.append(h("button", { onclick: a.act }, h("span", { class: "ic" }, icon(a.ic)), a.text)));
  };
  const inChat = S.chat.length > 0;
  let open = !inChat;
  const row = h("div", { class: "chips" }, ...tabs.map(([id, ic, label, n]) =>
    h("button", { class: `chip${open && S.tab === id ? " active" : ""}`, onclick: (e) => {
        const same = S.tab === id && open;
        S.tab = id; open = inChat ? !same : true;
        row.querySelectorAll(".chip").forEach((b) => b.classList.remove("active"));
        if (open) e.currentTarget.classList.add("active");
        body.classList.toggle("hidden", !open); fill();
      } },
      icon(ic), label, n ? h("span", { class: "n" }, n) : null)));
  fill();
  body.classList.toggle("hidden", !open);
  return h("div", {}, row, body);
}

function pendingList() {
  const items = [];
  if (S.today && S.today.status === "not_checked_in" && S.today.working_day && !S.today.on_leave)
    items.push({ ic: "clock", text: "You haven't checked in today", meta: "Check in", act: () => doCheckIn("office") });
  if (isMgr() && S.approvals.length)
    items.push({ ic: "checksq", text: `${plural(S.approvals.length, "leave request")} waiting for your approval`, meta: "Review", act: () => go("approvals") });
  S.requests.filter((r) => r.status === "pending").slice(0, 4).forEach((r) =>
    items.push({ ic: "leave", text: `${TYPE[r.leave_type]} leave · ${fmtRange(r.start_date, r.end_date)} · awaiting ${r.approver}`, meta: r.id, act: () => go("leave") }));
  const unread = S.notes.items.filter((n) => !n.is_read).length;
  if (unread) items.push({ ic: "bell", text: `${plural(unread, "unread notification")}`, meta: "Open", act: (e) => { e.stopPropagation(); toggleDropdown("notes", document.querySelector(".topbar .rel")); } });
  return items;
}

function actionList() {
  const t = S.today || {};
  const a = [{ ic: "plus", text: "Apply for leave", act: () => ask("I want to apply for leave") }];
  if (t.status === "not_checked_in" && t.working_day && !t.on_leave) {
    a.push({ ic: "clock", text: "Check in at the office", act: () => doCheckIn("office") });
    a.push({ ic: "home", text: "Check in — working from home", act: () => doCheckIn("wfh") });
  } else if (t.check_in && !t.check_out) a.push({ ic: "logout", text: "Check out for today", act: doCheckOut });
  if (isMgr()) a.push({ ic: "checksq", text: "Review approval requests", act: () => go("approvals") });
  if (isHR()) a.push({ ic: "org", text: "Open all leaves", act: () => go("hr") });
  a.push({ ic: "leave", text: "View my leave and requests", act: () => go("leave") });
  return a;
}

function threadView() {
  const th = h("div", { class: "thread", id: "thread" });
  S.chat.forEach((m, idx) => {
    if (m.role === "user") { th.append(h("div", { class: "msg user" }, h("div", { class: "bubble" }, m.content))); return; }
    const bubble = h("div", { class: "bubble" });
    if (m.typing) bubble.append(h("span", { class: "typing" }, h("i"), h("i"), h("i")));
    else bubble.append(h("div", { html: md(m.content) }));
    if (m.citations?.length) bubble.append(h("div", { class: "sources" }, ...m.citations.map((c) => {
      const p = c.section_path.split(" > ");
      return h("span", {}, `${c.ref.replace(/[[\]]/g, "")} · ${p[0]}${p.length > 1 ? " — " + p[p.length - 1] : ""}`);
    })));
    if (m.pending) bubble.append(proposalCard(m.pending, idx));
    th.append(h("div", { class: "msg bot" }, h("div", { class: "bot-ic" }, icon("sparkle")), bubble));
  });
  return th;
}

function proposalCard(pa, idx) {
  const p = pa.payload;
  const title = pa.type === "submit_leave"
    ? `${TYPE[p.leave_type]} leave · ${fmtRange(p.start_date, p.end_date)} · ${plural(p.days, "day")}`
    : `${p.decision === "approve" ? "Approve" : "Reject"} ${p.request_id} · ${p.requester_name}`;
  const sub = pa.type === "submit_leave" ? `Approver: ${p.approver_name} · Balance after: ${num(p.balance_after)}` : "";
  const act = async (kind) => {
    try {
      const r = await api("POST", `/actions/${pa.action_id}/${kind}`, { session_id: S.sessionId });
      S.chat[idx].pending = null; S.chat.push({ role: "bot", content: r.message }); store.set("chat", S.chat);
      toast(kind === "confirm" ? "Done" : "Cancelled");
      await refresh();
    } catch {}
  };
  return h("div", { class: "proposal" }, h("div", { class: "t" }, title), sub ? h("div", { class: "small muted" }, sub) : null,
    h("div", { class: "btns" }, h("button", { class: "btn primary", onclick: () => act("confirm") }, icon("check"), "Confirm"),
      h("button", { class: "btn", onclick: () => act("cancel") }, "Cancel")));
}

async function ask(text) {
  if (S.busy) return;
  if (S.page !== "home") { S.page = "home"; store.set("page", "home"); renderShell(); }
  S.busy = true;
  S.chat.forEach((m) => (m.pending = null));
  S.chat.push({ role: "user", content: text }, { role: "bot", typing: true, content: "" });
  renderPage();
  try {
    const r = await api("POST", "/chat", { session_id: S.sessionId, message: text });
    S.chat[S.chat.length - 1] = { role: "bot", content: r.answer, citations: r.citations || [], pending: r.pending_action || null };
  } catch (e) {
    S.chat[S.chat.length - 1] = { role: "bot", content: e.message === "auth" ? "" : "Sorry — I couldn't reach the assistant. Please try again." };
  }
  S.busy = false; store.set("chat", S.chat.filter((m) => !m.typing));
  if (!S.token) return;
  await loadCore().catch(() => {});
  renderShell();
  setTimeout(() => $("#composer")?.focus(), 20);
}

async function newChat() {
  S.sessionId = (await api("POST", "/sessions")).session_id; store.set("session", S.sessionId);
  S.chat = []; store.set("chat", []); S.tab = "suggestions"; renderPage();
}

function recentCard() {
  const rows = S.requests.slice(0, 5);
  return h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", {}, "Recent leave requests"), h("button", { class: "link-btn", onclick: () => go("leave") }, "View all")),
    rows.length ? rows.map((r) => h("div", { class: "list-row" }, h("div", { class: "ic" }, icon("cal")),
      h("div", {}, h("div", { class: "t" }, `${TYPE[r.leave_type]} leave · ${plural(r.days, "day")}`), h("div", { class: "s" }, `${fmtRange(r.start_date, r.end_date)} · ${r.approver}`)),
      h("div", { class: "end" }, pill(r.status))))
      : h("div", { class: "empty" }, "No leave requests yet."));
}

function leaveWidget() {
  const by = Object.fromEntries(S.balances.map((b) => [b.leave_type, b]));
  const el = by.EL || S.balances[0] || { available: 0, name: "Leave" };
  return h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", {}, "Leave")),
    h("div", { class: "widget-hero" }, h("div", { class: "circle" }, icon("leave")),
      h("div", { class: "big" }, `${num(el.available)} days`), h("div", { class: "sub" }, `${el.name} available`)),
    h("div", { class: "mini-bal" }, ...["CL", "SL", "EL"].filter((k) => by[k]).map((k) => h("div", {}, h("b", {}, num(by[k].available)), h("span", {}, TYPE[k])))),
    h("button", { class: "btn soft block", onclick: () => ask("I want to apply for leave") }, "Request leave"));
}

function todayWidget() {
  const t = S.today || {};
  const date = new Date((t.date || "") + "T00:00:00").toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
  let body;
  if (t.on_leave) body = [h("div", { class: "muted" }, `You're on leave today (${t.on_leave}).`)];
  else if (t.working_day === false) body = [h("div", { class: "muted" }, "No check-in needed today.")];
  else if (t.status === "not_checked_in") body = [h("div", { class: "muted", style: "margin-bottom:12px" }, "You haven't checked in yet."),
    h("div", { style: "display:flex;gap:8px" }, h("button", { class: "btn primary", style: "flex:1", onclick: () => doCheckIn("office") }, "Check in"),
      h("button", { class: "btn", style: "flex:1", onclick: () => doCheckIn("wfh") }, "Work from home"))];
  else body = [h("div", { class: "list-row" }, h("div", { class: "ic", style: "background:var(--green-soft);color:var(--green)" }, icon("check")),
      h("div", {}, h("div", { class: "t" }, t.status === "wfh" ? "Working from home" : "At the office"),
        h("div", { class: "s" }, `In ${t.check_in}${t.check_out ? ` · Out ${t.check_out}` : ""}${t.late ? " · late" : ""}`))),
    !t.check_out ? h("button", { class: "btn block", style: "margin-top:10px", onclick: doCheckOut }, "Check out") : null];
  return h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", {}, "Today"), h("span", { class: "small muted" }, date)), ...body);
}

function approvalsWidget() {
  const rows = S.approvals.slice(0, 3);
  return h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", {}, "Approvals"), S.approvals.length ? h("span", { class: "pill pending" }, `${S.approvals.length} waiting`) : null),
    rows.length ? rows.map((r) => h("div", { class: "list-row" }, h("div", { class: "avatar sm soft" }, initials(r.employee)),
      h("div", {}, h("div", { class: "t" }, r.employee), h("div", { class: "s" }, `${TYPE[r.leave_type]} · ${fmtRange(r.start_date, r.end_date)}`))))
      : h("div", { class: "muted" }, "No requests waiting for you."),
    S.approvals.length ? h("button", { class: "btn soft block", style: "margin-top:12px", onclick: () => go("approvals") }, "Review requests") : null);
}

function orgWidget() {
  const s = S.hr || {};
  return h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", {}, "Organisation")),
    h("div", { class: "mini-bal" }, h("div", {}, h("b", {}, num(s.pending)), h("span", {}, "Pending")),
      h("div", {}, h("b", {}, num(s.on_leave_today)), h("span", {}, "On leave")), h("div", {}, h("b", {}, num(s.headcount)), h("span", {}, "Headcount"))),
    h("button", { class: "btn soft block", onclick: () => go("hr") }, "Open all leaves"));
}

// ------------------------------------------------------------------ my leave
const BAL_STYLE = { CL: ["sun", "ic-amber"], SL: ["heart", "ic-coral"], EL: ["leave", "ic-green"] };
function balanceMetrics(balances) {
  return h("div", { class: "metrics" }, ...balances.map((b) => {
    const [ic, cls] = BAL_STYLE[b.leave_type] || ["cal", "ic-blue"];
    return h("div", { class: "metric" }, h("div", { class: `ic ${cls}` }, icon(ic)),
      h("div", {}, h("div", { class: "v" }, `${num(b.available)} days`), h("div", { class: "l" }, `${b.name} · ${num(b.used)} used${Number(b.pending) ? ` · ${num(b.pending)} pending` : ""}`)));
  }));
}
function requestsTable(rows, withEmployee = false) {
  if (!rows.length) return h("div", { class: "empty" }, icon("inbox"), h("div", {}, "No leave requests."));
  return h("div", { class: "table-wrap" }, h("table", { class: "data" },
    h("thead", {}, h("tr", {}, withEmployee ? h("th", {}, "Employee") : null, h("th", {}, "Request"), h("th", {}, "Type"), h("th", {}, "Dates"),
      h("th", {}, "Days"), h("th", {}, "Status"), h("th", {}, "Approver"), h("th", {}, "Applied"))),
    h("tbody", {}, ...rows.map((r) => h("tr", {},
      withEmployee ? h("td", {}, h("div", { class: "person" }, h("div", { class: "avatar sm soft" }, initials(r.employee)),
        h("div", {}, h("div", { class: "n" }, r.employee), h("div", { class: "d" }, r.department)))) : null,
      h("td", { class: "muted small" }, r.id), h("td", {}, TYPE[r.leave_type]), h("td", {}, fmtRange(r.start_date, r.end_date)), h("td", {}, num(r.days)),
      h("td", {}, pill(r.status), r.decision_note ? h("div", { class: "small muted", style: "margin-top:4px" }, `“${r.decision_note}”`) : null),
      h("td", {}, r.approver), h("td", { class: "muted" }, fmtDate(r.created_at, true)))))));
}
function pageLeave(c) {
  c.append(h("div", { class: "page-h" }, h("h1", {}, "My leave"), h("button", { class: "btn primary", onclick: () => ask("I want to apply for leave") }, icon("plus"), "Apply for leave")),
    balanceMetrics(S.balances),
    h("div", { class: "two-col" },
      h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", {}, "My requests")), requestsTable(S.requests)),
      h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", {}, "Notifications"),
          S.notes.unread ? h("button", { class: "link-btn", onclick: async () => { await api("POST", "/notifications/read", {}); await refresh(); } }, "Mark all read") : null),
        S.notes.items.length ? S.notes.items.slice(0, 8).map(noteRow) : h("div", { class: "empty" }, "Nothing new."))));
}

// ------------------------------------------------------------------ approvals
function pageApprovals(c) {
  c.append(h("div", { class: "page-h" }, h("h1", {}, "Approvals"), h("span", { class: "muted" }, S.approvals.length ? `${plural(S.approvals.length, "request")} waiting for your decision` : "")));
  if (!S.approvals.length) { c.append(h("div", { class: "card", style: "max-width:1240px" }, h("div", { class: "empty" }, icon("checksq"), h("div", {}, "You're all caught up — no requests waiting for you.")))); return; }
  const list = h("div", { class: "stack", style: "max-width:1240px" });
  S.approvals.forEach((r) => {
    const meta = [r.balance_after !== null ? `Balance after: ${num(r.balance_after)}` : null, r.teammates_away ? `${plural(r.teammates_away, "teammate")} away` : null,
      `Applied ${fmtDate(r.created_at)}`, r.reason ? `“${r.reason}”` : null].filter(Boolean);
    list.append(h("div", { class: "card" }, h("div", { class: "approval" }, h("div", { class: "avatar" }, initials(r.employee)),
      h("div", { class: "body" }, h("div", { class: "t" }, r.employee, h("span", { class: "muted small", style: "font-weight:400" }, ` · ${r.designation}`)),
        h("div", { class: "what" }, `${TYPE[r.leave_type]} leave · ${fmtRange(r.start_date, r.end_date)} · ${plural(r.days, "day")}`),
        h("div", { class: "meta" }, ...meta.map((m) => h("span", {}, m)))),
      h("div", { class: "btns" }, h("button", { class: "btn primary", onclick: (e) => decide(r, "approve", null, e.currentTarget) }, icon("check"), "Approve"),
        h("button", { class: "btn danger", onclick: () => rejectModal(r) }, "Reject")))));
  });
  c.append(list);
}
async function decide(r, decision, comment, btn) {
  if (btn) btn.disabled = true;
  try {
    await api("POST", `/leave-requests/${r.id}/decision`, { decision, comment });
    toast(`${decision === "approve" ? "Approved" : "Rejected"} ${r.employee.split(" ")[0]}'s leave`);
    await refresh();
  } catch { if (btn) btn.disabled = false; }
}
function rejectModal(r) {
  const reason = h("textarea", { placeholder: "Reason (shared with the employee, optional)" });
  const close = () => back.remove();
  const back = h("div", { class: "modal-back", onclick: (e) => e.target === back && close() },
    h("div", { class: "modal" }, h("h3", {}, "Reject leave request"),
      h("div", { class: "muted" }, `${r.employee} · ${TYPE[r.leave_type]} leave · ${fmtRange(r.start_date, r.end_date)}`), reason,
      h("div", { class: "btns" }, h("button", { class: "btn", onclick: close }, "Cancel"),
        h("button", { class: "btn primary", onclick: async () => { close(); await decide(r, "reject", reason.value.trim() || null); } }, "Reject request"))));
  document.body.append(back); reason.focus();
}

// ------------------------------------------------------------------ HR: all leaves
const HRF = { q: "", department: "", status: "", role: "", tab: "requests", emp: null };
async function pageHR(c) {
  const s = S.hr || {};
  c.append(h("div", { class: "page-h" }, h("h1", {}, "All leaves")),
    h("div", { class: "metrics" },
      metric("inbox", "ic-amber", s.pending, "Pending requests"), metric("leave", "ic-green", s.on_leave_today, "On leave today"),
      metric("check", "ic-blue", s.approved_this_month, "Approved this month"), metric("users", "ic-coral", s.headcount, "Active headcount")));
  const card = h("div", { class: "card", style: "max-width:1240px" });
  const tabs = h("div", { class: "tabs" }, ...[["requests", "Requests"], ["employee", "By employee"]].map(([id, label]) =>
    h("button", { class: HRF.tab === id ? "active" : "", onclick: () => { HRF.tab = id; renderPage(); } }, label)));
  card.append(tabs);
  c.append(card);
  if (HRF.tab === "requests") await hrRequests(card); else await hrEmployee(card);
}
const metric = (ic, cls, v, l) => h("div", { class: "metric" }, h("div", { class: `ic ${cls}` }, icon(ic)), h("div", {}, h("div", { class: "v" }, num(v)), h("div", { class: "l" }, l)));

async function hrRequests(card) {
  const depts = await api("GET", "/departments");
  const out = h("div", {});
  const count = h("div", { class: "small muted", style: "margin-bottom:10px" });
  const load = async () => {
    const qs = new URLSearchParams({ limit: "1000" });
    for (const k of ["q", "department", "status", "role"]) if (HRF[k]) qs.set(k, HRF[k]);
    const rows = await api("GET", `/hr/leave-requests?${qs}`);
    count.textContent = plural(rows.length, "request"); out.innerHTML = ""; out.append(requestsTable(rows, true));
  };
  let tmr;
  const sel = (key, label, opts) => h("select", { class: "input", onchange: (e) => { HRF[key] = e.target.value; load(); } },
    h("option", { value: "" }, label), ...opts.map(([v, t]) => h("option", { value: v, ...(HRF[key] === v ? { selected: true } : {}) }, t)));
  card.append(h("div", { class: "filters" },
    h("div", { class: "search" }, icon("search"), h("input", { class: "input", placeholder: "Search name or request ID", value: HRF.q,
      oninput: (e) => { HRF.q = e.target.value; clearTimeout(tmr); tmr = setTimeout(load, 300); } })),
    sel("department", "All departments", depts.map((d) => [d, d])),
    sel("status", "Any status", ["pending", "approved", "rejected", "cancelled"].map((s) => [s, s[0].toUpperCase() + s.slice(1)])),
    sel("role", "Everyone", [["manager", "Managers"], ["employee", "Employees"], ["hr_admin", "HR"]])), count, out);
  await load();
}

async function hrEmployee(card) {
  const people = await api("GET", "/employees");
  const list = h("div", { class: "picker", style: "max-height:220px" });
  const detail = h("div", { style: "margin-top:18px" });
  const show = async (p) => {
    HRF.emp = p;
    const d = await api("GET", `/employees/${p.employee_id}/leave`);
    detail.innerHTML = "";
    detail.append(h("div", { class: "person", style: "margin-bottom:14px" }, h("div", { class: "avatar" }, initials(p.name)),
      h("div", {}, h("div", { class: "n" }, p.name), h("div", { class: "d" }, `${p.designation} · ${p.department} · ${p.role.replace("_", " ")}`))),
      balanceMetrics(d.balances), requestsTable(d.requests));
  };
  const draw = (q) => {
    list.innerHTML = ""; const ql = q.toLowerCase();
    people.filter((p) => !ql || `${p.name} ${p.department} ${p.designation}`.toLowerCase().includes(ql)).slice(0, 50).forEach((p) =>
      list.append(h("button", { class: HRF.emp?.employee_id === p.employee_id ? "sel" : "", onclick: () => { show(p); draw(q); } },
        h("div", { class: "avatar sm soft" }, initials(p.name)), h("div", {}, h("div", { style: "font-weight:600;color:var(--navy)" }, p.name),
          h("div", { class: "small muted" }, `${p.designation} · ${p.department}`)), h("span", { class: "role-tag" }, p.role.replace("_", " ")))));
  };
  card.append(h("div", { class: "search" }, icon("search"), h("input", { class: "input", style: "width:100%", placeholder: "Find an employee or manager", oninput: (e) => draw(e.target.value) })), list, detail);
  draw("");
  if (HRF.emp) show(HRF.emp);
}

// ------------------------------------------------------------------ boot
async function boot() {
  if (!S.token) return renderLogin();
  try {
    S.me = await api("GET", "/me", undefined, { quiet: true });
    if (!S.sessionId) { S.sessionId = (await api("POST", "/sessions")).session_id; store.set("session", S.sessionId); }
    await loadCore();
    if ((S.page === "approvals" && !isMgr()) || (S.page === "hr" && !isHR())) S.page = "home";
    renderShell();
  } catch (e) { if (e.message !== "auth") { store.clear(); S.token = null; renderLogin(); } }
}
boot();
