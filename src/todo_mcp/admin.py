"""Notfall-Befehle für den Server (über `fly ssh console` aufrufen)."""

import os
import sys

from .db import Store


def reset_two_factor() -> None:
    """Schaltet die Zwei-Faktor-Anmeldung ab, falls App und Wiederherstellungscodes verloren sind.
    Wer das ausführen kann, hat ohnehin vollen Zugriff auf den Server."""
    store = Store(os.environ.get("TODO_DB_PATH", "data/todo.db"))
    for key in ("totp_secret", "totp_pending", "totp_last", "totp_recovery"):
        store.set_setting(key, "")
    print("Zwei-Faktor-Anmeldung ist aus. Anmelden geht wieder mit dem Key allein.", file=sys.stderr)
