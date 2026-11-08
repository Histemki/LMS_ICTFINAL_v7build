"""Smart classroom check-in for the LMS (assignment D2).

Two ways to mark attendance, ONE shared server-side check:
  * NFC door reader (the IoT device, e.g. ESP32 + PN532) -> POST /api/checkin  {"method": "nfc", ...}
  * QR code shown by the teacher, scanned by the student  -> POST /api/checkin  {"method": "qr", ...}

Both find the lesson in `schedule`, check `enrollments`, and write `attendance` + one `checkins` row.

The module does not import app_postgresql.py (that would be a circular import). The app hands its own
helpers to register(), and calls init_schema(cur) from init_db(). Everything new lives in this file.
"""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

from flask import jsonify, request
from psycopg2.extras import RealDictCursor

try:  # optional: without it the teacher still gets the text code, just no picture
    import segno
except ImportError:  # pragma: no cover
    segno = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# ------------------------------------------------------------------ settings
SECRET = os.getenv("CHECKIN_SECRET", "dev-only-change-me")  # pepper for card hashes + QR signatures
TZ_NAME = os.getenv("CHECKIN_TZ", "Asia/Almaty")             # the timetable has no time zone: local campus time
EARLY_MIN = 10          # a student may check in this many minutes before the class starts ...
LATE_MIN = 15           # ... and this many minutes after it starts
QR_SECONDS = 60         # lifetime of one QR code
RETENTION_DAYS = 30     # raw check-in rows are deleted after this many days
RATE_LIMIT = 60         # requests per minute per reader / per user
CARD_RE = re.compile(r"^[0-9A-F]{8,20}$")   # NFC UID as hex text, 4..10 bytes

METHODS = ("nfc", "qr")
RESULTS = ("ok", "duplicate", "unknown_card", "no_lesson", "not_enrolled", "bad_code", "wrong_role")

SCHEMA = """
-- one NFC reader per room; only a hash of the reader's key is stored
CREATE TABLE IF NOT EXISTS devices (
    id SERIAL PRIMARY KEY,
    room VARCHAR(20) NOT NULL UNIQUE,
    name VARCHAR(60) NOT NULL DEFAULT '',
    key_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT LOCALTIMESTAMP,
    last_seen TIMESTAMP
);

-- a student card is stored only as a keyed hash (never the raw card UID)
ALTER TABLE users ADD COLUMN IF NOT EXISTS nfc_token_hash VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS one_card_per_user ON users(nfc_token_hash) WHERE nfc_token_hash IS NOT NULL;

-- raw log of every check-in attempt. Short-lived: purge_old() deletes rows after RETENTION_DAYS.
CREATE TABLE IF NOT EXISTS checkins (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,          -- NULL for an unknown card
    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,     -- NULL for QR
    schedule_id INTEGER REFERENCES schedule(id) ON DELETE SET NULL,  -- the log must survive a deleted lesson
    method VARCHAR(5) NOT NULL CHECK (method IN ('nfc','qr')),
    result VARCHAR(20) NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkins_created ON checkins(created_at);
CREATE INDEX IF NOT EXISTS idx_checkins_user ON checkins(user_id, created_at);

-- who looked at the raw log (the admin is the only role that can)
CREATE TABLE IF NOT EXISTS checkin_audit (
    id SERIAL PRIMARY KEY,
    admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action VARCHAR(40) NOT NULL,
    created_at TIMESTAMP NOT NULL
);
"""


def init_schema(cur):
    """Called by init_db(). Safe to run on every start."""
    cur.execute(SCHEMA)
    # One attendance row per student, course and day. An old database may already contain duplicates;
    # then the index cannot be built, and the advisory lock in mark() still prevents new duplicates.
    cur.execute("SAVEPOINT attendance_unique")
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_attendance_per_day "
                    "ON attendance(user_id, course_id, class_date)")
        cur.execute("RELEASE SAVEPOINT attendance_unique")
    except Exception as err:  # psycopg2.errors.UniqueViolation or similar
        cur.execute("ROLLBACK TO SAVEPOINT attendance_unique")
        print("checkin: duplicate attendance rows exist, unique index skipped:", str(err).splitlines()[0])


# ------------------------------------------------------------------ small helpers
def now():
    """Local campus time (naive). CHECKIN_TEST_NOW='2026-11-03T09:05:00' fixes the clock for tests only."""
    fixed = os.getenv("CHECKIN_TEST_NOW")
    if fixed:
        return datetime.fromisoformat(fixed)
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TZ_NAME)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()


