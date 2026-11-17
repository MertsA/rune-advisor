// The web planner's worker: CPython (Pyodide) with the planner package, one planner call at a time.
//
// The page (planner.js) sends {id, op, ...} and gets back {id, type: "done", value} or {id, type: "error",
// error}; while a planner call runs it also gets {id, type: "progress", done, total, message}. A running call
// blocks this worker, so the page keeps the job table and cancels by ending the worker.
//
// Everything is loaded from this site (manifest.json lists the files): nothing comes from a CDN.
"use strict";

importScripts("pyodide/pyodide.js");

const HOME = "/home/pyodide";
let web = null; // rune_advisor.app.web, once loaded

function note(message) {
  postMessage({ type: "status", message });
}

async function bytes(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return new Uint8Array(await response.arrayBuffer());
}

async function init(cfg) {
  const started = performance.now();
  note("starting Python");
  const manifest = await (await fetch("manifest.json")).json();
  const py = await loadPyodide({ indexURL: new URL("pyodide/", self.location.href).href });
  const site = py.runPython("import site; site.getsitepackages()[0]");
  note("loading the planner");
  const wheels = await Promise.all(manifest.wheels.map((w) => bytes("wheels/" + w)));
  for (const wheel of wheels) py.unpackArchive(wheel, "wheel", { extractDir: site });
  // The wiki's tables and the quest list, fetched when the site was built: the planner's HTTP cache.
  py.FS.mkdirTree(`${HOME}/.cache/rune-advisor`);
  py.unpackArchive(await bytes(manifest.cache), "zip", { extractDir: `${HOME}/.cache/rune-advisor` });
  // The two files the desktop planner reads, as the page keeps them.
  py.FS.mkdirTree(`${HOME}/.config/rune-advisor`);
  py.FS.writeFile(`${HOME}/.config/rune-advisor/rune_advisor.toml`, cfg.settings || "");
  if (cfg.overrides) py.FS.writeFile(`${HOME}/.config/rune-advisor/overrides.toml`, cfg.overrides);
  const env = py.pyimport("os").environ;
  env.set("RUNE_ADVISOR_API_URL", cfg.api);
  env.set("RUNE_ADVISOR_API_TOKEN", cfg.token);
  web = py.pyimport("rune_advisor.app.web");
  return { seconds: (performance.now() - started) / 1000, version: manifest.version, meta: JSON.parse(web.meta()) };
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
