"""KI-Assistent: macht aus einer Beschreibung in eigenen Worten fertig ausgefüllte Aufgaben.

Modellunabhängig über zwei Protokolle:
- Anthropic (Claude) über das offizielle SDK mit Structured Outputs,
- die OpenAI-kompatible Chat-Completions-API für alle anderen (OpenAI, Google Gemini, OpenRouter,
  Mistral oder ein eigener Endpunkt).

Dieselbe Anleitung (GUIDANCE) steckt auch im herunterladbaren Skill für Claude-Apps, damit Aufgaben
überall gleich angelegt werden.
"""

import base64
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import anthropic
import httpx
from cryptography.fernet import Fernet, InvalidToken

from .db import PRIORITIES, Store

PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com", "model": "claude-opus-5-5"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": ""},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": ""},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": ""},
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1", "model": ""},
    "custom": {"label": "Eigener Endpunkt (OpenAI-kompatibel)", "base_url": "", "model": ""},
}

# Claude-Modelle mit Structured Outputs; ältere bekommen stattdessen ein erzwungenes Werkzeug
STRUCTURED = re.compile(r"^claude-((opus|sonnet|haiku|fable|mythos)-[5-9]|opus-4-[158]|haiku-4-5)")
EFFORT = re.compile(r"^claude-((opus|sonnet|haiku|fable|mythos)-[5-9]|opus-4-[5-8]|sonnet-4-6)")
# Bei einer Ablehnung durch Sicherheitsfilter beantwortet der Server die Anfrage mit einem passenden Modell
FALLBACKS = re.compile(r"^claude-(fable-5-1|opus-5-5|opus-5|sonnet-5-5)$")

MAX_TASKS = 10
TIMEOUT = 90.0


class AgentError(Exception):
    """Fehler mit einer Meldung, die so in der Oberfläche erscheinen kann."""


# --- Anleitung (auch Inhalt des Skills) -------------------------------------

GUIDANCE = """## Was eine gute Aufgabe ausmacht

- **Titel:** kurz und konkret, beginnt mit einem Verb („Angebot an Müller GmbH schicken“), höchstens
  etwa 80 Zeichen. Kein Datum, keine Priorität und kein Projektname im Titel.
- **Beschreibung (notes, Markdown):** Ein bis zwei Sätze: worum es geht und warum. Danach, wenn es
  mehrere Schritte sind, eine kurze Checkliste („- [ ] …“, höchstens fünf Punkte). Namen, Beträge,
  Links und Referenzen aus der Beschreibung übernehmen. Nichts erfinden, was der Nutzer nicht gesagt hat.
- **Frist (due_date):** nur bei einer echten Deadline („bis Freitag“, „spätestens“, ein Termin).
  Eine Uhrzeit (due_time, HH:MM) nur, wenn eine genannt ist.
- **Geplanter Tag (plan_date):** wenn der Nutzer sagt, wann er daran arbeiten will („morgen kümmere
  ich mich darum“, „heute noch“). Das ist ein Tag ohne Uhrzeit, keine Frist. „Nächste Woche“ ohne
  Tag → Montag der nächsten Woche als plan_date.
- **Priorität:** high nur bei klarer Dringlichkeit oder Wichtigkeit, low bei „irgendwann“ oder
  „wenn Zeit ist“, sonst keine.
- **Projekt und Tags:** vorhandene in exakt derselben Schreibweise wiederverwenden. Neue nur, wenn
  sie eindeutig passen.
- **Quest:** zuordnen, wenn die Aufgabe ein Ziel erkennbar voranbringt (vertikale Kohärenz). Im
  Zweifel keine Quest – Sonstiges ist erlaubt.
- **Aufteilen:** mehrere voneinander unabhängige Dinge → mehrere Aufgaben. Teilschritte einer Sache
  → eine Aufgabe mit Checkliste.
- Relative Angaben („morgen“, „Freitag“, „in zwei Wochen“) in echte Daten umrechnen; Zeitzone
  Europe/Berlin. Ein Wochentag meint den nächsten kommenden."""

