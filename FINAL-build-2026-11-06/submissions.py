"""Student work submitted as a LINK (Google Drive, OneDrive, GitHub ...). No files are uploaded.

A student can send or replace the link until the task is graded; after the deadline the work is still accepted
but marked late. The teacher opens the link from the gradebook (see gradebook.py, which lists the submissions).
Own file like checkin.py / gradebook.py / materials.py: app_postgresql.py imports it, calls init_schema(cur)
from init_db() and register(...) before `__main__`.
"""
from flask import jsonify, request

from materials import clean_url  # same link rules as course materials: http(s) only, no scripts

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id SERIAL PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    url VARCHAR(500) NOT NULL,
    comment VARCHAR(500) NOT NULL DEFAULT '',
    submitted_at TIMESTAMP NOT NULL DEFAULT localtimestamp,
    updated_at TIMESTAMP NOT NULL DEFAULT localtimestamp,
    UNIQUE (assignment_id, user_id)
);
"""

COMMENT_MAX = 500


def init_schema(cur):
    cur.execute(SCHEMA)


def register(app, *, db, query_one, guard, Refuse):
    def student_task(user, assignment_id):
        """The task if the student is enrolled in its course, else 404 (never reveals other courses)."""
        task = query_one("""SELECT a.id FROM assignments a
                            JOIN enrollments e ON e.course_id = a.course_id AND e.user_id = %s
                            WHERE a.id = %s""", (user["id"], assignment_id))
        if not task:
            raise Refuse(404, "Task not found in your courses")

    def student_only():
        user, error = guard()
        if error:
            return None, error
        if user["role"] != "student":
            raise Refuse(403, "Only students submit work")
        return user, None

    @app.put("/api/submissions")
    def put_submission():
        """Student sends or replaces the link to the work. Body: assignment_id, url, comment (optional)."""
        user, error = student_only()
        if error:
            return error
        data = request.get_json(silent=True) or {}
        try:
            assignment_id = int(data.get("assignment_id"))
        except (TypeError, ValueError):
            raise Refuse(400, "Choose a task")
        url = clean_url(data.get("url"))
        comment = str(data.get("comment") or "").strip()
        if not url:
            raise Refuse(400, "Enter a full web link starting with http:// or https://")
        if len(comment) > COMMENT_MAX:
            raise Refuse(400, f"Comment is too long (max {COMMENT_MAX} characters)")
        student_task(user, assignment_id)
        if query_one("SELECT 1 AS found FROM grades WHERE user_id = %s AND assignment_id = %s",
                     (user["id"], assignment_id)):
            raise Refuse(409, "This task is already graded, the work can no longer be changed")

        conn = db()
        with conn.cursor() as cur:
            cur.execute("""UPDATE submissions SET url = %s, comment = %s, updated_at = localtimestamp
                           WHERE user_id = %s AND assignment_id = %s""",
                        (url, comment, user["id"], assignment_id))
            if not cur.rowcount:
                cur.execute("INSERT INTO submissions(assignment_id, user_id, url, comment) VALUES (%s,%s,%s,%s)",
                            (assignment_id, user["id"], url, comment))
        conn.commit()
        saved = query_one("""SELECT s.url, s.comment, to_char(s.submitted_at, 'YYYY-MM-DD HH24:MI') AS submitted_at,
                                    (s.submitted_at > a.due_date + a.due_time) AS late
                             FROM submissions s JOIN assignments a ON a.id = s.assignment_id
                             WHERE s.user_id = %s AND s.assignment_id = %s""", (user["id"], assignment_id))
        return jsonify(ok=True, submission=saved)

    @app.delete("/api/submissions/<int:assignment_id>")
    def withdraw_submission(assignment_id):
        """Student takes the work back (only while the task is not graded)."""
        user, error = student_only()
        if error:
            return error
        if query_one("SELECT 1 AS found FROM grades WHERE user_id = %s AND assignment_id = %s",
                     (user["id"], assignment_id)):
            raise Refuse(409, "This task is already graded, the work can no longer be changed")
        if not query_one("SELECT 1 AS found FROM submissions WHERE user_id = %s AND assignment_id = %s",
                         (user["id"], assignment_id)):
            raise Refuse(404, "Nothing was submitted for this task")
        conn = db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM submissions WHERE user_id = %s AND assignment_id = %s", (user["id"], assignment_id))
        conn.commit()
        return jsonify(ok=True)
