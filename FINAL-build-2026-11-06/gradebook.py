"""Gradebook: teachers give and change grades, students see them with dates and changes.

Kept in its own file, like checkin.py. app_postgresql.py only does three small things:
`import gradebook`, `gradebook.init_schema(cur)` inside init_db() and `gradebook.register(...)`
before `__main__`. The old GET /api/grades (sum per course) is not touched.

Tables: grades gets graded_at / graded_by / comment; grade_history keeps every change
(old score -> new score, who, when), so a student can see "changed, was 7/10".
"""
import math

from flask import jsonify, request

SCHEMA = """
ALTER TABLE grades ADD COLUMN IF NOT EXISTS graded_at TIMESTAMP;
ALTER TABLE grades ADD COLUMN IF NOT EXISTS graded_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE grades ADD COLUMN IF NOT EXISTS comment TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS grade_history (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    old_score NUMERIC(7,2),              -- NULL = the grade was given for the first time
    new_score NUMERIC(7,2),              -- NULL = the grade was removed
    max_score NUMERIC(7,2) NOT NULL,
    changed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    changed_at TIMESTAMP NOT NULL DEFAULT localtimestamp,
    comment TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS grade_history_lookup ON grade_history(user_id, assignment_id, id);
"""

COMMENT_MAX = 300


def init_schema(cur):
    cur.execute(SCHEMA)
    # grades that existed before this feature: "graded" at the task's deadline (never in the future)
    cur.execute("""UPDATE grades g
                   SET graded_at = LEAST(a.due_date + a.due_time, localtimestamp)
                   FROM assignments a WHERE a.id = g.assignment_id AND g.graded_at IS NULL""")
    cur.execute("ALTER TABLE grades ALTER COLUMN graded_at SET DEFAULT localtimestamp")
    cur.execute("ALTER TABLE grades ALTER COLUMN graded_at SET NOT NULL")
    # one grade per student and task; if an old database already has duplicates, skip the index
    cur.execute("SAVEPOINT grades_unique")
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_grade_per_task ON grades(user_id, assignment_id)")
        cur.execute("RELEASE SAVEPOINT grades_unique")
    except Exception as err:  # psycopg2.errors.UniqueViolation
        cur.execute("ROLLBACK TO SAVEPOINT grades_unique")
        print("gradebook: duplicate grades exist, unique index skipped:", str(err).splitlines()[0])


