// Tokens derived from the account key, and the settings blob a planner token unlocks. Shared by the account page
// and the planner page; `src/rune_advisor/token_keys.py` is the same thing in Python and the reference the tests
// hold this file to.
//
//   auth = HKDF-SHA256(key, salt "", info "rune-advisor/v1/token/<id>/<epoch>/auth", 32 bytes)
//   enc  = HKDF-SHA256(key, salt "", info "rune-advisor/v1/token/<id>/<epoch>/enc", 32 bytes)
//   bearer        = "rat_" + base64url(auth)        the only part ever sent, as Authorization: Bearer
//   planner token = bearer + "." + base64url(enc)   enc is the AES-256-GCM key of the token's settings blob
//
// A settings blob is a 12-byte random nonce followed by the AES-GCM ciphertext of a JSON object, with
// "rune-advisor/v1/blob" as additional data. The enc key differs for every token and epoch, so a blob only
// opens with the token it was written for. Nothing here touches the page or the network.
"use strict";

(function (root) {
  const subtle = root.crypto.subtle;
  const text = new TextEncoder();
  const BLOB_AAD = text.encode("rune-advisor/v1/blob");

  function base64(bytes) {
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return root.btoa(s);
  }

  function unbase64(s) {
    const raw = root.atob(s);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  const b64url = (bytes) => base64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const unb64url = (s) => unbase64(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));

  async function hkdf(key, info) {
    const ikm = await subtle.importKey("raw", text.encode(key), "HKDF", false, ["deriveBits"]);
    const bits = await subtle.deriveBits(
      { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: text.encode(info) }, ikm, 256);
    return new Uint8Array(bits);
  }

  const part = (key, id, epoch, what) => hkdf(key, "rune-advisor/v1/token/" + id + "/" + epoch + "/" + what);

  // 32 random hex digits: a new token's id. Never reused, or a revoked token's value would come back.
  function randomId() {
    const bytes = root.crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  async function bearer(key, id, epoch) {
    return "rat_" + b64url(await part(key, id, epoch, "auth"));
  }

  async function plannerToken(key, id, epoch) {
    return (await bearer(key, id, epoch)) + "." + b64url(await part(key, id, epoch, "enc"));
  }

  // {bearer, enc}: what may be sent, and the blob key (null for a token without one).
  function split(token) {
    const [sent, enc] = token.trim().split(".", 2);
    return { bearer: sent, enc: enc ? unb64url(enc) : null };
  }

  async function aesKey(enc) {
    return subtle.importKey("raw", enc, "AES-GCM", false, ["encrypt", "decrypt"]);
  }

  // An object -> the blob's base64 text for the API.
  async function sealBlob(enc, value) {
    const nonce = root.crypto.getRandomValues(new Uint8Array(12));
    const sealed = await subtle.encrypt({ name: "AES-GCM", iv: nonce, additionalData: BLOB_AAD },
      await aesKey(enc), text.encode(JSON.stringify(value)));
    const out = new Uint8Array(12 + sealed.byteLength);
    out.set(nonce, 0);
    out.set(new Uint8Array(sealed), 12);
    return base64(out);
  }

  // The API's base64 text -> the object. Throws when the key is wrong or the bytes were changed.
  async function openBlob(enc, data) {
    const bytes = unbase64(data);
    const plain = await subtle.decrypt({ name: "AES-GCM", iv: bytes.slice(0, 12), additionalData: BLOB_AAD },
      await aesKey(enc), bytes.slice(12));
    return JSON.parse(new TextDecoder().decode(plain));
  }

  root.RATokenKeys = {
    randomId, bearer, plannerToken, split, sealBlob, openBlob,
    settingsKey: (key, id, epoch) => part(key, id, epoch, "enc"),
  };
})(typeof window !== "undefined" ? window : globalThis);
