import os
import hashlib
import html
import logging
import re
from urllib.parse import urlencode
from datetime import date, datetime, time as dtime, timedelta
import random
import secrets
import threading
import time
import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, abort, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import checkin  # NFC / QR classroom check-in (D2), kept in its own file
import materials  # links to videos / resources attached to courses (own file)
import submissions  # students send work as a link (own file)
import gradebook  # teachers give grades, students see them (own file, like checkin)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:YOUR_PASSWORD@localhost:5432/lms_db"
)

DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "Lms#2026demo")  # password given to all demo users
BCRYPT_ROUNDS = 10          # cost factor of the bcrypt hash
MAX_ATTEMPTS = 5            # failed logins before the account is locked
LOCK_MINUTES = 20           # how long the account stays locked
SESSION_SECONDS = 2 * 60 * 60
DEMO_MODE = os.getenv("DEMO_MODE", "0" if os.getenv("APP_ENV", "development").lower() == "production" else "1").strip() == "1"
RECOVERY_WINDOW_SECONDS = 15 * 60
RECOVERY_MAX_REQUESTS = 5
RECOVERY_RESPONSE_SECONDS = 0.35
RECOVERY_EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@astanait\.edu\.kz$", re.IGNORECASE)
RECOVERY_OUTBOX = []  # Demo-only mail queue. Raw tokens never go to the database or logs.
RECOVERY_LIMITS = {}
RECOVERY_LIMITS_LOCK = threading.Lock()
RECOVERY_DUMMY_HASH = bcrypt.hashpw(b"recovery-timing-check", bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()

app = Flask(__name__, static_folder="static")
SESSIONS = {}  # Demo-only in-memory sessions: token -> (user_id, expires_at)
# Failed-login counters live in server memory only (never in the database):
# email -> [failed_count, locked_until_timestamp]. They reset when the server restarts.
ATTEMPTS = {}
ATTEMPTS_LOCK = threading.Lock()
ALLOWED_ORIGINS = {"http://127.0.0.1:5500", "http://localhost:5500"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    full_name VARCHAR(100) NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('student','teacher','admin')),
    password_hash VARCHAR(255)
);

-- for databases created before passwords were added
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);
-- login attempts are no longer stored in the database
ALTER TABLE users DROP COLUMN IF EXISTS failed_attempts;
ALTER TABLE users DROP COLUMN IF EXISTS locked_until;

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    code VARCHAR(20) NOT NULL UNIQUE,
    title VARCHAR(150) NOT NULL,
    teacher_id INTEGER NOT NULL REFERENCES users(id),
    description TEXT NOT NULL DEFAULT ''
);
ALTER TABLE courses ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS course_teachers (
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (course_id, teacher_id)
);

CREATE TABLE IF NOT EXISTS enrollments (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, course_id)
);

-- kinds of tasks: Quiz, Homework, Project, Midterm Exam, Final Exam ...
CREATE TABLE IF NOT EXISTS assignment_types (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE
);

-- every task of a course: its type, form of control (current / midterm / final), max score, deadline
CREATE TABLE IF NOT EXISTS assignments (
    id SERIAL PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title VARCHAR(100) NOT NULL,
    type_id INTEGER NOT NULL REFERENCES assignment_types(id),
    control_form VARCHAR(10) NOT NULL CHECK (control_form IN ('current','midterm','final')),
    max_score NUMERIC(7,2) NOT NULL CHECK (max_score > 0),
    due_date DATE NOT NULL,
    UNIQUE (course_id, title)
);
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS due_time TIME NOT NULL DEFAULT '23:59';
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS grades (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    score NUMERIC(7,2) NOT NULL,
    max_score NUMERIC(7,2) NOT NULL,
    CHECK (score >= 0 AND score <= max_score)
);

CREATE TABLE IF NOT EXISTS attendance (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    class_date DATE NOT NULL,
    present SMALLINT NOT NULL CHECK (present IN (0,1))
);

