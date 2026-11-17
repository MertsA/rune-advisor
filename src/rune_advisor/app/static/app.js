// Rune Advisor planner frontend. Plain JS, no build step, no network beyond this server.
// Every string from the planner goes into the DOM as text nodes (see `h`), never as markup.
"use strict";

const POLL_MS = 700;
const state = { player: "", meta: null, tab: "profile", job: {}, loaded: {} };

// Build an element; string children become text nodes, so planner text can't inject markup.
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// The web planner (packaging/web/planner.js) answers these calls in the page instead of a local server.
async function api(method, path, body) {
  if (window.raTransport) return window.raTransport(method, path, body);
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
  if (!res.ok) throw new Error((data && data.error) || `request failed (${res.status})`);
  return data;
}

const fmtH = (x) => (x == null ? "-" : x < 0.1 ? "<0.1h" : `${x.toFixed(1)}h`);
const fmtN = (x) => (x == null ? "-" : Number(x).toLocaleString());
const $ = (sel, root = document) => root.querySelector(sel);

// ---- jobs ----

function setStatus(view, ...nodes) {
  const box = $(`#panel-${view} [data-status]`);
  box.replaceChildren(...nodes.filter(Boolean));
}

// Start a planner job and poll it; resolves with the result, rejects with a readable error.
async function runJob(view, options, refresh) {
  if (state.job[view]) { await api("POST", `/api/jobs/${state.job[view]}/cancel`, {}).catch(() => {}); }
  const job = await api("POST", "/api/jobs", { view, player: state.player, options, refresh: !!refresh });
  state.job[view] = job.id;
  const cancel = h("button", { type: "button" }, "Cancel");
  cancel.addEventListener("click", () => api("POST", `/api/jobs/${job.id}/cancel`, {}).catch(() => {}));
  const bar = h("progress", { max: 1, value: 0, "aria-label": "planning progress" });
  const msg = h("span", {}, "starting");
  setStatus(view, bar, msg, cancel);
  for (;;) {
    const j = await api("GET", `/api/jobs/${job.id}`);
    if (state.job[view] !== job.id) throw new Error("superseded");
    const p = j.progress;
    if (p.total) { bar.max = p.total; bar.value = p.done; } else { bar.removeAttribute("value"); }
    msg.textContent = `${p.message || j.state}  (${j.elapsed}s)`;
    if (j.state === "done") { state.job[view] = null; return j; }
    if (j.state === "error") { state.job[view] = null; throw new Error(j.error); }
    if (j.state === "cancelled") { state.job[view] = null; throw new Error("cancelled"); }
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}

async function load(view, options, refresh) {
  const out = $(`#panel-${view} [data-out]`);
  try {
    const j = await runJob(view, options, refresh);
    setStatus(view, h("span", {}, j.cached ? "Cached result. " : `Planned in ${j.elapsed}s. `),
      refreshButton(view));
    out.replaceChildren(RENDER[view](j.result));
    state.loaded[view] = true;
  } catch (e) {
    if (e.message === "superseded") return;
    setStatus(view, h("span", { class: "error", role: "alert" }, e.message === "cancelled" ? "Cancelled." : `Could not plan: ${e.message}`));
  }
}

function refreshButton(view) {
  const b = h("button", { type: "button" }, "Refresh");
  b.title = "Re-read the player's data and plan again";
  b.addEventListener("click", () => load(view, optionsFor(view), true));
  return b;
}

// ---- forms ----

function optionsFor(view) {
  const f = $(`#panel-${view} form`);
  const get = (n) => f.elements[n];
  if (view === "guide") {
    const tracks = [...f.querySelectorAll("[data-tracks] input:checked")].map((i) => i.value);
    const all = f.querySelectorAll("[data-tracks] input").length;
    return { tracks: tracks.length === all ? [] : tracks, mood: get("mood").value || null,
             session: get("session").value || null, top: get("top").value || 10 };
  }
  if (view === "next") return { hours: get("hours").value || 2, mood: get("mood").value || null, replan: get("replan").checked };
  if (view === "focus") return { goal: get("goal").value };
  return {};
}

// ---- renderers ----

function chips(entry) {
  return [...entry.tags.map((t) => h("span", { class: "chip" }, t)),
    entry.confidence !== "modelled" ? h("span", { class: "chip warn" }, entry.confidence) : null];
}

function list(items) { return h("ul", {}, items.map((x) => h("li", {}, x))); }

function entryNode(e, extra) {
  const detail = h("div", { class: "detail" }, h("dl", {},
    e.why ? [h("dt", {}, "Why"), h("dd", {}, e.why)] : null,
    e.left.length ? [h("dt", {}, "Left"), h("dd", {}, list(e.left))] : null,
    e.unsure.length ? [h("dt", {}, "Unsure"), h("dd", {}, list(e.unsure))] : null,
    e.prep.length ? [h("dt", {}, "Prep"), h("dd", {}, list(e.prep))] : null,
    extra && extra.shares && extra.shares.length ? [h("dt", {}, "Shared with earlier picks"), h("dd", {}, list(extra.shares))] : null,
    e.steps.length ? [h("dt", {}, "Steps"), h("dd", {}, list(e.steps))] : null));
  const hours = Math.abs(e.play_hours - e.hours) >= 0.05 ? `${fmtH(e.play_hours)} to play, ${fmtH(e.hours)} net` : fmtH(e.hours);
  return h("li", {}, h("details", { class: "entry" },
    h("summary", {}, h("span", { class: "head" }, e.headline), h("span", { class: "meta" }, `${hours}  ${e.value}`),
      h("span", { class: "meta" }, e.track), chips(e)),
    detail));
}

function notesNode(notes) {
  return notes && notes.length ? h("div", { class: "notes" }, h("h3", {}, "Notes"), list(notes)) : null;
}

const RENDER = {
  profile(p) {
    const journal = p.journal_quest_points != null ? ` (journal ${p.journal_quest_points})` : "";
    const bonus = p.skills.some((s) => s.bonus_xp);
    return h("div", {},
      h("div", { class: "summary" }, h("span", {}, `Quest points ${p.quest_points}/${p.max_quest_points}${journal}`),
        h("span", {}, `RuneScore ${fmtN(p.runescore)}`), h("span", {}, p.ironman ? "Ironman" : "Regular"),
        h("span", {}, p.members ? "Members" : "Free-to-play"),
        p.rested_xp != null ? h("span", {}, `Rested xp ${fmtN(p.rested_xp)}`) : null),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Skill"), h("th", {}, "Level"), h("th", {}, "XP"), h("th", {}, "Rank"),
        bonus ? h("th", {}, "Bonus XP") : null)),
        h("tbody", {}, p.skills.map((s) => h("tr", {}, h("td", {}, s.name), h("td", {}, s.level), h("td", {}, fmtN(s.xp)),
          h("td", {}, fmtN(s.rank)), bonus ? h("td", {}, fmtN(s.bonus_xp)) : null)))),
      notesNode(p.notes));
  },

  guide(g) {
    return h("div", {}, g.tracks.map((t) => h("section", {},
      h("h3", {}, t.track, " ", h("span", { class: "muted" }, `- ${t.description}`)),
      t.entries.length ? h("ol", { class: "entries" }, t.entries.map((e) => entryNode(e)))
        : h("p", { class: "muted" }, "Nothing here (missing data, or filtered out)."),
      t.total > t.entries.length ? h("p", { class: "muted" }, `${t.total - t.entries.length} more not shown.`) : null)),
      notesNode(g.notes));
  },

  next(n) {
    return h("div", {},
      h("p", {}, `${n.hours} h session, about ${n.planned_hours.toFixed(1)} h planned.`),
      n.picks.length ? h("ol", { class: "entries" }, n.picks.map((p) => entryNode(p, p)))
        : h("p", { class: "muted" }, "Nothing fits: try more hours or another mood."),
      n.prep.length ? h("div", {}, h("h3", {}, "Prep for the session"), list(n.prep)) : null,
      notesNode(n.notes));
  },

  focus(r) {
    const show = (f) => h("section", {},
      h("h3", {}, f.goal), h("p", {}, f.summary),
      f.other_needs.length ? h("div", {}, h("strong", {}, "Needs"), list(f.other_needs)) : null,
      f.route.length ? h("div", {}, h("strong", {}, "Route"), list(f.route)) : null,
      f.prep.length ? h("div", {}, h("strong", {}, "Prep"), list(f.prep)) : null,
      f.any_method.length ? h("p", {}, h("strong", {}, "Any method also: "), f.any_method.join("; ")) : null,
      f.options.length ? h("ol", { class: "entries" }, f.options.slice(0, 12).map((o) => h("li", {}, h("details", { class: "entry", open: !!o.extras.length },
        h("summary", {}, h("span", { class: "head" }, o.activity), h("span", { class: "meta" }, `${o.rate}${o.hours != null ? "  " + fmtH(o.hours) : ""}`)),
        o.extras.length ? h("div", { class: "detail" }, h("dl", {}, h("dt", {}, "Also advances"), h("dd", {}, list(o.extras)))) : null)))) : null,
      f.sections.map((s) => h("div", { class: "focus-section" }, show(s))));
    return h("div", {}, show(r.focus), notesNode(r.notes));
  },
};

