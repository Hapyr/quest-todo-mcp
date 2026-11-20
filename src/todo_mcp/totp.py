"""Zwei-Faktor-Anmeldung: Einmalcodes aus einer Authenticator-App (TOTP, RFC 6238) und
Wiederherstellungscodes für den Fall, dass das Handy weg ist."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

import segno

PERIOD = 30
DIGITS = 6
WINDOW = 1  # ein Zeitschritt Toleranz in jede Richtung für ungenaue Uhren
RECOVERY_CODES = 8


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def verify(secret: str, code: str, last_counter: int, now: float | None = None) -> int | None:
    """Liefert den Zeitschritt des gültigen Codes oder None. Ein Code gilt nur einmal: Schritte bis
    einschließlich last_counter werden abgelehnt (Schutz gegen abgefangene und wiederholte Codes)."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != DIGITS:
        return None
    current = int((now or time.time()) // PERIOD)
    for counter in range(current - WINDOW, current + WINDOW + 1):
        if counter > last_counter and hmac.compare_digest(_code(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Aufgaben") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?" + urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": PERIOD})


def qr_svg(uri: str) -> str:
    """QR-Code als SVG, immer schwarz auf weiß – so lesen ihn alle Authenticator-Apps, auch im Dunkelmodus."""
    return segno.make(uri, error="m").svg_inline(scale=5, border=3, dark="#000", light="#fff", omitsize=True)


def new_recovery_codes() -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # ohne leicht verwechselbare Zeichen
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)) for _ in range(RECOVERY_CODES)]


def hash_recovery(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