EXAMPLES = """## Beispiele

Heute ist Mittwoch, der 18. November 2026. Es gibt die Quest „Neue Kunden gewinnen“ und das Projekt „Vertrieb“.

**Nutzer:** „Bis Freitag muss das Angebot für Müller raus, vorher noch mit Lisa die Preise klären. Morgen fang ich an.“

→ eine Aufgabe:
- Titel: „Angebot an Müller GmbH schicken“
- Beschreibung: „Angebot für Müller fertigstellen und verschicken. - [ ] Preise mit Lisa klären - [ ] Angebot schreiben - [ ] An Müller schicken“
- Frist: 2026-11-20 · geplant: 2026-11-19 · Projekt: Vertrieb · Quest: Neue Kunden gewinnen · Priorität: high

**Nutzer:** „Zahnarzt anrufen und irgendwann mal die Steuerunterlagen sortieren“

→ zwei Aufgaben:
- „Zahnarzt anrufen“ – Beschreibung: „Termin beim Zahnarzt vereinbaren.“
- „Steuerunterlagen sortieren“ – Beschreibung: „Belege und Unterlagen für die Steuererklärung ordnen.“ · Priorität: low"""


def system_prompt(store: Store, now: datetime) -> str:
    quests = [q for q in store.quests() if q["status"] == "active"]
    quest_lines = "\n".join(
        f"- {q['id']}: {q['title']}" + (f" – {q['notes'][:200]}" if q["notes"] else "") for q in quests
    ) or "- (keine)"
    projects = ", ".join(p["project"] for p in store.projects()) or "(keine)"
    tags = ", ".join(t["tag"] for t in store.tags()) or "(keine)"
    weekday = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][now.weekday()]
    return f"""Du legst Aufgaben in der persönlichen Aufgabenliste des Nutzers an. Der Nutzer beschreibt eine oder
mehrere Aufgaben in eigenen Worten; du machst daraus sauber ausgefüllte Aufgaben.

{GUIDANCE}

{EXAMPLES}

## Vorhandene Daten

Projekte: {projects}
Tags: {tags}
Aktive Quests (quest_id: Titel – Beschreibung):
{quest_lines}

## Antwort

Gib die Aufgaben über das Ergebnisformat zurück. "reply" ist ein kurzer Satz auf Deutsch, der sagt,
was du angelegt hast. Felder, die du nicht sicher weißt, bleiben leer (null).

Heute ist {weekday}, der {now.date().isoformat()}, {now.strftime('%H:%M')} Uhr (Europe/Berlin)."""


# --- Ergebnisformat -----------------------------------------------------------

_FIELDS = {
    "title": ({"type": "string"}, "Kurzer Titel mit Verb"),
    "notes": ({"type": "string"}, "Beschreibung in Markdown: Kontext, ggf. Checkliste"),
    "due_date": ({"type": "string"}, "Frist, YYYY-MM-DD"),
    "due_time": ({"type": "string"}, "Uhrzeit der Frist, HH:MM"),
    "plan_date": ({"type": "string"}, "Tag, an dem daran gearbeitet wird, YYYY-MM-DD"),
    "priority": ({"type": "string", "enum": list(PRIORITIES)}, "low, medium oder high"),
    "project": ({"type": "string"}, "Projektname, vorhandene bevorzugen"),
    "tags": ({"type": "array", "items": {"type": "string"}}, "Tags ohne #"),
    "quest_id": ({"type": "integer"}, "ID einer aktiven Quest"),
}
_REQUIRED = ("title", "notes")


def task_schema(strict: bool) -> dict:
    """strict: alle Felder Pflicht, Unbekanntes als null (Structured Outputs). Sonst nur Titel und
    Beschreibung Pflicht und keine null-Typen – das verstehen auch OpenAI-kompatible Anbieter."""
    props = {}
    for name, (schema, desc) in _FIELDS.items():
        if strict and name not in _REQUIRED:
            props[name] = {"anyOf": [schema, {"type": "null"}], "description": desc}
        else:
            props[name] = {**schema, "description": desc}
    item = {"type": "object", "properties": props, "required": list(props) if strict else list(_REQUIRED),
            "additionalProperties": False}
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "Ein kurzer Satz an den Nutzer"},
            "tasks": {"type": "array", "items": item, "description": "Die anzulegenden Aufgaben"},
        },
        "required": ["reply", "tasks"],
        "additionalProperties": False,
    }


def _valid_date(value) -> str | None:
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) and value else None
    except ValueError:
        return None


