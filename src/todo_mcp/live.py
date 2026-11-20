"""Echtzeit-Synchronisierung per Server-Sent Events.

Jede Schreiboperation im Store meldet (Art, ID). Die Meldungen werden kurz gesammelt und dann als
ein Ereignis mit den geänderten Zeilen an alle offenen Oberflächen geschickt. Schreibzugriffe laufen
weiter über die REST-Routen bzw. MCP-Tools; der Kanal ist nur Server → Browser.

Läuft in einem Prozess (eine Fly-Maschine mit SQLite), deshalb genügt ein Verteiler im Speicher.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable

from .db import Store

log = logging.getLogger("todo.live")

HEARTBEAT = 20  # Sekunden; hält die Verbindung durch Proxys (Fly schließt nach 60 s Stille)
COALESCE = 0.02  # Sekunden; mehrere Änderungen einer Aktion (z. B. Timer wechseln) in ein Ereignis
QUEUE_SIZE = 64  # wer so weit zurückliegt, wird getrennt und lädt nach dem Wiederverbinden neu


class Live:
    def __init__(self, store: Store, etag: Callable[[], str], settings: Callable[[], dict]):
        self.store = store
        self.etag = etag
        self.settings = settings
        self.clients: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pending: dict[str, set[int | None]] = {}
        self.flush_scheduled = False
        store.listeners.append(self._on_change)

    def start(self) -> None:
        self.loop = asyncio.get_running_loop()

    def _on_change(self, kind: str, ident: int | None) -> None:
        """Wird aus beliebigen Threads aufgerufen (MCP-Tools laufen teils in Worker-Threads)."""
        if self.loop is None or self.loop.is_closed() or not self.clients:
            return
        self.loop.call_soon_threadsafe(self._collect, kind, ident)

    def _collect(self, kind: str, ident: int | None) -> None:
        self.pending.setdefault(kind, set()).add(ident)
        if not self.flush_scheduled:
            self.flush_scheduled = True
            self.loop.call_later(COALESCE, self._flush)

    def _payload(self, pending: dict[str, set[int | None]]) -> dict:
        getters = {"tasks": self.store.get_task, "quests": self.store.get_quest, "sessions": self.store.get_session}
        data: dict = {"etag": self.etag(), "deleted": {}}
        for kind, get in getters.items():
            rows, gone = [], []
            for ident in sorted(pending.get(kind, ())):
                row = get(ident)
                if row:
                    rows.append(row)
                else:
                    gone.append(ident)
            if rows:
                data[kind] = rows
            if gone:
                data["deleted"][kind] = gone
        if "settings" in pending:
            data["settings"] = self.settings()
        return data

    def _flush(self) -> None:
        self.flush_scheduled = False
        pending, self.pending = self.pending, {}
        if not self.clients:
            return
        message = f"data: {json.dumps(self._payload(pending), separators=(',', ':'), ensure_ascii=False)}\n\n"
        for queue in list(self.clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Platz für das Ende-Signal schaffen; der Browser verbindet neu und lädt den ganzen Stand
                self.clients.discard(queue)
                queue.get_nowait()
                queue.put_nowait(None)
                log.info("Langsamer Client getrennt")

    def close_all(self) -> None:
        """Alle Streams beenden, z. B. nach „Überall abmelden“; gültige Sitzungen verbinden sich neu."""
        for queue in list(self.clients):
            self.clients.discard(queue)
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.clients.add(queue)
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), HEARTBEAT)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if message is None:
                    return
                yield message
        finally:
            self.clients.discard(queue)
