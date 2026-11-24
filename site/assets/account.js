// The account page: everything an account key can do, done from the browser against the Rune Advisor API.
//
// Rules this file keeps:
// - The key lives in one variable (`key`) for as long as the page is open. It is never written to
//   localStorage, sessionStorage, a cookie, the address bar or the console.
// - Tokens made here are derived from the key (tokenkeys.js): the page picks an id, derives the value and sends
//   it to be registered, so it can show any of them again later. A read token's copy for a planner carries,
//   after a dot, the key to that planner's settings blob; the part after the dot is never sent anywhere.
// - "Open the web planner" (shown only where the page names a planner, <meta name="planner">) derives the
//   account's web planner token and leaves it, never the key, in sessionStorage for the planner page: it is
//   gone when the tab closes, so the next visit needs the key again. Every visit derives the same token.
// - A token we retired after a leak (pending) gets its next value here at the next sign-in, and its settings
//   blob is re-encrypted for it: only this page, holding the key, can do either.
// - Keys and tokens travel only in the Authorization header, to the one API host named in the page's
//   <meta name="api-base">. Nothing else is contacted.
// - Text from the API is put on the page with textContent, never as HTML.
// - No inline script or style: the site's Content-Security-Policy forbids both.
"use strict";

(function () {
  const API = document.querySelector('meta[name="api-base"]').content.replace(/\/+$/, "");
  const $ = (id) => document.getElementById(id);

  const K = window.RATokenKeys;
  let key = null; // the account key, in memory only

  // --- talking to the API ---------------------------------------------------------------------------------

  // One request. Resolves to {status, body}; network failures resolve to status 0 with a readable reason.
  // `useKey`: true (default) sends the account key, false nothing, a string that bearer value instead.
  async function api(method, path, body, useKey, player, tokenId) {
    const headers = {};
    // So does a token's id: nothing that says whose request it is goes in the address.
    if (tokenId) headers["X-Token-Id"] = tokenId;
    // A character's name goes in a header, never in the address, so web server logs do not record it.
    if (player) headers["X-Player"] = encodeURIComponent(player);
    if (typeof useKey === "string") headers["Authorization"] = "Bearer " + useKey;
    else if (useKey !== false && key) headers["Authorization"] = "Bearer " + key;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    try {
      const response = await fetch(API + path, {
        method: method,
        headers: headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "omit",
        cache: "no-store",
        referrerPolicy: "no-referrer",
      });
      let parsed = {};
      try {
        parsed = await response.json();
      } catch (e) {
        // The web server's own error pages (rate limit, maintenance) are not JSON.
        parsed = { reason: response.status === 429 ? "too many requests; wait a little" : "unexpected reply" };
      }
      return { status: response.status, body: parsed };
    } catch (e) {
      return { status: 0, body: { reason: "could not reach the API" } };
    }
  }

  function reason(result) {
    return (result.body && result.body.reason) || "error " + result.status;
  }

  // --- the bot check ---------------------------------------------------------------------------------------

  // Cloudflare Turnstile guards account creation. Its script is fetched only when someone asks for a new
  // account, so opening or managing an account never contacts Cloudflare. Resolves to the script's API, or
  // null when it could not be loaded (a content blocker, no network).
  const TURNSTILE_SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
  let turnstileScript = null;
  let turnstileWidget = null;

  function loadTurnstile() {
    if (!turnstileScript) {
      turnstileScript = new Promise((resolve) => {
        const script = document.createElement("script");
        script.src = TURNSTILE_SCRIPT;
        script.async = true;
        script.onload = () => resolve(window.turnstile || null);
        script.onerror = () => {
          turnstileScript = null; // let a later press try again
          resolve(null);
        };
        document.head.appendChild(script);
      });
    }
    return turnstileScript;
  }

  // Shows the check and resolves to its one-use answer for the API, or null when there is none (no site key
  // on the page, the script did not load, or the check failed). The API decides whether an answer is needed.
  async function botCheck() {
    const box = $("bot-check");
    const sitekey = box.dataset.sitekey;
    if (!sitekey) return null;
    const turnstile = await loadTurnstile();
    if (!turnstile) return null;
    if (turnstileWidget !== null) {
      turnstile.remove(turnstileWidget); // an answer works once: every press gets a fresh check
      turnstileWidget = null;
    }
    show("bot-check", true);
    return new Promise((resolve) => {
      turnstileWidget = turnstile.render(box, {
        sitekey: sitekey,
        callback: (answer) => resolve(answer),
        "error-callback": () => resolve(null),
      });
    });
  }

  // --- small page helpers ---------------------------------------------------------------------------------

  function status(id, text, kind) {
    const el = $(id);
    el.textContent = text || "";
    el.className = "status" + (kind ? " " + kind : "");
  }

  function show(id, visible) {
    $(id).hidden = !visible;
  }

  function cell(row, text) {
    const td = document.createElement("td");
    td.textContent = text;
    row.appendChild(td);
    return td;
  }

  function fact(list, name, value) {
    const dt = document.createElement("dt");
    dt.textContent = name;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.appendChild(dt);
    list.appendChild(dd);
  }

  // --- the account view -----------------------------------------------------------------------------------

  async function loadAccount() {
    const account = await api("GET", "/v1/account");
    if (account.status !== 200) return account;
    let tokens = await api("GET", "/v1/tokens");
    if (tokens.status !== 200) return tokens;
    if (await finishRotations(tokens.body.tokens)) {
      tokens = await api("GET", "/v1/tokens");
      if (tokens.status !== 200) return tokens;
    }

    const a = account.body;
    const facts = $("facts");
    facts.replaceChildren();
    fact(facts, "Account id", a.id);
    fact(facts, "Made", a.created);
    fact(facts, "Last used", a.last_active);
    fact(facts, "Characters", a.characters + " of " + a.max_characters + " allowed");
    fact(facts, "Tokens", a.tokens + " of " + a.max_tokens + " allowed");
    fact(facts, "Deleted if unused for", a.retention_days + " days");

    // The characters stored under the account, with the day each last posted.
    const held = a.players || [];
    const playerRows = $("players").tBodies[0];
    playerRows.replaceChildren();
    for (const p of held) {
      const row = playerRows.insertRow();
      cell(row, p.player);
      cell(row, p.last_post || "unknown");
      cell(row, String(p.posts == null ? "" : p.posts));
      consentCell(row, p, "consent-status", loadAccount);
    }
    show("players", held.length > 0);
    show("no-players", held.length === 0);

    const body = $("tokens").tBodies[0];
    body.replaceChildren();
    for (const t of tokens.body.tokens) {
      const row = body.insertRow();
      cell(row, (t.label || "(no label)") + (t.kind === "web" ? " (this site's planner)" : ""));
      cell(row, t.scopes.join(", "));
      // "May read": which characters a read token is limited to. A plugin's token reads nothing.
      cell(row, t.characters ? "only " + t.characters.join(", ")
        : t.scopes.includes("read") ? "every character" : "nothing (posts only)");
      // "Posts for": the characters this token's plugin has posted, with the day of the last post, so that
      // an unused or orphaned token can be told from the one in use before it is revoked.
      const posted = (t.posted || []).map((p) => p.player + " (" + p.last_post + ")").join(", ");
      cell(row, posted || (t.scopes.includes("submit") ? "nothing yet" : ""));
      cell(row, t.pending ? "being replaced" : t.prefix + "...");
      cell(row, t.created);
      cell(row, t.last_used || "never");
      const actions = cell(row, "");
      if (t.derived && !t.pending) {
        // Derived from the key, so it can be shown again, or replaced by a new value (the old one stops working).
        const reveal = document.createElement("button");
        reveal.type = "button";
        reveal.textContent = "Show";
        reveal.addEventListener("click", () => showToken(t, "Here it is again."));
        actions.appendChild(reveal);
        const renew = document.createElement("button");
        renew.type = "button";
        renew.textContent = "Replace";
        renew.addEventListener("click", () => replaceToken(t));
        actions.appendChild(renew);
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = "danger";
      button.textContent = "Revoke";
      button.addEventListener("click", () => revokeToken(t.id, t.label || t.prefix + "..."));
      actions.appendChild(button);
      if ((t.posted || []).length > 0) {
        // Deletes exactly the characters this token posted for, and revokes it so they are not posted again.
        const wipe = document.createElement("button");
        wipe.type = "button";
        wipe.className = "danger";
        wipe.textContent = "Delete what it posted";
        wipe.addEventListener("click", () => deletePosted(t.id, t.label || t.prefix + "...", t.posted));
        actions.appendChild(wipe);
      }
    }
    show("tokens", tokens.body.tokens.length > 0);
    show("no-tokens", tokens.body.tokens.length === 0);
    return account;
  }

  function closeAccount(message, kind) {
    key = null;
    $("key").value = "";
    $("new-key-value").value = "";
    $("new-token-value").value = "";
    $("delete-confirm").value = "";
    $("player-name").value = "";
    show("new-key", false);
    show("new-token", false);
    show("account", false);
    show("token-view", false);
    status("token-status", "");
    status("delete-status", "");
    status("signin-status", message || "", kind);
  }

  // The "Analysis" cell of a character's row: whether its data may be analysed to improve the planner, and
  // the button to change that. `p` is {player, consent, consent_since}; works with the account key or with a
  // plugin's token for the characters it posted. The name travels in the X-Player header.
  function consentCell(row, p, statusId, reload) {
    const td = cell(row, p.consent ? "allowed since " + p.consent_since + " " : "not allowed ");
    const flip = document.createElement("button");
    flip.type = "button";
    flip.textContent = p.consent ? "Stop allowing" : "Allow";
    flip.addEventListener("click", async () => {
      const result = await api("POST", "/v1/consent", { consent: !p.consent }, true, p.player);
      if (result.status !== 200) {
        status(statusId, "Could not change it: " + reason(result), "error");
        return;
      }
      status(statusId, (result.body.consent ? "Analysis allowed for " : "Analysis no longer allowed for ") + p.player +
        ". The plugin's settings show it after its next post.", "ok");
      await reload();
    });
    td.appendChild(flip);
  }

  // What a plugin's token posted, when the page was opened with such a token instead of the account key.
  async function loadTokenView() {
    const result = await api("GET", "/v1/posted");
    if (result.status !== 200) return result;
    const facts = $("token-facts");
    facts.replaceChildren();
    fact(facts, "Token", (result.body.token.label || "(no label)") + ", starts with " + result.body.token.prefix + "...");
    fact(facts, "Made", result.body.token.created);
    fact(facts, "Last used", result.body.token.last_used || "never");
    const rows = $("token-posted").tBodies[0];
    rows.replaceChildren();
    for (const p of result.body.posted) {
      const row = rows.insertRow();
      cell(row, p.player);
      cell(row, p.last_post);
      consentCell(row, p, "token-view-status", loadTokenView);
    }
    show("token-posted", result.body.posted.length > 0);
    show("token-posted-none", result.body.posted.length === 0);
    return result;
  }

  async function openAccount(candidate) {
    // A planner token pasted here: only its part before the dot is a credential.
    key = candidate.startsWith("rat_") ? K.split(candidate).bearer : candidate;
    status("signin-status", "Opening...");
    show("token-view", false);
    const result = await loadAccount();
    if (result.status === 200) {
      $("key").value = "";
      status("signin-status", "");
      show("account", true);
      // Sent here by the planner page (/account/#planner): go straight back to it with a token.
      if (PLANNER && location.hash === "#planner") openPlanner();
      return true;
    }
    show("account", false);
    if (result.status === 401) {
      // Not an account key. A plugin's token opens a smaller view: what it posted, and deleting that.
      const asToken = await loadTokenView();
      if (asToken.status === 200) {
        $("key").value = "";
        status("signin-status", "");
        status("token-view-status", "");
        show("token-view", true);
        return true;
      }
    }
    key = null;
    status("signin-status", result.status === 401 ? "That key is not recognised." : "Could not open the account: " + reason(result), "error");
    return false;
  }

  // --- actions --------------------------------------------------------------------------------------------

  $("open-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const candidate = $("key").value.trim();
    show("new-key", false);
    if (!candidate) {
      status("signin-status", "Paste your account key first.", "error");
      return;
    }
    await openAccount(candidate);
  });

  $("create-button").addEventListener("click", async () => {
    $("create-button").disabled = true;
    status("signin-status", "Checking that you are not a bot...");
    const answer = await botCheck();
    status("signin-status", "Making an account...");
    const result = await api("POST", "/v1/accounts", answer ? { captcha: answer } : {}, false);
    show("bot-check", false);
    $("create-button").disabled = false;
    if (result.status === 403 && !answer) {
      status("signin-status", "The bot check could not be loaded. If a content blocker is on, allow " +
        "challenges.cloudflare.com for this page and try again.", "error");
      return;
    }
    if (result.status !== 201) {
      status("signin-status", "Could not make an account: " + reason(result), "error");
      return;
    }
    $("new-key-value").value = result.body.key;
    show("new-key", true);
    await openAccount(result.body.key);
  });

  $("forget-button").addEventListener("click", () => closeAccount("Closed. The key was forgotten by this page."));

  // The characters field only applies to read tokens.
  for (const radio of document.querySelectorAll('input[name="scope"]')) {
    radio.addEventListener("change", () => show("characters-field", radio.value === "read" && radio.checked));
  }

  $("token-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const scope = document.querySelector('input[name="scope"]:checked').value;
    const id = K.randomId();
    const request = { id: id, token: await K.bearer(key, id, 0), label: $("token-label").value.trim(), scopes: [scope] };
    const characters = $("token-characters").value.split(",").map((c) => c.trim()).filter(Boolean);
    if (scope === "read" && characters.length) request.characters = characters;
    show("new-token", false);
    status("token-status", "Making a token...");
    const result = await api("POST", "/v1/tokens", request);
    if (result.status !== 201) {
      status("token-status", "Could not make a token: " + reason(result), "error");
      return;
    }
    $("token-label").value = "";
    $("token-characters").value = "";
    await showToken({ id: id, epoch: 0, scopes: [scope] },
      scope === "submit" ? "Made. Paste it into the plugin's settings." : "Made. Give it to the planner.");
    await loadAccount();
  });

  // --- derived tokens -------------------------------------------------------------------------------------

  // What a token's user pastes: the plugin's bearer value, or for a read token the planner token (with the
  // settings key after the dot).
  async function tokenValue(t) {
    return t.scopes.includes("read") ? K.plannerToken(key, t.id, t.epoch) : K.bearer(key, t.id, t.epoch);
  }

  async function showToken(t, message) {
    $("new-token-value").value = await tokenValue(t);
    show("new-token", true);
    status("token-status", message, "ok");
  }

  // Re-encrypt a token's settings blob for `epoch` after its value changed. Only this page can: the old
  // epoch's settings key comes from the account key. False when there was a blob and it could not be carried.
  async function carryBlob(t, epoch) {
    if (!t.blob || t.blob.epoch === epoch) return true;
    const old = await api("GET", "/v1/tokens/blob", undefined, true, null, t.id);
    if (old.status === 404) return true;
    if (old.status !== 200) return false;
    let settings;
    try {
      settings = await K.openBlob(await K.settingsKey(key, t.id, old.body.epoch), old.body.data);
    } catch (e) {
      return false; // not readable with this key: leave it, the planner starts from defaults
    }
    const data = await K.sealBlob(await K.settingsKey(key, t.id, epoch), settings);
    const wrote = await api("POST", "/v1/blob", { version: old.body.version, data: data }, await K.bearer(key, t.id, epoch));
    return wrote.status === 200;
  }

  // Tokens we retired after a leak wait as "pending" until this page gives them their next value; blobs left at
  // an older epoch are re-encrypted. True when anything changed (the caller lists the tokens again).
  async function finishRotations(tokens) {
    let changed = false;
    const replaced = [];
    for (const t of tokens) {
      if (!t.derived) continue;
      if (t.pending) {
        const done = await api("POST", "/v1/tokens/rotate", { epoch: t.epoch, token: await K.bearer(key, t.id, t.epoch) },
          true, null, t.id);
        if (done.status !== 200) continue;
        changed = true;
        if (t.kind !== "web") replaced.push(t.label || t.id.slice(0, 6));
      }
      if (t.blob && t.blob.epoch !== t.epoch && (await carryBlob(t, t.epoch))) changed = true;
    }
    if (replaced.length) {
      status("token-status", "For safety we replaced the value of: " + replaced.join(", ") +
        ". Use Show to copy the new value into whatever used the old one.", "ok");
    }
    return changed;
  }

  async function replaceToken(t) {
    const name = t.label || t.prefix + "...";
    if (!window.confirm('Replace the token "' + name + '"? The old value stops working at once; its settings carry over.')) return;
    const epoch = t.epoch + 1;
    const result = await api("POST", "/v1/tokens/rotate", { epoch: epoch, token: await K.bearer(key, t.id, epoch) },
      true, null, t.id);
    if (result.status !== 200) {
      status("token-status", "Could not replace it: " + reason(result), "error");
      return;
    }
    const carried = await carryBlob(t, epoch);
    await showToken({ ...t, epoch: epoch }, carried ? "Replaced. Copy the new value into whatever used the old one."
      : "Replaced, but its saved settings could not be carried over.");
    await loadAccount();
  }

  // --- the web planner ------------------------------------------------------------------------------------

  // The planner page (same site) reads characters with the account's web planner token (kind "web"). It gets it
  // here, so using it takes the account key: the key opens the account, this derives the token (making it the
  // first time) and hands it to the planner through sessionStorage, which lasts as long as this tab. Every tab
  // and browser derives the same token, so they share its settings and none of them signs another out.
  const PLANNER = (document.querySelector('meta[name="planner"]') || {}).content || "";
  const PLANNER_LABEL = "Web planner";
  const PLANNER_TOKEN = "ra-planner-token"; // the sessionStorage name packaging/web/planner.js reads

  async function openPlanner() {
    status("planner-status", "Opening the planner...");
    const tokens = await api("GET", "/v1/tokens");
    if (tokens.status !== 200) return status("planner-status", "Could not list tokens: " + reason(tokens), "error");
    let web = null;
    for (const t of tokens.body.tokens) {
      if (t.kind === "web") web = t;
      // Made by this page before tokens were derived: one per visit, held by no one once its tab closed.
      else if (!t.derived && t.label === PLANNER_LABEL && t.scopes.length === 1 && t.scopes[0] === "read") {
        await api("DELETE", "/v1/tokens", undefined, true, null, t.id);
      }
    }
    if (web && web.pending) {
      return status("planner-status", "The planner's token is being replaced; reopen the account and try again.", "error");
    }
    if (!web) {
      const id = K.randomId();
      const made = await api("POST", "/v1/tokens",
        { id: id, token: await K.bearer(key, id, 0), label: PLANNER_LABEL, scopes: ["read"], kind: "web" });
      if (made.status !== 201) return status("planner-status", "Could not make a token: " + reason(made), "error");
      web = { id: id, epoch: 0, scopes: ["read"] };
    }
    try {
      sessionStorage.setItem(PLANNER_TOKEN, await K.plannerToken(key, web.id, web.epoch));
    } catch (e) {
      return status("planner-status", "This browser refuses session storage, which the planner needs.", "error");
    }
    key = null;
    location.assign(PLANNER);
  }

  if (PLANNER) {
    show("planner-panel", true);
    $("planner-button").addEventListener("click", openPlanner);
  }

  // With the account key: delete the characters one token posted for, and revoke that token.
  async function deletePosted(id, name, posted) {
    const names = posted.map((p) => p.player).join(", ");
    if (!window.confirm('Delete what the token "' + name + '" posted (' + names + ") and revoke the token?")) return;
    const result = await api("DELETE", "/v1/tokens/posted", undefined, true, null, id);
    if (result.status !== 200) {
      status("token-status", "Could not delete: " + reason(result), "error");
      return;
    }
    status("token-status", "Deleted " + (result.body.deleted.join(", ") || "nothing") + "; the token is revoked.", "ok");
    await loadAccount();
  }

  // Opened with a plugin's token: delete what it posted. The token is revoked with it, so the view closes.
  $("token-delete-button").addEventListener("click", async () => {
    if (!window.confirm("Delete everything this token posted and revoke the token? This cannot be undone.")) return;
    const result = await api("DELETE", "/v1/posted");
    if (result.status !== 200) {
      status("token-view-status", "Could not delete: " + reason(result), "error");
      return;
    }
    key = null;
    show("token-view", false);
    status("signin-status", "Deleted " + (result.body.deleted.join(", ") || "nothing (the token had posted nothing)") +
      ". The token is revoked: its plugin has stopped posting.", "ok");
  });

  $("token-forget-button").addEventListener("click", () => {
    key = null;
    show("token-view", false);
    status("signin-status", "Closed. The token was forgotten by this page.");
  });

  async function revokeToken(id, name) {
    if (!window.confirm('Revoke the token "' + name + '"? Whatever uses it stops working at once, and settings ' +
      "saved with it are deleted.")) return;
    const result = await api("DELETE", "/v1/tokens", undefined, true, null, id);
    if (result.status !== 200) {
      status("token-status", "Could not revoke: " + reason(result), "error");
      return;
    }
    show("new-token", false);
    $("new-token-value").value = "";
    status("token-status", "Revoked.", "ok");
    await loadAccount();
  }

  $("player-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("player-name").value.trim();
    if (!name) {
      status("delete-status", "Type the character's name first.", "error");
      return;
    }
    if (!window.confirm('Delete the stored state of "' + name + '"?')) return;
    const result = await api("DELETE", "/v1/players", undefined, true, name);
    if (result.status === 200) {
      $("player-name").value = "";
      status("delete-status", 'Deleted the state of "' + name + '".', "ok");
      await loadAccount();
    } else if (result.status === 404) {
      status("delete-status", 'This account has no character called "' + name + '".', "error");
    } else {
      status("delete-status", "Could not delete: " + reason(result), "error");
    }
  });

  $("delete-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if ($("delete-confirm").value.trim() !== "DELETE") {
      status("delete-status", "Type DELETE in capitals to confirm.", "error");
      return;
    }
    const result = await api("DELETE", "/v1/account");
    if (result.status !== 200) {
      status("delete-status", "Could not delete the account: " + reason(result), "error");
      return;
    }
    closeAccount(
      "The account is deleted: " + result.body.characters + " character(s) and " + result.body.tokens + " token(s) removed.",
      "ok"
    );
  });

  // Copy buttons: data-copy names the read-only field holding a key or token.
  for (const button of document.querySelectorAll("button[data-copy]")) {
    button.addEventListener("click", async () => {
      const field = $(button.dataset.copy);
      try {
        await navigator.clipboard.writeText(field.value);
        button.textContent = "Copied";
      } catch (e) {
        field.select(); // no clipboard permission: leave it selected for Ctrl+C
        button.textContent = "Select and copy";
      }
      setTimeout(() => (button.textContent = "Copy"), 2000);
    });
  }
})();
