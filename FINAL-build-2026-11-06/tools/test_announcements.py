from tools.test_recovery import RecoveryTests
import app_postgresql as appmod

class AnnouncementApiTests(RecoveryTests):
    def test_non_admin_roles_cannot_create_update_or_delete(self):
        self.store.users[3] = {"id":3,"email":"teacher@astanait.edu.kz","full_name":"Teacher","role":"teacher","password_hash":"x"}
        appmod.SESSIONS.update({"student-token":(2,9999999999),"teacher-token":(3,9999999999)})
        body={"title":"Notice","body":"Hello","category":"General","audience":"everyone","pinned":False}
        for role_token in ("student-token","teacher-token"):
            headers={"Authorization":role_token}
            self.assertEqual(self.client.post("/api/announcements",json=body,headers=headers).status_code,403)
            self.assertEqual(self.client.put("/api/announcements/1",json=body,headers=headers).status_code,403)
            self.assertEqual(self.client.patch("/api/announcements/1",json=body,headers=headers).status_code,403)
            self.assertEqual(self.client.delete("/api/announcements/1",headers=headers).status_code,403)


    def test_student_feed_is_scoped_to_enrollment_and_hides_expired(self):
        appmod.SESSIONS["student-token"]=(2,9999999999)
        seen={}
        def fake_rows(sql,args=()):
            seen["sql"],seen["args"]=sql,args
            return []
        appmod.rows=fake_rows
        response=self.client.get("/api/announcements?q=exam&category=Exams&offset=10",headers={"Authorization":"student-token"})
        self.assertEqual(response.status_code,200)
        self.assertIn("FROM enrollments",seen["sql"])
        self.assertIn("a.expires_at IS NULL",seen["sql"])
        self.assertIn("ORDER BY a.pinned DESC",seen["sql"])
        self.assertIn("%exam%",seen["args"])

    def test_student_teacher_and_admin_can_read_their_feed(self):
        self.store.users[3] = {"id":3,"email":"teacher@astanait.edu.kz","full_name":"Teacher","role":"teacher","password_hash":"x"}
        appmod.SESSIONS.update({"student-token":(2,9999999999),"teacher-token":(3,9999999999),"admin-token":(200,9999999999)})
        appmod.rows=lambda sql,args=(): []
        for access in ("student-token","teacher-token","admin-token"):
            response=self.client.get("/api/announcements?offset=0&limit=10",headers={"Authorization":access})
            self.assertEqual(response.status_code,200)
            self.assertIsInstance(response.json,list)

    def test_unknown_api_paths_are_404_and_cors_preflight_still_works(self):
        self.assertEqual(self.client.get("/api/xyz").status_code,404)
        self.assertEqual(self.client.post("/api/xyz").status_code,404)
        missing_options=self.client.options("/api/xyz",headers={"Origin":"http://localhost:5500","Access-Control-Request-Method":"GET"})
        self.assertEqual(missing_options.status_code,404)
        preflight=self.client.options("/api/announcements",headers={"Origin":"http://localhost:5500","Access-Control-Request-Method":"GET"})
        self.assertEqual(preflight.status_code,200)
        self.assertIn("GET",preflight.headers["Access-Control-Allow-Methods"])
        self.assertEqual(preflight.headers["Access-Control-Allow-Origin"],"http://localhost:5500")

    def test_announcements_require_login(self):
        self.assertEqual(self.client.get("/api/announcements").status_code,401)
        self.assertEqual(self.client.get("/api/announcements/").status_code,401)
        self.assertEqual(self.client.get("/api/announcements/unread-count").status_code,401)
        body={"title":"Notice","body":"Hello","category":"General","audience":"everyone"}
        self.assertEqual(self.client.post("/api/announcements",json=body).status_code,401)
        self.assertEqual(self.client.put("/api/announcements/1",json=body).status_code,401)
        self.assertEqual(self.client.delete("/api/announcements/1").status_code,401)

    def test_admin_create_edit_delete_routes(self):
        appmod.SESSIONS["admin-token"]=(200,9999999999)
        calls=[]
        def execute(sql,args=(),returning=False):
            calls.append((sql,args))
            return {"id":7} if returning else None
        appmod.execute=execute
        body={"title":"Exam dates","body":"See https://example.edu/schedule","category":"Exams","audience":"everyone","pinned":True,"expires_at":None}
        headers={"Authorization":"admin-token"}
        self.assertEqual(self.client.post("/api/announcements",json=body,headers=headers).status_code,201)
        self.assertEqual(self.client.put("/api/announcements/7",json=body,headers=headers).status_code,200)
        self.assertEqual(self.client.patch("/api/announcements/7",json=body,headers=headers).status_code,200)
        self.assertEqual(self.client.post("/api/announcements/7",json=body,headers=headers).status_code,200)
        self.assertEqual(self.client.delete("/api/announcements/7",headers=headers).status_code,200)
        self.assertEqual(len(calls),5)
        self.assertTrue(any("updated_at=CURRENT_TIMESTAMP" in sql for sql,_ in calls))

if __name__ == "__main__":
    import unittest
    unittest.main()