CREATE TABLE IF NOT EXISTS project_groups (
    id SERIAL PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    name VARCHAR(60) NOT NULL,
    max_size INTEGER NOT NULL CHECK (max_size BETWEEN 2 AND 6),
    created_by INTEGER NOT NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES project_groups(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);

-- weekly timetable: one row = one class that repeats every week
CREATE TABLE IF NOT EXISTS schedule (
    id SERIAL PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    weekday SMALLINT NOT NULL CHECK (weekday BETWEEN 1 AND 7),   -- 1 = Monday ... 7 = Sunday
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    room VARCHAR(20) NOT NULL,
    kind VARCHAR(20) NOT NULL CHECK (kind IN ('Lecture','Practice','Lab')),
    CHECK (end_time > start_time)
);
ALTER TABLE schedule ADD COLUMN IF NOT EXISTS class_date DATE;
CREATE INDEX IF NOT EXISTS idx_schedule_course ON schedule(course_id);
CREATE INDEX IF NOT EXISTS idx_schedule_date ON schedule(class_date);

-- kinds of applications a student can submit (IROS, SSQI, SocGPA ...): add a row to add a new kind
CREATE TABLE IF NOT EXISTS application_types (
    id SERIAL PRIMARY KEY,
    code VARCHAR(20) NOT NULL UNIQUE,
    title VARCHAR(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type_id INTEGER NOT NULL REFERENCES application_types(id),
    message TEXT NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    created_at TIMESTAMP NOT NULL DEFAULT LOCALTIMESTAMP,
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TIMESTAMP,
    review_comment TEXT NOT NULL DEFAULT ''
);
-- one pending application of each type per student
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_application ON applications(user_id, type_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS password_resets (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT LOCALTIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id);

CREATE TABLE IF NOT EXISTS support_requests (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'new' CHECK (status IN ('new','resolved')),
    created_at TIMESTAMP NOT NULL DEFAULT LOCALTIMESTAMP
);
CREATE TABLE IF NOT EXISTS announcements (
    id SERIAL PRIMARY KEY,
    title VARCHAR(120) NOT NULL,
    body VARCHAR(2000) NOT NULL,
    category VARCHAR(20) NOT NULL CHECK (category IN ('General','Important','Exams','Events')),
    audience VARCHAR(20) NOT NULL CHECK (audience IN ('everyone','students','teachers','course')),
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    pinned BOOLEAN NOT NULL DEFAULT FALSE,
    author_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT LOCALTIMESTAMP,
    updated_at TIMESTAMP,
    expires_at TIMESTAMP,
    CHECK ((audience = 'course' AND course_id IS NOT NULL) OR (audience <> 'course' AND course_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_announcements_feed ON announcements(pinned DESC, created_at DESC);
CREATE TABLE IF NOT EXISTS announcement_reads (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    read_at TIMESTAMP NOT NULL DEFAULT LOCALTIMESTAMP,
    PRIMARY KEY (user_id, announcement_id)
);
"""


APPLICATION_TYPES = [("IROS", "IROS"), ("SSQI", "SSQI"), ("SocGPA", "SocGPA")]  # code, title
ADMIN = (200, "admin@astanait.edu.kz", "Registrar Office")  # reviews the applications

ASSIGNMENT_TYPES = ["Quiz", "Homework", "Project", "Midterm Exam", "Final Exam"]

# title, type, control form, max score, deadline, time, already graded?, spread (days)
# Graded tasks are finished. The others show up in the Dashboard timeline (the spread moves the
# deadline by 0..spread-1 days depending on the course, so courses do not all end on the same day).
ASSIGNMENT_PLAN = [
    ("Quiz 1",       "Quiz",         "current", 10, "2026-09-10", "23:59", True,  1),
    ("Homework 1",   "Homework",     "current", 20, "2026-09-24", "23:59", True,  1),
    ("Midterm Exam", "Midterm Exam", "midterm", 30, "2026-10-15", "23:00", True,  1),
    ("Project",      "Project",      "current", 20, "2026-10-10", "23:59", False, 3),
    ("Homework 2",   "Homework",     "current", 20, "2026-10-25", "23:50", False, 5),
    ("Final Exam",   "Final Exam",   "final",   40, "2026-12-15", "23:00", False, 4),
]


def plan_for_course(course_id):
    rows = []
    for title, type_name, form, max_score, due, due_time, graded, spread in ASSIGNMENT_PLAN:
        due_day = date.fromisoformat(due) + timedelta(days=course_id % spread)
        rows.append((title, type_name, form, max_score, due_day.isoformat(), due_time, graded))
    return rows


def insert_assignment(cur, course_id, row):
    """Adds one task to a course (skips it if the course already has a task with this title)."""
    title, type_name, form, max_score, due_day, due_time, _graded = row
    cur.execute(
        """INSERT INTO assignments(course_id,title,type_id,control_form,max_score,due_date,due_time)
           SELECT %s, %s, id, %s, %s, %s::date, %s::time FROM assignment_types WHERE name = %s
           ON CONFLICT (course_id, title) DO NOTHING
           RETURNING id""",
        (course_id, title, form, max_score, due_day, due_time, type_name)
    )
    found = cur.fetchone()
    return found[0] if found else None


# One teacher per course (course 1 -> first teacher, course 2 -> second, ...).
TEACHERS = [
    (1,   "teacher@astanait.edu.kz",        "Diana Kazbekova"),
    (102, "a.sarsenov@astanait.edu.kz",     "Aidar Sarsenov"),
    (103, "m.omarova@astanait.edu.kz",      "Madina Omarova"),
    (104, "e.tulegenov@astanait.edu.kz",    "Erlan Tulegenov"),
    (105, "a.nurpeisova@astanait.edu.kz",   "Aigerim Nurpeisova"),
    (106, "b.zhaksylykov@astanait.edu.kz",  "Bauyrzhan Zhaksylykov"),
    (107, "s.abenova@astanait.edu.kz",      "Saltanat Abenova"),
    (108, "d.mukhamedov@astanait.edu.kz",   "Daniyar Mukhamedov"),
    (109, "g.kenzhebayeva@astanait.edu.kz", "Gulnara Kenzhebayeva"),
    (110, "t.alimov@astanait.edu.kz",       "Timur Alimov"),
]


# Demo timetable: 5 time slots a day, every course gets 2 classes a week, no two courses share a slot.
LESSON_SLOTS = [("09:00", "10:30"), ("10:40", "12:10"), ("13:00", "14:30"), ("14:40", "16:10"), ("16:20", "17:50")]


def seed_schedule(cur):
    """Fills an empty timetable (new database or a database made before the timetable existed)."""
    cur.execute("SELECT COUNT(*) FROM schedule")
    if cur.fetchone()[0]:
        return
    cur.execute("SELECT id FROM courses ORDER BY id")
    for (course_id,) in cur.fetchall():
        for k, kind in enumerate(("Lecture", "Practice")):
            n = ((course_id - 1) % 10) * 2 + k
            start, end = LESSON_SLOTS[(n // 5) % 5]
            cur.execute(
                """INSERT INTO schedule(course_id,weekday,start_time,end_time,room,kind)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (course_id, n % 5 + 1, start, end, f"C1.{100 + n}", kind))


class Refuse(Exception):
    """Raised inside a request to stop it with a readable JSON error (status + message)."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def hash_password(password):
    """bcrypt with a random salt: the same password gives a different hash every time."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def check_password(password, hashed):
    raw = password.encode()
    if len(raw) > 72:  # bcrypt limit
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode())
    except ValueError:
        return False


# Used when the e-mail does not exist, so the answer takes the same time as for a real account.
DUMMY_HASH = hash_password("not-a-real-password")


def db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL)
    return g.db


@app.teardown_appcontext
def close_db(_error):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query_one(sql, args=()):
    with db().cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
        return dict(row) if row is not None else None


def rows(sql, args=()):
    with db().cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args)
        return [dict(row) for row in cur.fetchall()]


def execute(sql, args=(), returning=False):
    conn = db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args)
        result = dict(cur.fetchone()) if returning and cur.description else None
    conn.commit()
    return result


def migrate_old_database(cur):
    """Brings a database made by an older version of this file up to date (does nothing for a new one)."""
    # 1) grades used a free-text column "item" -> now they point to a row of the assignments table
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'grades' AND column_name = 'item'""")
    if cur.fetchone():
        names = "CASE g.item WHEN 'Assignment' THEN 'Homework' WHEN 'Midterm' THEN 'Midterm Exam' ELSE g.item END"
        cur.execute("ALTER TABLE grades ADD COLUMN IF NOT EXISTS assignment_id INTEGER REFERENCES assignments(id) ON DELETE CASCADE")
        cur.execute(f"INSERT INTO assignment_types(name) SELECT DISTINCT {names} FROM grades g ON CONFLICT DO NOTHING")
        cur.execute(f"""
            INSERT INTO assignments(course_id,title,type_id,control_form,max_score,due_date)
            SELECT g.course_id, g.item, t.id,
                   CASE g.item WHEN 'Midterm' THEN 'midterm' ELSE 'current' END,
                   MAX(g.max_score), DATE '2026-09-30'
            FROM grades g JOIN assignment_types t ON t.name = {names}
            GROUP BY g.course_id, g.item, t.id
            ON CONFLICT DO NOTHING""")
        cur.execute("""UPDATE grades SET assignment_id = a.id FROM assignments a
                       WHERE a.course_id = grades.course_id AND a.title = grades.item""")
        cur.execute("ALTER TABLE grades ALTER COLUMN assignment_id SET NOT NULL")
        cur.execute("ALTER TABLE grades DROP COLUMN item")

    # 2) all courses had the same teacher -> give every course its own teacher
    cur.execute("SELECT COUNT(*) FROM users WHERE role = 'teacher'")
    teacher_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM courses")
    course_count = cur.fetchone()[0]
    if course_count and teacher_count < 2:
        for tid, mail, name in TEACHERS:
            cur.execute(
                """INSERT INTO users(id,email,full_name,role,password_hash) VALUES (%s,%s,%s,'teacher',%s)
                   ON CONFLICT DO NOTHING""",
                (tid, mail, name, hash_password(DEMO_PASSWORD)))
        for i, (tid, _mail, _name) in enumerate(TEACHERS, 1):
            cur.execute("UPDATE courses SET teacher_id = %s WHERE id = %s", (tid, i))

    # 3) make sure every course has upcoming / not yet graded tasks for the Dashboard timeline
    cur.execute("SELECT id FROM courses")
    for (course_id,) in cur.fetchall():
        for row in plan_for_course(course_id):
            if not row[6]:
                insert_assignment(cur, course_id, row)


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            for type_name in ASSIGNMENT_TYPES:
                cur.execute("INSERT INTO assignment_types(name) VALUES (%s) ON CONFLICT DO NOTHING", (type_name,))
            migrate_old_database(cur)
            cur.execute("SELECT COUNT(*) FROM users")
            user_count = cur.fetchone()[0]

            # Create demo data only on the first run, so existing data is not overwritten.
            if user_count == 0:
                random.seed(1)
                for tid, mail, name in TEACHERS:
                    cur.execute(
                        "INSERT INTO users(id,email,full_name,role) VALUES (%s,%s,%s,'teacher')",
                        (tid, mail, name)
                    )
                titles = [
                    "ICT", "Databases", "Calculus", "Physics", "English",
                    "Algorithms", "Web Development", "Statistics",
                    "Discrete Math", "Networks"
                ]
                course_assignments = {}  # course id -> [(assignment id, max score, graded?)]
                for i, title in enumerate(titles, 1):
                    cur.execute(
                        "INSERT INTO courses(id,code,title,teacher_id) VALUES (%s,%s,%s,%s)",
                        (i, f"CSE{100+i}", title, TEACHERS[i - 1][0])
                    )
                    course_assignments[i] = []
                    for row in plan_for_course(i):
                        course_assignments[i].append((insert_assignment(cur, i, row), row[3], row[6]))
                for uid in range(2, 102):
                    cur.execute(
                        "INSERT INTO users(id,email,full_name,role) VALUES (%s,%s,%s,%s)",
                        (uid, f"26{uid:04d}@astanait.edu.kz", f"Student {uid-1}", "student")
                    )
                    for course_id in random.sample(range(1, 11), 5):
                        cur.execute(
                            "INSERT INTO enrollments(user_id,course_id) VALUES (%s,%s)",
                            (uid, course_id)
                        )
                        for k in range(8):
                            cur.execute(
                                """INSERT INTO attendance(user_id,course_id,class_date,present)
                                   VALUES (%s,%s,%s,%s)""",
                                (uid, course_id, f"2026-09-{k*3+1:02d}", int(random.random() < 0.88))
                            )
                        for assignment_id, max_score, graded in course_assignments[course_id]:
                            if not graded:
                                continue  # e.g. the final exam has not taken place yet
                            score = round(random.uniform(0.5, 1) * max_score, 1)
                            cur.execute(
                                """INSERT INTO grades(user_id,course_id,assignment_id,score,max_score)
                                   VALUES (%s,%s,%s,%s,%s)""",
                                (uid, course_id, assignment_id, score, max_score)
                            )

                # Keep the manually assigned demo IDs available for future inserts.
                cur.execute("""
                    SELECT setval(pg_get_serial_sequence('grades','id'),
                                  COALESCE((SELECT MAX(id) FROM grades), 1))
                """)
                cur.execute("""
                    SELECT setval(pg_get_serial_sequence('attendance','id'),
                                  COALESCE((SELECT MAX(id) FROM attendance), 1))
                """)

            seed_schedule(cur)
            checkin.init_schema(cur)  # devices, checkins, users.nfc_token_hash
            materials.init_schema(cur)  # course_materials
            submissions.init_schema(cur)  # submissions (links to student work)
            gradebook.init_schema(cur)  #grades.graded_at/graded_by/comment, grade_history
            for code, title in APPLICATION_TYPES:
                cur.execute("INSERT INTO application_types(code,title) VALUES (%s,%s) ON CONFLICT DO NOTHING", (code, title))
            cur.execute(
                """INSERT INTO users(id,email,full_name,role,password_hash) VALUES (%s,%s,%s,'admin',%s)
                   ON CONFLICT DO NOTHING""",
                (*ADMIN, hash_password(DEMO_PASSWORD)))

            # Give every user without a password hash the demo password (each hash has its own salt),
            # then make the column mandatory.
            cur.execute("SELECT id FROM users WHERE password_hash IS NULL")
            for (uid,) in cur.fetchall():
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                            (hash_password(DEMO_PASSWORD), uid))
            cur.execute("ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL")
            cur.execute("INSERT INTO course_teachers(course_id,teacher_id) SELECT id,teacher_id FROM courses ON CONFLICT DO NOTHING")
            cur.execute("CREATE SEQUENCE IF NOT EXISTS courses_id_seq")
            cur.execute("ALTER SEQUENCE courses_id_seq OWNED BY courses.id")
            cur.execute("ALTER TABLE courses ALTER COLUMN id SET DEFAULT nextval('courses_id_seq')")
            cur.execute("SELECT setval('courses_id_seq', GREATEST(COALESCE((SELECT MAX(id) FROM courses),1),1))")
            cur.execute("SELECT COUNT(*) FROM announcements")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    """INSERT INTO announcements(title,body,category,audience,course_id,pinned,author_id,expires_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [
                        ("Welcome to the LMS", "Welcome! Check this page regularly for university updates.", "General", "everyone", None, True, ADMIN[0], None),
                        ("Database course update", "Please review the latest course materials before your next class.", "Important", "course", 2, False, ADMIN[0], None),
                        ("Teacher meeting", "A faculty meeting is scheduled for Friday at 15:00.", "Events", "teachers", None, False, ADMIN[0], None),
                    ])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.after_request
def cors(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return response


@app.errorhandler(Exception)
def handle_error(err):
    """Always answer with JSON so the interface can show a readable message."""
    if isinstance(err, Refuse):
        return jsonify(error=str(err)), err.status
    if isinstance(err, HTTPException):
        return jsonify(error=err.description), err.code
    app.logger.exception(err)
    if isinstance(err, psycopg2.OperationalError):
        return jsonify(error="Database is unavailable - check DATABASE_URL"), 503
    return jsonify(error="Internal server error"), 500


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/icons/<path:filename>")
def icons(filename):
    return send_from_directory("static/icons", filename, max_age=86400)


@app.get("/map/<path:filename>")
def map_files(filename):
    return send_from_directory("static/map", filename, max_age=86400)

@app.get("/ui/<path:filename>")
def ui_files(filename):
    return send_from_directory("static/ui", filename)


@app.get("/media/<path:filename>")
def media(filename):
    return send_from_directory("static/media", filename, max_age=86400)


class RecoveryLogRedactor(logging.Filter):
    def filter(self, record):
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(re.sub(r"([?&](?:recover_)?token=)[^&\s\"]+", r"\1[redacted]", str(value), flags=re.IGNORECASE) for value in args)
        return True


logging.getLogger("werkzeug").addFilter(RecoveryLogRedactor())


def _recovery_rate_limited(email, ip, bucket="recover"):
    now = time.monotonic()
    key = (bucket, email, ip or "unknown")
    with RECOVERY_LIMITS_LOCK:
        for old_key, timestamps in list(RECOVERY_LIMITS.items()):
            kept = [stamp for stamp in timestamps if now - stamp < RECOVERY_WINDOW_SECONDS]
            if kept:
                RECOVERY_LIMITS[old_key] = kept
            else:
                RECOVERY_LIMITS.pop(old_key, None)
        if key not in RECOVERY_LIMITS and len(RECOVERY_LIMITS) >= 10000:
            return True
        stamps = RECOVERY_LIMITS.setdefault(key, [])
        if len(stamps) >= RECOVERY_MAX_REQUESTS:
            return True
        stamps.append(now)
        return False


def _valid_university_email(value):
    email = str(value or "").strip().lower()
    return email if RECOVERY_EMAIL_RE.fullmatch(email) else None


def _recovery_email_response(started, status=200, **payload):
    remaining = RECOVERY_RESPONSE_SECONDS - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
    return jsonify(**payload), status


def send_recovery_email(email, token):
    """Email adapter: queue a Demo inbox message in development; replace with SMTP later."""
    if not DEMO_MODE:
        return
    base = request.url_root.rstrip("/")
    link = f"{base}/?" + urlencode({"recover_token": token})
    RECOVERY_OUTBOX.insert(0, {"email": email, "link": link, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    del RECOVERY_OUTBOX[50:]


@app.get("/demo-inbox")
def demo_inbox():
    if not DEMO_MODE:
        abort(404)
    items = []
    for message in RECOVERY_OUTBOX:
        email = html.escape(message["email"])
        link = html.escape(message["link"], quote=True)
        sent = html.escape(message["created_at"])
        items.append(f"<article><h2>Password recovery</h2><p>To: {email}</p><p>Sent: {sent}</p><a href=\"{link}\">Set a new password</a></article>")
    body = "".join(items) or "<p>No recovery messages yet.</p>"
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Demo inbox</title>"
            "<style>body{font:16px system-ui;max-width:760px;margin:32px auto;padding:0 16px;background:#d4d5d9;color:#111}article{background:#d9d9d9;border-radius:12px;padding:18px;margin:14px 0}a,button{min-height:44px}</style>"
            f"<h1>Demo inbox</h1><p>Local development mail preview.</p>{body}</html>")


@app.post("/api/recover/request")
def recover_request():
    started = time.monotonic()
    data = request.get_json(silent=True) or {}
    email = _valid_university_email(data.get("email"))
    if not email:
        return jsonify(error="Enter your university email address ending in @astanait.edu.kz."), 400
    if _recovery_rate_limited(email, request.remote_addr):
        return jsonify(error="Too many recovery requests. Wait 15 minutes before trying again."), 429

    user = query_one("SELECT id, email, password_hash FROM users WHERE email = %s", (email,))
    check_password("recovery-timing-check", user["password_hash"] if user else RECOVERY_DUMMY_HASH)
    if user:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        conn = db()
        with conn.cursor() as cur:
            cur.execute("UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE user_id = %s AND used_at IS NULL", (user["id"],))
            cur.execute("INSERT INTO password_resets(user_id, token_hash, expires_at) VALUES (%s, %s, CURRENT_TIMESTAMP + INTERVAL '30 minutes')", (user["id"], token_hash))
        conn.commit()
        send_recovery_email(user["email"], token)
    return _recovery_email_response(started, message="If this address is registered, we sent a link. It is valid for 30 minutes.", demo_mode=DEMO_MODE)


def _recovery_record(token, lock=False):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    suffix = " FOR UPDATE" if lock else ""
    return query_one("""SELECT id, user_id, used_at IS NOT NULL AS used, expires_at <= CURRENT_TIMESTAMP AS expired
                        FROM password_resets WHERE token_hash = %s""" + suffix, (token_hash,))


def _recovery_token_error(record):
    if not record:
        return (jsonify(error="This link is invalid. Request a new one."), 400)
    if record["used"]:
        return (jsonify(error="This link has already been used. Request a new one."), 409)
    if record["expired"]:
        return (jsonify(error="This link has expired. Request a new one."), 410)
    return None


@app.get("/api/recover/check")
def recover_check():
    token = str(request.args.get("token", ""))
    if len(token) < 32 or len(token) > 256:
        return jsonify(error="This link is invalid. Request a new one."), 400
    problem = _recovery_token_error(_recovery_record(token))
    return problem if problem else jsonify(ok=True)


@app.post("/api/recover/reset")
def recover_reset():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", ""))
    password = str(data.get("password", ""))
    confirm = str(data.get("confirm_password", ""))
    if len(token) < 32 or len(token) > 256:
        return jsonify(error="This link is invalid. Request a new one."), 400
    if password != confirm:
        return jsonify(error="The passwords do not match. Enter them again."), 400
    if len(password) < 8 or len(password.encode("utf-8")) > 72 or not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        return jsonify(error="Use at least 8 characters, including a letter and a number."), 400
    conn = db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cur.execute("""SELECT id, user_id, used_at IS NOT NULL AS used, expires_at <= CURRENT_TIMESTAMP AS expired
                       FROM password_resets WHERE token_hash = %s FOR UPDATE""", (token_hash,))
        record = cur.fetchone()
        if record:
            record = dict(record)
        problem = _recovery_token_error(record)
        if problem:
            conn.rollback()
            return problem
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), record["user_id"]))
        cur.execute("UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE user_id = %s AND used_at IS NULL", (record["user_id"],))
    conn.commit()
    for session_token, (uid, _expiry) in list(SESSIONS.items()):
        if uid == record["user_id"]:
            SESSIONS.pop(session_token, None)
    return jsonify(ok=True)


