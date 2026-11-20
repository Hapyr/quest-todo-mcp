"""MCP-Server für eine persönliche Aufgabenliste mit Weboberfläche.

Neue Aufgaben entstehen ausschließlich über die MCP-Tools; die Website zeigt sie an und erlaubt
Bearbeiten, Statuswechsel und Löschen. Dazu kommen Quests (übergeordnete Ziele), die Tagesplanung
ohne feste Uhrzeiten und ein Fokus-Timer, der die Arbeitszeit je Aufgabe erfasst.
"""

import asyncio
import base64
import contextlib
import gzip
import hashlib
import html
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from .agent import SKILL_NAME, Agent, AgentError, skill_zip
from .auth import KeyAuth
from .db import OPEN_STATUSES, PRIORITIES, QUEST_STATUSES, STATUSES, Store
from .live import Live
from .push import Push

ACCESS_KEY = os.environ.get("TODO_ACCESS_KEY", "")
PUBLIC_URL = os.environ.get("TODO_PUBLIC_URL", "http://localhost:8000").rstrip("/")
DB_PATH = os.environ.get("TODO_DB_PATH", "data/todo.db")
TZ = ZoneInfo(os.environ.get("TODO_TIMEZONE", "Europe/Berlin"))
PORT = int(os.environ.get("PORT", "8000"))
STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "todo_session"
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
QUEST_COLORS = ["#d93025", "#1a73e8", "#1e8e3e", "#9334e6", "#e8710a", "#12b5cb", "#e52592", "#f9ab00"]
DEFAULT_WEEKLY_TARGET = 20  # Stunden Quest-Zeit pro Woche

if len(ACCESS_KEY) < 24:
    raise SystemExit("TODO_ACCESS_KEY fehlt oder ist kürzer als 24 Zeichen (z. B. `openssl rand -hex 32`).")

store = Store(DB_PATH)
auth = KeyAuth(store, ACCESS_KEY, PUBLIC_URL)
live = Live(store, etag=lambda: data_etag(), settings=lambda: settings_payload())
agent = Agent(store, ACCESS_KEY, TZ)
# VAPID verlangt eine https:- oder mailto:-Adresse als Absenderkennung
push = Push(store, TZ, contact=os.environ.get(
    "TODO_PUSH_CONTACT", PUBLIC_URL if PUBLIC_URL.startswith("https://") else "mailto:todo@localhost"))

INSTRUCTIONS = """Persönliche Aufgabenliste des Nutzers. Neue Aufgaben legst nur du an; der Nutzer kann
Aufgaben zusätzlich in der Weboberfläche bearbeiten. Lies deshalb vor Änderungen den aktuellen Stand.

- "Was steht an?" → `overview` aufrufen; es liefert auch das heutige Datum.
- Relative Angaben ("morgen", "Freitag", "nächste Woche") selbst in YYYY-MM-DD umrechnen,
  Zeitzone Europe/Berlin. Uhrzeit nur setzen, wenn der Nutzer eine nennt.
- Vor dem Ändern oder Löschen die ID per `list_tasks` ermitteln, wenn sie nicht bekannt ist.
  Bei mehreren Treffern nachfragen statt raten.
- Projekte und Tags wiederverwenden (`list_projects`), statt Varianten mit anderer Schreibweise anzulegen.
- Status: todo = offen, doing = in Arbeit, deferred = zurückgestellt, done = erledigt.
  Zurückgestellte Aufgaben ruhen: Sie erscheinen nicht im Überblick und lösen keine Erinnerungen aus.
  Wieder aufnehmen = Status auf todo setzen.
- Quests sind übergeordnete Ziele (z. B. „Videos regelmäßig veröffentlichen“). Aufgaben, die ein Ziel
  voranbringen, per quest_id zuordnen (`list_quests`). Erfasste Zeit an Quest-Aufgaben = Quest-Stunden.
- plan_date ist der Tag, an dem der Nutzer eine Aufgabe erledigen will (Tagesplan, ohne Uhrzeit);
  due_date bleibt die Frist. "Plan mir das für Donnerstag ein" → plan_date setzen, nicht due_date.
  Offene Aufgaben mit plan_date in der Vergangenheit rutschen automatisch in den heutigen Plan.
- Zeit wird über den Fokus-Timer erfasst (`start_focus`/`stop_focus`), Auswertung mit `time_report`.
- Beim Anlegen: kurzer Titel mit Verb; in notes eine knappe Beschreibung (worum es geht, bei mehreren
  Schritten eine kurze Checkliste). Nichts erfinden, was der Nutzer nicht gesagt hat."""

mcp = MCPServer(
    name="todo",
    title="Aufgaben",
    instructions=INSTRUCTIONS,
    auth_server_provider=auth,
    auth=AuthSettings(
        issuer_url=PUBLIC_URL,
        resource_server_url=f"{PUBLIC_URL}/mcp",
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["todo"], default_scopes=["todo"]
        ),
        revocation_options=RevocationOptions(enabled=True),
    ),
)

# --- Hilfsfunktionen -------------------------------------------------------


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now(TZ).date()


def check_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ToolError(f"Ungültiges Datum '{value}', erwartet YYYY-MM-DD")


def check_time(value: str) -> str:
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ToolError(f"Ungültige Uhrzeit '{value}', erwartet HH:MM")


def clean_tags(tags: list[str]) -> list[str]:
    seen: list[str] = []
    for t in tags:
        t = t.strip().lstrip("#")
        if t and t not in seen:
            seen.append(t)
    return seen


def require(task_id: int) -> dict:
    task = store.get_task(task_id)
    if task is None:
        raise ToolError(f"Aufgabe {task_id} existiert nicht")
    return task


def require_quest(quest_id: int) -> dict:
    quest = store.get_quest(quest_id)
    if quest is None:
        raise ToolError(f"Quest {quest_id} existiert nicht")
    return quest


