import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


WEB_DIR = Path(__file__).resolve().parent / "web"

LEARNER_LABELS = ["L01", "L02", "L03", "L04", "L05", "L06", "L07", "L08"]
PARTICIPATION_LEVELS = ("Low", "Medium", "High")

TITLE_MAX = 60
DURATION_MIN = 10
DURATION_MAX = 180
MAX_BODY_BYTES = 4096

DEMO_USERS = (("demo01", "demo-pass-1"), ("demo02", "demo-pass-2"))


ACTIVITIES = [
    {"id": "A01", "title": "Build a paper bridge", "level": "Grade 5-7", "minutes": 40, "topic": "Engineering",
     "materials": "Household items", "offline": True, "steps": "Fold paper into beams, span a 20 cm gap, add coins until it fails."},
    {"id": "A02", "title": "Solar oven from a box", "level": "Grade 6-8", "minutes": 60, "topic": "Science",
     "materials": "Household items", "offline": True, "steps": "Line a box with foil, tilt a lid reflector, measure the temperature rise."},
    {"id": "A03", "title": "Count and sort local seeds", "level": "Grade 3-5", "minutes": 30, "topic": "Maths",
     "materials": "None", "offline": True, "steps": "Sort seeds by size, make a tally chart, compare groups."},
    {"id": "A04", "title": "Water filter model", "level": "Grade 7-9", "minutes": 50, "topic": "Science",
     "materials": "Kit needed", "offline": True, "steps": "Layer sand, gravel and cloth in a bottle and compare water clarity."},
    {"id": "A05", "title": "Intro to block coding", "level": "Grade 7-9", "minutes": 45, "topic": "Coding",
     "materials": "Kit needed", "offline": False, "steps": "Needs internet: follow the online block editor to move a sprite."},
    {"id": "A06", "title": "Shadow clock", "level": "Grade 3-5", "minutes": 30, "topic": "Science",
     "materials": "None", "offline": True, "steps": "Mark a stick shadow every 30 minutes and plot the angle."},
    {"id": "A07", "title": "Pattern puzzles", "level": "Grade 5-7", "minutes": 20, "topic": "Maths",
     "materials": "None", "offline": True, "steps": "Continue number patterns and invent your own rule."},
]
ACTIVITY_IDS = {a["id"] for a in ACTIVITIES}


def get_activity(activity_id):
    return next((a for a in ACTIVITIES if a["id"] == activity_id), None)


_SLOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
_LABEL_RE = re.compile(r"^L0[1-8]$")


