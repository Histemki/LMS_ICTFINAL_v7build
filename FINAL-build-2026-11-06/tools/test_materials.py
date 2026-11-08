"""Unit tests for materials.py (no PostgreSQL needed). Run from the project folder:
    python -m unittest tools.test_materials -v
They check link validation, the embed builder and who may add / remove / see materials."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app_postgresql as appmod  # noqa: E402
import materials  # noqa: E402

USERS = {
    2: {"id": 2, "email": "s@astanait.edu.kz", "full_name": "Student", "role": "student"},
    3: {"id": 3, "email": "t@astanait.edu.kz", "full_name": "Teacher", "role": "teacher"},
    4: {"id": 4, "email": "o@astanait.edu.kz", "full_name": "Other", "role": "teacher"},
    5: {"id": 5, "email": "a@astanait.edu.kz", "full_name": "Admin", "role": "admin"},
}


class FakeCursor:
    def __init__(self, log): self.log = log
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, args=()): self.log.append((" ".join(sql.split()), args))
    def fetchone(self): return (11,)


class FakeConn:
    def __init__(self): self.log = []
    def cursor(self, **k): return FakeCursor(self.log)
    def commit(self): pass


class LinkTests(unittest.TestCase):
    def test_only_plain_http_links_are_accepted(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://x.com/a", "//x.com", "https://", "https://localhost",
                    "https://a.com/ b", "https://a.com/<script>", "https://user:pw@a.com/", "", None, "x" * 600):
            self.assertIsNone(materials.clean_url(bad), bad)
        self.assertEqual(materials.clean_url("  https://example.com/a?b=1  "), "https://example.com/a?b=1")

    def test_youtube_and_vimeo_links_become_embeds_built_from_the_id(self):
        yt = "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
        for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5", "https://youtu.be/dQw4w9WgXcQ?si=x",
                    "https://youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/embed/dQw4w9WgXcQ"):
            self.assertEqual(materials.embed_url(url), yt, url)
        self.assertEqual(materials.embed_url("https://vimeo.com/76979871"), "https://player.vimeo.com/video/76979871")
        for url in ("https://example.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=short",
                    "https://www.youtube.com/watch?v=\"onload=x", "https://evil.com/?u=youtube.com/watch?v=dQw4w9WgXcQ"):
            self.assertIsNone(materials.embed_url(url), url)

    def test_host_hides_www(self):
        self.assertEqual(materials.host_of("https://www.example.com/x"), "example.com")


class MaterialApiTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn()
        self.old = (appmod.db, appmod.query_one, appmod.rows)
        self.duplicate, self.listed = False, []
        appmod.db = lambda: self.conn
        appmod.query_one = self.query
        appmod.rows = self.rows
        for key, uid in (("s", 2), ("t", 3), ("o", 4), ("a", 5)):
            appmod.SESSIONS[key] = (uid, 9999999999)
        self.client = appmod.app.test_client()

    def tearDown(self):
        appmod.db, appmod.query_one, appmod.rows = self.old
        for key in ("s", "t", "o", "a"):
            appmod.SESSIONS.pop(key, None)

    def query(self, sql, args=()):
        sql = " ".join(sql.split())
        if "FROM users WHERE id" in sql: return dict(USERS[args[0]])
        if "FROM course_teachers" in sql: return {"found": 1} if args[1] == 3 else None
        if "FROM courses WHERE id" in sql: return {"found": 1}
        if "FROM course_materials WHERE course_id" in sql: return {"found": 1} if self.duplicate else None
        if "FROM course_materials WHERE id" in sql: return {"id": args[0], "course_id": 5}
        if "WHERE m.id" in sql: return {"id": 11, "course_id": 5, "title": "T", "url": "https://youtu.be/dQw4w9WgXcQ",
                                        "description": "", "created_at": "2026-11-06", "author": "Teacher"}
        return None

    def rows(self, sql, args=()):
        self.listed.append((" ".join(sql.split()), args))
        if "FROM enrollments" in sql or "FROM course_teachers" in sql or "FROM courses c" in sql:
            return [{"id": 5, "code": "CSE105", "title": "English", "teacher": "Teacher"}]
        return [{"id": 1, "course_id": 5, "title": "Intro", "url": "https://youtu.be/dQw4w9WgXcQ", "description": "",
                 "created_at": "2026-11-06", "author": "Teacher"}]

    def post(self, token="t", **body):
        payload = {"course_id": 5, "title": "Lecture 1", "url": "https://youtu.be/dQw4w9WgXcQ", "description": ""}
        payload.update(body)
        return self.client.post("/api/materials", json=payload, headers={"Authorization": token})

    def writes(self):
        return [s for s, _ in self.conn.log if s.startswith(("INSERT", "DELETE"))]

    def test_student_cannot_add_or_remove(self):
        self.assertEqual(self.post("s").status_code, 403)
        self.assertEqual(self.client.delete("/api/materials/1", headers={"Authorization": "s"}).status_code, 403)
        self.assertEqual(self.client.post("/api/materials", json={}).status_code, 401)
        self.assertEqual(self.writes(), [])

    def test_teacher_only_for_own_course_admin_for_any(self):
        self.assertEqual(self.post("o").status_code, 403)
        self.assertEqual(self.post("t").status_code, 201)
        self.assertEqual(self.post("a").status_code, 201)
        self.assertEqual(len(self.writes()), 2)

    def test_bad_input_is_refused(self):
        for body in ({"url": "javascript:alert(1)"}, {"url": ""}, {"title": ""}, {"title": "x" * 121},
                     {"description": "x" * 501}, {"course_id": "abc"}):
            self.assertEqual(self.post(**body).status_code, 400, body)
        self.assertEqual(self.writes(), [])

    def test_same_link_twice_in_a_course_is_refused(self):
        self.duplicate = True
        self.assertEqual(self.post().status_code, 409)

    def test_created_material_carries_embed_and_host(self):
        body = self.post().json
        self.assertEqual(body["embed"], "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ")
        self.assertEqual(body["host"], "youtu.be")
        self.assertTrue(body["can_delete"])

    def test_delete_rules(self):
        self.assertEqual(self.client.delete("/api/materials/1", headers={"Authorization": "o"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/materials/1", headers={"Authorization": "t"}).status_code, 200)
        self.assertEqual(self.client.delete("/api/materials/1", headers={"Authorization": "a"}).status_code, 200)

    def test_listing_is_scoped_by_role(self):
        student = self.client.get("/api/materials", headers={"Authorization": "s"}).json
        self.assertFalse(student["can_edit"])
        self.assertFalse(student["courses"][0]["materials"][0]["can_delete"])
        self.assertIn("FROM enrollments", self.listed[0][0])
        teacher = self.client.get("/api/materials", headers={"Authorization": "t"}).json
        self.assertTrue(teacher["can_edit"])
        self.assertIn("FROM course_teachers", self.listed[2][0])
        self.assertTrue(self.client.get("/api/materials", headers={"Authorization": "a"}).json["can_edit"])


if __name__ == "__main__":
    unittest.main()
