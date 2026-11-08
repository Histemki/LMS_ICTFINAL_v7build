"""Unit tests for gradebook.py (no PostgreSQL needed: db helpers are replaced by small fakes).
Run from the project folder:  python -m unittest tools.test_grades -v
They check the rules that matter: who may grade, score limits, enrolment, history on change, no-op on same score."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app_postgresql as appmod  # noqa: E402

TEACHER = {"id": 3, "email": "teacher@astanait.edu.kz", "full_name": "Teacher", "role": "teacher"}
OTHER_TEACHER = {"id": 4, "email": "other@astanait.edu.kz", "full_name": "Other", "role": "teacher"}
STUDENT = {"id": 2, "email": "260002@astanait.edu.kz", "full_name": "Student", "role": "student"}
USERS = {2: STUDENT, 3: TEACHER, 4: OTHER_TEACHER}
TASK = {"id": 7, "course_id": 5, "max_score": 10.0}


class FakeCursor:
    def __init__(self, log): self.log = log
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, args=()): self.log.append((" ".join(sql.split()), args))


class FakeConn:
    def __init__(self): self.log, self.commits = [], 0
    def cursor(self, **k): return FakeCursor(self.log)
    def commit(self): self.commits += 1


class GradebookTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn()
        self.old = (appmod.db, appmod.query_one, appmod.rows)
        self.enrolled, self.old_grade = True, None
        appmod.db = lambda: self.conn
        appmod.query_one = self.query
        appmod.rows = lambda *a, **k: []
        appmod.SESSIONS.update({"s": (2, 9999999999), "t": (3, 9999999999), "o": (4, 9999999999)})
        self.client = appmod.app.test_client()

    def tearDown(self):
        appmod.db, appmod.query_one, appmod.rows = self.old
        for key in ("s", "t", "o"):
            appmod.SESSIONS.pop(key, None)

    def query(self, sql, args=()):
        sql = " ".join(sql.split())
        if "FROM users WHERE id" in sql: return dict(USERS[args[0]])
        if "FROM course_teachers" in sql: return {"found": 1} if args[1] == 3 else None
        if "FROM assignments WHERE id" in sql: return dict(TASK) if args[0] == TASK["id"] else None
        if "FROM enrollments e JOIN users" in sql: return {"found": 1} if self.enrolled else None
        if "FROM grades WHERE user_id" in sql: return self.old_grade
        if "FROM grades g" in sql: return {"id": 1, "score": 8.0, "max_score": 10.0, "comment": "", "graded_at": "2026-11-06 10:00"}
        if "FROM grades WHERE id" in sql: return {"id": 1, "user_id": 2, "course_id": 5, "assignment_id": 7, "score": 8.0, "max_score": 10.0}
        return None

    def put(self, token="t", **body):
        payload = {"assignment_id": 7, "user_id": 2, "score": 8}
        payload.update(body)
        return self.client.put("/api/grades", json=payload, headers={"Authorization": token})

    def writes(self):
        return [s for s, _ in self.conn.log if s.startswith(("INSERT", "UPDATE", "DELETE"))]

    def test_only_teachers_can_change_grades(self):
        self.assertEqual(self.put("s").status_code, 403)
        self.assertEqual(self.client.delete("/api/grades/1", headers={"Authorization": "s"}).status_code, 403)
        self.assertEqual(self.client.get("/api/courses/5/grades", headers={"Authorization": "s"}).status_code, 403)
        self.assertEqual(self.client.put("/api/grades", json={}).status_code, 401)
        self.assertEqual(self.writes(), [])

    def test_teacher_of_another_course_is_refused(self):
        self.assertEqual(self.put("o").status_code, 404)
        self.assertEqual(self.client.get("/api/courses/5/grades", headers={"Authorization": "o"}).status_code, 403)
        self.assertEqual(self.writes(), [])

    def test_score_must_be_a_number_inside_the_limit(self):
        for bad in (-1, 10.5, "abc", "nan", "inf", None):
            self.assertEqual(self.put(score=bad).status_code, 400, bad)
        self.assertEqual(self.writes(), [])

    def test_student_must_be_enrolled(self):
        self.enrolled = False
        self.assertEqual(self.put().status_code, 400)
        self.assertEqual(self.writes(), [])

    def test_first_grade_is_saved_and_logged(self):
        response = self.put(score=8, comment="  Good  ")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["changed"])
        self.assertEqual(len(self.writes()), 2)
        self.assertTrue(self.writes()[0].startswith("INSERT INTO grades"))
        history_args = [a for s, a in self.conn.log if s.startswith("INSERT INTO grade_history")][0]
        self.assertIsNone(history_args[3])          # old score: none, first grade
        self.assertEqual(history_args[4], 8.0)      # new score
        self.assertEqual(history_args[7], "Good")   # comment is trimmed

    def test_changing_a_grade_keeps_the_old_score_in_history(self):
        self.old_grade = {"id": 1, "score": 6.0, "comment": ""}
        self.assertEqual(self.put(score=9).status_code, 200)
        self.assertTrue(self.writes()[0].startswith("UPDATE grades"))
        history_args = [a for s, a in self.conn.log if s.startswith("INSERT INTO grade_history")][0]
        self.assertEqual((history_args[3], history_args[4]), (6.0, 9.0))

    def test_saving_the_same_score_changes_nothing(self):
        self.old_grade = {"id": 1, "score": 8.0, "comment": ""}
        response = self.put(score=8)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["changed"])
        self.assertEqual(self.writes(), [])

    def test_removal_is_logged_before_delete(self):
        response = self.client.delete("/api/grades/1", headers={"Authorization": "t"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.writes()[0].startswith("INSERT INTO grade_history"))
        self.assertTrue(self.writes()[1].startswith("DELETE FROM grades"))

    def test_gradebook_for_students_only(self):
        self.assertEqual(self.client.get("/api/grades/details", headers={"Authorization": "t"}).status_code, 403)
        self.assertEqual(self.client.get("/api/grades/details", headers={"Authorization": "s"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