def check_color(value: str) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ToolError(f"Ungültige Farbe '{value}', erwartet #rrggbb")
    return value.lower()


def check_stamp(value: str) -> str:
    """Zeitpunkt als ISO; ohne Zeitzone gilt die lokale."""
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(f"Ungültiger Zeitpunkt '{value}', erwartet YYYY-MM-DDTHH:MM")
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d.astimezone(TZ).isoformat(timespec="seconds")


def seconds(session: dict, now: datetime | None = None) -> int:
    end = datetime.fromisoformat(session["ended_at"]) if session["ended_at"] else (now or datetime.now(TZ))
    return max(0, int((end - datetime.fromisoformat(session["started_at"])).total_seconds()))


def duration(secs: int) -> str:
    h, m = divmod(round(secs / 60), 60)
    return f"{h} h {m} min" if h else f"{m} min"


def week_bounds(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def weekly_target() -> float:
    return float(store.get_setting("weekly_target_hours") or DEFAULT_WEEKLY_TARGET)


def settings_payload() -> dict:
    return {"weekly_target_hours": weekly_target()}


def data_etag() -> str:
    """Ändert sich mit jeder Schreiboperation und um Mitternacht."""
    return f'"{store.version()}-{today().isoformat()}"'


def focus_state() -> dict | None:
    running = store.running_session()
    if running is None:
        return None
    task = store.get_task(running["task_id"])
    return {"task_id": running["task_id"], "task": task["title"] if task else None,
            "since": running["started_at"], "running_for": duration(seconds(running))}


# --- MCP-Tools -------------------------------------------------------------

Status = Literal["todo", "doing", "deferred", "done"]
Priority = Literal["low", "medium", "high"]
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


@mcp.tool(title="Überblick", annotations=READ)
def overview() -> dict:
    """Was steht an: heutiger Tagesplan, überfällige, heutige und in Arbeit befindliche Aufgaben sowie
    die nächsten 7 Tage, dazu ein laufender Fokus-Timer und die Quest-Zeit dieser Woche.
    Zurückgestellte Aufgaben sind nur mitgezählt. Liefert außerdem das heutige Datum."""
    t = today()
    open_tasks = store.query_tasks(["status IN ('todo', 'doing')"], [], 1000)
    t_iso, week_iso = t.isoformat(), (t + timedelta(days=7)).isoformat()
    monday, sunday = week_bounds(t)
    report = time_report(monday.isoformat(), sunday.isoformat())
    return {
        "today": f"{t_iso} ({WEEKDAYS[t.weekday()]})",
        "planned_today": [x for x in open_tasks if x["plan_date"] and x["plan_date"] <= t_iso],
        "focus": focus_state(),
        "quest_time_this_week": f"{report['quest_time']} von {report['weekly_target_hours']:g} h Ziel",
        "overdue": [x for x in open_tasks if x["due_date"] and x["due_date"] < t_iso],
        "due_today": [x for x in open_tasks if x["due_date"] == t_iso],
        "in_progress": [x for x in open_tasks if x["status"] == "doing" and (x["due_date"] or "") != t_iso],
        "next_7_days": [x for x in open_tasks if x["due_date"] and t_iso < x["due_date"] <= week_iso],
        "open_without_date": sum(1 for x in open_tasks if not x["due_date"]),
        "open_total": len(open_tasks),
        "deferred_total": len(store.query_tasks(["status = 'deferred'"], [], 1000)),
    }


@mcp.tool(title="Aufgaben suchen", annotations=READ)
def list_tasks(
    status: Annotated[list[Status] | None, Field(description="Standard: offene (todo, doing); zurückgestellte nur mit 'deferred'")] = None,
    project: str | None = None,
    tag: str | None = None,
    due_from: Annotated[str | None, Field(description="YYYY-MM-DD, inklusive")] = None,
    due_to: Annotated[str | None, Field(description="YYYY-MM-DD, inklusive")] = None,
    search: Annotated[str | None, Field(description="Teiltext in Titel oder Notizen")] = None,
    quest_id: int | None = None,
    plan_date: Annotated[str | None, Field(description="YYYY-MM-DD, für diesen Tag eingeplant")] = None,
    limit: int = 100,
) -> dict:
    """Aufgaben filtern. Sortiert nach Fälligkeit, dann Priorität."""
    where, params = [], []
    statuses = status or list(OPEN_STATUSES)
    where.append(f"status IN ({', '.join('?' for _ in statuses)})")
    params += statuses
    if project:
        where.append("project = ? COLLATE NOCASE")
        params.append(project)
    if tag:
        where.append("EXISTS (SELECT 1 FROM json_each(tasks.tags) WHERE value = ? COLLATE NOCASE)")
        params.append(tag.lstrip("#"))
    if due_from:
        where.append("due_date >= ?")
        params.append(check_date(due_from))
    if due_to:
        where.append("due_date <= ?")
        params.append(check_date(due_to))
    if search:
        where.append("(title LIKE ? OR notes LIKE ?)")
        params += [f"%{search}%"] * 2
    if quest_id is not None:
        where.append("quest_id = ?")
        params.append(quest_id)
    if plan_date:
        where.append("plan_date = ?")
        params.append(check_date(plan_date))
    tasks = store.query_tasks(where, params, max(1, min(limit, 500)))
    return {"count": len(tasks), "tasks": tasks}


@mcp.tool(title="Aufgabe anzeigen", annotations=READ)
def get_task(task_id: int) -> dict:
    """Eine Aufgabe mit allen Feldern und der insgesamt erfassten Zeit."""
    task = require(task_id)
    tracked = sum(seconds(x) for x in store.sessions() if x["task_id"] == task_id)
    return {**task, "tracked": duration(tracked)}


@mcp.tool(title="Projekte und Tags", annotations=READ)
def list_projects() -> dict:
    """Alle vorhandenen Projekte (mit Anzahl offener Aufgaben) und Tags."""
    return {"projects": store.projects(), "tags": store.tags()}


@mcp.tool(title="Aufgabe anlegen", annotations=WRITE)
def add_task(
    title: str,
    notes: Annotated[str, Field(description="Details, Markdown erlaubt")] = "",
    due_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    due_time: Annotated[str | None, Field(description="HH:MM, nur mit due_date")] = None,
    priority: Priority | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    status: Status = "todo",
    quest_id: Annotated[int | None, Field(description="ID aus list_quests")] = None,
    plan_date: Annotated[str | None, Field(description="YYYY-MM-DD, Tag im Tagesplan")] = None,
) -> dict:
    """Neue Aufgabe anlegen."""
    if not title.strip():
        raise ToolError("Titel darf nicht leer sein")
    if due_time and not due_date:
        raise ToolError("due_time braucht ein due_date")
    if quest_id is not None:
        require_quest(quest_id)
    return store.insert_task(
        {
            "title": title.strip(),
            "notes": notes.strip(),
            "status": status,
            "priority": priority,
            "due_date": check_date(due_date) if due_date else None,
            "due_time": check_time(due_time) if due_time else None,
            "project": project.strip() if project and project.strip() else None,
            "tags": clean_tags(tags or []),
            "quest_id": quest_id,
            "plan_date": check_date(plan_date) if plan_date else None,
        },
        now_iso(),
    )


@mcp.tool(title="Aufgabe ändern", annotations=WRITE)
def update_task(
    task_id: int,
    title: str | None = None,
    notes: str | None = None,
    status: Status | None = None,
    due_date: Annotated[str | None, Field(description="YYYY-MM-DD; leerer String entfernt das Datum (und die Uhrzeit)")] = None,
    due_time: Annotated[str | None, Field(description="HH:MM; leerer String entfernt die Uhrzeit")] = None,
    priority: Annotated[Priority | Literal["none"] | None, Field(description="'none' entfernt die Priorität")] = None,
    project: Annotated[str | None, Field(description="Leerer String entfernt das Projekt")] = None,
    tags: Annotated[list[str] | None, Field(description="Ersetzt alle Tags; [] entfernt sie")] = None,
    quest_id: Annotated[int | None, Field(description="ID aus list_quests; 0 entfernt die Zuordnung")] = None,
    plan_date: Annotated[str | None, Field(description="YYYY-MM-DD; leerer String nimmt die Aufgabe aus dem Plan")] = None,
) -> dict:
    """Felder einer Aufgabe ändern. Nicht angegebene Felder bleiben unverändert."""
    current = require(task_id)
    fields: dict = {}
    if title is not None:
        if not title.strip():
            raise ToolError("Titel darf nicht leer sein")
        fields["title"] = title.strip()
    if notes is not None:
        fields["notes"] = notes.strip()
    if status is not None:
        fields["status"] = status
    if due_date is not None:
        fields["due_date"] = check_date(due_date) if due_date else None
        if not due_date:
            fields["due_time"] = None
    if due_time is not None:
        if due_time and not (fields.get("due_date") or (current["due_date"] and "due_date" not in fields)):
            raise ToolError("due_time braucht ein due_date")
        fields["due_time"] = check_time(due_time) if due_time else None
    if priority is not None:
        fields["priority"] = None if priority == "none" else priority
    if project is not None:
        fields["project"] = project.strip() or None
    if tags is not None:
        fields["tags"] = clean_tags(tags)
    if quest_id is not None:
        fields["quest_id"] = require_quest(quest_id)["id"] if quest_id else None
    if plan_date is not None:
        fields["plan_date"] = check_date(plan_date) if plan_date else None
    if not fields:
        return current
    now = now_iso()
    # Erledigte oder zurückgestellte Aufgaben laufen nicht weiter
    running = store.running_session()
    if fields.get("status") in ("done", "deferred") and running and running["task_id"] == task_id:
        store.stop_session(now)
    return store.update_task(task_id, fields, now)


@mcp.tool(title="Aufgaben erledigen", annotations=WRITE)
def complete_tasks(task_ids: list[int]) -> dict:
    """Eine oder mehrere Aufgaben als erledigt markieren."""
    for task_id in task_ids:
        require(task_id)
    return {"completed": [update_task(i, status="done") for i in task_ids]}


@mcp.tool(
    title="Aufgaben löschen",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
)
def delete_tasks(task_ids: list[int]) -> dict:
    """Aufgaben endgültig löschen. Für Erledigtes lieber complete_tasks verwenden."""
    for task_id in task_ids:
        require(task_id)
    return {"deleted": [i for i in task_ids if store.delete_task(i)]}


@mcp.tool(title="Quests anzeigen", annotations=READ)
def list_quests(include_completed: bool = False) -> dict:
    """Quests (übergeordnete Ziele) mit Anzahl offener Aufgaben und erfasster Zeit diese Woche und gesamt."""
    monday, _ = week_bounds(today())
    sessions = store.sessions()
    quest_of = {t["id"]: t["quest_id"] for t in store.all_tasks()}
    open_count: dict[int, int] = {}
    for t in store.query_tasks(["status IN ('todo', 'doing')", "quest_id IS NOT NULL"], [], 10000):
        open_count[t["quest_id"]] = open_count.get(t["quest_id"], 0) + 1
    out = []
    for q in store.quests():
        if q["status"] == "completed" and not include_completed:
            continue
        mine = [x for x in sessions if quest_of.get(x["task_id"]) == q["id"]]
        week = [x for x in mine if x["started_at"][:10] >= monday.isoformat()]
        out.append({**q, "open_tasks": open_count.get(q["id"], 0),
                    "this_week": duration(sum(map(seconds, week))), "total": duration(sum(map(seconds, mine)))})
    return {"quests": out}


@mcp.tool(title="Quest anlegen", annotations=WRITE)
def add_quest(
    title: str,
    notes: Annotated[str, Field(description="Warum das Ziel wichtig ist, woran man den Erfolg erkennt")] = "",
    color: Annotated[str | None, Field(description="#rrggbb, sonst automatisch")] = None,
) -> dict:
    """Neue Quest (übergeordnetes Ziel) anlegen. Vorher mit list_quests prüfen, ob es sie schon gibt."""
    if not title.strip():
        raise ToolError("Titel darf nicht leer sein")
    color = check_color(color) if color else QUEST_COLORS[len(store.quests()) % len(QUEST_COLORS)]
    return store.insert_quest({"title": title.strip(), "notes": notes.strip(), "color": color}, now_iso())


@mcp.tool(title="Quest ändern", annotations=WRITE)
def update_quest(
    quest_id: int,
    title: str | None = None,
    notes: str | None = None,
    color: Annotated[str | None, Field(description="#rrggbb")] = None,
    status: Annotated[Literal["active", "completed"] | None, Field(description="completed = Ziel erreicht")] = None,
) -> dict:
    """Quest umbenennen, beschreiben, umfärben, abschließen oder wieder aktivieren."""
    current = require_quest(quest_id)
    fields: dict = {}
    if title is not None:
        if not title.strip():
            raise ToolError("Titel darf nicht leer sein")
        fields["title"] = title.strip()
    if notes is not None:
        fields["notes"] = notes.strip()
    if color is not None:
        fields["color"] = check_color(color)
    if status is not None:
        fields["status"] = status
    return store.update_quest(quest_id, fields, now_iso()) if fields else current


@mcp.tool(title="Fokus starten", annotations=WRITE)
def start_focus(task_id: int) -> dict:
    """Startet den Timer für eine Aufgabe. Ein laufender Timer einer anderen Aufgabe wird beendet.
    Eine offene Aufgabe wechselt dabei auf 'doing'."""
    task = require(task_id)
    if task["status"] in ("done", "deferred"):
        raise ToolError("Die Aufgabe ist erledigt oder zurückgestellt")
    now = now_iso()
    store.start_session(task_id, now)
    if task["status"] == "todo":
        store.update_task(task_id, {"status": "doing"}, now)
    return {"focus": focus_state()}


@mcp.tool(title="Fokus beenden", annotations=WRITE)
def stop_focus(complete: Annotated[bool, Field(description="Aufgabe dabei als erledigt markieren")] = False) -> dict:
    """Hält den laufenden Timer an und speichert die Zeit."""
    running = store.running_session()
    if running is None:
        return {"stopped": None}
    stopped = store.stop_session(now_iso())
    task = update_task(running["task_id"], status="done") if complete else store.get_task(running["task_id"])
    return {"stopped": {"task": task, "duration": duration(seconds(stopped)) if stopped else "unter 10 s, verworfen"}}


@mcp.tool(title="Zeitauswertung", annotations=READ)
def time_report(
    date_from: Annotated[str | None, Field(description="YYYY-MM-DD, Standard: Montag dieser Woche")] = None,
    date_to: Annotated[str | None, Field(description="YYYY-MM-DD, inklusive, Standard: Sonntag dieser Woche")] = None,
) -> dict:
    """Erfasste Fokuszeit im Zeitraum: gesamt, Quest-Zeit gegenüber Sonstigem, je Quest, je Tag und je Aufgabe."""
    monday, sunday = week_bounds(today())
    start = check_date(date_from) if date_from else monday.isoformat()
    end = check_date(date_to) if date_to else sunday.isoformat()
    tasks = {t["id"]: t for t in store.all_tasks()}
    quests = {q["id"]: q["title"] for q in store.quests()}
    now = datetime.now(TZ)
    by_quest: dict[str, int] = {}
    by_day: dict[str, int] = {}
    by_task: dict[int, int] = {}
    quest_secs = total = 0
    for x in store.sessions(start, end):
        secs = seconds(x, now)
        total += secs
        quest_id = tasks.get(x["task_id"], {}).get("quest_id")
        if quest_id:
            quest_secs += secs
            by_quest[quests.get(quest_id, "?")] = by_quest.get(quests.get(quest_id, "?"), 0) + secs
        by_day[x["started_at"][:10]] = by_day.get(x["started_at"][:10], 0) + secs
        by_task[x["task_id"]] = by_task.get(x["task_id"], 0) + secs
    return {
        "from": start, "to": end,
        "total": duration(total), "quest_time": duration(quest_secs), "other_time": duration(total - quest_secs),
        "quest_share_percent": round(100 * quest_secs / total) if total else 0,
        "weekly_target_hours": weekly_target(),
        "by_quest": {k: duration(v) for k, v in sorted(by_quest.items(), key=lambda kv: -kv[1])},
        "by_day": {k: duration(v) for k, v in sorted(by_day.items())},
        "by_task": [{"task_id": k, "title": tasks[k]["title"] if k in tasks else "?", "time": duration(v)}
                    for k, v in sorted(by_task.items(), key=lambda kv: -kv[1])[:20]],
    }


# --- Weboberfläche ---------------------------------------------------------

_failed_logins: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "?")