def register(app, *, db, query_one, rows, guard, teacher_guard, Refuse):
    STAMP = "to_char(g.graded_at, 'YYYY-MM-DD HH24:MI')"

    def owns_course(teacher, course_id):
        return query_one("SELECT 1 AS found FROM course_teachers WHERE course_id = %s AND teacher_id = %s",
                         (course_id, teacher["id"])) is not None

    def own_assignment(teacher, assignment_id):
        """The task (with its course) if the teacher teaches that course, else a 404."""
        task = query_one("SELECT id, course_id, max_score::float8 AS max_score FROM assignments WHERE id = %s",
                         (assignment_id,))
        if not task or not owns_course(teacher, task["course_id"]):
            raise Refuse(404, "Task not found in your courses")
        return task

    # ---------------------------------------------------------------- teacher
    @app.get("/api/courses/<int:course_id>/grades")
    def course_gradebook(course_id):
        """Teacher: every student of the course, every task and the grades already given."""
        teacher, error = teacher_guard()
        if error:
            return error
        if not owns_course(teacher, course_id):
            raise Refuse(403, "This is not your course")
        return jsonify(
            assignments=rows("""
                SELECT a.id, a.title, t.name AS type, a.control_form, a.max_score::float8 AS max_score,
                       a.due_date::text AS due_date
                FROM assignments a JOIN assignment_types t ON t.id = a.type_id
                WHERE a.course_id = %s ORDER BY a.due_date, a.id""", (course_id,)),
            students=rows("""
                SELECT u.id, u.full_name AS name, u.email
                FROM enrollments e JOIN users u ON u.id = e.user_id
                WHERE e.course_id = %s AND u.role = 'student' ORDER BY u.id""", (course_id,)),
            grades=rows(f"""
                SELECT g.id, g.user_id, g.assignment_id, g.score::float8 AS score, g.comment,
                       {STAMP} AS graded_at,
                       EXISTS (SELECT 1 FROM grade_history h WHERE h.user_id = g.user_id
                               AND h.assignment_id = g.assignment_id AND h.old_score IS NOT NULL) AS changed
                FROM grades g WHERE g.course_id = %s""", (course_id,)),
            # links to the students' work (see submissions.py), shown next to the score field
            submissions=rows("""
                SELECT s.assignment_id, s.user_id, s.url, s.comment,
                       to_char(s.submitted_at, 'YYYY-MM-DD HH24:MI') AS submitted_at,
                       (s.submitted_at > a.due_date + a.due_time) AS late
                FROM submissions s JOIN assignments a ON a.id = s.assignment_id
                WHERE a.course_id = %s""", (course_id,)))

    @app.put("/api/grades")
    def put_grade():
        """Teacher: give or change one grade. Body: assignment_id, user_id, score, comment (optional)."""
        teacher, error = teacher_guard()
        if error:
            return error
        data = request.get_json(silent=True) or {}
        try:
            assignment_id = int(data.get("assignment_id"))
            user_id = int(data.get("user_id"))
            score = float(data.get("score"))
        except (TypeError, ValueError):
            raise Refuse(400, "Send assignment_id, user_id and a numeric score")
        comment = str(data.get("comment") or "").strip()
        if len(comment) > COMMENT_MAX:
            raise Refuse(400, f"Comment is too long (max {COMMENT_MAX} characters)")
        task = own_assignment(teacher, assignment_id)
        if not math.isfinite(score) or not 0 <= score <= task["max_score"]:
            raise Refuse(400, f"Score must be between 0 and {task['max_score']:g}")
        score = round(score, 2)
        if not query_one("""SELECT 1 AS found FROM enrollments e JOIN users u ON u.id = e.user_id
                            WHERE e.user_id = %s AND e.course_id = %s AND u.role = 'student'""",
                         (user_id, task["course_id"])):
            raise Refuse(400, "This student is not enrolled in the course")

        old = query_one("SELECT id, score::float8 AS score, comment FROM grades WHERE user_id = %s AND assignment_id = %s",
                        (user_id, assignment_id))
        conn = db()
        with conn.cursor() as cur:
            if old and old["score"] == score and old["comment"] == comment:
                changed = False
            elif old:
                cur.execute("""UPDATE grades SET score = %s, max_score = %s, comment = %s,
                                      graded_by = %s, graded_at = localtimestamp WHERE id = %s""",
                            (score, task["max_score"], comment, teacher["id"], old["id"]))
                changed = True
            else:
                cur.execute("""INSERT INTO grades(user_id, course_id, assignment_id, score, max_score, comment, graded_by)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                            (user_id, task["course_id"], assignment_id, score, task["max_score"], comment, teacher["id"]))
                changed = True
            if changed:
                cur.execute("""INSERT INTO grade_history(user_id, course_id, assignment_id, old_score, new_score,
                                                         max_score, changed_by, comment)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (user_id, task["course_id"], assignment_id, old["score"] if old else None, score,
                             task["max_score"], teacher["id"], comment))
        conn.commit()
        saved = query_one(f"""SELECT g.id, g.score::float8 AS score, g.max_score::float8 AS max_score, g.comment,
                                     {STAMP} AS graded_at FROM grades g
                              WHERE g.user_id = %s AND g.assignment_id = %s""", (user_id, assignment_id))
        return jsonify(ok=True, changed=changed, grade=saved)

    @app.delete("/api/grades/<int:grade_id>")
    def delete_grade(grade_id):
        """Teacher: take a grade back. The removal stays in the history."""
        teacher, error = teacher_guard()
        if error:
            return error
        grade = query_one("SELECT id, user_id, course_id, assignment_id, score::float8 AS score, max_score::float8 AS max_score "
                          "FROM grades WHERE id = %s", (grade_id,))
        if not grade or not owns_course(teacher, grade["course_id"]):
            raise Refuse(404, "Grade not found in your courses")
        conn = db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO grade_history(user_id, course_id, assignment_id, old_score, new_score,
                                                     max_score, changed_by)
                           VALUES (%s,%s,%s,%s,NULL,%s,%s)""",
                        (grade["user_id"], grade["course_id"], grade["assignment_id"], grade["score"],
                         grade["max_score"], teacher["id"]))
            cur.execute("DELETE FROM grades WHERE id = %s", (grade_id,))
        conn.commit()
        return jsonify(ok=True)

    # ---------------------------------------------------------------- student
    @app.get("/api/grades/details")
    def grade_details():
        """Student: grades per course with every task, plus the latest events (new / changed grades)."""
        user, error = guard()
        if error:
            return error
        if user["role"] != "student":
            raise Refuse(403, "Only students have a gradebook")
        items = rows(f"""
            SELECT g.id, c.id AS course_id, c.title AS course, a.title AS assignment, t.name AS type,
                   a.control_form, g.score::float8 AS score, g.max_score::float8 AS max_score,
                   {STAMP} AS graded_at, g.comment, u.full_name AS graded_by,
                   (SELECT h.old_score::float8 FROM grade_history h
                    WHERE h.user_id = g.user_id AND h.assignment_id = g.assignment_id
                    ORDER BY h.id DESC LIMIT 1) AS previous_score
            FROM grades g
            JOIN assignments a ON a.id = g.assignment_id
            JOIN assignment_types t ON t.id = a.type_id
            JOIN courses c ON c.id = g.course_id
            LEFT JOIN users u ON u.id = g.graded_by
            WHERE g.user_id = %s
            ORDER BY g.graded_at DESC, g.id DESC""", (user["id"],))
        courses = {c["id"]: {"course_id": c["id"], "course": c["title"], "score": 0.0, "max_score": 0.0,
                             "percent": None, "items": []}
                   for c in rows("""SELECT c.id, c.title FROM enrollments e JOIN courses c ON c.id = e.course_id
                                    WHERE e.user_id = %s ORDER BY c.title""", (user["id"],))}
        for item in items:
            course = courses.setdefault(item["course_id"], {"course_id": item["course_id"], "course": item["course"],
                                                            "score": 0.0, "max_score": 0.0, "percent": None, "items": []})
            course["score"] += item["score"]
            course["max_score"] += item["max_score"]
            course["items"].append(item)
        for course in courses.values():
            course["score"] = round(course["score"], 2)
            course["max_score"] = round(course["max_score"], 2)
            if course["max_score"]:
                course["percent"] = round(100 * course["score"] / course["max_score"], 1)
        total = sum(i["max_score"] for i in items)
        return jsonify(
            average=round(100 * sum(i["score"] for i in items) / total, 1) if total else None,
            graded=len(items),
            courses=list(courses.values()),
            recent=[dict(i, course=i["course"]) for i in items[:15]])