@app.post("/api/support-requests")
def create_support_request():
    data = request.get_json(silent=True) or {}
    email = _valid_university_email(data.get("email"))
    message = str(data.get("message", "")).strip()
    if not email:
        return jsonify(error="Enter your university email address ending in @astanait.edu.kz."), 400
    if len(message) < 10 or len(message) > 1000:
        return jsonify(error="Write a short message between 10 and 1000 characters."), 400
    if _recovery_rate_limited(email, request.remote_addr, "support"):
        return jsonify(error="Too many support requests. Wait 15 minutes before trying again."), 429
    row = execute("INSERT INTO support_requests(email,message) VALUES (%s,%s) RETURNING id", (email, message), returning=True)
    return jsonify(ok=True, id=row["id"]), 201


@app.get("/api/support-requests")
def list_support_requests():
    user, error = admin_guard()
    if error:
        return error
    result = rows("""SELECT id,email,message,status,to_char(created_at,'YYYY-MM-DD HH24:MI') AS created_at
                    FROM support_requests ORDER BY created_at DESC, id DESC""")
    return jsonify(result)


@app.patch("/api/support-requests/<int:request_id>")
def update_support_request(request_id):
    user, error = admin_guard()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", ""))
    if status not in ("new", "resolved"):
        return jsonify(error="Status must be new or resolved."), 400
    row = execute("UPDATE support_requests SET status = %s WHERE id = %s RETURNING id", (status, request_id), returning=True)
    if not row:
        return jsonify(error="Support request not found."), 404
    return jsonify(ok=True, id=row["id"], status=status)