// ---- shell ----

function selectTab(view, focus) {
  state.tab = view;
  for (const b of document.querySelectorAll(".tabs [role=tab]")) {
    const on = b.dataset.view === view;
    b.setAttribute("aria-selected", on);
    b.tabIndex = on ? 0 : -1;
    if (on && focus) b.focus();
  }
  for (const p of document.querySelectorAll("[role=tabpanel]")) p.hidden = p.dataset.view !== view;
  if (view === "profile" && !state.loaded.profile) load("profile", {}, false);
}

function openPlayer(name) {
  state.player = name;
  state.loaded = {};
  for (const o of document.querySelectorAll("[data-out]")) o.replaceChildren();
  for (const s of document.querySelectorAll("[data-status]")) s.replaceChildren();
  $("#welcome").hidden = true;
  $("#workspace").hidden = false;
  $("#who").hidden = false;
  $("#who-name").textContent = name;
  api("POST", "/api/prefs", { last_player: name }).catch(() => {});
  selectTab("profile");
}

function showWelcome() {
  for (const v of Object.keys(state.job)) if (state.job[v]) api("POST", `/api/jobs/${state.job[v]}/cancel`, {}).catch(() => {});
  state.job = {};
  $("#welcome").hidden = false;
  $("#workspace").hidden = true;
  $("#who").hidden = true;
  $("#player-input").value = state.player;
  $("#player-input").focus();
}