async def throttled(request: Request, check, message: str) -> str | None:
    """Führt eine Prüfung mit Bremse gegen Durchprobieren aus (10 Fehlversuche je IP in 15 Minuten).
    Liefert eine Fehlermeldung oder None."""
    ip, now = client_ip(request), time.time()
    recent = [t for t in _failed_logins.get(ip, []) if t > now - 900]
    if len(recent) >= 10:
        return "Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen."
    if check():
        _failed_logins.pop(ip, None)
        return None
    _failed_logins[ip] = [*recent, now]
    if len(_failed_logins) > 5000:  # Speicher begrenzen, falls viele Adressen probieren
        for stale in [k for k, v in _failed_logins.items() if v[-1] < now - 900]:
            del _failed_logins[stale]
    await asyncio.sleep(1)
    return message


async def verify_login(request: Request, key: str | None, code: str) -> str | None:
    """Key (None = schon angemeldet) und – falls eingeschaltet – den zweiten Faktor prüfen.
    Der Code wird erst geprüft, wenn der Key stimmt, damit Fremde keine Wiederherstellungscodes verbrauchen."""
    two_factor = auth.two_factor_enabled()
    message = ("Der Code ist falsch." if key is None else "Key oder Code ist falsch.") if two_factor else "Der Key ist falsch."
    return await throttled(request, lambda: (key is None or auth.check_key(key)) and auth.check_second_factor(code), message)