def current_user():
    token = request.headers.get("Authorization", "")
    session = SESSIONS.get(token)
    if not session:
        return None
    uid, expires_at = session
    if expires_at < time.time():
        SESSIONS.pop(token, None)
        return None
    return query_one("SELECT * FROM users WHERE id = %s", (uid,))


def is_locked(email):
    with ATTEMPTS_LOCK:
        entry = ATTEMPTS.get(email)
        if not entry:
            return False
        if entry[1] and entry[1] <= time.time():  # lock expired -> forget this e-mail
            ATTEMPTS.pop(email, None)
            return False
        return entry[1] > time.time()


def register_failure(email):
    with ATTEMPTS_LOCK:
        now = time.time()
        for key in [k for k, v in ATTEMPTS.items() if v[1] and v[1] <= now]:
            ATTEMPTS.pop(key, None)  # clean up expired locks
        if email not in ATTEMPTS and len(ATTEMPTS) >= 10000:
            return  # protection against filling the memory with random e-mails
        entry = ATTEMPTS.setdefault(email, [0, 0])
        entry[0] += 1
        if entry[0] >= MAX_ATTEMPTS:  # 5th failure -> lock and start counting again
            entry[0] = 0
            entry[1] = now + LOCK_MINUTES * 60


def clear_failures(email):
    with ATTEMPTS_LOCK:
        ATTEMPTS.pop(email, None)


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if is_locked(email):
        return jsonify(error=f"Account is locked for {LOCK_MINUTES} minutes after too many failed attempts"), 429

    user = query_one("SELECT * FROM users WHERE email = %s", (email,))

    # Always run one bcrypt check, so the reply does not show whether the e-mail exists.
    ok = check_password(password, user["password_hash"] if user else DUMMY_HASH) and user is not None
    if not ok:
        register_failure(email)  # counted in memory, nothing is written to the database
        return jsonify(error="Wrong e-mail or password"), 401

    clear_failures(email)
    token = secrets.token_hex(16)
    SESSIONS[token] = (user["id"], time.time() + SESSION_SECONDS)
    return jsonify(token=token, name=user["full_name"], email=user["email"], role=user["role"])


@app.post("/api/logout")
def logout():
    SESSIONS.pop(request.headers.get("Authorization", ""), None)
    return jsonify(ok=True)


def guard():
    user = current_user()
    return user, (None if user else (jsonify(error="Not signed in"), 401))


def teacher_guard():
    """Like guard(), but only teachers pass. The UI hides teacher buttons, this is the real protection."""
    user, error = guard()
    if error:
        return None, error
    if user["role"] != "teacher":
        return None, (jsonify(error="Only teachers can do this"), 403)
    return user, None


def owns_course(user, course_id):
    return query_one("SELECT 1 AS found FROM course_teachers WHERE course_id = %s AND teacher_id = %s",
                     (course_id, user["id"])) is not None


def parse_hhmm(value):
    """'09:30' -> time(9, 30). Raises ValueError for anything else."""
    text = str(value).strip()
    if len(text) != 5:
        raise ValueError(text)
    return dtime.fromisoformat(text)


@app.get("/api/courses")
def courses():
    user, error = guard()
    if error:
        return error
    if user["role"] == "teacher":  # a teacher sees the courses he teaches
        return jsonify(rows("""
            SELECT c.id, c.code, c.title, t.full_name AS teacher,
                   (SELECT COUNT(*) FROM enrollments e WHERE e.course_id = c.id) AS students
            FROM courses c JOIN users t ON t.id = c.teacher_id
            WHERE EXISTS (SELECT 1 FROM course_teachers ct WHERE ct.course_id=c.id AND ct.teacher_id=%s)
            ORDER BY c.title
        """, (user["id"],)))
    return jsonify(rows("""
        SELECT c.id, c.code, c.title, t.full_name AS teacher
        FROM enrollments e
        JOIN courses c ON c.id = e.course_id
        JOIN users t ON t.id = c.teacher_id
        WHERE e.user_id = %s
        ORDER BY c.title
    """, (user["id"],)))


@app.get("/api/courses/<int:course_id>/students")
def course_students(course_id):
    """Teacher only: students of his course and the group each one is already in (if any)."""
    user, error = teacher_guard()
    if error:
        return error
    if not owns_course(user, course_id):
        raise Refuse(403, "This is not your course")
    return jsonify(rows("""
        SELECT u.id, u.full_name AS name, u.email,
               (SELECT p.id FROM group_members m JOIN project_groups p ON p.id = m.group_id
                WHERE m.user_id = u.id AND p.course_id = e.course_id LIMIT 1) AS group_id
        FROM enrollments e JOIN users u ON u.id = e.user_id
        WHERE e.course_id = %s AND u.role = 'student'
        ORDER BY u.id
    """, (course_id,)))


@app.get("/api/grades")
def grades():
    user, error = guard()
    if error:
        return error
    return jsonify(rows("""
        SELECT c.title AS course, SUM(g.score) AS score, SUM(g.max_score) AS max_score
        FROM grades g JOIN courses c ON c.id = g.course_id
        WHERE g.user_id = %s
        GROUP BY c.id, c.title
        ORDER BY c.title
    """, (user["id"],)))


# ---------------------------------------------------------------- assignments and deadlines
ASSIGNMENT_COLUMNS = """a.id, a.course_id, c.title AS course, teacher.full_name AS teacher, a.title, t.name AS type,
       a.control_form, a.max_score, a.description, a.due_date::text AS due_date,
       to_char(a.due_time, 'HH24:MI') AS due_time,
       (a.due_date + a.due_time) < localtimestamp AS overdue,
       EXTRACT(EPOCH FROM ((a.due_date + a.due_time) - localtimestamp))::bigint AS seconds_left"""


@app.get("/api/assignments")
def assignments():
    user, error = guard()
    if error:
        return error
    if user["role"] == "teacher":  # all tasks of his own courses
        return jsonify(rows(f"""
            SELECT {ASSIGNMENT_COLUMNS}, NULL::numeric AS score,
                   (SELECT COUNT(*) FROM submissions sb WHERE sb.assignment_id = a.id) AS submitted,
                   (SELECT COUNT(*) FROM enrollments en WHERE en.course_id = a.course_id) AS enrolled
            FROM assignments a
            JOIN courses c ON c.id = a.course_id
                 AND EXISTS (SELECT 1 FROM course_teachers ct WHERE ct.course_id = c.id AND ct.teacher_id = %s)
            JOIN users teacher ON teacher.id = c.teacher_id
            JOIN assignment_types t ON t.id = a.type_id
            ORDER BY a.due_date, a.due_time, c.title
        """, (user["id"],)))
    return jsonify(rows(f"""
        SELECT {ASSIGNMENT_COLUMNS}, g.score,
               sb.url AS submission_url, sb.comment AS submission_comment,
               to_char(sb.submitted_at, 'YYYY-MM-DD HH24:MI') AS submitted_at,
               (sb.submitted_at > a.due_date + a.due_time) AS submitted_late
        FROM assignments a
        JOIN courses c ON c.id = a.course_id
        JOIN users teacher ON teacher.id = c.teacher_id
        JOIN assignment_types t ON t.id = a.type_id
        JOIN enrollments e ON e.course_id = a.course_id AND e.user_id = %s
        LEFT JOIN grades g ON g.assignment_id = a.id AND g.user_id = %s
        LEFT JOIN submissions sb ON sb.assignment_id = a.id AND sb.user_id = e.user_id
        ORDER BY a.due_date, a.due_time, c.title
    """, (user["id"], user["id"])))


