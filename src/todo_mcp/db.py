"""SQLite-Speicher für Aufgaben und OAuth-Zustand."""

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

STATUSES = ("todo", "doing", "deferred", "done")
OPEN_STATUSES = ("todo", "doing")  # zurückgestellte Aufgaben zählen nicht als anstehend
PRIORITIES = ("low", "medium", "high")

QUEST_STATUSES = ("active", "completed")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    notes        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'doing', 'deferred', 'done')),
    priority     TEXT CHECK (priority IN ('low', 'medium', 'high')),
    due_date     TEXT,            -- YYYY-MM-DD
    due_time     TEXT,            -- HH:MM
    project      TEXT,
    tags         TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT
);
"""
TASKS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date);
"""

SCHEMA = TASKS_TABLE + TASKS_INDEXES + """

CREATE TABLE IF NOT EXISTS quests (       -- übergeordnete Ziele, denen Aufgaben zugeordnet werden
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    notes        TEXT NOT NULL DEFAULT '',
    color        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (     -- erfasste Fokuszeit
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    started_at TEXT NOT NULL,             -- ISO mit Zeitzone
    ended_at   TEXT                       -- NULL = Timer läuft
);
CREATE INDEX IF NOT EXISTS idx_sessions_task ON sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(started_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta VALUES ('version', 0);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint   TEXT PRIMARY KEY,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    user_agent TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_notifications (
    key     TEXT PRIMARY KEY,      -- verhindert doppelte Erinnerungen, auch über Neustarts
    sent_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    data      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,   -- sha256 des Tokens, Klartext wird nie gespeichert
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id  TEXT NOT NULL,
    scopes     TEXT NOT NULL,
    expires_at INTEGER,
    resource   TEXT,
    pair_hash  TEXT                -- zugehöriger Refresh- bzw. Access-Token
);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.Lock()
        # Werden bei jeder Änderung mit (Art, ID) aufgerufen, z. B. für die Echtzeit-Synchronisierung
        self.listeners: list[Callable[[str, int | None], None]] = []

    def _migrate(self) -> None:
        self._migrate_deferred()
        # Quest-Zuordnung und geplanter Tag kamen später dazu
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        for col in ("quest_id INTEGER", "plan_date TEXT"):
            if col.split()[0] not in cols:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {col}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_plan ON tasks(plan_date)")

    def _migrate_deferred(self) -> None:
        """Ältere Datenbanken kennen den Status 'deferred' nicht. SQLite kann einen CHECK-Constraint
        nicht ändern, deshalb wird die Tabelle neu aufgebaut (IDs und Autoincrement-Zähler bleiben)."""
        sql = self._conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone()[0]
        if "'deferred'" in sql:
            return
        self._conn.executescript(f"""
            BEGIN;
            ALTER TABLE tasks RENAME TO tasks_old;
            {TASKS_TABLE}
            INSERT INTO tasks SELECT * FROM tasks_old;
            DELETE FROM sqlite_sequence WHERE name = 'tasks';
            UPDATE sqlite_sequence SET name = 'tasks' WHERE name = 'tasks_old';
            DROP TABLE tasks_old;
            {TASKS_INDEXES}
            COMMIT;
        """)

    def _q(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _x(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _bump(self) -> None:
        self._x("UPDATE meta SET value = value + 1 WHERE key = 'version'")

    def _changed(self, kind: str, *ids: int | None) -> None:
        """Version hochzählen und Beobachter informieren. kind: tasks, quests, sessions oder settings."""
        self._bump()
        for listener in self.listeners:
            for ident in ids:
                listener(kind, ident)

    def touch_settings(self) -> None:
        self._changed("settings", None)

    # --- Aufgaben ---------------------------------------------------------

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["tags"] = json.loads(d["tags"])
        return d

    def get_task(self, task_id: int) -> dict | None:
        rows = self._q("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return self._row(rows[0]) if rows else None

    def all_tasks(self) -> list[dict]:
        return [self._row(r) for r in self._q("SELECT * FROM tasks ORDER BY id")]

    def query_tasks(self, where: list[str], params: list, limit: int) -> list[dict]:
        sql = "SELECT * FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Fällige zuerst, dann nach Priorität, dann älteste zuerst
        sql += """ ORDER BY due_date IS NULL, due_date, due_time IS NULL, due_time,
                   CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
                   id LIMIT ?"""
        return [self._row(r) for r in self._q(sql, [*params, limit])]

    def insert_task(self, fields: dict, now: str) -> dict:
        fields = {**fields, "tags": json.dumps(fields.get("tags") or []), "created_at": now, "updated_at": now}
        if fields.get("status") == "done":
            fields["completed_at"] = now
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self._x(f"INSERT INTO tasks ({cols}) VALUES ({marks})", list(fields.values()))
        self._changed("tasks", cur.lastrowid)
        return self.get_task(cur.lastrowid)

    def update_task(self, task_id: int, fields: dict, now: str) -> dict | None:
        current = self.get_task(task_id)
        if current is None:
            return None
        if "tags" in fields:
            fields["tags"] = json.dumps(fields["tags"] or [])
        if "status" in fields and fields["status"] != current["status"]:
            fields["completed_at"] = now if fields["status"] == "done" else None
        fields["updated_at"] = now
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._x(f"UPDATE tasks SET {assignments} WHERE id = ?", [*fields.values(), task_id])
        self._changed("tasks", task_id)
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> bool:
        sessions = [r["id"] for r in self._q("SELECT id FROM sessions WHERE task_id = ?", (task_id,))]
        deleted = self._x("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount > 0
        self._x("DELETE FROM sessions WHERE task_id = ?", (task_id,))
        self._changed("sessions", *sessions)
        self._changed("tasks", task_id)
        return deleted

    def projects(self) -> list[dict]:
        rows = self._q(
            """SELECT project, COUNT(*) AS total, SUM(status IN ('todo', 'doing')) AS open
               FROM tasks WHERE project IS NOT NULL GROUP BY project ORDER BY project"""
        )
        return [dict(r) for r in rows]

    def tags(self) -> list[dict]:
        rows = self._q(
            """SELECT j.value AS tag, COUNT(*) AS total FROM tasks, json_each(tasks.tags) AS j
               GROUP BY j.value ORDER BY j.value"""
        )
        return [dict(r) for r in rows]

    # --- Quests -----------------------------------------------------------

    def quests(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM quests ORDER BY status = 'completed', id")]

    def get_quest(self, quest_id: int) -> dict | None:
        rows = self._q("SELECT * FROM quests WHERE id = ?", (quest_id,))
        return dict(rows[0]) if rows else None

    def insert_quest(self, fields: dict, now: str) -> dict:
        fields = {**fields, "created_at": now, "updated_at": now}
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self._x(f"INSERT INTO quests ({cols}) VALUES ({marks})", list(fields.values()))
        self._changed("quests", cur.lastrowid)
        return self.get_quest(cur.lastrowid)

    def update_quest(self, quest_id: int, fields: dict, now: str) -> dict | None:
        current = self.get_quest(quest_id)
        if current is None:
            return None
        if "status" in fields and fields["status"] != current["status"]:
            fields["completed_at"] = now if fields["status"] == "completed" else None
        fields["updated_at"] = now
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._x(f"UPDATE quests SET {assignments} WHERE id = ?", [*fields.values(), quest_id])
        self._changed("quests", quest_id)
        return self.get_quest(quest_id)

    def delete_quest(self, quest_id: int) -> bool:
        """Aufgaben bleiben erhalten und verlieren nur die Zuordnung."""
        tasks = [r["id"] for r in self._q("SELECT id FROM tasks WHERE quest_id = ?", (quest_id,))]
        self._x("UPDATE tasks SET quest_id = NULL WHERE quest_id = ?", (quest_id,))
        deleted = self._x("DELETE FROM quests WHERE id = ?", (quest_id,)).rowcount > 0
        self._changed("tasks", *tasks)
        self._changed("quests", quest_id)
        return deleted

    # --- Fokuszeit --------------------------------------------------------

    def sessions(self, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        """Sitzungen, deren Beginn (lokales Datum) im Zeitraum liegt."""
        where, params = [], []
        if date_from:
            where.append("substr(started_at, 1, 10) >= ?")
            params.append(date_from)
        if date_to:
            where.append("substr(started_at, 1, 10) <= ?")
            params.append(date_to)
        sql = "SELECT * FROM sessions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY started_at"
        return [dict(r) for r in self._q(sql, params)]

    def get_session(self, session_id: int) -> dict | None:
        rows = self._q("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return dict(rows[0]) if rows else None

    def running_session(self) -> dict | None:
        rows = self._q("SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def start_session(self, task_id: int, now: str) -> dict:
        """Es läuft immer höchstens ein Timer: ein laufender wird beendet."""
        self.stop_session(now)
        cur = self._x("INSERT INTO sessions (task_id, started_at) VALUES (?, ?)", (task_id, now))
        self._changed("sessions", cur.lastrowid)
        return self.get_session(cur.lastrowid)

    def stop_session(self, now: str, min_seconds: int = 10) -> dict | None:
        """Beendet den laufenden Timer. Versehentliche Klicks (kürzer als min_seconds) werden verworfen."""
        running = self.running_session()
        if running is None:
            return None
        if (_ts(now) - _ts(running["started_at"])) < min_seconds:
            self._x("DELETE FROM sessions WHERE id = ?", (running["id"],))
            self._changed("sessions", running["id"])
            return None
        self._x("UPDATE sessions SET ended_at = ? WHERE id = ?", (now, running["id"]))
        self._changed("sessions", running["id"])
        return self.get_session(running["id"])

    def insert_session(self, task_id: int, started_at: str, ended_at: str) -> dict:
        cur = self._x("INSERT INTO sessions (task_id, started_at, ended_at) VALUES (?, ?, ?)",
                      (task_id, started_at, ended_at))
        self._changed("sessions", cur.lastrowid)
        return self.get_session(cur.lastrowid)

    def update_session(self, session_id: int, fields: dict) -> dict | None:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._x(f"UPDATE sessions SET {assignments} WHERE id = ?", [*fields.values(), session_id])
        self._changed("sessions", session_id)
        return self.get_session(session_id)

    def delete_session(self, session_id: int) -> bool:
        deleted = self._x("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0
        self._changed("sessions", session_id)
        return deleted

    # --- Sonstiges --------------------------------------------------------

    def version(self) -> int:
        """Zählt jede Schreiboperation hoch — dient als ETag für die Oberfläche."""
        return self._q("SELECT value FROM meta WHERE key = 'version'")[0]["value"]

    # --- OAuth ------------------------------------------------------------

    def get_client(self, client_id: str) -> str | None:
        rows = self._q("SELECT data FROM oauth_clients WHERE client_id = ?", (client_id,))
        return rows[0]["data"] if rows else None

    def put_client(self, client_id: str, data: str) -> None:
        self._x("INSERT OR REPLACE INTO oauth_clients (client_id, data) VALUES (?, ?)", (client_id, data))

    def put_token(self, token_hash: str, kind: str, client_id: str, scopes: list[str],
                  expires_at: int | None, resource: str | None, pair_hash: str | None) -> None:
        self._x(
            "INSERT INTO oauth_tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_hash, kind, client_id, json.dumps(scopes), expires_at, resource, pair_hash),
        )

    def get_token(self, token_hash: str, kind: str) -> dict | None:
        rows = self._q("SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ?", (token_hash, kind))
        if not rows:
            return None
        d = dict(rows[0])
        if d["expires_at"] is not None and d["expires_at"] < time.time():
            self.delete_token(token_hash)
            return None
        d["scopes"] = json.loads(d["scopes"])
        return d

    def delete_token(self, token_hash: str) -> None:
        self._x("DELETE FROM oauth_tokens WHERE token_hash = ? OR pair_hash = ?", (token_hash, token_hash))

    def clients(self) -> list[tuple[str, str]]:
        return [(r["client_id"], r["data"]) for r in self._q("SELECT client_id, data FROM oauth_clients")]

    def delete_clients(self, client_ids: list[str] | None = None) -> None:
        """Ohne Liste: alle Clients."""
        if client_ids is None:
            self._x("DELETE FROM oauth_clients")
        for client_id in client_ids or []:
            self._x("DELETE FROM oauth_clients WHERE client_id = ?", (client_id,))

    def clients_with_tokens(self) -> set[str]:
        return {r["client_id"] for r in self._q("SELECT DISTINCT client_id FROM oauth_tokens")}

    def delete_all_tokens(self) -> None:
        self._x("DELETE FROM oauth_tokens")

    def purge_expired_tokens(self) -> None:
        self._x("DELETE FROM oauth_tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (int(time.time()),))

    # --- Einstellungen & Push ---------------------------------------------

    def get_setting(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_setting(self, key: str, value: str) -> None:
        self._x("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    def subscriptions(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM push_subscriptions")]

    def add_subscription(self, endpoint: str, p256dh: str, auth: str, user_agent: str | None) -> None:
        self._x(
            "INSERT OR REPLACE INTO push_subscriptions VALUES (?, ?, ?, ?, ?)",
            (endpoint, p256dh, auth, user_agent, int(time.time())),
        )

    def delete_subscription(self, endpoint: str) -> None:
        self._x("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def mark_sent(self, key: str) -> bool:
        """True, wenn die Benachrichtigung noch nicht verschickt wurde (und jetzt als verschickt gilt)."""
        self._x("DELETE FROM sent_notifications WHERE sent_at < ?", (int(time.time()) - 30 * 86400,))
        return self._x("INSERT OR IGNORE INTO sent_notifications VALUES (?, ?)", (key, int(time.time()))).rowcount == 1