def logged_in(request: Request) -> bool:
    return auth.check_session(request.cookies.get(SESSION_COOKIE))


def with_session(response: Response) -> Response:
    response.set_cookie(
        SESSION_COOKIE, auth.make_session(), max_age=90 * 24 * 3600, httponly=True,
        secure=PUBLIC_URL.startswith("https://"), samesite="lax",
    )
    return response


# Login- und Freigabeseiten kommen ohne Skripte aus. Kein form-action: Nach der OAuth-Freigabe leitet
# der Server zum Client (z. B. claude.ai) weiter, das würde form-action blockieren.
CARD_CSP = ("default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; manifest-src 'self'; frame-ancestors 'self'; base-uri 'none'")


def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    template = (STATIC / "card.html").read_text()
    return HTMLResponse(template.replace("{{title}}", html.escape(title)).replace("{{body}}", body), status_code,
                        headers={"Content-Security-Policy": CARD_CSP, "Cache-Control": "no-store"})


def key_form(action: str, error: str | None, hidden: str = "", button: str = "Anmelden", ask_key: bool = True) -> str:
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    key = """<label class="field"><input type="password" name="key" required autofocus autocomplete="current-password" placeholder=" ">
      <span>Zugangs-Key</span></label>""" if ask_key else ""
    code = f"""<label class="field"><input name="code" required {"" if ask_key else "autofocus"} inputmode="numeric"
      autocomplete="one-time-code" maxlength="20" placeholder=" "><span>Code aus der Authenticator-App</span></label>
      <p class="hint">Handy nicht zur Hand? Ein Wiederherstellungscode funktioniert auch.</p>""" if auth.two_factor_enabled() else ""
    return f"""<form method="post" action="{action}">
      {hidden}
      {key}{code}
      {err}
      <div class="actions"><button class="filled" type="submit">{button}</button></div>
    </form>"""


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True})