@app.get("/api/assignment-types")
def assignment_types():
    _user, error = guard()
    if error:
        return error
    return jsonify(rows("SELECT id, name FROM assignment_types ORDER BY id"))


@app.post("/api/assignments")
def create_assignment():
    """Teacher adds a task to one of his courses with an exact deadline (date + time)."""
    user, error = teacher_guard()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    form = str(data.get("control_form", "current"))
    try:
        course_id = int(data.get("course_id"))
        type_id = int(data.get("type_id"))
        max_score = float(data.get("max_score"))
        due_date = date.fromisoformat(str(data.get("due_date", "")))
        due_time = parse_hhmm(data.get("due_time", ""))
    except (TypeError, ValueError):
        raise Refuse(400, "Fill in course, type, max score and the deadline (date and time HH:MM)")

    if not 1 <= len(title) <= 100:
        raise Refuse(400, "Title must be 1-100 characters")
    if len(description) > 1000:
        raise Refuse(400, "Description is too long (max 1000 characters)")
    if form not in ("current", "midterm", "final"):
        raise Refuse(400, "Unknown form of control")
    if not 0 < max_score <= 1000:
        raise Refuse(400, "Max score must be between 0 and 1000")
    if not owns_course(user, course_id):
        raise Refuse(403, "You can add tasks only to your own courses")
    if not query_one("SELECT 1 AS found FROM assignment_types WHERE id = %s", (type_id,)):
        raise Refuse(400, "Unknown task type")

    due_text = due_time.strftime("%H:%M")
    future = query_one("SELECT (%s::date + %s::time) > localtimestamp AS ok", (due_date.isoformat(), due_text))
    if not future["ok"]:
        raise Refuse(400, "The deadline must be in the future")

    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO assignments(course_id,title,type_id,control_form,max_score,due_date,due_time,description)
               VALUES (%s,%s,%s,%s,%s,%s::date,%s::time,%s)
               ON CONFLICT (course_id, title) DO NOTHING
               RETURNING id""",
            (course_id, title, type_id, form, max_score, due_date.isoformat(), due_text, description))
        created = cur.fetchone()
    conn.commit()
    if not created:
        raise Refuse(409, "This course already has a task with this title")
    return jsonify(id=created[0]), 201


@app.delete("/api/assignments/<int:assignment_id>")
def delete_assignment(assignment_id):
    user, error = teacher_guard()
    if error:
        return error
    found = query_one(
        """SELECT a.id FROM assignments a JOIN courses c ON c.id = a.course_id
           WHERE a.id = %s
             AND EXISTS (SELECT 1 FROM course_teachers ct WHERE ct.course_id = c.id AND ct.teacher_id = %s)""",
        (assignment_id, user["id"]))
    if not found:
        raise Refuse(404, "Task not found in your courses")
    if query_one("SELECT 1 AS found FROM grades WHERE assignment_id = %s LIMIT 1", (assignment_id,)):
        raise Refuse(409, "This task already has grades and cannot be deleted")
    execute("DELETE FROM assignments WHERE id = %s", (assignment_id,))
    return jsonify(ok=True)


# ---------------------------------------------------------------- timetable
WEEKDAY_MAX = 7
LESSON_KINDS = ("Lecture", "Practice", "Lab")


@app.get("/api/schedule")
def schedule():
    user, error = guard()
    if error:
        return error
    if user["role"] == "teacher":
        join, where = "", "WHERE EXISTS (SELECT 1 FROM course_teachers ct WHERE ct.course_id=c.id AND ct.teacher_id=%s)"
    elif user["role"] == "admin":
        join, where = "", ""
    else:
        join, where = "JOIN enrollments e ON e.course_id = c.id AND e.user_id = %s", ""
    return jsonify(rows(f"""
        SELECT s.id, s.course_id, c.title AS course, c.code, u.full_name AS teacher, s.weekday,
               to_char(s.start_time, 'HH24:MI') AS starts, to_char(s.end_time, 'HH24:MI') AS ends,
               s.room, s.kind, s.class_date::text AS class_date
        FROM schedule s
        JOIN courses c ON c.id = s.course_id
        JOIN users u ON u.id = c.teacher_id
        {join}
        {where}
        ORDER BY s.weekday, s.start_time, c.title
    """, (user["id"],)))


@app.post("/api/schedule")
def create_lesson():
    user, error = admin_guard()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    room = str(data.get("room", "")).strip()
    kind = str(data.get("kind", ""))
    try:
        course_id = int(data.get("course_id"))
        weekday = int(data.get("weekday"))
        start = parse_hhmm(data.get("start", ""))
        end = parse_hhmm(data.get("end", ""))
    except (TypeError, ValueError):
        raise Refuse(400, "Fill in course, day, start and end time (HH:MM)")
    if not 1 <= weekday <= WEEKDAY_MAX or kind not in LESSON_KINDS or not 1 <= len(room) <= 20:
        raise Refuse(400, "Check day, type and room (1-20 characters)")
    if end <= start:
        raise Refuse(400, "The class must end after it starts")
    if not query_one("SELECT 1 AS found FROM courses WHERE id=%s", (course_id,)):
        raise Refuse(400, "Course not found")

    start_text, end_text = start.strftime("%H:%M"), end.strftime("%H:%M")
    clash = query_one(
        """SELECT 1 AS found FROM schedule s
           WHERE s.weekday = %s AND s.start_time < %s::time AND s.end_time > %s::time
             AND (s.room = %s OR EXISTS (SELECT 1 FROM course_teachers a JOIN course_teachers b ON b.teacher_id=a.teacher_id
               WHERE a.course_id=s.course_id AND b.course_id=%s)) LIMIT 1""",
        (weekday, end_text, start_text, room, course_id))
    if clash:
        raise Refuse(409, "At this time you already have a class or the room is taken")
    created = execute(
        """INSERT INTO schedule(course_id,weekday,start_time,end_time,room,kind)
           VALUES (%s,%s,%s::time,%s::time,%s,%s) RETURNING id""",
        (course_id, weekday, start_text, end_text, room, kind), returning=True)
    return jsonify(id=created["id"]), 201


@app.delete("/api/schedule/<int:lesson_id>")
def delete_lesson(lesson_id):
    user, error = admin_guard()
    if error:
        return error
    conn = db()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM schedule WHERE id = %s", (lesson_id,))
        deleted = cur.rowcount
    conn.commit()
    if not deleted:
        raise Refuse(404, "Class not found in your courses")
    return jsonify(ok=True)


# ---------------------------------------------------------------- student data
@app.get("/api/attendance")
def attendance():
    user, error = guard()
    if error:
        return error
    return jsonify(rows("""
        SELECT c.title AS course, COUNT(*) AS total, SUM(a.present) AS present
        FROM attendance a JOIN courses c ON c.id = a.course_id
        WHERE a.user_id = %s
        GROUP BY c.id, c.title
        ORDER BY c.title
    """, (user["id"],)))


@app.get("/api/dashboard")
def dashboard():
    user, error = guard()
    if error:
        return error
    uid = user["id"]
    if user["role"] == "teacher":
        stats = query_one("""
            SELECT (SELECT COUNT(*) FROM courses WHERE teacher_id = %(u)s) AS courses,
                   (SELECT COUNT(DISTINCT e.user_id) FROM enrollments e
                    JOIN courses c ON c.id = e.course_id WHERE c.teacher_id = %(u)s) AS students,
                   (SELECT COUNT(*) FROM project_groups p
                    JOIN courses c ON c.id = p.course_id WHERE c.teacher_id = %(u)s) AS groups,
                   (SELECT COUNT(*) FROM assignments a JOIN courses c ON c.id = a.course_id
                    WHERE c.teacher_id = %(u)s AND (a.due_date + a.due_time) >= localtimestamp) AS assignments
        """, {"u": uid})
        return jsonify(stats)
    courses_count = query_one(
        "SELECT COUNT(*) AS value FROM enrollments WHERE user_id = %s", (uid,)
    )["value"]
    grade = query_one(
        "SELECT 100.0 * SUM(score) / NULLIF(SUM(max_score), 0) AS value FROM grades WHERE user_id = %s",
        (uid,)
    )["value"]
    attendance_pct = query_one(
        "SELECT 100.0 * SUM(present) / NULLIF(COUNT(*), 0) AS value FROM attendance WHERE user_id = %s",
        (uid,)
    )["value"]
    group_count = query_one(
        "SELECT COUNT(*) AS value FROM group_members WHERE user_id = %s", (uid,)
    )["value"]
    return jsonify(
        courses=courses_count or 0,
        avg_grade=round(float(grade or 0), 1),
        attendance=round(float(attendance_pct or 0), 1),
        groups=group_count or 0
    )


# ---------------------------------------------------------------- project groups
GROUP_SELECT = """
    SELECT p.id, p.course_id, p.name, p.max_size, c.title AS course,
           COALESCE((SELECT json_agg(json_build_object('id', u.id, 'name', u.full_name, 'email', u.email)
                                     ORDER BY u.full_name)
                     FROM group_members m JOIN users u ON u.id = m.user_id
                     WHERE m.group_id = p.id), '[]'::json) AS participants
    FROM project_groups p
    JOIN courses c ON c.id = p.course_id