def clean_task(raw: dict, quest_ids: set[int]) -> dict | None:
    """Modellausgabe in Felder für add_task übersetzen; Unbrauchbares fällt weg statt zu scheitern."""
    if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
        return None
    out = {"title": str(raw["title"]).strip()[:200], "notes": str(raw.get("notes") or "").strip()}
    for key in ("due_date", "plan_date"):
        if value := _valid_date(raw.get(key)):
            out[key] = value
    due_time = raw.get("due_time")
    if out.get("due_date") and isinstance(due_time, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", due_time):
        out["due_time"] = due_time
    if raw.get("priority") in PRIORITIES:
        out["priority"] = raw["priority"]
    if isinstance(raw.get("project"), str) and raw["project"].strip():
        out["project"] = raw["project"].strip()[:80]
    if isinstance(raw.get("tags"), list):
        out["tags"] = [str(t) for t in raw["tags"] if isinstance(t, str)][:8]
    if isinstance(raw.get("quest_id"), int) and raw["quest_id"] in quest_ids:
        out["quest_id"] = raw["quest_id"]
    return out


# --- Konfiguration -------------------------------------------------------------


@dataclass
class Config:
    provider: str
    base_url: str
    model: str
    api_key: str | None


def check_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local) or not parsed.hostname:
        raise AgentError("Der Endpunkt muss eine https://-Adresse sein.")
    return url


