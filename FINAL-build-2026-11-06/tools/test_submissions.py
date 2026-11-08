"""Unit tests for submissions.py (no PostgreSQL needed). Run from the project folder:
    python -m unittest tools.test_submissions -v
They check who may submit, link validation, enrolment, and that a graded task is closed."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app_postgresql as appmod  # noqa: E402

USERS = {
    2: {"id": 2, "email": "s@astanait.edu.kz", "full_name": "Student", "role": "student"},
    3: {"id": 3, "email": "t@astanait.edu.kz", "full_name": "Teacher", "role": "teacher"},
    5: {"id": 5, "email": "a@astanait.edu.kz", "full_name": "Admin", "role": "admin"},
}


class FakeCursor:
    rowcount = 1
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, args=()):
        sql = " ".join(sql.split())
        self.conn.log.append((sql, args))
        self.rowcount = 1 if (sql.startswith("UPDATE") and self.conn.has_submission) else 0 if sql.startswith("UPDATE") else 1


class FakeConn:
    def __init__(self): self.log, self.has_submission = [], False
    def cursor(self, **k): return FakeCursor(self)
    def commit(self): pass


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn()
        self.old = (appmod.db, appmod.query_one)
        self.enrolled, self.graded, self.submitted = True, False, False
        appmod.db = lambda: self.conn
        appmod.query_one = self.query
        for key, uid in (("s", 2), ("t", 3), ("a", 5)):
            appmod.SESSIONS[key] = (uid, 9999999999)
        self.client = appmod.app.test_client()

    def tearDown(self):
        appmod.db, appmod.query_one = self.old
        for key in ("s", "t", "a"):
            appmod.SESSIONS.pop(key, None)

    def query(self, sql, args=()):
        sql = " ".join(sql.split())
        if "FROM users WHERE id" in sql: return dict(USERS[args[0]])
        if "FROM assignments a JOIN enrollments" in sql: return {"id": 7} if self.enrolled else None
        if "FROM grades WHERE" in sql: return {"found": 1} if self.graded else None
        if "FROM submissions WHERE" in sql: return {"found": 1} if self.submitted else None
        if "FROM submissions s JOIN assignments" in sql:
            return {"url": "https://drive.google.com/x", "comment": "", "submitted_at": "2026-11-06 10:00", "late": False}
        return None

    def put(self, token="s", **body):
        payload = {"assignment_id": 7, "url": "https://drive.google.com/file/d/abc/view", "comment": " done "}
        payload.update(body)
        return self.client.put("/api/submissions", json=payload, headers={"Authorization": token})

    def writes(self):
        return [s for s, _ in self.conn.log if s.startswith(("INSERT", "UPDATE", "DELETE"))]

    def test_only_students_submit(self):
        self.assertEqual(self.put("t").status_code, 403)
        self.assertEqual(self.put("a").status_code, 403)
        self.assertEqual(self.client.delete("/api/submissions/7", headers={"Authorization": "t"}).status_code, 403)
        self.assertEqual(self.client.put("/api/submissions", json={}).status_code, 401)
        self.assertEqual(self.writes(), [])

    def test_link_must_be_a_plain_web_address(self):
        for bad in ("javascript:alert(1)", "file:///c:/x", "drive.google.com/x", "", None, "https://a.com/ x"):
            self.assertEqual(self.put(url=bad).status_code, 400, bad)
        self.assertEqual(self.put(comment="x" * 501).status_code, 400)
        self.assertEqual(self.put(assignment_id="abc").status_code, 400)
        self.assertEqual(self.writes(), [])

    def test_student_must_be_enrolled_in_the_course(self):
        self.enrolled = False
        self.assertEqual(self.put().status_code, 404)
        self.assertEqual(self.writes(), [])

    def test_first_submission_is_inserted_with_trimmed_comment(self):
        response = self.put()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertTrue(self.writes()[0].startswith("UPDATE submissions"))
        self.assertTrue(self.writes()[1].startswith("INSERT INTO submissions"))
        self.assertEqual(self.conn.log[1][1][3], "done")

    def test_resubmitting_replaces_the_link_instead_of_adding_a_row(self):
        self.conn.has_submission = True
        self.assertEqual(self.put().status_code, 200)
        self.assertEqual(len(self.writes()), 1)
        self.assertTrue(self.writes()[0].startswith("UPDATE submissions"))

    def test_graded_task_is_closed_for_changes_and_withdrawal(self):
        self.graded = True
        self.assertEqual(self.put().status_code, 409)
        self.assertEqual(self.client.delete("/api/submissions/7", headers={"Authorization": "s"}).status_code, 409)
        self.assertEqual(self.writes(), [])

    def test_withdraw(self):
        self.assertEqual(self.client.delete("/api/submissions/7", headers={"Authorization": "s"}).status_code, 404)
        self.submitted = True
        self.assertEqual(self.client.delete("/api/submissions/7", headers={"Authorization": "s"}).status_code, 200)
        self.assertTrue(self.writes()[0].startswith("DELETE FROM submissions"))


if __name__ == "__main__":
    unittest.main()