def accepts_gzip(request: Request) -> bool:
    return "gzip" in request.headers.get("accept-encoding", "")


def compressed(request: Request, body: bytes, media_type: str, headers: dict[str, str]) -> Response:
    headers = {**headers, "Vary": "Accept-Encoding"}
    if len(body) > 1024 and accepts_gzip(request):
        return Response(gzip.compress(body, 6), media_type=media_type, headers={**headers, "Content-Encoding": "gzip"})
    return Response(body, media_type=media_type, headers=headers)


_page_cache: dict = {}


def index_page() -> tuple[bytes, bytes, str]:
    """index.html roh und gzip-komprimiert, neu eingelesen nur wenn sich die Datei ändert."""
    path = STATIC / "index.html"
    mtime = path.stat().st_mtime_ns
    if _page_cache.get("mtime") != mtime:
        raw = path.read_bytes()
        # Nur das eine eingebettete Skript darf laufen – eingeschleustes HTML kann kein Skript ausführen
        scripts = re.findall(rb"<script>(.*?)</script>", raw, re.S)
        hashes = " ".join(f"'sha256-{base64.b64encode(hashlib.sha256(x).digest()).decode()}'" for x in scripts)
        csp = (f"default-src 'self'; script-src {hashes}; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
               "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; worker-src 'self'; "
               "manifest-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
        _page_cache.update(mtime=mtime, raw=raw, gz=gzip.compress(raw, 9), etag=f'"{hashlib.sha1(raw).hexdigest()[:16]}"', csp=csp)
    return _page_cache["raw"], _page_cache["gz"], _page_cache["etag"]


@mcp.custom_route("/", methods=["GET"])
async def index(request: Request) -> Response:
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    raw, gz, etag = index_page()
    headers = {"Cache-Control": "no-cache", "ETag": etag, "Vary": "Accept-Encoding", "Content-Security-Policy": _page_cache["csp"]}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    if accepts_gzip(request):
        return Response(gz, media_type="text/html; charset=utf-8", headers={**headers, "Content-Encoding": "gzip"})
    return Response(raw, media_type="text/html; charset=utf-8", headers=headers)


@mcp.custom_route("/login", methods=["GET", "POST"])
async def login(request: Request) -> Response:
    error = None
    if request.method == "POST" and not same_origin(request):
        # Fremde Seiten dürfen keine Fehlversuche auslösen (das würde dich für 15 Minuten aussperren)
        return page("Abgelehnt", '<h1>Anfrage abgelehnt</h1><p class="sub">Das Formular kam nicht von dieser Seite.</p>', 403)
    if request.method == "POST":
        form = await request.form()
        error = await verify_login(request, str(form.get("key", "")), str(form.get("code", "")))
        if error is None:
            return with_session(RedirectResponse("/", status_code=303))
    elif logged_in(request):
        return RedirectResponse("/", status_code=303)
    what = "deinem Zugangs-Key und dem Code aus der Authenticator-App" if auth.two_factor_enabled() else "deinem Zugangs-Key"
    body = f'<h1>Aufgaben</h1><p class="sub">Melde dich mit {what} an.</p>' + key_form("/login", error)
    return page("Anmelden", body, 401 if error else 200)


@mcp.custom_route("/logout", methods=["POST"])
async def logout(request: Request) -> Response:
    if not same_origin(request):
        return Response(status_code=403)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@mcp.custom_route("/api/tasks", methods=["GET"])
async def api_tasks(request: Request) -> Response:
    if not logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    etag = data_etag()
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    body = JSONResponse({
        "today": today().isoformat(), "now": now_iso(), "tasks": store.all_tasks(),
        "quests": store.quests(), "sessions": store.sessions(), "settings": settings_payload(),
    }).body
    return compressed(request, body, "application/json", {"ETag": etag, "Cache-Control": "no-cache"})


@mcp.custom_route("/api/events", methods=["GET"])
async def api_events(request: Request) -> Response:
    """Server-Sent Events: jede Änderung (Web, Handy, Claude) kommt sofort bei allen offenen Seiten an."""
    if not logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return StreamingResponse(live.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def same_origin(request: Request) -> bool:
    """CSRF-Schutz für schreibende Browser-Anfragen: nur von der eigenen Seite."""
    origin = request.headers.get("origin")
    if origin is None:
        return request.headers.get("sec-fetch-site") in (None, "same-origin")
    return origin == f"{request.url.scheme}://{request.url.netloc}" or origin == PUBLIC_URL


EDITABLE = {"title", "notes", "status", "due_date", "due_time", "priority", "project", "tags", "quest_id", "plan_date"}


def invalid_fields(body: dict) -> str | None:
    """Typprüfung, die bei MCP-Aufrufen das SDK übernimmt."""
    for key in ("title", "notes", "due_date", "due_time", "project", "plan_date", "color", "started_at", "ended_at"):
        if key in body and not isinstance(body[key], str):
            return f"{key} muss ein Text sein"
    if body.get("quest_id", 0) is None:
        body["quest_id"] = 0  # null aus der Oberfläche = Zuordnung entfernen
    for key in ("quest_id", "task_id"):
        if key in body and (not isinstance(body[key], int) or isinstance(body[key], bool)):
            return f"{key} muss eine Zahl sein"
    if "status" in body and body["status"] not in STATUSES:
        return f"status muss einer von {', '.join(STATUSES)} sein"
    if "priority" in body and body["priority"] not in (*PRIORITIES, "none"):
        return "priority muss low, medium, high oder none sein"
    if "tags" in body and not (isinstance(body["tags"], list) and all(isinstance(t, str) for t in body["tags"])):
        return "tags muss eine Liste von Texten sein"
    return None


@mcp.custom_route("/api/tasks/{task_id:int}", methods=["PATCH", "DELETE"])
async def api_task(request: Request) -> Response:
    if denied := guard(request):
        return denied
    task_id = request.path_params["task_id"]
    try:
        if request.method == "DELETE":
            delete_tasks([task_id])
            return Response(status_code=204)
        body = await json_body(request, EDITABLE)
        # Gleiche Prüfregeln wie das MCP-Tool
        return JSONResponse(update_task(task_id, **body))
    except ToolError as e:
        return tool_error(e)
    except (ValueError, TypeError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)


def guard(request: Request) -> Response | None:
    """Schreibende Browser-Anfragen: angemeldet und von der eigenen Seite."""
    if not logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


async def json_body(request: Request, allowed: set[str]) -> dict:
    body = await request.json()
    if not isinstance(body, dict) or not set(body) <= allowed:
        raise ToolError(f"Erlaubte Felder: {', '.join(sorted(allowed))}")
    problem = invalid_fields(body)
    if problem:
        raise ToolError(problem)
    return body


def tool_error(e: ToolError) -> Response:
    return JSONResponse({"error": str(e)}, status_code=404 if "existiert nicht" in str(e) else 400)


@mcp.custom_route("/api/quests", methods=["POST"])
@mcp.custom_route("/api/quests/{quest_id:int}", methods=["PATCH", "DELETE"])
async def api_quest(request: Request) -> Response:
    if denied := guard(request):
        return denied
    try:
        if request.method == "POST":
            return JSONResponse(add_quest(**await json_body(request, {"title", "notes", "color"})))
        quest_id = request.path_params["quest_id"]
        if request.method == "DELETE":
            require_quest(quest_id)
            store.delete_quest(quest_id)
            return Response(status_code=204)
        body = await json_body(request, {"title", "notes", "color", "status"})
        if "status" in body and body["status"] not in QUEST_STATUSES:
            raise ToolError("status muss active oder completed sein")
        return JSONResponse(update_quest(quest_id, **body))
    except ToolError as e:
        return tool_error(e)
    except (ValueError, TypeError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)


@mcp.custom_route("/api/focus", methods=["POST"])
async def api_focus(request: Request) -> Response:
    """{"task_id": n} startet den Timer, {} hält ihn an."""
    if denied := guard(request):
        return denied
    try:
        body = await json_body(request, {"task_id"})
        return JSONResponse(start_focus(body["task_id"]) if "task_id" in body else stop_focus())
    except ToolError as e:
        return tool_error(e)
    except (ValueError, TypeError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)


def checked_span(task_id: int, started_at: str, ended_at: str) -> tuple[int, str, str]:
    require(task_id)
    start, end = check_stamp(started_at), check_stamp(ended_at)
    span = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    if span <= 0:
        raise ToolError("Das Ende muss nach dem Beginn liegen")
    if span > 24 * 3600:
        raise ToolError("Ein Eintrag darf höchstens 24 Stunden lang sein")
    return task_id, start, end


@mcp.custom_route("/api/sessions", methods=["POST"])
@mcp.custom_route("/api/sessions/{session_id:int}", methods=["PATCH", "DELETE"])
async def api_session(request: Request) -> Response:
    """Zeit nachtragen, korrigieren oder löschen."""
    if denied := guard(request):
        return denied
    try:
        if request.method == "POST":
            body = await json_body(request, {"task_id", "started_at", "ended_at"})
            return JSONResponse(store.insert_session(*checked_span(body["task_id"], body["started_at"], body["ended_at"])))
        session = store.get_session(request.path_params["session_id"])
        if session is None:
            raise ToolError("Eintrag existiert nicht")
        if request.method == "DELETE":
            store.delete_session(session["id"])
            return Response(status_code=204)
        if session["ended_at"] is None:
            raise ToolError("Der Timer läuft noch")
        body = {**session, **await json_body(request, {"task_id", "started_at", "ended_at"})}
        task_id, start, end = checked_span(body["task_id"], body["started_at"], body["ended_at"])
        return JSONResponse(store.update_session(session["id"], {"task_id": task_id, "started_at": start, "ended_at": end}))
    except ToolError as e:
        return tool_error(e)
    except (KeyError, ValueError, TypeError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)


@mcp.custom_route("/api/tasks", methods=["POST"])
async def api_create_task(request: Request) -> Response:
    """Aufgabe direkt in der Oberfläche anlegen (ohne KI)."""
    if denied := guard(request):
        return denied
    try:
        body = await json_body(request, {"title", "notes", "plan_date", "due_date", "priority", "project", "quest_id"})
        if body.get("quest_id") == 0:
            body.pop("quest_id")
        if body.get("priority") == "none":
            body.pop("priority")
        return JSONResponse(add_task(**body))
    except ToolError as e:
        return tool_error(e)
    except (ValueError, TypeError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)


@mcp.custom_route("/api/agent", methods=["POST"])
async def api_agent(request: Request) -> Response:
    """KI-Assistent: Beschreibung in eigenen Worten → angelegte Aufgaben."""
    if denied := guard(request):
        return denied
    try:
        text = str((await request.json()).get("text", "")).strip()
    except (ValueError, AttributeError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)
    if not text:
        return JSONResponse({"error": "Beschreib zuerst, was zu tun ist."}, status_code=400)
    if len(text) > 4000:
        return JSONResponse({"error": "Die Beschreibung ist zu lang (höchstens 4000 Zeichen)."}, status_code=400)
    try:
        reply, fields = await agent.run(text)
    except AgentError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    created = []
    for f in fields:
        try:
            created.append(add_task(**f))
        except ToolError:
            created.append(add_task(title=f["title"], notes=f.get("notes", "")))  # Felder verwerfen, Aufgabe behalten
    return JSONResponse({"reply": reply, "tasks": created, "model": agent.config().model})


@mcp.custom_route("/api/agent/config", methods=["GET", "PUT"])
async def api_agent_config(request: Request) -> Response:
    if request.method == "GET":
        if not logged_in(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(agent.public_config())
    if denied := guard(request):
        return denied
    try:
        body = await request.json()
        return JSONResponse(agent.save(
            str(body.get("provider", "")), str(body.get("base_url") or ""), str(body.get("model") or ""),
            str(body["api_key"]) if body.get("api_key") else None, bool(body.get("clear_key")),
        ))
    except AgentError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except (ValueError, AttributeError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)


@mcp.custom_route("/api/agent/models", methods=["POST"])
async def api_agent_models(request: Request) -> Response:
    """Modelle beim Anbieter abfragen – prüft dabei gleich den Key, auch vor dem Speichern."""
    if denied := guard(request):
        return denied
    try:
        body = await request.json()
        cfg = agent.draft(str(body.get("provider", "")), body.get("base_url"), body.get("api_key") or None)
        return JSONResponse({"models": await agent.models(cfg)})
    except AgentError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except (ValueError, AttributeError):
        return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)


@mcp.custom_route(f"/skill/{SKILL_NAME}.zip", methods=["GET"])
async def skill_download(request: Request) -> Response:
    """Skill für Claude-Apps: legt Aufgaben über den todo-Connector genauso an wie der Assistent hier."""
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return Response(skill_zip(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{SKILL_NAME}.zip"'})


@mcp.custom_route("/api/settings", methods=["PATCH"])
async def api_settings(request: Request) -> Response:
    if denied := guard(request):
        return denied
    try:
        body = await request.json()
        target = body["weekly_target_hours"]
        if isinstance(target, bool) or not isinstance(target, (int, float)) or not 0 < target <= 168:
            raise ValueError
        store.set_setting("weekly_target_hours", str(target))
        store.touch_settings()
        return JSONResponse(settings_payload())
    except (KeyError, ValueError, TypeError):
        return JSONResponse({"error": "Wochenziel muss zwischen 0 und 168 Stunden liegen"}, status_code=400)


@mcp.custom_route("/oauth/login", methods=["GET", "POST"])
async def oauth_login(request: Request) -> Response:
    """Zustimmungsseite im OAuth-Flow: wer den Key kennt (oder schon eingeloggt ist), erlaubt den Zugriff."""
    if request.method == "POST":
        form = await request.form()
        request_id = str(form.get("req", ""))
    else:
        request_id = request.query_params.get("req", "")
    pending = auth.pending_request(request_id)
    if pending is None:
        return page("Abgelaufen", '<h1>Anfrage abgelaufen</h1><p class="sub">Bitte starte die Verbindung in Claude erneut.</p>', 400)
    client, params = pending
    client_name = html.escape(client.client_name or "Ein MCP-Client")
    target = html.escape(urlparse(str(params.redirect_uri)).hostname or str(params.redirect_uri))
    if request.method == "POST" and not same_origin(request):
        return page("Abgelehnt", '<h1>Anfrage abgelehnt</h1><p class="sub">Das Formular kam nicht von dieser Seite.</p>', 403)

    # Eine Freigabe gilt 90 Tage – deshalb immer Key (und ggf. Code) verlangen, auch wenn schon angemeldet.
    # Ein einzelner Klick auf einen untergeschobenen Link reicht so nicht.
    error = None
    if request.method == "POST":
        if form.get("action") == "deny":
            return RedirectResponse(auth.deny(request_id), status_code=303)
        error = await verify_login(request, str(form.get("key", "")), str(form.get("code", "")))
        if error is None:
            return with_session(RedirectResponse(auth.approve(request_id), status_code=303))

    hidden = f'<input type="hidden" name="req" value="{html.escape(request_id)}">'
    intro = (f'<h1>Zugriff erlauben?</h1><p class="sub"><b>{client_name}</b> möchte deine Aufgaben lesen, '
             f'anlegen und bearbeiten. Danach geht es weiter zu <b>{target}</b>.</p>')
    return page("Zugriff erlauben", intro + key_form("/oauth/login", error, hidden, "Erlauben"), 401 if error else 200)


# --- Sicherheit: Zwei-Faktor-Anmeldung & Abmelden überall --------------------


@mcp.custom_route("/api/security", methods=["GET"])
async def api_security(request: Request) -> Response:
    if not logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"two_factor": auth.two_factor_enabled(), "recovery_left": auth.recovery_left()})


@mcp.custom_route("/api/security/{action:str}", methods=["POST"])
async def api_security_action(request: Request) -> Response:
    if denied := guard(request):
        return denied
    action = request.path_params["action"]
    try:
        code = str((await request.json()).get("code", ""))
    except (ValueError, AttributeError):
        code = ""
    if action == "2fa-setup":
        if auth.two_factor_enabled():
            return JSONResponse({"error": "Die Zwei-Faktor-Anmeldung ist schon aktiv."}, status_code=400)
        return JSONResponse(auth.begin_two_factor(urlparse(PUBLIC_URL).hostname or "Aufgaben"))
    if action == "2fa-enable":
        result: dict = {}
        def enable() -> bool:
            result["codes"] = auth.enable_two_factor(code)
            return result["codes"] is not None
        if error := await throttled(request, enable, "Der Code passt nicht. Prüfe die Uhrzeit des Handys und versuch es erneut."):
            return JSONResponse({"error": error}, status_code=400)
        live.close_all()  # alte Sitzungen sind ungültig – offene Live-Verbindungen auch
        return with_session(JSONResponse({"recovery_codes": result["codes"]}))
    if action == "2fa-disable":
        if error := await throttled(request, lambda: auth.disable_two_factor(code), "Der Code ist falsch."):
            return JSONResponse({"error": error}, status_code=400)
        return JSONResponse({"ok": True})
    if action == "recovery-codes":
        if error := await throttled(request, lambda: auth.check_second_factor(code), "Der Code ist falsch."):
            return JSONResponse({"error": error}, status_code=400)
        return JSONResponse({"recovery_codes": auth.new_recovery_codes()})
    if action == "logout-all":
        auth.revoke_all()
        live.close_all()
        return with_session(JSONResponse({"ok": True}))
    return JSONResponse({"error": "Unbekannte Aktion"}, status_code=404)


# --- PWA & Push ------------------------------------------------------------


@mcp.custom_route("/manifest.webmanifest", methods=["GET"])
async def manifest(request: Request) -> Response:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@mcp.custom_route("/sw.js", methods=["GET"])
async def service_worker(request: Request) -> Response:
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@mcp.custom_route("/icons/{name:str}", methods=["GET"])
async def icons(request: Request) -> Response:
    path = (STATIC / "icons" / request.path_params["name"]).resolve()
    if path.parent != (STATIC / "icons").resolve() or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


@mcp.custom_route("/api/push", methods=["GET", "POST", "DELETE"])
async def api_push(request: Request) -> Response:
    """GET: Public Key, POST: Abo speichern, DELETE: Abo entfernen."""
    if not logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "GET":
        return JSONResponse({"publicKey": push.public_key})
    if not same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
        endpoint = body["endpoint"]
        if request.method == "DELETE":
            store.delete_subscription(endpoint)
            return Response(status_code=204)
        if not str(endpoint).startswith("https://"):
            raise ValueError
        store.add_subscription(endpoint, body["keys"]["p256dh"], body["keys"]["auth"], request.headers.get("user-agent"))
        return JSONResponse({"ok": True})
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "Ungültiges Abo"}, status_code=400)