def card_token(uid):
    """Keyed hash of a card UID. A bare hash of a 4-byte UID could be brute-forced, the secret prevents that."""
    return hmac.new(SECRET.encode(), b"card:" + uid.encode(), hashlib.sha256).hexdigest()


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()  # the key is 128 random bits, so a plain hash is enough


def _sig(body):
    return hmac.new(SECRET.encode(), b"qr:" + body.encode(), hashlib.sha256).hexdigest()[:20]


def make_code(schedule_id, ttl=QR_SECONDS):
    """'<schedule id>.<expiry>.<signature>': no personal data, only a lesson id and a deadline."""
    body = f"{schedule_id}.{int(time.time()) + ttl}"
    return f"{body}.{_sig(body)}"


def parse_code(code):
    """Returns the schedule id, or None if the code is malformed, forged or expired."""
    parts = str(code).strip().split(".")
    if len(parts) != 3 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    if not hmac.compare_digest(_sig(f"{parts[0]}.{parts[1]}"), parts[2]):
        return None
    if int(parts[1]) < time.time():
        return None
    return int(parts[0])


# A lesson is either weekly (class_date IS NULL, repeats on `weekday`) or a one-off on `class_date`
# (lessons created by the admin have a date).
ON_DAY = "(s.class_date = %s OR (s.class_date IS NULL AND s.weekday = %s))"


def is_today(lesson, when):
    if lesson.get("class_date") is not None:
        return lesson["class_date"] == when.date()
    return lesson["weekday"] == when.isoweekday()


def window_open(lesson, when):
    start = datetime.combine(when.date(), lesson["start_time"])
    return start - timedelta(minutes=EARLY_MIN) <= when <= start + timedelta(minutes=LATE_MIN)


_hits = {}
_hits_lock = threading.Lock()