def parse_slot(text):
    if not isinstance(text, str) or not _SLOT_RE.match(text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def format_slot(moment):
    return moment.strftime("%Y-%m-%dT%H:%M")


def validate_attendance(attendance, learners):
    if attendance in (None, {}):
        return None
    if not isinstance(attendance, dict):
        return "Attendance must list each learner label."
    for label, entry in attendance.items():
        if label not in learners or not isinstance(entry, dict):
            return "Attendance can only list learners that are in this plan."
        if not isinstance(entry.get("present"), bool) or entry.get("level") not in PARTICIPATION_LEVELS:
            return "For each learner choose Present yes or no and a participation level Low, Medium or High."
    return None


def validate_plan(plan):
    errors = {}
    title = plan.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        errors["title"] = "Enter a plan title, for example 'Bridge day'."
    elif len(title) > TITLE_MAX:
        errors["title"] = f"Shorten the title to {TITLE_MAX} characters or fewer."

    if plan.get("activity_id") not in ACTIVITY_IDS:
        errors["activity_id"] = "Choose an activity from the list."

    slot = plan.get("slot_start")
    if not isinstance(slot, str) or not _SLOT_RE.match(slot):
        errors["slot_start"] = "Enter the start as date and time, for example 2026-11-10T09:00."
    elif parse_slot(slot) is None:
        errors["slot_start"] = "That date or time does not exist. Check it and try again."

    duration = plan.get("duration_min")
    if (not isinstance(duration, int) or isinstance(duration, bool)
            or not DURATION_MIN <= duration <= DURATION_MAX):
        errors["duration_min"] = f"Enter a duration between {DURATION_MIN} and {DURATION_MAX} minutes."

    learners = plan.get("learners")
    if not isinstance(learners, list) or not learners:
        errors["learners"] = "Add at least one learner label, L01 to L08."
    elif not all(isinstance(x, str) and _LABEL_RE.match(x) for x in learners) or len(set(learners)) != len(learners):
        errors["learners"] = "Use only unique anonymous labels L01 to L08. Do not type learner names."
    elif (message := validate_attendance(plan.get("attendance"), learners)):
        errors["attendance"] = message
    return errors


def find_clash(existing_plans, plan):
    start = parse_slot(plan["slot_start"])
    end = start + timedelta(minutes=plan["duration_min"])
    for other in existing_plans:
        if other["id"] == plan["id"]:
            continue
        other_start = parse_slot(other["slot_start"])
        other_end = other_start + timedelta(minutes=other["duration_min"])
        if start < other_end and other_start < end:
            return {"id": other["id"], "title": other["title"], "slot_start": other["slot_start"],
                    "free_from": format_slot(other_end)}
    return None


def filter_activities(activities, level=None, topic=None, materials=None, max_minutes=None, offline_only=False):
    result = []
    for a in activities:
        if level and a["level"] != level:
            continue
        if topic and a["topic"] != topic:
            continue
        if materials and a["materials"] != materials:
            continue
        if max_minutes and a["minutes"] > int(max_minutes):
            continue
        if offline_only and not a["offline"]:
            continue
        result.append(a)
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, facilitator_id TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, salt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions_tok(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS plans(
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL, activity_id TEXT NOT NULL,
    slot_start TEXT NOT NULL, duration_min INTEGER NOT NULL, learners TEXT NOT NULL,
    attendance TEXT NOT NULL DEFAULT '{}', saved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS saved_activities(
    user_id INTEGER NOT NULL, activity_id TEXT NOT NULL, PRIMARY KEY (user_id, activity_id));
"""


class Database:
    def __init__(self, path=":memory:"):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def add_user(self, facilitator_id, pw_hash, salt):
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO users(facilitator_id, pw_hash, salt) VALUES(?,?,?)",
                              (facilitator_id, pw_hash, salt))
            self.conn.commit()

    def get_user(self, facilitator_id):
        with self.lock:
            return self.conn.execute("SELECT * FROM users WHERE facilitator_id=?", (facilitator_id,)).fetchone()

    def create_session(self, token, user_id):
        with self.lock:
            self.conn.execute("INSERT INTO sessions_tok VALUES(?,?)", (token, user_id))
            self.conn.commit()

    def user_id_for_token(self, token):
        with self.lock:
            row = self.conn.execute("SELECT user_id FROM sessions_tok WHERE token=?", (token,)).fetchone()
        return row["user_id"] if row else None

    def delete_sessions(self, user_id):
        with self.lock:
            self.conn.execute("DELETE FROM sessions_tok WHERE user_id=?", (user_id,))
            self.conn.commit()

    @staticmethod
    def _plan_from_row(row):
        plan = dict(row)
        plan["learners"] = json.loads(plan["learners"])
        plan["attendance"] = json.loads(plan["attendance"])
        return plan

    def get_plan(self, plan_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        return self._plan_from_row(row) if row else None

    def plans_for_user(self, user_id):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM plans WHERE user_id=? ORDER BY slot_start", (user_id,)).fetchall()
        plans = [self._plan_from_row(r) for r in rows]
        for p in plans:
            p.pop("user_id")
        return plans

    def upsert_plan(self, user_id, plan, saved_at):
        with self.lock:
            self.conn.execute(
                "INSERT INTO plans(id,user_id,title,activity_id,slot_start,duration_min,learners,attendance,saved_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
                "activity_id=excluded.activity_id, slot_start=excluded.slot_start, duration_min=excluded.duration_min, "
                "learners=excluded.learners, attendance=excluded.attendance, saved_at=excluded.saved_at",
                (plan["id"], user_id, plan["title"], plan["activity_id"], plan["slot_start"], plan["duration_min"],
                 json.dumps(plan["learners"]), json.dumps(plan.get("attendance") or {}), saved_at))
            self.conn.commit()

    def saved_activity_ids(self, user_id):
        with self.lock:
            rows = self.conn.execute("SELECT activity_id FROM saved_activities WHERE user_id=? ORDER BY activity_id",
                                     (user_id,)).fetchall()
        return [r["activity_id"] for r in rows]

    def add_saved_activity(self, user_id, activity_id):
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO saved_activities VALUES(?,?)", (user_id, activity_id))
            self.conn.commit()


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def ensure_demo_users(db):
    for facilitator_id, password in DEMO_USERS:
        salt = secrets.token_hex(8)
        db.add_user(facilitator_id, hash_password(password, salt), salt)


def login(db, facilitator_id, password):
    user = db.get_user(str(facilitator_id))
    if not user or not secrets.compare_digest(user["pw_hash"], hash_password(str(password), user["salt"])):
        return None
    token = secrets.token_urlsafe(24)
    db.create_session(token, user["id"])
    return token


def user_for_token(db, token):
    return db.user_id_for_token(token) if token else None


def logout(db, user_id):
    db.delete_sessions(user_id)


_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")


def search_activities(params):
    max_minutes = params.get("max_minutes")
    if max_minutes and not str(max_minutes).isdigit():
        return 422, {"error": "max_minutes must be a number."}
    found = filter_activities(ACTIVITIES, level=params.get("level"), topic=params.get("topic"),
                              materials=params.get("materials"), max_minutes=max_minutes,
                              offline_only=params.get("offline_only") in ("1", "true"))
    return 200, found


def saved_activities(db, user_id):
    return 200, [get_activity(i) for i in db.saved_activity_ids(user_id) if get_activity(i)]


def save_activity(db, user_id, activity_id):
    activity = get_activity(activity_id)
    if activity is None:
        return 404, {"error": "That activity does not exist."}
    if not activity["offline"]:
        return 422, {"error": "This activity needs internet, so it cannot be saved for offline use."}
    db.add_saved_activity(user_id, activity_id)
    return 200, {"status": "saved", "id": activity_id}


def save_plan(db, user_id, plan, now=None):
    if not isinstance(plan, dict):
        return 400, {"errors": {"id": "Request must be a plan object."}}
    if not isinstance(plan.get("id"), str) or not _PLAN_ID_RE.match(plan["id"]):
        return 400, {"errors": {"id": "Plan id is missing or invalid."}}
    errors = validate_plan(plan)
    if errors:
        return 422, {"errors": errors}

    clean = dict(plan)
    clean["title"] = plan["title"].strip()
    with db.lock:
        existing = db.get_plan(plan["id"])
        if existing and existing["user_id"] != user_id:
            return 403, {"errors": {"id": "Not your plan."}}
        clash = find_clash(db.plans_for_user(user_id), clean)
        if clash:
            return 409, {"errors": {"slot_start": f"This time clashes with '{clash['title']}'. "
                                                   f"Choose a start at or after {clash['free_from']}."}, "clash": clash}
        saved_at = plan.get("saved_at") or now or datetime.now(timezone.utc).isoformat()
        if existing and existing["saved_at"] > saved_at:
            return 200, {"status": "ignored_older", "id": plan["id"]}
        db.upsert_plan(user_id, clean, saved_at)
    return 200, {"status": "synced", "id": plan["id"]}


def list_plans(db, user_id):
    return 200, db.plans_for_user(user_id)


def export_plans_csv(db, user_id):
    rows = ["id,title,activity_id,slot_start,duration_min,learners,present"]
    for p in db.plans_for_user(user_id):
        title = p["title"].replace('"', '""')
        if title[:1] in "=+-@":
            title = "'" + title
        present = sum(1 for a in p["attendance"].values() if a.get("present"))
        rows.append(f'{p["id"]},"{title}",{p["activity_id"]},{p["slot_start"]},{p["duration_min"]},'
                    f'"{" ".join(p["learners"])}",{present}')
    return "\n".join(rows) + "\n"


CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json"}


def make_handler(db):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, content_type="application/json"):
            if isinstance(body, bytes):
                data = body
            elif content_type == "application/json":
                data = json.dumps(body).encode()
            else:
                data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store" if content_type == "application/json" else "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _user_id(self):
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            return user_for_token(db, token)

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None, 400
            if length > MAX_BODY_BYTES:
                return None, 413
            try:
                return json.loads(self.rfile.read(length) or b"{}"), None
            except (ValueError, UnicodeDecodeError):
                return None, 400

        def _serve_static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            file = (WEB_DIR / name).resolve()
            inside = WEB_DIR.resolve() in file.parents
            if not inside or not file.is_file() or file.suffix not in CONTENT_TYPES:
                return False
            self._send(200, file.read_bytes(), CONTENT_TYPES[file.suffix])
            return True

        def do_GET(self):
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                if not self._serve_static(url.path):
                    self._send(404, {"error": "Not found"})
                return
            user_id = self._user_id()
            if user_id is None:
                return self._send(401, {"error": "Log in first."})
            if url.path == "/api/activities":
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                return self._send(*search_activities(params))
            if url.path == "/api/saved":
                return self._send(*saved_activities(db, user_id))
            if url.path == "/api/plans":
                return self._send(*list_plans(db, user_id))
            if url.path == "/api/export.csv":
                return self._send(200, export_plans_csv(db, user_id), "text/csv")
            self._send(404, {"error": "Not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            body, error = self._read_json()
            if error:
                return self._send(error, {"error": "Bad or oversized request."})
            if path == "/api/login":
                if not isinstance(body, dict):
                    return self._send(400, {"error": "Bad request."})
                token = login(db, body.get("facilitator_id", ""), body.get("password", ""))
                if token is None:
                    return self._send(401, {"error": "Facilitator ID or password is wrong. Check both and try again."})
                return self._send(200, {"token": token})
            user_id = self._user_id()
            if user_id is None:
                return self._send(401, {"error": "Log in first."})
            if path == "/api/logout":
                logout(db, user_id)
                return self._send(200, {"status": "logged_out"})
            if path == "/api/plans":
                return self._send(*save_plan(db, user_id, body))
            if path == "/api/saved":
                if not isinstance(body, dict):
                    return self._send(400, {"error": "Bad request."})
                return self._send(*save_activity(db, user_id, body.get("activity_id")))
            self._send(404, {"error": "Not found"})

    return Handler


def create_server(db_path=":memory:", host="127.0.0.1", port=8000):
    db = Database(db_path)
    ensure_demo_users(db)
    server = ThreadingHTTPServer((host, port), make_handler(db))
    server.db = db
    return server


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    server = create_server(os.environ.get("STEMMATE_DB", "stemmate.db"), port=port)
    print(f"STEMMate Namibia running on http://127.0.0.1:{port}  (synthetic demo login: demo01 / demo-pass-1)")
    print("Stop with Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
