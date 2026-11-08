"""Dependency-free API regression tests for the password recovery workflow."""
import re
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
import unittest
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

import app_postgresql as appmod


class FakeDatabase:
    def __init__(self):
        self.users = {
            2: {"id": 2, "email": "260002@astanait.edu.kz", "full_name": "Student One", "role": "student", "password_hash": appmod.hash_password("Oldpass123")},
            200: {"id": 200, "email": "admin@astanait.edu.kz", "full_name": "Registrar Office", "role": "admin", "password_hash": appmod.hash_password("Adminpass123")},
        }
        self.resets = []
        self.support = []

    def cursor(self, *args, **kwargs):
        return FakeCursor(self)

    def commit(self): pass
    def rollback(self): pass


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.result = None
        self.rowcount = 0
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, args=()):
        q = " ".join(sql.lower().split())
        if q.startswith("update password_resets set used_at"):
            uid = args[0]
            for row in self.store.resets:
                if row["user_id"] == uid and not row["used"]: row["used"] = True
            self.rowcount = 1
        elif q.startswith("insert into password_resets"):
            uid, token_hash = args
            self.store.resets.append({"id": len(self.store.resets)+1, "user_id": uid, "token_hash": token_hash, "used": False, "expired": False})
            self.rowcount = 1
        elif q.startswith("select id, user_id, used_at") and "password_resets" in q:
            row = next((r for r in self.store.resets if r["token_hash"] == args[0]), None)
            self.result = None if row is None else {"id":row["id"],"user_id":row["user_id"],"used":row["used"],"expired":row["expired"]}
        elif q.startswith("update users set password_hash"):
            self.store.users[args[1]]["password_hash"] = args[0]
        elif q.startswith("insert into support_requests"):
            self.store.support.append({"id":len(self.store.support)+1,"email":args[0],"message":args[1],"status":"new","created_at":"2026-11-01 12:00"})
            self.rowcount = 1
        elif q.startswith("update support_requests set status"):
            row=next((x for x in self.store.support if x["id"]==args[1]),None)
            if row: row["status"]=args[0];self.result={"id":row["id"]};self.rowcount=1
            else:self.result=None;self.rowcount=0
        else:
            raise AssertionError("Unhandled SQL in test fake: "+q)
    def fetchone(self): return self.result


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store=FakeDatabase()
        self.old_db=appmod.db;self.old_query=appmod.query_one;self.old_execute=appmod.execute;self.old_rows=appmod.rows
        appmod.db=lambda:self.store
        def query(sql,args=()):
            q=" ".join(sql.lower().split())
            if "from users where email" in q:
                return next((dict(u) for u in self.store.users.values() if u["email"]==args[0]),None)
            if "from users where id" in q:
                return dict(self.store.users.get(args[0])) if args[0] in self.store.users else None
            if "from password_resets" in q:
                r=next((x for x in self.store.resets if x["token_hash"]==args[0]),None)
                return None if not r else {"id":r["id"],"user_id":r["user_id"],"used":r["used"],"expired":r["expired"]}
            raise AssertionError("Unhandled query: "+q)
        appmod.query_one=query
        def execute(sql,args=(),returning=False):
            q=" ".join(sql.lower().split())
            if q.startswith("insert into support_requests"):
                self.store.support.append({"id":len(self.store.support)+1,"email":args[0],"message":args[1],"status":"new","created_at":"2026-11-01 12:00"})
                return {"id":self.store.support[-1]["id"]}
            if q.startswith("update support_requests"):
                row=next((x for x in self.store.support if x["id"]==args[1]),None)
                if row: row["status"]=args[0];return {"id":row["id"]}
                return None
            raise AssertionError("Unhandled execute: "+q)
        appmod.execute=execute
        appmod.rows=lambda *a,**k:[dict(x) for x in self.store.support]
        appmod.RECOVERY_LIMITS.clear();appmod.RECOVERY_OUTBOX.clear();appmod.SESSIONS.clear();appmod.ATTEMPTS.clear()
        self.client=appmod.app.test_client()
    def tearDown(self):
        appmod.db=self.old_db;appmod.query_one=self.old_query;appmod.execute=self.old_execute;appmod.rows=self.old_rows
        appmod.RECOVERY_LIMITS.clear();appmod.RECOVERY_OUTBOX.clear();appmod.SESSIONS.clear()
    def request_link(self,email="260002@astanait.edu.kz"):
        r=self.client.post("/api/recover/request",json={"email":email})
        self.assertEqual(r.status_code,200)
        return parse_qs(urlparse(appmod.RECOVERY_OUTBOX[0]["link"]).query)["recover_token"][0]
    def test_unknown_address_has_same_response_and_timing_floor(self):
        start=datetime.now().timestamp();known=self.client.post("/api/recover/request",json={"email":"260002@astanait.edu.kz"});known_time=datetime.now().timestamp()-start
        appmod.RECOVERY_LIMITS.clear();appmod.RECOVERY_OUTBOX.clear()
        start=datetime.now().timestamp();unknown=self.client.post("/api/recover/request",json={"email":"nobody@astanait.edu.kz"});unknown_time=datetime.now().timestamp()-start
        self.assertEqual(known.json,unknown.json);self.assertGreaterEqual(known_time,.34);self.assertGreaterEqual(unknown_time,.34)
        self.assertNotIn("nobody",str(unknown.json));self.assertEqual(len(self.store.resets),1)
    def test_password_reset_changes_password_revokes_sessions_and_is_one_time(self):
        token=self.request_link();self.assertEqual(self.client.get("/api/recover/check",query_string={"token":token}).status_code,200)
        appmod.SESSIONS["old-session"]=(2,9999999999)
        weak=self.client.post("/api/recover/reset",json={"token":token,"password":"weak","confirm_password":"weak"});self.assertEqual(weak.status_code,400)
        mismatch=self.client.post("/api/recover/reset",json={"token":token,"password":"Newpass123","confirm_password":"Otherpass123"});self.assertEqual(mismatch.status_code,400)
        changed=self.client.post("/api/recover/reset",json={"token":token,"password":"Newpass123","confirm_password":"Newpass123"});self.assertEqual(changed.status_code,200)
        self.assertTrue(appmod.check_password("Newpass123",self.store.users[2]["password_hash"]))
        self.assertNotIn("old-session",appmod.SESSIONS)
        self.assertEqual(self.client.get("/api/recover/check",query_string={"token":token}).status_code,409)
        old=self.client.post("/api/login",json={"email":"260002@astanait.edu.kz","password":"Oldpass123"});self.assertEqual(old.status_code,401)
        new=self.client.post("/api/login",json={"email":"260002@astanait.edu.kz","password":"Newpass123"});self.assertEqual(new.status_code,200)
    def test_expired_and_invalid_links_are_clear(self):
        token=self.request_link();self.store.resets[0]["expired"]=True
        self.assertEqual(self.client.get("/api/recover/check",query_string={"token":token}).status_code,410)
        self.assertEqual(self.client.get("/api/recover/check",query_string={"token":"x"}).status_code,400)
    def test_sixth_request_is_rate_limited(self):
        for _ in range(5): self.client.post("/api/recover/request",json={"email":"nobody@astanait.edu.kz"})
        sixth=self.client.post("/api/recover/request",json={"email":"nobody@astanait.edu.kz"})
        self.assertEqual(sixth.status_code,429)
    def test_support_requests_are_admin_only_and_can_be_resolved(self):
        created=self.client.post("/api/support-requests",json={"email":"260002@astanait.edu.kz","message":"I did not get the recovery email."})
        self.assertEqual(created.status_code,201)
        appmod.SESSIONS["student"]=(2,9999999999);appmod.SESSIONS["admin"]=(200,9999999999)
        denied=self.client.get("/api/support-requests",headers={"Authorization":"student"});self.assertEqual(denied.status_code,403)
        allowed=self.client.get("/api/support-requests",headers={"Authorization":"admin"});self.assertEqual(allowed.status_code,200)
        patched=self.client.patch("/api/support-requests/1",json={"status":"resolved"},headers={"Authorization":"admin"});self.assertEqual(patched.status_code,200)
        self.assertEqual(self.store.support[0]["status"],"resolved")

if __name__=="__main__": unittest.main()