class Agent:
    def __init__(self, store: Store, secret: str, tz: ZoneInfo):
        self.store = store
        self.tz = tz
        # Der API-Key liegt verschlüsselt in der Datenbank; der Schlüssel hängt am Zugangs-Key der App
        self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"todo-agent-key:" + secret.encode()).digest()))

    def config(self) -> Config:
        provider = self.store.get_setting("agent_provider") or "anthropic"
        provider = provider if provider in PROVIDERS else "anthropic"
        key = None
        if token := self.store.get_setting("agent_key"):
            try:
                key = self.fernet.decrypt(token.encode()).decode()
            except InvalidToken:  # Zugangs-Key wurde geändert
                key = None
        return Config(
            provider=provider,
            base_url=self.store.get_setting("agent_base_url") or PROVIDERS[provider]["base_url"],
            model=self.store.get_setting("agent_model") or PROVIDERS[provider]["model"],
            api_key=key,
        )

    def public_config(self) -> dict:
        """Was die Oberfläche sehen darf – der Key selbst nie, nur seine letzten Zeichen."""
        cfg = self.config()
        return {
            "provider": cfg.provider, "base_url": cfg.base_url, "model": cfg.model,
            "has_key": bool(cfg.api_key), "key_hint": cfg.api_key[-4:] if cfg.api_key else None,
            "ready": bool(cfg.api_key and cfg.model),
            "providers": {k: {"label": v["label"], "base_url": v["base_url"], "model": v["model"]} for k, v in PROVIDERS.items()},
        }

    def save(self, provider: str, base_url: str, model: str, api_key: str | None, clear_key: bool) -> dict:
        if provider not in PROVIDERS:
            raise AgentError("Unbekannter Anbieter.")
        current = self.config()
        base_url = check_base_url(base_url) if provider == "custom" else PROVIDERS[provider]["base_url"]
        self.store.set_setting("agent_provider", provider)
        self.store.set_setting("agent_base_url", base_url)
        self.store.set_setting("agent_model", model.strip())
        moved = provider != current.provider or base_url != current.base_url
        if clear_key or (moved and not api_key):
            # Ein Key gehört zu genau einem Endpunkt – sonst ließe er sich an einen fremden Server schicken
            self.store.set_setting("agent_key", "")
        if api_key:
            self.store.set_setting("agent_key", self.fernet.encrypt(api_key.strip().encode()).decode())
        return self.public_config()

    def draft(self, provider: str, base_url: str | None, api_key: str | None) -> Config:
        """Konfiguration zum Ausprobieren vor dem Speichern; fehlt der Key, gilt der gespeicherte."""
        if provider not in PROVIDERS:
            raise AgentError("Unbekannter Anbieter.")
        current = self.config()
        url = check_base_url(base_url or "") if provider == "custom" else PROVIDERS[provider]["base_url"]
        # Den gespeicherten Key nur an denselben Endpunkt schicken
        key = api_key or (current.api_key if (current.provider, current.base_url) == (provider, url) else None)
        if not key and provider != "custom":
            raise AgentError("Bitte zuerst einen API-Key eintragen.")
        return Config(provider, url, current.model, key)

    # --- Aufrufe --------------------------------------------------------------

    async def models(self, cfg: Config) -> list[str]:
        if cfg.provider == "anthropic":
            client = self._anthropic(cfg)
            try:
                return [m.id async for m in client.models.list()]
            except anthropic.APIError as e:
                raise AgentError(_anthropic_message(e, cfg.model)) from e
        async with httpx.AsyncClient(timeout=20) as http:
            res = await _send(http.get(f"{cfg.base_url}/models", headers=_openai_headers(cfg)))
        _raise_for(res, cfg.model)
        data = res.json().get("data") or res.json().get("models") or []
        return sorted({str(m.get("id") or m.get("name", "")).removeprefix("models/") for m in data if isinstance(m, dict)} - {""})

    async def run(self, text: str) -> tuple[str, list[dict]]:
        cfg = self.config()
        if not cfg.api_key and cfg.provider != "custom":
            raise AgentError("Kein API-Key hinterlegt – unter Einstellungen → KI-Assistent einrichten.")
        if not cfg.model:
            raise AgentError("Kein Modell gewählt – unter Einstellungen → KI-Assistent einrichten.")
        system = system_prompt(self.store, datetime.now(self.tz))
        result = await (self._run_anthropic(cfg, system, text) if cfg.provider == "anthropic"
                        else self._run_openai(cfg, system, text))
        quest_ids = {q["id"] for q in self.store.quests()}
        tasks = [t for t in (clean_task(raw, quest_ids) for raw in (result.get("tasks") or [])[:MAX_TASKS]) if t]
        if not tasks:
            raise AgentError(str(result.get("reply") or "Das Modell hat keine Aufgabe erkannt. Beschreib sie etwas genauer."))
        return str(result.get("reply") or ""), tasks

    def _anthropic(self, cfg: Config) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(api_key=cfg.api_key, base_url=cfg.base_url, timeout=TIMEOUT, max_retries=1)

    async def _run_anthropic(self, cfg: Config, system: str, text: str) -> dict:
        client = self._anthropic(cfg)
        request = {"model": cfg.model, "max_tokens": 16000, "system": system,
                   "messages": [{"role": "user", "content": text}]}
        output_config: dict = {}
        if EFFORT.match(cfg.model):
            output_config["effort"] = "low"  # einfache Extraktion: schnell und günstig
        if STRUCTURED.match(cfg.model):
            output_config["format"] = {"type": "json_schema", "schema": task_schema(strict=True)}
        else:  # ältere Modelle: ein Werkzeug, dessen Aufruf erzwungen wird
            request["tools"] = [{"name": "create_tasks", "description": "Legt die beschriebenen Aufgaben an.",
                                 "input_schema": task_schema(strict=False)}]
            request["tool_choice"] = {"type": "tool", "name": "create_tasks"}
        if output_config:
            request["output_config"] = output_config
        try:
            if FALLBACKS.match(cfg.model):
                message = await client.beta.messages.create(
                    **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            else:
                message = await client.messages.create(**request)
        except anthropic.APIError as e:
            raise AgentError(_anthropic_message(e, cfg.model)) from e
        if message.stop_reason == "refusal":
            raise AgentError("Das Modell hat die Anfrage abgelehnt.")
        if message.stop_reason == "max_tokens":
            raise AgentError("Die Antwort war zu lang. Beschreib weniger Aufgaben auf einmal.")
        for block in message.content:
            if block.type == "tool_use" and block.name == "create_tasks":
                return block.input if isinstance(block.input, dict) else {}
            if block.type == "text":
                return _json_object(block.text)
        raise AgentError("Das Modell hat keine Aufgaben geliefert.")

    async def _run_openai(self, cfg: Config, system: str, text: str) -> dict:
        body = {
            "model": cfg.model,
            "messages": [{"role": "system", "content": system + "\n\nRufe dafür das Werkzeug create_tasks auf."},
                         {"role": "user", "content": text}],
            "tools": [{"type": "function", "function": {
                "name": "create_tasks", "description": "Legt die beschriebenen Aufgaben an.",
                "parameters": task_schema(strict=False)}}],
            "tool_choice": "required",
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            res = await _send(http.post(f"{cfg.base_url}/chat/completions", json=body, headers=_openai_headers(cfg)))
            if res.status_code == 400:  # nicht jeder Anbieter kennt "required"
                body["tool_choice"] = "auto"
                res = await _send(http.post(f"{cfg.base_url}/chat/completions", json=body, headers=_openai_headers(cfg)))
        _raise_for(res, cfg.model)
        try:
            message = res.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise AgentError("Unerwartete Antwort vom Anbieter.")
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name") == "create_tasks":
                args = fn.get("arguments")
                return args if isinstance(args, dict) else _json_object(args or "")
        return _json_object(message.get("content") or "")


def _json_object(text: str) -> dict:
    """JSON aus einer Textantwort holen, auch wenn das Modell es in Fließtext oder ```json verpackt."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AgentError("Das Modell hat keine Aufgaben geliefert.")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise AgentError("Die Antwort des Modells war kein gültiges JSON.")
    return data if isinstance(data, dict) else {}


def _openai_headers(cfg: Config) -> dict:
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    if cfg.provider == "openrouter":
        headers["X-Title"] = "Aufgaben"
    return headers


async def _send(request) -> httpx.Response:
    try:
        return await request
    except httpx.TimeoutException:
        raise AgentError("Der Anbieter hat nicht rechtzeitig geantwortet.")
    except httpx.HTTPError:
        raise AgentError("Keine Verbindung zum Anbieter.")


def _raise_for(res: httpx.Response, model: str) -> None:
    if res.is_success:
        return
    try:
        err = res.json()
        err = err[0] if isinstance(err, list) and err else err  # Gemini antwortet mit einer Liste
        inner = err.get("error") if isinstance(err, dict) else None
        detail = inner.get("message") if isinstance(inner, dict) else inner
    except ValueError:
        detail = None
    messages = {401: "Der API-Key ist ungültig.", 403: "Der API-Key hat keine Berechtigung dafür.",
                404: f"Modell oder Endpunkt nicht gefunden ({model or 'kein Modell'}).",
                429: "Rate-Limit oder Guthaben erreicht – kurz warten oder Guthaben prüfen."}
    if res.status_code in messages:
        raise AgentError(messages[res.status_code])
    if res.status_code >= 500:
        raise AgentError("Der Anbieter hat gerade Probleme. Bitte gleich noch einmal versuchen.")
    raise AgentError(f"Anfrage abgelehnt: {detail or res.status_code}")


def _anthropic_message(e: anthropic.APIError, model: str) -> str:
    # Vom Spezifischen zum Allgemeinen
    if isinstance(e, anthropic.AuthenticationError):
        return "Der API-Key ist ungültig."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "Der API-Key hat keine Berechtigung dafür."
    if isinstance(e, anthropic.NotFoundError):
        return f"Modell nicht gefunden ({model})."
    if isinstance(e, anthropic.RateLimitError):
        return "Rate-Limit oder Guthaben erreicht – kurz warten oder Guthaben prüfen."
    if isinstance(e, anthropic.APIStatusError):
        return "Der Anbieter hat gerade Probleme." if e.status_code >= 500 else f"Anfrage abgelehnt: {e.message}"
    if isinstance(e, anthropic.APITimeoutError):
        return "Der Anbieter hat nicht rechtzeitig geantwortet."
    return "Keine Verbindung zum Anbieter."


# --- Skill für Claude-Apps --------------------------------------------------------

SKILL_NAME = "aufgaben-anlegen"


def skill_markdown() -> str:
    return f"""---
name: {SKILL_NAME}
description: Legt Aufgaben in der persönlichen Aufgabenliste an (todo-Connector), wenn der Nutzer etwas beschreibt, das erledigt werden soll – z. B. „schreib auf“, „erinnere mich“, „pack das auf meine Liste“, „plan mir … ein“, „ich muss noch …“. Füllt Titel, Beschreibung, Frist, geplanten Tag, Priorität, Projekt, Tags und Quest sinnvoll aus.
---

# Aufgaben anlegen

Voraussetzung ist der todo-Connector (MCP) mit den Tools `overview`, `list_projects`, `list_quests`
und `add_task`.

## Ablauf

1. Datum klären: `overview` aufrufen, falls das heutige Datum nicht bekannt ist.
2. Kontext holen: `list_projects` (Projekte und Tags) und `list_quests` (aktive Ziele), damit
   vorhandene Namen wiederverwendet und Aufgaben passenden Quests zugeordnet werden.
3. Je Aufgabe einmal `add_task` aufrufen.
4. Kurz bestätigen: ein Satz mit Titel und den wichtigsten Feldern (Frist, Tag, Quest). Nicht die
   ganze Beschreibung wiederholen.

Ist eine Beschreibung so unklar, dass kein sinnvoller Titel möglich ist, kurz nachfragen statt raten.

{GUIDANCE}

{EXAMPLES}
"""


def skill_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{SKILL_NAME}/SKILL.md", skill_markdown())
    return buf.getvalue()
