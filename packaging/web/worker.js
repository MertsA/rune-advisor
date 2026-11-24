// The web planner's worker: CPython (Pyodide) with the planner package, one planner call at a time.
//
// The page (planner.js) sends {id, op, ...} and gets back {id, type: "done", value} or {id, type: "error",
// error}; while a planner call runs it also gets {id, type: "progress", done, total, message}. A running call
// blocks this worker, so the page keeps the job table and cancels by ending the worker.
//
// manifest.json (from our own servers) lists the files. The big ones may come from a CDN (`assets`): the wheels
// and the wiki tables are fetched with the hash the manifest gives (`integrity`), so the browser refuses a file
// that differs from the one built. Pyodide's own files are loaded by Pyodide's loader, unchecked.
"use strict";

importScripts("pyodide.js"); // a worker may only import scripts from the page's own site

const HOME = "/home/pyodide";
let web = null; // rune_advisor.app.web, once loaded

function note(message) {
  postMessage({ type: "status", message });
}

// One file from the manifest ({path, integrity}), refused unless it is the file the manifest names.
async function bytes(base, entry) {
  let response;
  try {
    response = await fetch(new URL(entry.path, base), { integrity: entry.integrity, credentials: "omit" });
  } catch (e) { // the browser says no more than "failed to fetch" for both
    throw new Error(`${entry.path.split("/").pop()} could not be downloaded, or was not the file this version expects`);
  }
  if (!response.ok) throw new Error(`${entry.path}: ${response.status}`);
  return new Uint8Array(await response.arrayBuffer());
}

async function init(cfg) {
  const started = performance.now();
  note("starting Python");
  const manifest = await (await fetch("manifest.json", { cache: "no-cache" })).json();
  const base = new URL(manifest.assets, self.location.href);
  const py = await loadPyodide({ indexURL: new URL(manifest.pyodide, base).href });
  const site = py.runPython("import site; site.getsitepackages()[0]");
  note("loading the planner");
  const wheels = await Promise.all(manifest.wheels.map((w) => bytes(base, w)));
  for (const wheel of wheels) py.unpackArchive(wheel, "wheel", { extractDir: site });
  // The wiki's tables and the quest list, fetched when the site was built: the planner's HTTP cache.
  py.FS.mkdirTree(`${HOME}/.cache/rune-advisor`);
  py.unpackArchive(await bytes(base, manifest.cache), "zip", { extractDir: `${HOME}/.cache/rune-advisor` });
  // The two files the desktop planner reads, as the page keeps them.
  py.FS.mkdirTree(`${HOME}/.config/rune-advisor`);
  py.FS.writeFile(`${HOME}/.config/rune-advisor/rune_advisor.toml`, cfg.settings || "");
  if (cfg.overrides) py.FS.writeFile(`${HOME}/.config/rune-advisor/overrides.toml`, cfg.overrides);
  const env = py.pyimport("os").environ;
  env.set("RUNE_ADVISOR_API_URL", cfg.api);
  env.set("RUNE_ADVISOR_API_TOKEN", cfg.token);
  web = py.pyimport("rune_advisor.app.web");
  return { seconds: (performance.now() - started) / 1000, version: manifest.version, tables: manifest.tables_fetched,
           meta: JSON.parse(web.meta()) };
}

function run(id, msg) {
  const progress = (done, total, message) => postMessage({ id, type: "progress", done, total, message });
  const reply = JSON.parse(web.run(msg.view, JSON.stringify(msg.body), !!msg.refresh, progress));
  if (reply.error !== undefined) throw new Error(reply.error);
  return reply.result;
}

self.onmessage = async (event) => {
  const msg = event.data;
  try {
    let value;
    if (msg.op === "init") value = await init(msg);
    else if (msg.op === "run") value = run(msg.id, msg);
    else if (msg.op === "forget") value = web.forget();
    else throw new Error(`unknown op ${msg.op}`);
    postMessage({ id: msg.id, type: "done", value });
  } catch (e) {
    postMessage({ id: msg.id, type: "error", error: String((e && e.message) || e) });
  }
};
