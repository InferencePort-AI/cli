"""Wire primitives shared with the desktop's ecdhAesSession/proxy52458Client."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode(value: str) -> bytes:
    # Buffer.from(value, 'base64url') accepts standard base64, ignores noise,
    # stops at padding, and discards a trailing incomplete sextet.
    value = re.sub(r"[^A-Za-z0-9+/_-]", "", value.split("=", 1)[0])
    if len(value) % 4 == 1:
        value = value[:-1]
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def derive_key(secret: bytes, challenge: str, server_nonce: str, client_nonce: str) -> bytes:
    return hashlib.sha256(secret + challenge.encode() + decode(server_nonce) + decode(client_nonce)).digest()


def decrypt_body(key: bytes, raw: bytes) -> bytes:
    if not raw:
        return raw
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(obj, dict) or obj.get("type") != "enc" or not all(
            isinstance(obj.get(k), str) for k in ("iv", "tag", "data")
        ):
            return raw
        tag = decode(obj["tag"])
        if len(tag) not in (4, 8, 12, 13, 14, 15, 16):
            return raw
        decryptor = Cipher(algorithms.AES(key), modes.GCM(decode(obj["iv"]), tag, min_tag_length=len(tag))).decryptor()
        # Never return update() output until final authentication succeeds.
        return decryptor.update(decode(obj["data"])) + decryptor.finalize()
    except (ValueError, TypeError, KeyError, InvalidTag):
        return raw


@dataclass
class Session:
    key: bytes
    email: str
    role: str
    expires_at: int


class Sessions:
    def __init__(self):
        self.records = {}
        self.lock = threading.Lock()

    def lookup(self, session_id: str):
        now = int(time.time() * 1000)
        with self.lock:
            self.records = {k: v for k, v in self.records.items() if v.expires_at > now}
            return self.records.get(session_id)

    def establish(self, public_key: str, client_nonce: str, email: str, role: str):
        challenge, nonce = encode(secrets.token_bytes(32)), encode(secrets.token_bytes(16))
        private = ec.generate_private_key(ec.SECP256R1())
        point = decode(public_key)
        # OpenSSL ECDH also accepts valid hybrid points.
        if len(point) == 65 and point[0] in (6, 7) and point[0] % 2 == point[-1] % 2:
            point = b"\x04" + point[1:]
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
        key = derive_key(private.exchange(ec.ECDH(), peer), challenge, nonce, client_nonce)
        sid = encode(secrets.token_bytes(24))
        with self.lock:
            self.records[sid] = Session(key, email, role or "member", int(time.time() * 1000) + 3600000)
        return {"sessionId": sid, "challenge": challenge,
                "serverPublicKey": encode(private.public_key().public_bytes(
                    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)), "serverNonce": nonce}