"""


@app.get("/api/groups")
def groups():
    """Teacher: all groups of his courses. Student: only the groups he is a member of. Both see participants."""
    user, error = guard()
    if error:
        return error
    teacher = user["role"] == "teacher"
    if teacher:
        found = rows(GROUP_SELECT + " WHERE c.teacher_id = %s ORDER BY c.title, p.name", (user["id"],))
    else:
        found = rows(GROUP_SELECT + """ JOIN group_members me ON me.group_id = p.id AND me.user_id = %s
                                        ORDER BY p.id DESC""", (user["id"],))
    for group in found:
        group["members"] = len(group["participants"])
        if not teacher:  # students see the names of the team, e-mails are for teachers only
            for person in group["participants"]:
                person.pop("email", None)
    return jsonify(found)


@app.post("/api/groups")
def create_group():
    """Only a teacher creates groups, inside one of his own courses."""
    user, error = teacher_guard()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    try:
        course_id = int(data.get("course_id"))
        max_size = int(data.get("max_size"))
    except (TypeError, ValueError):
        raise Refuse(400, "Invalid input")
    if not 1 <= len(name) <= 60 or not 2 <= max_size <= 6:
        raise Refuse(400, "Name 1-60 chars, size 2-6")
    if not owns_course(user, course_id):
        raise Refuse(403, "You can create groups only in your own courses")
    created = execute(
        """INSERT INTO project_groups(course_id,name,max_size,created_by)
           VALUES (%s,%s,%s,%s) RETURNING id""",
        (course_id, name, max_size, user["id"]), returning=True)
    return jsonify(id=created["id"]), 201


@app.delete("/api/groups/<int:group_id>")
def delete_group(group_id):
    user, error = teacher_guard()
    if error:
        return error
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """DELETE FROM project_groups p USING courses c
               WHERE p.id = %s AND c.id = p.course_id AND c.teacher_id = %s""",
            (group_id, user["id"]))
        deleted = cur.rowcount
    conn.commit()
    if not deleted:
        raise Refuse(404, "Group not found in your courses")
    return jsonify(ok=True)


@app.post("/api/groups/<int:group_id>/members")
def add_member(group_id):
    """Teacher puts a specific student of the course into a group."""
    user, error = teacher_guard()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        student_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        raise Refuse(400, "Choose a student")

    conn = db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # FOR UPDATE locks the group row, so two teachers' clicks cannot overfill the group
        cur.execute(
            """SELECT p.course_id, p.max_size FROM project_groups p JOIN courses c ON c.id = p.course_id
               WHERE p.id = %s AND c.teacher_id = %s FOR UPDATE OF p""",
            (group_id, user["id"]))
        group = cur.fetchone()
        if not group:
            raise Refuse(404, "Group not found in your courses")
        cur.execute(
            """SELECT 1 FROM enrollments e JOIN users u ON u.id = e.user_id
               WHERE e.user_id = %s AND e.course_id = %s AND u.role = 'student'""",
            (student_id, group["course_id"]))
        if not cur.fetchone():
            raise Refuse(400, "This student is not enrolled in the course")
        cur.execute(
            """SELECT 1 FROM group_members m JOIN project_groups p ON p.id = m.group_id
               WHERE m.user_id = %s AND p.course_id = %s""",
            (student_id, group["course_id"]))
        if cur.fetchone():
            raise Refuse(409, "This student is already in a group of this course")
        cur.execute("SELECT COUNT(*) AS n FROM group_members WHERE group_id = %s", (group_id,))
        if cur.fetchone()["n"] >= group["max_size"]:
            raise Refuse(409, "The group is full")
        cur.execute("INSERT INTO group_members(group_id,user_id) VALUES (%s,%s)", (group_id, student_id))
    conn.commit()
    return jsonify(ok=True), 201


@app.delete("/api/groups/<int:group_id>/members/<int:student_id>")
def remove_member(group_id, student_id):
    user, error = teacher_guard()
    if error:
        return error
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """DELETE FROM group_members m USING project_groups p, courses c
               WHERE m.group_id = %s AND m.user_id = %s AND p.id = m.group_id
                 AND c.id = p.course_id AND c.teacher_id = %s""",
            (group_id, student_id, user["id"]))
        deleted = cur.rowcount
    conn.commit()
    if not deleted:
        raise Refuse(404, "Participant not found in your groups")
    return jsonify(ok=True)


# ---------------------------------------------------------------- administrator course and dated schedule management
@app.get("/api/admin/teachers")
def admin_teachers():
    _user, error = admin_guard()
    if error: return error
    return jsonify(rows("SELECT id,full_name AS name,email FROM users WHERE role='teacher' ORDER BY full_name"))

@app.get("/api/admin/courses")
def admin_courses():
    _user, error = admin_guard()
    if error: return error
    return jsonify(rows("""SELECT c.id,c.code,c.title,c.description,c.teacher_id,
      COALESCE(array_agg(ct.teacher_id) FILTER (WHERE ct.teacher_id IS NOT NULL), ARRAY[]::integer[]) AS teacher_ids,
      COALESCE(string_agg(u.full_name, ', ' ORDER BY u.full_name), '') AS teachers
      FROM courses c LEFT JOIN course_teachers ct ON ct.course_id=c.id LEFT JOIN users u ON u.id=ct.teacher_id
      GROUP BY c.id ORDER BY c.title"""))

def course_values(data):
    code=str(data.get('code','')).strip().upper(); title=str(data.get('title','')).strip(); desc=str(data.get('description','')).strip()
    try: ids=list(dict.fromkeys(int(x) for x in data.get('teacher_ids', [])))
    except (TypeError,ValueError): raise Refuse(400,'Choose at least one registered teacher')
    if not 1<=len(code)<=20 or not re.fullmatch(r'[A-Z0-9._-]+',code): raise Refuse(400,'Course code must be 1-20 letters, numbers, dots, _ or -')
    if not 1<=len(title)<=150: raise Refuse(400,'Course title must be 1-150 characters')
    if len(desc)>4000: raise Refuse(400,'Description is too long (maximum 4000 characters)')
    if not ids: raise Refuse(400,'Choose at least one registered teacher')
    found=query_one("SELECT COUNT(*) AS n FROM users WHERE role='teacher' AND id = ANY(%s)",(ids,))
    if found['n']!=len(ids): raise Refuse(400,'One or more selected users are not registered teachers')
    return code,title,desc,ids

@app.post("/api/admin/courses")
def admin_course_create():
    _user,error=admin_guard()
    if error: return error
    code,title,desc,ids=course_values(request.get_json(silent=True) or {})
    conn=db()
    try:
      with conn.cursor() as cur:
        cur.execute("INSERT INTO courses(code,title,description,teacher_id) VALUES (%s,%s,%s,%s) RETURNING id",(code,title,desc,ids[0])); cid=cur.fetchone()[0]
        cur.executemany("INSERT INTO course_teachers(course_id,teacher_id) VALUES (%s,%s)",[(cid,x) for x in ids])
      conn.commit()
    except Exception as ex:
      conn.rollback()
      if 'unique' in str(ex).lower(): raise Refuse(409,'A course with this code already exists')
      raise
    finally: conn.close()
    return jsonify(id=cid),201

@app.route("/api/admin/courses/<int:course_id>",methods=["PUT","DELETE"])
def admin_course_item(course_id):
    _user,error=admin_guard()
    if error: return error
    if request.method=='DELETE':
      execute("DELETE FROM courses WHERE id=%s",(course_id,))
      return jsonify(ok=True)
    code,title,desc,ids=course_values(request.get_json(silent=True) or {})
    conn=db()
    try:
      with conn.cursor() as cur:
        cur.execute("UPDATE courses SET code=%s,title=%s,description=%s,teacher_id=%s WHERE id=%s",(code,title,desc,ids[0],course_id))
        if not cur.rowcount: raise Refuse(404,'Course not found')
        cur.execute("DELETE FROM course_teachers WHERE course_id=%s",(course_id,))
        cur.executemany("INSERT INTO course_teachers(course_id,teacher_id) VALUES (%s,%s)",[(course_id,x) for x in ids])
      conn.commit()
    except Exception:
      conn.rollback(); raise
    finally: conn.close()
    return jsonify(ok=True)

@app.get("/api/admin/schedule")
def admin_schedule():
    _user,error=admin_guard()
    if error: return error
    return jsonify(rows("""SELECT s.id,s.course_id,c.title AS course,c.code,s.class_date::text AS class_date,
      to_char(s.start_time,'HH24:MI') AS starts,to_char(s.end_time,'HH24:MI') AS ends,s.room,s.kind,
      COALESCE(string_agg(u.full_name, ', ' ORDER BY u.full_name),'') AS teachers
      FROM schedule s JOIN courses c ON c.id=s.course_id LEFT JOIN course_teachers ct ON ct.course_id=c.id
      LEFT JOIN users u ON u.id=ct.teacher_id WHERE s.class_date IS NOT NULL
      GROUP BY s.id,c.title,c.code ORDER BY s.class_date,s.start_time,c.title"""))

def lesson_payload(data):
    try:
      cid=int(data.get('course_id')); day=date.fromisoformat(str(data.get('class_date',''))); start=parse_hhmm(data.get('start','')); end=parse_hhmm(data.get('end',''))
    except (TypeError,ValueError): raise Refuse(400,'Choose a course, valid date and start/end times (HH:MM)')
    room=str(data.get('room','')).strip(); kind=str(data.get('kind','Lecture'))
    if end<=start: raise Refuse(400,'End time must be later than start time')
    if not 1<=len(room)<=20: raise Refuse(400,'Room must be 1-20 characters')
    if kind not in LESSON_KINDS: raise Refuse(400,'Choose Lecture, Practice or Lab')
    if not query_one('SELECT 1 AS found FROM courses WHERE id=%s',(cid,)): raise Refuse(400,'Course not found')
    return cid,day,start,end,room,kind

def check_lesson_conflict(cid,day,start,end,room,exclude=None):
    # Serialize schedule writes so two simultaneous admin requests cannot both pass the overlap check.
    query_one("SELECT pg_advisory_xact_lock(74219031)")
    args=[day.isoformat(),day.isoweekday(),end.strftime('%H:%M'),start.strftime('%H:%M'),room]
    exclude_sql=''
    if exclude is not None: exclude_sql=' AND s.id <> %s'; args.append(exclude)
    clash=query_one(f"""SELECT s.id,s.room,c.title FROM schedule s JOIN courses c ON c.id=s.course_id
      WHERE (s.class_date=%s OR (s.class_date IS NULL AND s.weekday=%s)) AND s.start_time < %s::time AND s.end_time > %s::time
      AND (s.room=%s OR EXISTS (SELECT 1 FROM course_teachers a JOIN course_teachers b ON b.teacher_id=a.teacher_id
        WHERE a.course_id=s.course_id AND b.course_id=%s)) {exclude_sql} LIMIT 1""" ,tuple(args[:5]+[cid]+args[5:]))
    if clash:
      reason='room '+clash['room'] if clash['room']==room else 'a teacher'
      raise Refuse(409,f'Schedule conflict: {reason} is already occupied at this time by {clash["title"]}')

@app.post("/api/admin/schedule")
def admin_lesson_create():
    _user,error=admin_guard()
    if error: return error
    cid,day,start,end,room,kind=lesson_payload(request.get_json(silent=True) or {})
    check_lesson_conflict(cid,day,start,end,room)
    result=execute("INSERT INTO schedule(course_id,weekday,start_time,end_time,room,kind,class_date) VALUES (%s,%s,%s::time,%s::time,%s,%s,%s) RETURNING id",(cid,day.isoweekday(),start.strftime('%H:%M'),end.strftime('%H:%M'),room,kind,day.isoformat()),returning=True)
    return jsonify(id=result['id']),201

@app.route("/api/admin/schedule/<int:lesson_id>",methods=["PUT","DELETE"])
def admin_lesson_item(lesson_id):
    _user,error=admin_guard()
    if error: return error
    found=query_one('SELECT 1 AS found FROM schedule WHERE id=%s AND class_date IS NOT NULL',(lesson_id,))
    if not found: raise Refuse(404,'Lesson not found')
    if request.method=='DELETE': execute('DELETE FROM schedule WHERE id=%s',(lesson_id,)); return jsonify(ok=True)
    cid,day,start,end,room,kind=lesson_payload(request.get_json(silent=True) or {})
    check_lesson_conflict(cid,day,start,end,room,lesson_id)
    execute("UPDATE schedule SET course_id=%s,weekday=%s,start_time=%s::time,end_time=%s::time,room=%s,kind=%s,class_date=%s WHERE id=%s",(cid,day.isoweekday(),start.strftime('%H:%M'),end.strftime('%H:%M'),room,kind,day.isoformat(),lesson_id))
    return jsonify(ok=True)


# ---------------------------------------------------------------- applications (IROS, SSQI, SocGPA ...)
def admin_guard():
    user, error = guard()
    if error:
        return None, error
    if user["role"] != "admin":
        return None, (jsonify(error="Only the registrar office (admin) can do this"), 403)
    return user, None


ANNOUNCEMENT_CATEGORIES = {"General", "Important", "Exams", "Events"}
ANNOUNCEMENT_AUDIENCES = {"everyone", "students", "teachers", "course"}


def announcement_payload(data):
    title = str(data.get("title", "")).strip()
    body = str(data.get("body", "")).strip()
    category = str(data.get("category", ""))
    audience = str(data.get("audience", ""))
    pinned = data.get("pinned") is True or str(data.get("pinned", "")).lower() == "true"
    if not 1 <= len(title) <= 120:
        raise Refuse(400, "Title must be 1-120 characters")
    if not 1 <= len(body) <= 2000:
        raise Refuse(400, "Text must be 1-2000 characters")
    if category not in ANNOUNCEMENT_CATEGORIES:
        raise Refuse(400, "Choose a valid category")
    if audience not in ANNOUNCEMENT_AUDIENCES:
        raise Refuse(400, "Choose a valid audience")
    course_id = None
    if audience == "course":
        try: course_id = int(data.get("course_id"))
        except (TypeError, ValueError): raise Refuse(400, "Choose a course")
        if not query_one("SELECT id FROM courses WHERE id = %s", (course_id,)):
            raise Refuse(400, "The selected course does not exist")
    expires_at = None
    raw_expiry = str(data.get("expires_at") or "").strip()
    if raw_expiry:
        try:
            expires_at = datetime.combine(date.fromisoformat(raw_expiry), dtime.max)
        except ValueError:
            raise Refuse(400, "Enter a valid expiration date")
        if expires_at.date() < date.today():
            raise Refuse(400, "Expiration date cannot be in the past")
    return title, body, category, audience, course_id, pinned, expires_at


ANNOUNCEMENT_SELECT = """
    SELECT a.id,a.title,a.body,a.category,a.audience,a.course_id,a.pinned,
           to_char(a.created_at,'YYYY-MM-DD HH24:MI') AS created_at,
           to_char(a.updated_at,'YYYY-MM-DD HH24:MI') AS updated_at,
           to_char(a.expires_at,'YYYY-MM-DD') AS expires_at,
           (a.expires_at IS NOT NULL AND a.expires_at < CURRENT_TIMESTAMP) AS expired,
           u.full_name AS author,c.title AS course,
           EXISTS(SELECT 1 FROM announcement_reads ar WHERE ar.user_id=%s AND ar.announcement_id=a.id) AS is_read
    FROM announcements a JOIN users u ON u.id=a.author_id LEFT JOIN courses c ON c.id=a.course_id
