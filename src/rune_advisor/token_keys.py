"""Tokens derived from an account key, and the planner token that carries a settings key.

The account page (`site/assets/account.js`) derives every token it makes from the account key, so it can show any
of them again and so the server never holds anything it could derive from:

    auth = HKDF-SHA256(ikm = key, salt = "", info = "rune-advisor/v1/token/<id>/<epoch>/auth", 32 bytes)
    enc  = HKDF-SHA256(ikm = key, salt = "", info = "rune-advisor/v1/token/<id>/<epoch>/enc",  32 bytes)
    bearer        = "rat_" + base64url(auth)        what is sent as `Authorization: Bearer`
    planner token = bearer + "." + base64url(enc)   what a planner holds; the part after the dot is never sent

`id` is 32 random hex digits the page picks; `epoch` goes up when the token is rotated. `enc` is the AES-256-GCM key
of the token's settings blob (`docs/export-api.md`, "Settings blobs"). This module is the reference the JavaScript
is tested against, and what a desktop planner will use to split a pasted planner token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

DERIVED_PREFIX = "rat_"


def hkdf_sha256(ikm: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    """RFC 5869 HKDF with SHA-256 (an empty salt is a block of zeros, as the RFC says)."""
    prk = hmac.new(salt or bytes(32), ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _part(key: str, token_id: str, epoch: int, what: str) -> bytes:
    return hkdf_sha256(key.encode("utf-8"), f"rune-advisor/v1/token/{token_id}/{epoch}/{what}".encode("utf-8"))


def bearer(key: str, token_id: str, epoch: int) -> str:
    """The value a client sends for the derived token (id, epoch)."""
    return DERIVED_PREFIX + b64url(_part(key, token_id, epoch, "auth"))


def settings_key(key: str, token_id: str, epoch: int) -> bytes:
    """The AES-256-GCM key of the token's settings blob."""
    return _part(key, token_id, epoch, "enc")


def planner_token(key: str, token_id: str, epoch: int) -> str:
    return bearer(key, token_id, epoch) + "." + b64url(settings_key(key, token_id, epoch))


def split(token: str) -> tuple[str, bytes | None]:
    """(bearer, settings key or None) from what a planner holds: only the bearer may be sent anywhere."""
    sent, _, enc = token.strip().partition(".")
    return sent, (base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)) if enc else None)


def blob_aad(token_id: str, epoch: int) -> bytes:
    """Additional authenticated data of a settings blob: ties the ciphertext to its token and epoch."""
    return f"rune-advisor/v1/blob/{token_id}/{epoch}".encode("utf-8")
