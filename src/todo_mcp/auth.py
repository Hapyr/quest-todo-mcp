"""Einbenutzer-Auth: ein geheimer Key schützt MCP (Bearer oder OAuth) und die Weboberfläche,
optional zusätzlich ein zweiter Faktor (Einmalcode aus einer Authenticator-App).

- Claude Code / Skripte schicken den Key direkt als `Authorization: Bearer <key>` – nur solange die
  Zwei-Faktor-Anmeldung aus ist. Mit 2FA wäre der Key allein ein Weg daran vorbei; dann verbinden sich
  alle Clients per OAuth.
- claude.ai und die Claude-Apps verbinden sich per OAuth (Dynamic Client Registration + PKCE);
  beim Autorisieren fragt eine Login-Seite nach dem Key (und ggf. dem Code).
- Die Weboberfläche nutzt ein signiertes Cookie. Es enthält eine Epoche; „Überall abmelden“ und das
  Einschalten von 2FA erhöhen sie und machen damit alle Cookies und OAuth-Tokens ungültig.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import totp
from .db import Store

ACCESS_TOKEN_TTL = 60 * 60  # 1 Stunde
REFRESH_TOKEN_TTL = 90 * 24 * 60 * 60  # 90 Tage, wird bei jeder Nutzung erneuert
AUTH_CODE_TTL = 5 * 60
PENDING_TTL = 10 * 60
SESSION_TTL = 90 * 24 * 60 * 60
STATIC_CLIENT_ID = "static-key"
MAX_PENDING = 50
MAX_CLIENTS = 50
# OAuth-Codes gehen nur an Claude oder an lokale Clients (Claude Code, Desktop). Sonst könnte jemand einen
# eigenen Client registrieren und dir einen Freigabe-Link schicken, dessen Code bei ihm landet.
TRUSTED_REDIRECT_HOSTS = ("claude.ai", "claude.com")
LOOPBACK = ("localhost", "127.0.0.1", "::1")

log = logging.getLogger("todo.auth")


def redirect_allowed(uri: str) -> bool:
    u = urlparse(str(uri))
    host = (u.hostname or "").lower()
    if host in LOOPBACK:
        return u.scheme in ("http", "https")
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in TRUSTED_REDIRECT_HOSTS)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class KeyAuth:
    def __init__(self, store: Store, access_key: str, public_url: str):
        self.store = store
        self.access_key = access_key
        self.public_url = public_url.rstrip("/")
        self._session_secret = hmac.new(access_key.encode(), b"session-cookie", hashlib.sha256).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"todo-totp:" + access_key.encode()).digest()))
        self._totp_lock = threading.Lock()
        self._check_key_rotation()
        # Kurzlebiger Zustand im Speicher — ein Neustart bricht höchstens einen laufenden Login ab.
        self._pending: dict[str, tuple[float, OAuthClientInformationFull, AuthorizationParams]] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    def _check_key_rotation(self) -> None:
        """Wurde der Zugangs-Key gewechselt (z. B. nach einem Verdacht), gilt nichts mehr, was mit dem alten
        Key erlangt wurde: OAuth-Tokens und -Clients werden gelöscht, Cookies ungültig. Mit dem alten Key
        verschlüsselte Geheimnisse (2FA, LLM-Key) sind unlesbar und werden zurückgesetzt."""
        fingerprint = hmac.new(self.access_key.encode(), b"key-fingerprint", hashlib.sha256).hexdigest()
        stored = self.store.get_setting("key_fingerprint")
        if stored and not hmac.compare_digest(stored, fingerprint):
            log.warning("Zugangs-Key wurde geändert: alle Verbindungen getrennt, 2FA und LLM-Key zurückgesetzt")
            self.store.delete_all_tokens()
            self.store.delete_clients()
            for key in ("totp_secret", "totp_pending", "totp_last", "totp_recovery", "agent_key"):
                self.store.set_setting(key, "")
            self.store.set_setting("session_epoch", str(self._epoch() + 1))
        self.store.set_setting("key_fingerprint", fingerprint)

    # --- Key & Sitzungs-Cookie -------------------------------------------

    def check_key(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.strip().encode(), self.access_key.encode())

    def _epoch(self) -> int:
        return int(self.store.get_setting("session_epoch") or 0)

    def _sign(self, payload: str) -> str:
        return hmac.new(self._session_secret, payload.encode(), hashlib.sha256).hexdigest()

    def make_session(self) -> str:
        payload = f"{int(time.time()) + SESSION_TTL}.{self._epoch()}"
        return f"{payload}.{self._sign(payload)}"

    def check_session(self, cookie: str | None) -> bool:
        parts = (cookie or "").split(".")
        if len(parts) == 2:  # Cookies von vor der Epoche gelten, bis zum ersten „Überall abmelden“
            expires, sig, epoch = parts[0], parts[1], "0"
            payload = expires
        elif len(parts) == 3:
            expires, epoch, sig = parts
            payload = f"{expires}.{epoch}"
        else:
            return False
        return (hmac.compare_digest(sig.encode(), self._sign(payload).encode()) and expires.isdigit()
                and int(expires) > time.time() and epoch == str(self._epoch()))

    def revoke_all(self) -> None:
        """Alle Browser-Sitzungen und OAuth-Verbindungen beenden (der Aufrufer bekommt ein neues Cookie)."""
        self.store.set_setting("session_epoch", str(self._epoch() + 1))
        self.store.delete_all_tokens()
        self._pending.clear()
        self._codes.clear()

    # --- Zwei-Faktor-Anmeldung ----------------------------------------------

    def _read_secret(self, key: str) -> str | None:
        token = self.store.get_setting(key)
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None

    def two_factor_enabled(self) -> bool:
        return bool(self.store.get_setting("totp_secret"))

    def recovery_left(self) -> int:
        return len(json.loads(self.store.get_setting("totp_recovery") or "[]"))

    def check_second_factor(self, code: str) -> bool:
        """Code aus der App oder ein Wiederherstellungscode (der danach verbraucht ist)."""
        if not self.two_factor_enabled():
            return True
        secret = self._read_secret("totp_secret")
        if secret is None:  # Zugangs-Key geändert: Geheimnis nicht mehr lesbar – lieber sperren als öffnen
            return False
        with self._totp_lock:  # derselbe Code darf nicht in zwei gleichzeitigen Anfragen durchgehen
            counter = totp.verify(secret, code, int(self.store.get_setting("totp_last") or -1))
            if counter is not None:
                self.store.set_setting("totp_last", str(counter))
                return True
            hashes = json.loads(self.store.get_setting("totp_recovery") or "[]")
            hashed = totp.hash_recovery(code)
            if code.strip() and hashed in hashes:
                hashes.remove(hashed)
                self.store.set_setting("totp_recovery", json.dumps(hashes))
                return True
        return False

    def begin_two_factor(self, account: str) -> dict:
        secret = totp.new_secret()
        self.store.set_setting("totp_pending", self._fernet.encrypt(secret.encode()).decode())
        uri = totp.provisioning_uri(secret, account)
        return {"secret": secret, "uri": uri, "qr_svg": totp.qr_svg(uri)}

    def enable_two_factor(self, code: str) -> list[str] | None:
        """Aktiviert 2FA, wenn der Code zum gerade eingerichteten Geheimnis passt. Liefert die
        Wiederherstellungscodes (nur dieses eine Mal im Klartext) und beendet alle anderen Sitzungen."""
        secret = self._read_secret("totp_pending")
        counter = totp.verify(secret, code, -1) if secret else None
        if counter is None:
            return None
        codes = totp.new_recovery_codes()
        self.store.set_setting("totp_secret", self.store.get_setting("totp_pending"))
        self.store.set_setting("totp_last", str(counter))
        self.store.set_setting("totp_recovery", json.dumps([totp.hash_recovery(c) for c in codes]))
        self.store.set_setting("totp_pending", "")
        self.revoke_all()
        return codes

    def disable_two_factor(self, code: str) -> bool:
        if not self.check_second_factor(code):
            return False
        for key in ("totp_secret", "totp_pending", "totp_last", "totp_recovery"):
            self.store.set_setting(key, "")
        return True

    def new_recovery_codes(self) -> list[str]:
        codes = totp.new_recovery_codes()
        self.store.set_setting("totp_recovery", json.dumps([totp.hash_recovery(c) for c in codes]))
        return codes

    # --- Login-Schritt im OAuth-Flow --------------------------------------

    def pending_request(self, request_id: str) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
        entry = self._pending.get(request_id)
        if entry is None or entry[0] < time.time():
            self._pending.pop(request_id, None)
            return None
        return entry[1], entry[2]

    def approve(self, request_id: str) -> str | None:
        """Erzeugt nach erfolgreichem Login einen Auth-Code und liefert die Redirect-URL zum Client."""
        entry = self.pending_request(request_id)
        if entry is None:
            return None
        del self._pending[request_id]
        client, params = entry
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, request_id: str) -> str | None:
        entry = self.pending_request(request_id)
        if entry is None:
            return None
        del self._pending[request_id]
        _, params = entry
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    # --- OAuthAuthorizationServerProvider ---------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.redirect_uris or not all(redirect_allowed(u) for u in client_info.redirect_uris):
            raise RegistrationError(error="invalid_redirect_uri",
                                    error_description="Nur Claude (claude.ai) und lokale Clients sind erlaubt")
        self._prune_clients()
        self.store.put_client(client_info.client_id, client_info.model_dump_json())

    def _prune_clients(self) -> None:
        """Registrieren kann jeder – damit die Datenbank nicht vollläuft, fliegen ungenutzte Clients raus."""
        clients = self.store.clients()
        if len(clients) < MAX_CLIENTS:
            return
        in_use = self.store.clients_with_tokens()
        unused = [(json.loads(data).get("client_id_issued_at") or 0, cid) for cid, data in clients if cid not in in_use]
        self.store.delete_clients([cid for _, cid in sorted(unused)[: len(clients) - MAX_CLIENTS + 1]])

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        if not redirect_allowed(str(params.redirect_uri)):
            return f"{self.public_url}/oauth/login?req=invalid"  # nicht dorthin weiterleiten, auch nicht mit Fehler
        self._pending = {k: v for k, v in self._pending.items() if v[0] > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}
        if len(self._pending) >= MAX_PENDING:
            self._pending.pop(min(self._pending, key=lambda k: self._pending[k][0]))
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = (now + PENDING_TTL, client, params)
        return f"{self.public_url}/oauth/login?req={request_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "Authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        row = self.store.get_token(_hash(refresh_token), "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"],
                            expires_at=row["expires_at"], resource=row["resource"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token, scopes: list[str]) -> OAuthToken:
        self.store.delete_token(_hash(refresh_token.token))  # Rotation: alter Refresh- und Access-Token verfallen
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.check_key(token) and not self.two_factor_enabled():
            return AccessToken(token=token, client_id=STATIC_CLIENT_ID, scopes=["todo"], subject="owner")
        row = self.store.get_token(_hash(token), "access")
        if row is None:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"],
                           expires_at=row["expires_at"], resource=row["resource"], subject="owner")

    async def revoke_token(self, token) -> None:
        if token.client_id != STATIC_CLIENT_ID:
            self.store.delete_token(_hash(token.token))

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        self.store.purge_expired_tokens()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(time.time())
        ah, rh = _hash(access), _hash(refresh)
        self.store.put_token(ah, "access", client_id, scopes, now + ACCESS_TOKEN_TTL, resource, rh)
        self.store.put_token(rh, "refresh", client_id, scopes, now + REFRESH_TOKEN_TTL, resource, ah)
        return OAuthToken(access_token=access, expires_in=ACCESS_TOKEN_TTL,
                          scope=" ".join(scopes) or None, refresh_token=refresh)