async function init() {
  try {
    state.meta = await api("GET", "/api/meta");
  } catch (e) {
    const err = $("#welcome-error");
    err.hidden = false;
    err.textContent = `Could not reach the planner: ${e.message}`;
    return;
  }
  for (const sel of document.querySelectorAll("select[name=mood]"))
    for (const m of state.meta.moods) sel.append(h("option", { value: m }, m));
  const box = $("[data-tracks]");
  for (const [name, desc] of Object.entries(state.meta.tracks))
    box.append(h("label", { title: desc }, h("input", { type: "checkbox", value: name, checked: true }), name));
  for (const f of document.querySelectorAll("form.controls"))
    f.addEventListener("submit", (ev) => { ev.preventDefault(); load(f.dataset.view, optionsFor(f.dataset.view), false); });
  $("#player-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const name = $("#player-input").value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9 _-]{0,11}$/.test(name)) {
      const err = $("#welcome-error");
      err.hidden = false;
      err.textContent = "Use a RuneScape name: letters, digits, spaces, - and _ (up to 12).";
      return;
    }
    $("#welcome-error").hidden = true;
    openPlayer(name);
  });
  $("#change-player").addEventListener("click", showWelcome);
  const tabs = [...document.querySelectorAll(".tabs [role=tab]")];
  tabs.forEach((b, i) => {
    b.addEventListener("click", () => selectTab(b.dataset.view));
    b.addEventListener("keydown", (ev) => {
      const d = { ArrowRight: 1, ArrowLeft: -1 }[ev.key];
      if (d) { ev.preventDefault(); selectTab(tabs[(i + d + tabs.length) % tabs.length].dataset.view, true); }
    });
  });
  const last = (state.meta.prefs || {}).last_player;
  if (last) state.player = last;
  showWelcome();
}

init();
