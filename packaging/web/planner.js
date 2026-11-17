// The web planner's page side. It plays the desktop app's local server for app.js (window.raTransport answers
// the same /api/... calls) and runs the planner in a worker (worker.js).
//
// Rules this file keeps:
// - The page holds a read token, never the account key. The account page makes the token and leaves it in
//   sessionStorage (site/assets/account.js); it is gone when the tab closes. Without one, or when the API
//   refuses it, the page sends the user to the account page.
// - The token travels only in the Authorization header, to the one API host in <meta name="api-base">.
// - Settings and overrides (the desktop planner's two files) stay in this browser's localStorage.
// - No inline script or style, nothing loaded from another host.
"use strict";

(function () {
  const API = document.querySelector('meta[name="api-base"]').content.replace(/\/+$/, "");
  const TOKEN = "ra-planner-token";
  const ACCOUNT = "/account/#planner";
  const $ = (id) => document.getElementById(id);

  const stored = (name) => { try { return localStorage.getItem(name) || ""; } catch (e) { return ""; } };
  const store = (name, value) => { try { localStorage.setItem(name, value); } catch (e) { /* private mode */ } };

  let token = null;
  try { token = sessionStorage.getItem(TOKEN); } catch (e) { /* no storage: treated as signed out */ }

  function signOut() {
    try { sessionStorage.removeItem(TOKEN); } catch (e) { /* nothing stored */ }
    location.replace(ACCOUNT);
  }

  if (!token) {
    window.raTransport = () => new Promise(() => {}); // app.js waits while the page leaves
    signOut();
    return;
  }

  // ---- the worker ----

  let worker = null;
  let ready = null; // resolves to the worker's init answer
  let nextCall = 1;
  const calls = new Map(); // call id -> {resolve, reject, progress}

  function engine(text) {
    $("engine").textContent = text;
  }

  function call(op, fields, progress) {
    return new Promise((resolve, reject) => {
      const id = nextCall++;
      calls.set(id, { resolve, reject, progress });
      worker.postMessage({ id, op, ...fields });
    });
  }

  function start() {
    worker = new Worker("worker.js");
    worker.onmessage = (event) => {
      const m = event.data;
      if (m.type === "status") return engine(`Starting the planner: ${m.message}...`);
      const c = calls.get(m.id);
      if (!c) return;
      if (m.type === "progress") return c.progress && c.progress(m);
      calls.delete(m.id);
      if (m.type === "done") c.resolve(m.value); else c.reject(new Error(m.error));
    };
    worker.onerror = (event) => {
      engine("The planner failed to start.");
      for (const c of calls.values()) c.reject(new Error(event.message || "the planner stopped"));
      calls.clear();
    };
    ready = call("init", { api: API, token, settings: stored("ra-settings"), overrides: stored("ra-overrides") });
    ready.then((info) => engine(`Planner ready (started in ${info.seconds.toFixed(1)} s).`),
      (e) => engine(`The planner failed to start: ${e.message}`));
  }

  function restart() {
    worker.terminate();
    for (const c of calls.values()) c.reject(new Error("cancelled"));
    calls.clear();
    start();
  }

  // ---- jobs: what the desktop app's JobManager does, in the page ----

  const jobs = new Map(); // id -> job
  const cache = new Map(); // request key -> result
  let line = Promise.resolve(); // one planner call at a time

  function snapshot(job, withResult) {
    const out = { id: job.id, view: job.view, state: job.state, progress: job.progress, cached: job.cached,
                  elapsed: Math.round(((job.finished || Date.now()) - job.created) / 100) / 10 };
    if (job.state === "error") out.error = job.error;
    if (job.state === "done" && withResult) out.result = job.result;
    return out;
  }

  function submit(body) {
    const key = JSON.stringify([body.view, body.player, body.options || {}]);
    const job = { id: String(nextCall++), view: body.view, state: "queued", cached: false, created: Date.now(),
                  progress: { done: 0, total: 0, message: "" } };
    jobs.set(job.id, job);
    if (!body.refresh && cache.has(key)) {
      Object.assign(job, { state: "done", cached: true, result: cache.get(key), finished: Date.now() });
      return job;
    }
    line = line.then(async () => {
      if (job.state !== "queued") return; // cancelled while waiting
      try {
        await ready;
        if (job.state !== "queued") return;
        job.state = "running";
        job.result = await call("run", { view: body.view, body, refresh: !!body.refresh },
          (p) => { job.progress = { done: p.done, total: p.total, message: p.message }; });
        if (job.state !== "running") return; // cancelled meanwhile
        cache.set(key, job.result);
        job.state = "done";
      } catch (e) {
        if (job.state === "cancelled") return;
        job.state = "error";
        job.error = e.message;
      } finally {
        job.finished = job.finished || Date.now();
      }
    });
    return job;
  }

  function cancel(job) {
    if (job.state !== "queued" && job.state !== "running") return;
    const running = job.state === "running";
    job.state = "cancelled";
    job.finished = Date.now();
    if (running) restart(); // a running call cannot be interrupted: end the worker and start a fresh one
  }

  // ---- the calls app.js makes ----

  window.raTransport = async (method, path, body) => {
    if (method === "GET" && path === "/api/meta") {
      const info = await ready;
      return { ...info.meta, prefs: { last_player: stored("ra-last-player") } };
    }
    if (method === "POST" && path === "/api/prefs") {
      if (body.last_player) store("ra-last-player", body.last_player);
      return {};
    }
    if (method === "POST" && path === "/api/jobs") return snapshot(submit(body), false);
    if (method === "POST" && path === "/api/cache/clear") {
      cache.clear();
      await call("forget", {});
      return { ok: true };
    }
    const job = jobs.get(path.split("/")[3]);
    if (!job) throw new Error("no such job");
    if (method === "POST" && path.endsWith("/cancel")) cancel(job);
    return snapshot(job, method === "GET");
  };

  // ---- the page ----

  // The characters this token may read: checks the token on the way.
  async function listCharacters() {
    let response;
    try {
      response = await fetch(API + "/v1/players", { headers: { Authorization: "Bearer " + token },
                                                   credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer" });
    } catch (e) {
      $("characters-hint").textContent = "Could not reach the Rune Advisor API. Reload to try again.";
      return;
    }
    if (response.status === 401 || response.status === 403) return signOut();
    const names = ((await response.json()).players || []).map((p) => p.player).filter(Boolean).sort();
    for (const name of names) {
      const option = document.createElement("option");
      option.value = name;
      $("characters").append(option);
    }
    if (!names.length) {
      $("characters-hint").textContent = "No character has posted to this account yet: install the Rune Advisor "
        + "plugin, give it a token from the account page and log in to the game once.";
    } else if (!$("player-input").value) {
      $("player-input").value = names.includes(stored("ra-last-player")) ? stored("ra-last-player") : names[0];
    }
  }

  $("sign-out").addEventListener("click", signOut);
  $("settings-text").value = stored("ra-settings");
  $("overrides-text").value = stored("ra-overrides");
  $("settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    store("ra-settings", $("settings-text").value);
    store("ra-overrides", $("overrides-text").value);
    cache.clear();
    restart();
    $("settings-status").textContent = "Saved.";
  });

  start();
  listCharacters();
})();
