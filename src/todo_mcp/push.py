"""Web-Push-Benachrichtigungen: Erinnerung zur Fälligkeit und abendlicher Ausblick auf morgen."""

import asyncio
import base64
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from .db import Store

log = logging.getLogger("todo.push")

CHECK_INTERVAL = 30  # Sekunden
DUE_GRACE = timedelta(minutes=15)  # verpasste Erinnerungen (z. B. nach Neustart) nur so lange nachholen
EVENING_HOUR = 20
EVENING_UNTIL_HOUR = 23
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


class Push:
    def __init__(self, store: Store, tz: ZoneInfo, contact: str):
        self.store = store
        self.tz = tz
        self.contact = contact
        pem = store.get_setting("vapid_private_pem")
        if pem is None:
            vapid = Vapid()
            vapid.generate_keys()
            pem = vapid.private_pem().decode()
            store.set_setting("vapid_private_pem", pem)
        self.vapid = Vapid.from_pem(pem.encode())
        raw = self.vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    # --- Versand ------------------------------------------------------------

    def _send_one(self, sub: dict, payload: str) -> bool:
        """True, wenn die Nachricht beim Push-Dienst angenommen wurde. Abgelaufene Abos werden gelöscht."""
        try:
            webpush(
                {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=payload,
                vapid_private_key=self.vapid,
                vapid_claims={"sub": self.contact},
                ttl=6 * 3600,
                timeout=10,
            )
            return True
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                self.store.delete_subscription(sub["endpoint"])
                return False
            log.warning("Push an %s… fehlgeschlagen: %s", sub["endpoint"][:40], e)
            return False
        except Exception:
            log.exception("Push an %s… fehlgeschlagen", sub["endpoint"][:40])
            return False

    async def send(self, title: str, body: str, url: str = "/", tag: str | None = None) -> int:
        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
        subs = self.store.subscriptions()
        results = await asyncio.gather(*(asyncio.to_thread(self._send_one, s, payload) for s in subs))
        return sum(results)

    # --- Zeitplan -----------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    async def tick(self) -> None:
        if not self.store.subscriptions():
            return
        now = self._now()
        await self._due_reminders(now)
        await self._evening_outlook(now)

    async def _due_reminders(self, now: datetime) -> None:
        today = now.date().isoformat()
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        tasks = self.store.query_tasks(
            ["status IN ('todo', 'doing')", "due_time IS NOT NULL", "due_date IN (?, ?)"], [yesterday, today], 500
        )
        for t in tasks:
            due = datetime.fromisoformat(f"{t['due_date']}T{t['due_time']}").replace(tzinfo=self.tz)
            if not (due <= now < due + DUE_GRACE):
                continue
            # Datum und Uhrzeit im Schlüssel: wird die Aufgabe verschoben, kommt die Erinnerung erneut
            key = f"due:{t['id']}:{t['due_date']}T{t['due_time']}"
            if not self.store.mark_sent(key):
                continue
            body = f"Jetzt fällig · {t['due_time']} Uhr" + (f" · {t['project']}" if t["project"] else "")
            await self.send(t["title"], body, url=f"/#task-{t['id']}", tag=f"task-{t['id']}")

    async def _evening_outlook(self, now: datetime) -> None:
        if not (EVENING_HOUR <= now.hour < EVENING_UNTIL_HOUR):
            return
        if not self.store.mark_sent(f"evening:{now.date().isoformat()}"):
            return
        tomorrow: date = now.date() + timedelta(days=1)
        tasks = self.store.query_tasks(["status IN ('todo', 'doing')", "due_date = ?"], [tomorrow.isoformat()], 100)
        if not tasks:
            return
        lines = [f"{t['due_time'] + ' ' if t['due_time'] else ''}{t['title']}" for t in tasks]
        title = f"Morgen ({WEEKDAYS[tomorrow.weekday()]}): {len(tasks)} {'Aufgabe' if len(tasks) == 1 else 'Aufgaben'}"
        await self.send(title, "\n".join(lines[:6]) + ("\n…" if len(lines) > 6 else ""), url="/#list", tag="evening")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Fehler im Erinnerungs-Takt")
            await asyncio.sleep(CHECK_INTERVAL)
