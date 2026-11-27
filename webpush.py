"""
Web Push for Halo: notifications that come from the Halo app itself (with the
Halo icon), on iPhone (iOS 16.4+, Halo added to the Home Screen) and on desktop
and Android browsers.

Implements the standards directly so Halo needs no push library:
  * RFC 8291  Message Encryption for Web Push (aes128gcm)
  * RFC 8292  VAPID (identifies Halo to Apple / Google / Mozilla push services)
Only dependency: the `cryptography` package (on Raspberry Pi OS:
`sudo apt install python3-cryptography`, usually already installed).
"""

import base64
import json
import os
import struct
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_SIZE = 4096


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _public_bytes(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(plaintext: bytes, p256dh: str, auth: str, salt: bytes = None, sender_key=None) -> bytes:
    """Encrypt a push message for one browser subscription (RFC 8291, aes128gcm).

    p256dh / auth are the base64url strings from the browser's PushSubscription.
    salt and sender_key are only passed in tests; normally they're random per message.
    """
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    if len(ua_public) != 65 or len(auth_secret) < 16:
        raise ValueError("Invalid subscription keys")
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_key = sender_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_key.public_key())
    ecdh_secret = as_key.exchange(ec.ECDH(), ua_key)

    # RFC 8291 section 3.4: combine the ECDH secret with the auth secret
    prk_key = _hmac_sha256(auth_secret, ecdh_secret)
    ikm = _hmac_sha256(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")

    # RFC 8188: derive the content key and nonce
    salt = salt or os.urandom(16)
    prk = _hmac_sha256(salt, ikm)
    cek = _hmac_sha256(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac_sha256(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    if len(plaintext) + 1 + 16 > RECORD_SIZE:
        raise ValueError("Push message too long")
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)   # \x02 = last record
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


class Vapid:
    """Halo's VAPID key pair: lets push services know messages really come from this Halo."""

    def __init__(self, key_path: str):
        self.key_path = key_path
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                self.key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
            pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # private: owner only
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
        self.public_key = b64u(_public_bytes(self.key.public_key()))

    def authorization(self, endpoint: str, subject: str, lifetime: int = 12 * 3600) -> str:
        """Authorization header value for one push request (RFC 8292)."""
        u = urlparse(endpoint)
        claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + lifetime, "sub": subject}
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        body = b64u(json.dumps(claims, separators=(",", ":")).encode())
        signing_input = f"{head}.{body}".encode()
        r, s = decode_dss_signature(self.key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        token = f"{head}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        return f"vapid t={token}, k={self.public_key}"


def build_request(subscription: dict, payload: dict, vapid: Vapid, subject: str,
                  ttl: int = 24 * 3600, urgency: str = "normal"):
    """Returns (url, body, headers) for sending one notification to one device."""
    keys = subscription.get("keys") or {}
    body = encrypt(json.dumps(payload, separators=(",", ":")).encode(), keys.get("p256dh", ""), keys.get("auth", ""))
    endpoint = subscription["endpoint"]
    headers = {
        "Authorization": vapid.authorization(endpoint, subject),
        "TTL": str(ttl),
        "Urgency": urgency,
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "User-Agent": "Halo/2.0",
    }
    return endpoint, body, headers