@mcp.custom_route("/api/push/test", methods=["POST"])
async def api_push_test(request: Request) -> Response:
    if not logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    sent = await push.send("Benachrichtigungen aktiv", "So sehen deine Erinnerungen aus.", tag="test")
    return JSONResponse({"sent": sent})


def allowed_hosts() -> list[str]:
    host = urlparse(PUBLIC_URL).netloc
    return [host, "localhost:*", "127.0.0.1:*"]


app = mcp.streamable_http_app(
    transport_security=TransportSecuritySettings(
        allowed_hosts=allowed_hosts(),
        allowed_origins=[PUBLIC_URL, "http://localhost:*", "http://127.0.0.1:*", "https://claude.ai"],
    ),
)

MAX_BODY = 1_000_000  # Bytes; die größten echten Anfragen sind Beschreibungen mit wenigen KB

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
if PUBLIC_URL.startswith("https://"):
    SECURITY_HEADERS["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


class SecurityHeaders:
    """Reine ASGI-Middleware (bricht Streams wie SSE nicht auf) für Sicherheits-Header auf allen Antworten."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length", b"0")
        if length.isdigit() and int(length) > MAX_BODY:
            response = Response("Anfrage zu groß", status_code=413)
            return await response(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)

_mcp_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def lifespan(app_):
    """MCP-Sessions plus Erinnerungs-Takt im Hintergrund."""
    live.start()
    reminders = asyncio.create_task(push.run())
    try:
        async with _mcp_lifespan(app_):
            yield
    finally:
        reminders.cancel()


app.router.lifespan_context = lifespan


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # Offene Event-Streams sollen ein Deployment nicht aufhalten
    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=True, forwarded_allow_ips="*", timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
