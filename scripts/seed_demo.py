"""Demo-Datensatz mit ausgedachten Aufgaben, Quests und erfasster Zeit – zum Ausprobieren und für Screenshots.

    TODO_ACCESS_KEY=$(openssl rand -hex 32) TODO_DB_PATH=data/demo.db uv run python scripts/seed_demo.py

Danach den Server mit derselben TODO_DB_PATH starten. Vorhandene Daten in der Datei werden nicht gelöscht.
"""

import random
from datetime import datetime, timedelta

from todo_mcp import server as s

random.seed(7)
today = s.today()
monday = today - timedelta(days=today.weekday())
day = lambda n: (today + timedelta(days=n)).isoformat()


def stamp(d, hh, mm):
    return datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm, tzinfo=s.TZ).isoformat(timespec="seconds")


book = s.add_quest("Buch fertig schreiben", "Erster Entwurf bis Ende Januar – jeden Tag ein Stück.", "#9334e6")
run = s.add_quest("Halbmarathon laufen", "Im April unter 2 Stunden ins Ziel.", "#1e8e3e")
clients = s.add_quest("Drei neue Kunden gewinnen", "Bis März drei neue Projekte für das Studio.", "#1a73e8")

T = {}
def task(key, title, **kw):
    T[key] = s.add_task(title, **kw)

task("ch7", "Kapitel 7 überarbeiten", quest_id=book["id"], plan_date=day(0), priority="high",
     notes="Dialog im zweiten Teil straffen.\n- [ ] Szene am Bahnhof kürzen\n- [ ] Übergang zu Kapitel 8")
task("outline", "Gliederung für Kapitel 8", quest_id=book["id"], plan_date=day(1))
task("research", "Recherche: Hafenstädte um 1900", quest_id=book["id"])
task("interval", "Intervalltraining 6 × 800 m", quest_id=run["id"], plan_date=day(0), project="Training")
task("longrun", "Langer Lauf 16 km", quest_id=run["id"], plan_date=day(2), project="Training")
task("shoes", "Neue Laufschuhe testen", quest_id=run["id"], priority="low", project="Training")
task("offer", "Angebot für Bäckerei Sommer schicken", quest_id=clients["id"], plan_date=day(0), due_date=day(1),
     project="Studio", priority="high", notes="Website-Relaunch, drei Pakete anbieten.\n- [ ] Preise mit Lea abstimmen\n- [ ] Referenzen anhängen")
task("portfolio", "Portfolio-Seite aktualisieren", quest_id=clients["id"], plan_date=day(3), project="Studio")
task("linkedin", "LinkedIn-Post zum Relaunch-Projekt", quest_id=clients["id"], project="Studio", tags=["marketing"])
task("mails", "E-Mails beantworten", plan_date=day(0))
task("tax", "Steuerunterlagen sortieren", due_date=day(9), project="Privat", priority="medium")
task("dentist", "Zahnarzttermin vereinbaren", due_date=day(4), due_time="09:00", project="Privat")
task("gift", "Geschenk für Noahs Geburtstag", plan_date=day(1), project="Privat")
task("bike", "Fahrrad zur Inspektion bringen", project="Privat", status="deferred")
task("invoice", "Rechnung November schreiben", project="Studio", due_date=day(10))
task("ch6", "Kapitel 6 Korrektur lesen", quest_id=book["id"], plan_date=day(-2))
task("call", "Erstgespräch mit Weingut Brandt", quest_id=clients["id"], plan_date=day(-1), project="Studio")
s.complete_tasks([T["ch6"]["id"], T["call"]["id"]])

# Erfasste Zeit: dieser Tag, diese Woche, die letzten 11 Wochen
def session(key, d, start, minutes):
    hh, mm = map(int, start.split(":"))
    begin = datetime.fromisoformat(stamp(d, hh, mm))
    s.store.insert_session(T[key]["id"], begin.isoformat(timespec="seconds"),
                           (begin + timedelta(minutes=minutes)).isoformat(timespec="seconds"))

session("mails", today, "08:15", 25)
session("offer", today, "08:45", 70)
for i in range(today.weekday()):
    d = monday + timedelta(days=i)
    session(random.choice(["ch6", "outline"]), d, "08:30", random.choice([75, 90, 110]))
    session(random.choice(["call", "portfolio", "offer"]), d, "11:00", random.choice([45, 60, 80]))
    session(random.choice(["interval", "longrun"]), d, "18:00", random.choice([40, 55]))
    session("mails", d, "14:00", random.choice([20, 35]))
for w in range(1, 12):
    for i in range(5):
        d = monday - timedelta(weeks=w) + timedelta(days=i)
        if random.random() < 0.8:
            session(random.choice(["ch6", "research"]), d, "08:30", random.choice([50, 70, 90]))
        if random.random() < 0.6:
            session(random.choice(["call", "linkedin"]), d, "11:00", random.choice([30, 45, 60]))
        if random.random() < 0.5:
            session("longrun", d, "18:00", random.choice([40, 60]))
        session("mails", d, "14:00", random.choice([20, 30, 45]))

# Ein laufender Timer: seit 25 Minuten am Kapitel
s.store.start_session(T["ch7"]["id"], (datetime.now(s.TZ) - timedelta(minutes=25)).isoformat(timespec="seconds"))
s.store.update_task(T["ch7"]["id"], {"status": "doing"}, s.now_iso())
s.store.set_setting("weekly_target_hours", "15")
print(f"Demo-Daten angelegt: {len(T)} Aufgaben, 3 Quests, {len(s.store.sessions())} Zeiteinträge")
