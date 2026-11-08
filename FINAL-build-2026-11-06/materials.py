"""Course materials: links to videos and other resources (YouTube, Vimeo, any web page).

Only LINKS are stored - no files are uploaded. Admin can attach materials to any course, a teacher to
the courses he teaches, a student only reads the materials of the courses he is enrolled in.
Kept in its own file, like checkin.py and gradebook.py; app_postgresql.py only imports it, calls
init_schema(cur) from init_db() and register(...) before `__main__`.
"""
import re
from urllib.parse import parse_qs, urlparse

from flask import jsonify, request

SCHEMA = """
CREATE TABLE IF NOT EXISTS course_materials (
    id SERIAL PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title VARCHAR(120) NOT NULL,
    url VARCHAR(500) NOT NULL,
    description VARCHAR(500) NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT localtimestamp
);
CREATE INDEX IF NOT EXISTS course_materials_course ON course_materials(course_id, id);
"""

TITLE_MAX, URL_MAX, DESCRIPTION_MAX = 120, 500, 500
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_VIMEO_HOSTS = {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}


def init_schema(cur):
    cur.execute(SCHEMA)


def clean_url(value):
    """The link as a clean string, or None if it is not a plain http(s) link."""
    text = str(value or "").strip()
    if not text or len(text) > URL_MAX or re.search(r"[\s\x00-\x1f\x7f<>\"'\\]", text):
        return None
    try:
        parts = urlparse(text)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or "." not in host or parts.username or parts.password:
        return None
    return text


def embed_url(url):
    """Address of an embeddable player for YouTube / Vimeo links, otherwise None.
    Built from the extracted video id only - the original URL never goes into the iframe."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host in _YOUTUBE_HOSTS:
        video = None
        if path == "/watch":
            video = (parse_qs(parts.query).get("v") or [""])[0]
        else:
            found = re.match(r"^/(?:embed|shorts|live)/([^/?#]+)", path)
            video = found.group(1) if found else None
        if video and _VIDEO_ID.match(video):
            return f"https://www.youtube-nocookie.com/embed/{video}"
    elif host == "youtu.be":
        video = path.lstrip("/").split("/")[0]
        if _VIDEO_ID.match(video):
            return f"https://www.youtube-nocookie.com/embed/{video}"
    elif host in _VIMEO_HOSTS:
        found = re.match(r"^/(?:video/)?(\d{5,12})(?:/|$)", path)
        if found:
            return f"https://player.vimeo.com/video/{found.group(1)}"
    return None


def host_of(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def register(app, *, db, query_one, rows, guard, Refuse):
    def teaches(user, course_id):
        return query_one("SELECT 1 AS found FROM course_teachers WHERE course_id = %s AND teacher_id = %s",
                         (course_id, user["id"])) is not None

    def shape(row, can_delete):
        row = dict(row)
        row["embed"] = embed_url(row["url"])
        row["host"] = host_of(row["url"])
        row["can_delete"] = can_delete
        return row

    SELECT_MATERIAL = """SELECT m.id, m.course_id, m.title, m.url, m.description,
                                to_char(m.created_at, 'YYYY-MM-DD') AS created_at, u.full_name AS author
                         FROM course_materials m LEFT JOIN users u ON u.id = m.created_by"""

    @app.get("/api/materials")
    def list_materials():
        """Materials grouped by course, only for the courses of the signed-in user."""
        user, error = guard()
        if error:
            return error
        role = user["role"]
        if role == "student":
            courses = rows("""SELECT c.id, c.code, c.title, t.full_name AS teacher
                              FROM enrollments e JOIN courses c ON c.id = e.course_id
                              JOIN users t ON t.id = c.teacher_id
                              WHERE e.user_id = %s ORDER BY c.title""", (user["id"],))
        elif role == "teacher":
            courses = rows("""SELECT c.id, c.code, c.title, t.full_name AS teacher
                              FROM course_teachers ct JOIN courses c ON c.id = ct.course_id
                              JOIN users t ON t.id = c.teacher_id
                              WHERE ct.teacher_id = %s ORDER BY c.title""", (user["id"],))
        else:
            courses = rows("""SELECT c.id, c.code, c.title, t.full_name AS teacher
                              FROM courses c JOIN users t ON t.id = c.teacher_id ORDER BY c.title""")
        ids = [c["id"] for c in courses]
        by_course = {i: [] for i in ids}
        if ids:
            for row in rows(SELECT_MATERIAL + " WHERE m.course_id = ANY(%s) ORDER BY m.id DESC", (ids,)):
                by_course[row["course_id"]].append(shape(row, role in ("teacher", "admin")))
        for course in courses:
            course["materials"] = by_course[course["id"]]
        return jsonify(can_edit=role in ("teacher", "admin"), courses=courses)

    @app.post("/api/materials")
    def add_material():
        """Teacher (own course) or admin (any course) attaches a link. Body: course_id, title, url, description."""
        user, error = guard()
        if error:
            return error
        if user["role"] not in ("teacher", "admin"):
            raise Refuse(403, "Only teachers and the registrar office can add materials")
        data = request.get_json(silent=True) or {}
        try:
            course_id = int(data.get("course_id"))
        except (TypeError, ValueError):
            raise Refuse(400, "Choose a course")
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        url = clean_url(data.get("url"))
        if not 1 <= len(title) <= TITLE_MAX:
            raise Refuse(400, f"Title must be 1-{TITLE_MAX} characters")
        if len(description) > DESCRIPTION_MAX:
            raise Refuse(400, f"Description is too long (max {DESCRIPTION_MAX} characters)")
        if not url:
            raise Refuse(400, "Enter a full web link starting with http:// or https://")
        if user["role"] == "teacher":
            if not teaches(user, course_id):
                raise Refuse(403, "You can add materials only to your own courses")
        elif not query_one("SELECT 1 AS found FROM courses WHERE id = %s", (course_id,)):
            raise Refuse(404, "Course not found")
        if query_one("SELECT 1 AS found FROM course_materials WHERE course_id = %s AND url = %s", (course_id, url)):
            raise Refuse(409, "This link is already attached to the course")

        conn = db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO course_materials(course_id, title, url, description, created_by)
                           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                        (course_id, title, url, description, user["id"]))
            new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify(shape(query_one(SELECT_MATERIAL + " WHERE m.id = %s", (new_id,)), True)), 201

    @app.delete("/api/materials/<int:material_id>")
    def delete_material(material_id):
        user, error = guard()
        if error:
            return error
        if user["role"] not in ("teacher", "admin"):
            raise Refuse(403, "Only teachers and the registrar office can remove materials")
        found = query_one("SELECT id, course_id FROM course_materials WHERE id = %s", (material_id,))
        if not found or (user["role"] == "teacher" and not teaches(user, found["course_id"])):
            raise Refuse(404, "Material not found in your courses")
        conn = db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM course_materials WHERE id = %s", (material_id,))
        conn.commit()
        return jsonify(ok=True)