"""


def announcement_scope(user):
    if user["role"] == "admin":
        return "TRUE", ()
    if user["role"] == "student":
        scope = "(a.audience='everyone' OR a.audience='students' OR (a.audience='course' AND EXISTS(SELECT 1 FROM enrollments e WHERE e.user_id=%s AND e.course_id=a.course_id)))"
    else:
        scope = "(a.audience='everyone' OR a.audience='teachers' OR (a.audience='course' AND EXISTS(SELECT 1 FROM courses tc WHERE tc.teacher_id=%s AND tc.id=a.course_id)))"
    return scope, (user["id"],)


@app.get("/api/announcements", strict_slashes=False)
def list_announcements():
    user,error=guard()
    if error:return error
    q=str(request.args.get("q", "")).strip()[:120]
    category=str(request.args.get("category", "")).strip()
    try: offset=max(0,min(10000,int(request.args.get("offset",0))))
    except (TypeError,ValueError): raise Refuse(400,"Invalid offset")
    try: limit=max(1,min(10,int(request.args.get("limit",10))))
    except (TypeError,ValueError): limit=10
    if category and category not in ANNOUNCEMENT_CATEGORIES: raise Refuse(400,"Choose a valid category")
    scope,scope_args=announcement_scope(user)
    where=[scope]
    args=[user["id"]]+list(scope_args)
    if user["role"]!="admin": where.append("(a.expires_at IS NULL OR a.expires_at >= CURRENT_TIMESTAMP)")
    if category: where.append("a.category=%s");args.append(category)
    if q: where.append("(a.title ILIKE %s OR a.body ILIKE %s)");args.extend([f"%{q}%",f"%{q}%"])
    args.extend([limit,offset])
    sql=ANNOUNCEMENT_SELECT+" WHERE "+" AND ".join(where)+" ORDER BY a.pinned DESC,a.created_at DESC,a.id DESC LIMIT %s OFFSET %s"
    return jsonify(rows(sql,tuple(args)))


@app.get("/api/announcements/unread-count", strict_slashes=False)
def announcement_unread_count():
    user,error=guard()
    if error:return error
    scope,scope_args=announcement_scope(user)
    expiry="" if user["role"]=="admin" else " AND (a.expires_at IS NULL OR a.expires_at >= CURRENT_TIMESTAMP)"
    row=query_one("SELECT COUNT(*) AS count FROM announcements a WHERE "+scope+expiry+" AND NOT EXISTS(SELECT 1 FROM announcement_reads ar WHERE ar.user_id=%s AND ar.announcement_id=a.id)",tuple(scope_args)+(user["id"],))
    return jsonify(count=row["count"])


@app.post("/api/announcements/<int:announcement_id>/read", strict_slashes=False)
def mark_announcement_read(announcement_id):
    user,error=guard()
    if error:return error
    scope,scope_args=announcement_scope(user)
    expiry="" if user["role"]=="admin" else " AND (a.expires_at IS NULL OR a.expires_at >= CURRENT_TIMESTAMP)"
    allowed=query_one("SELECT a.id FROM announcements a WHERE a.id=%s AND "+scope+expiry,(announcement_id,)+tuple(scope_args))
    if not allowed: raise Refuse(404,"Announcement not found")
    conn=db()
    with conn.cursor() as cur: cur.execute("INSERT INTO announcement_reads(user_id,announcement_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",(user["id"],announcement_id))
    conn.commit()
    return jsonify(ok=True)


@app.post("/api/announcements/read-all", strict_slashes=False)
def mark_all_announcements_read():
    user,error=guard()
    if error:return error
    scope,scope_args=announcement_scope(user)
    expiry="" if user["role"]=="admin" else " AND (a.expires_at IS NULL OR a.expires_at >= CURRENT_TIMESTAMP)"
    conn=db()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO announcement_reads(user_id,announcement_id) SELECT %s,a.id FROM announcements a WHERE "+scope+expiry+" ON CONFLICT DO NOTHING",(user["id"],)+tuple(scope_args))
    conn.commit()
    return jsonify(ok=True)


@app.get("/api/announcements/courses", strict_slashes=False)
def announcement_courses():
    _admin,error=admin_guard()
    if error:return error
    return jsonify(rows("SELECT id,code,title FROM courses ORDER BY title"))


@app.post("/api/announcements", strict_slashes=False)
def create_announcement():
    user,error=admin_guard()
    if error:return error
    values=announcement_payload(request.get_json(silent=True) or {})
    row=execute("""INSERT INTO announcements(title,body,category,audience,course_id,pinned,author_id,expires_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",values[:6]+(user["id"],values[6]),returning=True)
    return jsonify(id=row["id"]),201


@app.route("/api/announcements/<int:announcement_id>", methods=["PUT", "PATCH", "POST"], strict_slashes=False)
def edit_announcement(announcement_id):
    user,error=admin_guard()
    if error:return error
    values=announcement_payload(request.get_json(silent=True) or {})
    row=execute("""UPDATE announcements SET title=%s,body=%s,category=%s,audience=%s,course_id=%s,pinned=%s,
                 expires_at=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id""",values[:6]+(values[6],announcement_id),returning=True)
    if not row: raise Refuse(404,"Announcement not found")
    return jsonify(ok=True,id=row["id"])


@app.delete("/api/announcements/<int:announcement_id>", strict_slashes=False)
def delete_announcement(announcement_id):
    _user,error=admin_guard()
    if error:return error
    row=execute("DELETE FROM announcements WHERE id=%s RETURNING id",(announcement_id,),returning=True)
    if not row: raise Refuse(404,"Announcement not found")
    return jsonify(ok=True)


APPLICATION_SELECT = """
    SELECT a.id, t.code AS type, t.title AS type_title, a.message, a.status,
           to_char(a.created_at, 'YYYY-MM-DD HH24:MI') AS created_at,
           a.review_comment, to_char(a.reviewed_at, 'YYYY-MM-DD HH24:MI') AS reviewed_at,
           u.full_name AS student, u.email
    FROM applications a
    JOIN application_types t ON t.id = a.type_id
    JOIN users u ON u.id = a.user_id
"""


@app.get("/api/application-types")
def application_types():
    _user, error = guard()
    if error:
        return error
    return jsonify(rows("SELECT id, code, title FROM application_types ORDER BY id"))


@app.get("/api/applications")
def applications():
    """Student: his own applications. Admin: all applications, pending first."""
    user, error = guard()
    if error:
        return error
    if user["role"] == "admin":
        return jsonify(rows(APPLICATION_SELECT + " ORDER BY (a.status = 'pending') DESC, a.created_at DESC"))
    if user["role"] != "student":
        raise Refuse(403, "Applications are for students")
    return jsonify(rows(APPLICATION_SELECT + " WHERE a.user_id = %s ORDER BY a.created_at DESC", (user["id"],)))


@app.post("/api/applications")
def create_application():
    user, error = guard()
    if error:
        return error
    if user["role"] != "student":
        raise Refuse(403, "Only students submit applications")
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    try:
        type_id = int(data.get("type_id"))
    except (TypeError, ValueError):
        raise Refuse(400, "Choose the type of application")
    if not 1 <= len(message) <= 1000:
        raise Refuse(400, "Message must be 1-1000 characters")
    if not query_one("SELECT 1 AS found FROM application_types WHERE id = %s", (type_id,)):
        raise Refuse(400, "Unknown type of application")
    conn = db()
    with conn.cursor() as cur:
        # the partial unique index allows only one pending application of each type per student
        cur.execute(
            """INSERT INTO applications(user_id,type_id,message) VALUES (%s,%s,%s)
               ON CONFLICT (user_id, type_id) WHERE status = 'pending' DO NOTHING
               RETURNING id""",
            (user["id"], type_id, message))
        created = cur.fetchone()
    conn.commit()
    if not created:
        raise Refuse(409, "You already have a pending application of this type")
    return jsonify(id=created[0]), 201


@app.delete("/api/applications/<int:application_id>")
def withdraw_application(application_id):
    """A student can take back his own application while it is still pending."""
    user, error = guard()
    if error:
        return error
    conn = db()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM applications WHERE id = %s AND user_id = %s AND status = 'pending'",
                    (application_id, user["id"]))
        deleted = cur.rowcount
    conn.commit()
    if not deleted:
        raise Refuse(404, "Pending application not found")
    return jsonify(ok=True)