def _limited(key):
    """In-memory limit: RATE_LIMIT requests per minute for one reader or one user."""
    minute = int(time.time() // 60)
    with _hits_lock:
        if len(_hits) > 5000:
            _hits.clear()
        stamp, count = _hits.get(key, (minute, 0))
        if stamp != minute:
            stamp, count = minute, 0
        _hits[key] = (stamp, count + 1)
        return count + 1 > RATE_LIMIT


def purge_old(connect):
    """Deletes raw check-ins older than RETENTION_DAYS. `connect` returns a new database connection."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM checkins WHERE created_at < %s", (now() - timedelta(days=RETENTION_DAYS),))
            deleted = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


# ------------------------------------------------------------------ routes
def register(app, *, db, query_one, rows, execute, guard, admin_guard, teacher_guard, Refuse, connect):
    """Adds the check-in endpoints to the Flask app."""
    if SECRET == "dev-only-change-me":
        print("checkin: CHECKIN_SECRET is not set - using the development secret (set it before real use)")

    def log(user_id, device_id, schedule_id, method, result):
        execute("INSERT INTO checkins(user_id,device_id,schedule_id,method,result,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)", (user_id, device_id, schedule_id, method, result, now()))

    def reader_from_headers():
        """The reader proves itself with its id and secret key (it has no user login)."""
        device_id = request.headers.get("X-Device-Id", "")
        key = request.headers.get("X-Device-Key", "")
        device = query_one("SELECT * FROM devices WHERE id = %s", (int(device_id),)) if device_id.isdigit() else None
        if _limited(("dev", device_id or request.remote_addr)):
            raise Refuse(429, "Too many requests")
        ok = device is not None and hmac.compare_digest(device["key_hash"], key_hash(key))
        if not ok:
            raise Refuse(401, "Unknown reader")
        execute("UPDATE devices SET last_seen = %s WHERE id = %s", (now(), device["id"]))
        return device

    def lesson_by_room(room, when):
        found = rows("""SELECT s.id, s.course_id, s.room, s.weekday, s.class_date, s.start_time, s.end_time, c.title AS course
                        FROM schedule s JOIN courses c ON c.id = s.course_id
                        WHERE s.room = %s AND """ + ON_DAY, (room, when.date(), when.isoweekday()))
        return next((l for l in found if window_open(l, when)), None)

    def lesson_by_id(schedule_id):
        return query_one("""SELECT s.id, s.course_id, s.room, s.weekday, s.class_date, s.start_time, s.end_time, c.title AS course
                            FROM schedule s JOIN courses c ON c.id = s.course_id WHERE s.id = %s""", (schedule_id,))

    def mark(user, lesson, when, method, device_id):
        """The shared check: enrolment, then ONE attendance row per student, course and day."""
        enrolled = query_one("SELECT 1 AS found FROM enrollments WHERE user_id = %s AND course_id = %s",
                             (user["id"], lesson["course_id"]))
        if not enrolled:
            log(user["id"], device_id, lesson["id"], method, "not_enrolled")
            raise Refuse(403, "You are not enrolled in this course")
        conn = db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (user["id"], lesson["course_id"]))
            cur.execute("SELECT id, present FROM attendance WHERE user_id = %s AND course_id = %s AND class_date = %s",
                        (user["id"], lesson["course_id"], when.date()))
            existing = cur.fetchone()
            if existing and existing["present"] == 1:
                result = "duplicate"
            elif existing:  # the teacher or a default had it as absent: the check-in corrects it
                cur.execute("UPDATE attendance SET present = 1 WHERE id = %s", (existing["id"],))
                result = "ok"
            else:
                cur.execute("INSERT INTO attendance(user_id,course_id,class_date,present) VALUES (%s,%s,%s,1)",
                            (user["id"], lesson["course_id"], when.date()))
                result = "ok"
            cur.execute("INSERT INTO checkins(user_id,device_id,schedule_id,method,result,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", (user["id"], device_id, lesson["id"], method, result, when))
        conn.commit()
        return jsonify(result=result, course=lesson["course"], room=lesson["room"],
                       message="Already checked in" if result == "duplicate" else "Checked in")

    # ---- the one endpoint both methods use
    @app.post("/api/checkin")
    def checkin():
        data = request.get_json(silent=True) or {}
        method = data.get("method")
        when = now()

        if method == "nfc":
            device = reader_from_headers()
            uid = str(data.get("card_uid", "")).strip().upper()
            if not CARD_RE.match(uid):
                raise Refuse(400, "Card UID must be 8-20 hex characters")
            user = query_one("SELECT id, role FROM users WHERE nfc_token_hash = %s", (card_token(uid),))
            if not user:
                log(None, device["id"], None, "nfc", "unknown_card")
                raise Refuse(404, "Card is not registered")
            if user["role"] != "student":
                log(user["id"], device["id"], None, "nfc", "wrong_role")
                raise Refuse(403, "Only students check in")
            lesson = lesson_by_room(device["room"], when)
            if not lesson:
                log(user["id"], device["id"], None, "nfc", "no_lesson")
                raise Refuse(409, "No class is open for check-in in this room right now")
            return mark(user, lesson, when, "nfc", device["id"])

        if method == "qr":
            user, error = guard()
            if error:
                return error
            if user["role"] != "student":
                raise Refuse(403, "Only students check in")
            if _limited(("usr", user["id"])):
                raise Refuse(429, "Too many requests")
            schedule_id = parse_code(data.get("code", ""))
            lesson = lesson_by_id(schedule_id) if schedule_id else None
            if not lesson:
                log(user["id"], None, None, "qr", "bad_code")
                raise Refuse(400, "This code is invalid or has expired - ask for the current one")
            if not is_today(lesson, when) or not window_open(lesson, when):
                log(user["id"], None, lesson["id"], "qr", "no_lesson")
                raise Refuse(409, "Check-in for this class is not open right now")
            return mark(user, lesson, when, "qr", None)

        raise Refuse(400, 'Send "method": "nfc" or "qr"')

    # ---- QR code for the teacher's screen
    @app.get("/api/checkin/qr")
    def checkin_qr():
        user, error = teacher_guard()
        if error:
            return error
        when = now()
        lessons = rows("""SELECT s.id, s.room, s.start_time, s.end_time, c.title AS course
                          FROM schedule s JOIN courses c ON c.id = s.course_id
                          WHERE (c.teacher_id = %s OR EXISTS (SELECT 1 FROM course_teachers ct
                                 WHERE ct.course_id = c.id AND ct.teacher_id = %s))
                            AND """ + ON_DAY, (user["id"], user["id"], when.date(), when.isoweekday()))
        lesson = next((l for l in lessons if window_open(l, when)), None)
        if not lesson:
            raise Refuse(409, "You have no class open for check-in right now")
        code = make_code(lesson["id"])
        url = request.host_url.rstrip("/") + "/?checkin=" + code
        image = segno.make(url, error="m").svg_data_uri(scale=5, border=2) if segno else None
        return jsonify(code=code, url=url, image=image, expires_in=QR_SECONDS, course=lesson["course"],
                       room=lesson["room"], start=lesson["start_time"].strftime("%H:%M"))

    # ---- student: own history only
    @app.get("/api/checkins")
    def my_checkins():
        user, error = guard()
        if error:
            return error
        return jsonify(rows("""SELECT to_char(k.created_at, 'YYYY-MM-DD HH24:MI') AS at, k.method, k.result, c.title AS course
                               FROM checkins k LEFT JOIN schedule s ON s.id = k.schedule_id
                               LEFT JOIN courses c ON c.id = s.course_id
                               WHERE k.user_id = %s ORDER BY k.created_at DESC LIMIT 30""", (user["id"],)))

    # ---- reader heartbeat: only `last_seen` is stored
    @app.post("/api/device/heartbeat")
    def heartbeat():
        device = reader_from_headers()
        return jsonify(ok=True, room=device["room"])

    # ---- admin: readers
    @app.get("/api/devices")
    def list_devices():
        _, error = admin_guard()
        if error:
            return error
        return jsonify(rows("""SELECT id, room, name, to_char(created_at, 'YYYY-MM-DD HH24:MI') AS created_at,
                                      to_char(last_seen, 'YYYY-MM-DD HH24:MI') AS last_seen FROM devices ORDER BY room"""))

    @app.post("/api/devices")
    def create_device():
        _, error = admin_guard()
        if error:
            return error
        data = request.get_json(silent=True) or {}
        room = str(data.get("room", "")).strip()
        name = str(data.get("name", "")).strip()[:60]
        if not 1 <= len(room) <= 20:
            raise Refuse(400, "Room must be 1-20 characters")
        if query_one("SELECT 1 AS found FROM devices WHERE room = %s", (room,)):
            raise Refuse(409, "This room already has a reader")
        key = secrets.token_hex(16)
        created = execute("INSERT INTO devices(room,name,key_hash) VALUES (%s,%s,%s) RETURNING id",
                          (room, name, key_hash(key)), returning=True)
        return jsonify(id=created["id"], room=room, key=key, note="Copy the key now: it is shown only once"), 201

    @app.delete("/api/devices/<int:device_id>")
    def delete_device(device_id):
        _, error = admin_guard()
        if error:
            return error
        conn = db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM devices WHERE id = %s", (device_id,))
            deleted = cur.rowcount
        conn.commit()
        if not deleted:
            raise Refuse(404, "Reader not found")
        return jsonify(ok=True)

    # ---- admin: link a card to a student
    @app.post("/api/cards")
    def link_card():
        _, error = admin_guard()
        if error:
            return error
        data = request.get_json(silent=True) or {}
        email = str(data.get("email", "")).strip().lower()
        uid = str(data.get("card_uid", "")).strip().upper()
        if not CARD_RE.match(uid):
            raise Refuse(400, "Card UID must be 8-20 hex characters")
        student = query_one("SELECT id FROM users WHERE email = %s AND role = 'student'", (email,))
        if not student:
            raise Refuse(404, "No student with this e-mail")
        taken = query_one("SELECT id FROM users WHERE nfc_token_hash = %s AND id <> %s", (card_token(uid), student["id"]))
        if taken:
            raise Refuse(409, "This card is already linked to another student")
        execute("UPDATE users SET nfc_token_hash = %s WHERE id = %s", (card_token(uid), student["id"]))
        return jsonify(ok=True), 201

    @app.delete("/api/cards/<int:user_id>")
    def unlink_card(user_id):
        _, error = admin_guard()
        if error:
            return error
        execute("UPDATE users SET nfc_token_hash = NULL WHERE id = %s", (user_id,))
        return jsonify(ok=True)

    # ---- admin: raw log (every look is written to checkin_audit)
    @app.get("/api/admin/checkins")
    def raw_log():
        admin, error = admin_guard()
        if error:
            return error
        execute("INSERT INTO checkin_audit(admin_id,action,created_at) VALUES (%s,%s,%s)",
                (admin["id"], "view_raw_checkins", now()))
        return jsonify(rows("""SELECT to_char(k.created_at, 'YYYY-MM-DD HH24:MI') AS at, u.full_name AS student,
                                      k.method, k.result, d.room AS reader
                               FROM checkins k LEFT JOIN users u ON u.id = k.user_id
                               LEFT JOIN devices d ON d.id = k.device_id
                               ORDER BY k.created_at DESC LIMIT 100"""))

    # ---- retention: delete old raw rows now and every 6 hours
    def purge_loop():
        time.sleep(15)  # let init_db() create the tables first
        while True:
            try:
                purge_old(connect)
            except Exception as err:  # the database may be down for a moment
                print("checkin: purge failed:", err)
            time.sleep(6 * 3600)

    threading.Thread(target=purge_loop, daemon=True).start()


if __name__ == "__main__":
    print("This module is used by app_postgresql.py; run that file instead.")