@app.post("/api/applications/<int:application_id>/review")
def review_application(application_id):
    user, error = admin_guard()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    decision = str(data.get("decision", ""))
    comment = str(data.get("comment", "")).strip()
    if decision not in ("approved", "rejected"):
        raise Refuse(400, "Decision must be approved or rejected")
    if len(comment) > 500:
        raise Refuse(400, "Comment is too long (max 500 characters)")
    if decision == "rejected" and not comment:
        raise Refuse(400, "Write the reason for rejection")
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE applications SET status = %s, review_comment = %s, reviewed_by = %s, reviewed_at = localtimestamp
               WHERE id = %s AND status = 'pending'""",
            (decision, comment, user["id"], application_id))
        updated = cur.rowcount
    conn.commit()
    if not updated:
        raise Refuse(409, "Application not found or already reviewed")
    return jsonify(ok=True)


checkin.register(app, db=db, query_one=query_one, rows=rows, execute=execute, guard=guard,
                 admin_guard=admin_guard, teacher_guard=teacher_guard, Refuse=Refuse,
                 connect=lambda: psycopg2.connect(DATABASE_URL))
submissions.register(app, db=lambda: db(), query_one=lambda *a, **k: query_one(*a, **k), guard=guard, Refuse=Refuse)
materials.register(app,db=lambda: db(), query_one=lambda *a, **k: query_one(*a, **k),
                   rows=lambda *a, **k: rows(*a, **k), guard=guard, Refuse=Refuse)
gradebook.register(app,db=lambda: db(), query_one=lambda *a, **k: query_one(*a, **k),
                   rows=lambda *a, **k: rows(*a, **k), guard=guard, teacher_guard=teacher_guard, Refuse=Refuse)


if __name__ == "__main__":
    init_db()
    print("Open http://127.0.0.1:5000")
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=5000)



