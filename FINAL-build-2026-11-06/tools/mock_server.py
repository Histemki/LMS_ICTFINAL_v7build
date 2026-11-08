"""Test stub for the LMS API (no PostgreSQL available in this sandbox).
Serves the REAL static files of one variant plus fake JSON. Used only to measure/verify the front-end."""
import sys, os, secrets, time, html
from datetime import date, datetime
from flask import Flask, jsonify, request, send_from_directory
root, port = sys.argv[1], int(sys.argv[2])
app = Flask(__name__)
S = os.path.join(root, "static")
groups = [{"id": 1, "name": "Team Alpha", "course": "Databases", "course_id": 2, "members": 3, "max_size": 4, "participants": [{"id": 1, "name": "Aruzhan S.", "email": "260001@astanait.edu.kz"}, {"id": 2, "name": "Test Student", "email": "260002@astanait.edu.kz"}, {"id": 3, "name": "Dias K.", "email": "260003@astanait.edu.kz"}]}]
recovery_messages = []
reset_links = {}
support_requests = []
mock_sessions = {}
mock_reads = set()
courses = [{"id": i + 1, "code": c, "title": t, "description":"", "teacher":"Test Teacher", "teacher_ids":[2], "teachers":"Test Teacher"} for i, (c, t) in
           enumerate([("MATH101", "Calculus"), ("CS201", "Databases"), ("ENG101", "English"), ("ICT101", "ICT"), ("PHY101", "Physics")])]
@app.get("/")
def index(): return send_from_directory(S, "index.html")
@app.get("/icons/<path:f>")
def icons(f): return send_from_directory(os.path.join(S, "icons"), f, max_age=86400)
@app.get("/media/<path:f>")
def media(f): return send_from_directory(os.path.join(S, "media"), f, max_age=86400)
@app.get("/ui/<path:f>")
def ui_files(f): return send_from_directory(os.path.join(S, "ui"), f)
@app.get("/map/<path:f>")
def map_files(f): return send_from_directory(os.path.join(S, "map"), f, max_age=86400)

@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    email = str(d.get("email", "260002@astanait.edu.kz")).strip().lower()
    role = "admin" if email == "admin@astanait.edu.kz" else "teacher" if email.startswith("teacher") else "student"
    token = "mock-" + role
    mock_sessions[token] = role
    return jsonify(token=token, name={"admin":"Registrar Office","teacher":"Test Teacher","student":"Test Student"}[role], email=email, role=role)
@app.post("/api/logout")
def logout(): return jsonify(ok=1)
@app.get("/api/courses")
def api_courses(): return jsonify(courses)

mock_teachers=[{"id":2,"name":"Test Teacher","email":"teacher@astanait.edu.kz"},{"id":3,"name":"Demo Teacher","email":"teacher2@astanait.edu.kz"}]
mock_lessons=[]
def admin_role():
    role=mock_sessions.get(request.headers.get("Authorization",""))
    if not role:return None,(jsonify(error="Not signed in"),401)
    if role!="admin":return None,(jsonify(error="Only admin can manage courses and schedule"),403)
    return role,None
@app.get("/api/admin/teachers")
def mock_admin_teachers():
    _r,e=admin_role()
    return e if e else jsonify(mock_teachers)
@app.get("/api/admin/courses")
def mock_admin_courses():
    _r,e=admin_role()
    return e if e else jsonify(courses)
def mock_course_payload(d):
    code=str(d.get("code","")).strip().upper();title=str(d.get("title","")).strip();desc=str(d.get("description","")).strip()
    try:ids=list(dict.fromkeys(int(x) for x in d.get("teacher_ids",[])))
    except (TypeError,ValueError):return None,"Choose at least one registered teacher"
    if not code or len(code)>20:return None,"Course code must be 1-20 characters"
    if not title or len(title)>150:return None,"Course title must be 1-150 characters"
    if len(desc)>4000:return None,"Description is too long"
    if not ids or any(not any(t["id"]==i for t in mock_teachers) for i in ids):return None,"Choose registered teachers"
    return {"code":code,"title":title,"description":desc,"teacher_ids":ids,"teachers":", ".join(t["name"] for t in mock_teachers if t["id"] in ids),"teacher":"Test Teacher"},None
@app.post("/api/admin/courses")
def mock_course_create():
    _r,e=admin_role()
    if e:return e
    v,msg=mock_course_payload(request.get_json(silent=True) or {})
    if msg:return jsonify(error=msg),400
    if any(c["code"]==v["code"] for c in courses):return jsonify(error="A course with this code already exists"),409
    v["id"]=max(c["id"] for c in courses)+1;courses.append(v);return jsonify(id=v["id"]),201
@app.route("/api/admin/courses/<int:cid>",methods=["PUT","DELETE"])
def mock_course_item(cid):
    _r,e=admin_role()
    if e:return e
    c=next((x for x in courses if x["id"]==cid),None)
    if not c:return jsonify(error="Course not found"),404
    if request.method=="DELETE":courses.remove(c);return jsonify(ok=True)
    v,msg=mock_course_payload(request.get_json(silent=True) or {})
    if msg:return jsonify(error=msg),400
    if any(x["id"]!=cid and x["code"]==v["code"] for x in courses):return jsonify(error="A course with this code already exists"),409
    c.update(v);return jsonify(ok=True)
@app.get("/api/admin/schedule")
def mock_admin_schedule():
    _r,e=admin_role()
    return e if e else jsonify(mock_lessons)
def lesson_values(d):
    try:
        cid=int(d.get("course_id"));day=date.fromisoformat(str(d.get("class_date","")));start=str(d.get("start",""));end=str(d.get("end",""))
        from datetime import time as dt
        a=dt.fromisoformat(start);b=dt.fromisoformat(end)
    except (TypeError,ValueError):return None,"Choose a course, valid date and times"
    room=str(d.get("room","")).strip();kind=str(d.get("kind","Lecture"))
    if not any(x["id"]==cid for x in courses):return None,"Course not found"
    if b<=a:return None,"End time must be later than start time"
    if not room or len(room)>20:return None,"Room must be 1-20 characters"
    if kind not in {"Lecture","Practice","Lab"}:return None,"Choose a valid lesson type"
    for x in mock_lessons:
        if x["class_date"]==day.isoformat() and x["id"]!=d.get("id") and a.isoformat(timespec="minutes")<x["ends"] and b.isoformat(timespec="minutes")>x["starts"]:
            if x["room"].casefold()==room.casefold() or set(x.get("teacher_ids",[])) & set(next(c for c in courses if c["id"]==cid)["teacher_ids"]):return None,"Schedule conflict: teacher or room is already occupied at this time"
    c=next(x for x in courses if x["id"]==cid)
    return {"course_id":cid,"class_date":day.isoformat(),"starts":a.isoformat(timespec="minutes"),"ends":b.isoformat(timespec="minutes"),"room":room,"kind":kind,"course":c["title"],"code":c["code"],"teachers":c["teachers"],"teacher_ids":c["teacher_ids"]},None
@app.post("/api/admin/schedule")
def mock_lesson_create():
    _r,e=admin_role()
    if e:return e
    v,msg=lesson_values(request.get_json(silent=True) or {})
    if msg:return jsonify(error=msg),409 if msg.startswith("Schedule conflict") else 400
    v["id"]=max([x["id"] for x in mock_lessons]+[0])+1;mock_lessons.append(v);return jsonify(id=v["id"]),201
@app.route("/api/admin/schedule/<int:lid>",methods=["PUT","DELETE"])
def mock_lesson_item(lid):
    _r,e=admin_role()
    if e:return e
    old=next((x for x in mock_lessons if x["id"]==lid),None)
    if not old:return jsonify(error="Lesson not found"),404
    if request.method=="DELETE":mock_lessons.remove(old);return jsonify(ok=True)
    d=request.get_json(silent=True) or {};d["id"]=lid;v,msg=lesson_values(d)
    if msg:return jsonify(error=msg),409 if msg.startswith("Schedule conflict") else 400
    old.update(v);return jsonify(ok=True)
@app.get("/api/dashboard")
def dash(): return jsonify(courses=5, avg_grade=82.3, groups=len(groups))
@app.get("/api/assignments")
def asg():
    role = mock_sessions.get(request.headers.get("Authorization"), "student")
    out = []
    for t in mock_tasks:
        late = t["due_date"] < "2026-11-06"
        row = {"id": t["id"], "course_id": 2, "title": t["title"], "type": t["type"], "course": "Databases", "teacher": "Test Teacher", "max_score": t["max_score"],
               "description": "", "due_date": t["due_date"], "due_time": "23:59", "control_form": t["control_form"], "overdue": late, "seconds_left": 86400 * 14}
        if role == "teacher":
            row.update(score=None, submitted=sum(1 for k in mock_subs if k[1] == t["id"]), enrolled=len(mock_students))
        else:
            g = mock_grades.get((2, t["id"])); s = mock_subs.get((2, t["id"]))
            row.update(score=g["score"] if g else None, submission_url=s["url"] if s else None, submission_comment=s["comment"] if s else None,
                       submitted_at=s["submitted_at"] if s else None, submitted_late=bool(s and late))
        out.append(row)
    return jsonify(out)
mock_subs = {}   # (user_id, assignment_id) -> {url, comment, submitted_at}
@app.put("/api/submissions")
def sub_put():
    d = request.get_json() or {}; aid = int(d.get("assignment_id", 0)); url = str(d.get("url", "")).strip()
    if not _re.match(r"^https?://[^\s]+\.[^\s]+$", url): return jsonify(error="Enter a full web link starting with http:// or https://"), 400
    if (2, aid) in mock_grades: return jsonify(error="This task is already graded, the work can no longer be changed"), 409
    mock_subs[(2, aid)] = {"url": url, "comment": d.get("comment", ""), "submitted_at": _stamp()}
    return jsonify(ok=True, submission=mock_subs[(2, aid)])
@app.delete("/api/submissions/<int:aid>")
def sub_del(aid):
    if (2, aid) in mock_grades: return jsonify(error="This task is already graded, the work can no longer be changed"), 409
    if not mock_subs.pop((2, aid), None): return jsonify(error="Nothing was submitted for this task"), 404
    return jsonify(ok=True)
@app.get("/api/attendance")
def att(): return jsonify([{"course": "ICT", "present": 8, "total": 10}])
@app.get("/api/grades")
def grd(): return jsonify([{"course": "ICT", "score": 88, "max_score": 100}])
@app.route("/api/groups", methods=["GET", "POST"])
def grp():
    if request.method == "POST":
        d = request.get_json(); groups.append({"id": len(groups) + 10, "name": d["name"], "course": "ICT", "members": 1, "max_size": int(d["max_size"])})
        return jsonify(ok=1), 201
    return jsonify(groups)
@app.delete("/api/groups/<int:i>")
def delg(i):
    groups[:] = [g for g in groups if g["id"] != i]; return jsonify(ok=1)
# --- extra stub data so every screen of the redesign can be looked at ---
@app.get("/api/schedule")
def sch(): return jsonify([{"weekday": 1, "starts": "09:00", "ends": "10:30", "course": "Databases", "kind": "Lecture", "room": "C1.100", "teacher": "Test Teacher"},
    {"weekday": 1, "starts": "11:00", "ends": "12:30", "course": "Calculus", "kind": "Practice", "room": "C1.204", "teacher": "Test Teacher"},
    {"weekday": 3, "starts": "14:00", "ends": "15:30", "course": "ICT", "kind": "Lab", "room": "C1.310", "teacher": "Test Teacher"}])
@app.get("/api/applications")
def apps_():
    if mock_sessions.get(request.headers.get("Authorization")) == "admin":
        return jsonify([{"id": 1, "student": "Test Student", "email": "260002@astanait.edu.kz", "type_title": "Academic leave", "message": "Medical reasons.", "created_at": "2026-11-01", "status": "pending", "review_comment": ""}])
    return jsonify([{"id": 1, "created_at": "2026-11-01", "type_title": "Certificate", "message": "Need a certificate of study.", "status": "approved", "review_comment": "Ready at the office"},
                    {"id": 2, "created_at": "2026-11-03", "type_title": "Retake", "message": "Illness during the exam.", "status": "pending", "review_comment": ""}])
@app.get("/api/application-types")
def apptypes(): return jsonify([{"id": 1, "title": "Certificate"}, {"id": 2, "title": "Retake"}])
@app.get("/api/checkins")
def chk(): return jsonify([{"at": "2026-11-02 09:03", "course": "Databases", "method": "nfc", "result": "ok"}, {"at": "2026-10-30 11:01", "course": "Calculus", "method": "qr", "result": "duplicate"}])
@app.get("/api/checkin/qr")
def chkqr(): return jsonify(course="Databases", room="C1.100", start="09:00", code="demo-code-1234", expires_in=60, image="")
@app.get("/api/devices")
def devs(): return jsonify([{"id": 1, "room": "C1.100", "name": "Door reader", "last_seen": "2026-11-02 08:59"}])
@app.get("/api/support-requests")
def supp(): return jsonify([{"id": 1, "email": "260009@astanait.edu.kz", "message": "I cannot reset my password.", "created_at": "2026-11-02", "status": "new"}])
@app.get("/api/assignment-types")
def atypes(): return jsonify([{"id": 1, "name": "Assignment"}, {"id": 2, "name": "Quiz"}])
@app.get("/api/courses/<int:i>/students")
def cstud(i): return jsonify([{"id": 5, "name": "Free Student", "email": "260005@astanait.edu.kz", "group_id": None}])

# --- gradebook stubs (the real logic lives in gradebook.py + PostgreSQL) ---
mock_grades = {}   # (user_id, assignment_id) -> dict
mock_students = [{"id": 2, "name": "Test Student", "email": "260002@astanait.edu.kz"}, {"id": 3, "name": "Aruzhan S.", "email": "260003@astanait.edu.kz"}]
mock_tasks = [{"id": 1, "title": "Homework 1", "type": "Homework", "control_form": "current", "max_score": 10, "due_date": "2026-09-20"},
              {"id": 2, "title": "Midterm", "type": "Midterm Exam", "control_form": "midterm", "max_score": 30, "due_date": "2026-10-15"},
              {"id": 3, "title": "Lab report 1", "type": "Laboratory", "control_form": "current", "max_score": 20, "due_date": "2026-11-20"}]
def _stamp(): return datetime.now().strftime("%Y-%m-%d %H:%M")
mock_grades[(2, 1)] = {"id": 1, "user_id": 2, "assignment_id": 1, "score": 9.0, "comment": "Well done", "graded_at": "2026-09-21 10:00", "changed": False, "prev": None}
mock_grades[(2, 2)] = {"id": 2, "user_id": 2, "assignment_id": 2, "score": 21.0, "comment": "", "graded_at": "2026-10-16 12:30", "changed": True, "prev": 18.0}
@app.get("/api/courses/<int:i>/grades")
def cgrades(i): return jsonify(assignments=mock_tasks, students=mock_students, grades=[{k: v for k, v in g.items() if k != "prev"} for g in mock_grades.values()],
    submissions=[{"assignment_id": a, "user_id": u, "url": s["url"], "comment": s["comment"], "submitted_at": s["submitted_at"], "late": False} for (u, a), s in mock_subs.items()])
@app.put("/api/grades")
def put_grade():
    d = request.get_json() or {}
    try: aid, uid, score = int(d["assignment_id"]), int(d["user_id"]), float(d["score"])
    except Exception: return jsonify(error="Send assignment_id, user_id and a numeric score"), 400
    task = next((t for t in mock_tasks if t["id"] == aid), None)
    if not task: return jsonify(error="Task not found in your courses"), 404
    if not 0 <= score <= task["max_score"]: return jsonify(error=f"Score must be between 0 and {task['max_score']:g}"), 400
    old = mock_grades.get((uid, aid)); changed = not old or old["score"] != score or old["comment"] != d.get("comment", "")
    g = {"id": old["id"] if old else 100 + len(mock_grades), "user_id": uid, "assignment_id": aid, "score": score, "comment": d.get("comment", ""),
         "graded_at": _stamp() if changed else old["graded_at"], "changed": bool(old) and (old["changed"] or changed), "prev": old["score"] if old and changed else (old or {}).get("prev")}
    mock_grades[(uid, aid)] = g
    return jsonify(ok=True, changed=changed, grade={"id": g["id"], "score": score, "max_score": task["max_score"], "comment": g["comment"], "graded_at": g["graded_at"]})
@app.delete("/api/grades/<int:gid>")
def del_grade(gid):
    for k, g in list(mock_grades.items()):
        if g["id"] == gid: del mock_grades[k]; return jsonify(ok=True)
    return jsonify(error="Grade not found in your courses"), 404
@app.get("/api/grades/details")
def grade_details():
    items = []
    for (uid, aid), g in mock_grades.items():
        if uid != 2: continue
        t = next(x for x in mock_tasks if x["id"] == aid)
        items.append({"id": g["id"], "course_id": 2, "course": "Databases", "assignment": t["title"], "type": t["type"], "control_form": t["control_form"],
                      "score": g["score"], "max_score": t["max_score"], "graded_at": g["graded_at"], "comment": g["comment"], "graded_by": "Test Teacher", "previous_score": g["prev"]})
    items.sort(key=lambda x: x["graded_at"], reverse=True)
    s = sum(i["score"] for i in items); m = sum(i["max_score"] for i in items)
    courses = [{"course_id": 2, "course": "Databases", "score": s, "max_score": m, "percent": round(100 * s / m, 1) if m else None, "items": items},
               {"course_id": 3, "course": "Calculus", "score": 0, "max_score": 0, "percent": None, "items": []}]
    return jsonify(average=round(100 * s / m, 1) if m else None, graded=len(items), courses=courses, recent=items[:15])

# --- materials stubs (the real logic lives in materials.py + PostgreSQL) ---
import re as _re
def _embed(u):
    f = _re.search(r"(?:youtu\.be/|v=)([A-Za-z0-9_-]{11})", u)
    return "https://www.youtube-nocookie.com/embed/" + f.group(1) if f and ("youtu" in u) else None
def _mat(i, cid, title, url, desc="", author="Test Teacher"):
    host = _re.sub(r"^www\.", "", _re.search(r"//([^/]+)", url).group(1))
    return {"id": i, "course_id": cid, "title": title, "url": url, "description": desc, "created_at": "2026-11-0%d" % (i % 9 + 1), "author": author, "embed": _embed(url), "host": host}
mock_materials = [_mat(1, 2, "Lecture 1: Relational model", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Watch before the first lab."),
                  _mat(2, 2, "PostgreSQL documentation", "https://www.postgresql.org/docs/", "Reference for the labs."),
                  _mat(3, 1, "Limits explained", "https://youtu.be/9bZkp7q19f0")]
@app.get("/api/materials")
def mat_list():
    role = mock_sessions.get(request.headers.get("Authorization"), "student")
    cs = courses if role == "admin" else courses[:3]
    out = [{"id": c["id"], "code": c["code"], "title": c["title"], "teacher": c["teacher"],
            "materials": [dict(m, can_delete=role != "student") for m in reversed(mock_materials) if m["course_id"] == c["id"]]} for c in cs]
    return jsonify(can_edit=role != "student", courses=out)
@app.post("/api/materials")
def mat_add():
    role = mock_sessions.get(request.headers.get("Authorization"), "student")
    if role == "student": return jsonify(error="Only teachers and the registrar office can add materials"), 403
    d = request.get_json() or {}; url = str(d.get("url", "")).strip()
    if not _re.match(r"^https?://[^\s]+\.[^\s]+$", url): return jsonify(error="Enter a full web link starting with http:// or https://"), 400
    m = _mat(len(mock_materials) + 10, int(d["course_id"]), d.get("title", ""), url, d.get("description", ""), "Test Teacher")
    mock_materials.append(m); return jsonify(dict(m, can_delete=True)), 201
@app.delete("/api/materials/<int:i>")
def mat_del(i):
    mock_materials[:] = [m for m in mock_materials if m["id"] != i]; return jsonify(ok=1)

# Recovery endpoints for this dependency-free development stub.
@app.post("/api/recover/request")
def recover_request():
    d = request.get_json(silent=True) or {}
    email = str(d.get("email", "")).strip().lower()
    if not email.endswith("@astanait.edu.kz") or "@" not in email:
        return jsonify(error="Enter your university email address ending in @astanait.edu.kz."), 400
    token = secrets.token_urlsafe(32)
    reset_links[token] = {"created": time.time(), "used": False}
    recovery_messages.append((email, token, time.strftime("%Y-%m-%d %H:%M:%S")))
    return jsonify(message="If this address is registered, we sent a link. It is valid for 30 minutes.", demo_mode=True)

@app.get("/api/recover/check")
def recover_check():
    record = reset_links.get(request.args.get("token", ""))
    if not record: return jsonify(error="This link is invalid. Request a new one."), 400
    if record["used"]: return jsonify(error="This link has already been used. Request a new one."), 409
    if time.time() - record["created"] > 1800: return jsonify(error="This link has expired. Request a new one."), 410
    return jsonify(ok=True)

@app.post("/api/recover/reset")
def recover_reset():
    d = request.get_json(silent=True) or {}
    token, password = str(d.get("token", "")), str(d.get("password", ""))
    record = reset_links.get(token)
    if not record: return jsonify(error="This link is invalid. Request a new one."), 400
    if record["used"]: return jsonify(error="This link has already been used. Request a new one."), 409
    if time.time() - record["created"] > 1800: return jsonify(error="This link has expired. Request a new one."), 410
    if password != d.get("confirm_password"): return jsonify(error="The passwords do not match. Enter them again."), 400
    if len(password) < 8 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return jsonify(error="Use at least 8 characters, including a letter and a number."), 400
    record["used"] = True
    return jsonify(ok=True)

@app.get("/demo-inbox")
def demo_inbox():
    cards = "".join(f'<article><h2>Password recovery</h2><p>To: {html.escape(email)}</p><p>Sent: {sent}</p><a href="/?recover_token={token}">Set a new password</a></article>' for email, token, sent in recovery_messages)
    return "<!doctype html><meta charset=utf-8><title>Demo inbox</title><h1>Demo inbox</h1>" + (cards or "<p>No recovery messages yet.</p>")

@app.post("/api/support-requests")
def create_support_request():
    d = request.get_json(silent=True) or {}
    email, message = str(d.get("email", "")).strip(), str(d.get("message", "")).strip()
    if not email.endswith("@astanait.edu.kz") or len(message) < 10:
        return jsonify(error="Enter a university email and a message of at least 10 characters."), 400
    support_requests.append({"id": len(support_requests) + 1, "email": email, "message": message, "status": "new"})
    return jsonify(ok=True, id=support_requests[-1]["id"]), 201

# In-memory announcements for the no-PostgreSQL development server.
announcement_items = [
    {"id":1,"title":"Welcome to the LMS","body":"Welcome! Check this page for campus updates.","category":"General","audience":"everyone","course_id":None,"pinned":True,"author_id":200,"created_at":"2026-11-01 09:00","updated_at":None,"expires_at":None,"author":"Registrar Office","course":None},
    {"id":2,"title":"Course materials","body":"Review the latest course materials before class.","category":"Important","audience":"course","course_id":1,"pinned":False,"author_id":200,"created_at":"2026-11-01 08:00","updated_at":None,"expires_at":None,"author":"Registrar Office","course":"Calculus"},
    {"id":3,"title":"Faculty meeting","body":"Faculty meeting on Friday at 15:00.","category":"Events","audience":"teachers","course_id":None,"pinned":False,"author_id":200,"created_at":"2026-10-31 14:00","updated_at":None,"expires_at":None,"author":"Registrar Office","course":None},
]

def announcement_role():
    return mock_sessions.get(request.headers.get("Authorization", ""))

def announcement_allowed(item, role):
    if role == "admin": return True
    if item["audience"] == "everyone": return True
    if role == "student": return item["audience"] == "students" or (item["audience"] == "course" and item["course_id"] == 1)
    if role == "teacher": return item["audience"] == "teachers" or (item["audience"] == "course" and item["course_id"] == 1)
    return False

def announcement_error(admin=False):
    role = announcement_role()
    if not role: return None, (jsonify(error="Not signed in"), 401)
    if admin and role != "admin": return None, (jsonify(error="Only admin can manage announcements"), 403)
    return role, None

def announcement_values(d):
    title, body = str(d.get("title", "")).strip(), str(d.get("body", "")).strip()
    category, audience = str(d.get("category", "")), str(d.get("audience", ""))
    if not 1 <= len(title) <= 120: return None, "Title must be 1-120 characters"
    if not 1 <= len(body) <= 2000: return None, "Text must be 1-2000 characters"
    if category not in {"General","Important","Exams","Events"}: return None, "Choose a valid category"
    if audience not in {"everyone","students","teachers","course"}: return None, "Choose a valid audience"
    cid = None
    if audience == "course":
        try: cid = int(d.get("course_id"))
        except (TypeError,ValueError): return None, "Choose a course"
        if not any(c["id"] == cid for c in courses): return None, "The selected course does not exist"
    expires = str(d.get("expires_at") or "").strip()
    if expires:
        try: exp = date.fromisoformat(expires)
        except ValueError: return None, "Enter a valid expiration date"
        if exp < date.today(): return None, "Expiration date cannot be in the past"
    else: expires = None
    return {"title":title,"body":body,"category":category,"audience":audience,"course_id":cid,"pinned":bool(d.get("pinned")),"expires_at":expires}, None

@app.get("/api/announcements")
def mock_announcements():
    role,error=announcement_error()
    if error: return error
    q=request.args.get("q","").strip().lower(); category=request.args.get("category","")
    try: offset=max(0,int(request.args.get("offset",0))); limit=min(10,max(1,int(request.args.get("limit",10))))
    except ValueError: return jsonify(error="Invalid offset or limit"),400
    visible=[]
    for a in announcement_items:
        expired=bool(a["expires_at"] and a["expires_at"] < date.today().isoformat())
        if role != "admin" and expired: continue
        if not announcement_allowed(a,role): continue
        if category and a["category"] != category: continue
        if q and q not in (a["title"]+" "+a["body"]).lower(): continue
        item=dict(a);item["expired"]=expired;item["is_read"]=(request.headers.get("Authorization"),a["id"]) in mock_reads;visible.append(item)
    visible.sort(key=lambda a:(a["created_at"],a["id"]),reverse=True)
    visible.sort(key=lambda a:not a["pinned"])
    return jsonify(visible[offset:offset+limit])

@app.get("/api/announcements/unread-count")
def mock_announcement_unread_count():
    role,error=announcement_error()
    if error: return error
    count=sum(1 for a in announcement_items if announcement_allowed(a,role) and (role=="admin" or not a["expires_at"] or a["expires_at"]>=date.today().isoformat()) and (request.headers.get("Authorization"),a["id"]) not in mock_reads)
    return jsonify(count=count)

@app.get("/api/announcements/courses")
def mock_announcement_courses():
    _role,error=announcement_error(admin=True)
    if error: return error
    return jsonify(courses)

@app.post("/api/announcements/<int:announcement_id>/read")
def mock_announcement_read(announcement_id):
    role,error=announcement_error()
    if error: return error
    item=next((a for a in announcement_items if a["id"]==announcement_id and announcement_allowed(a,role)),None)
    if not item: return jsonify(error="Announcement not found"),404
    mock_reads.add((request.headers.get("Authorization"),announcement_id));return jsonify(ok=True)

@app.post("/api/announcements/read-all")
def mock_announcement_read_all():
    role,error=announcement_error()
    if error: return error
    for a in announcement_items:
        if announcement_allowed(a,role): mock_reads.add((request.headers.get("Authorization"),a["id"]))
    return jsonify(ok=True)

@app.post("/api/announcements")
def mock_announcement_create():
    _role,error=announcement_error(admin=True)
    if error: return error
    values,message=announcement_values(request.get_json(silent=True) or {})
    if message: return jsonify(error=message),400
    values.update({"id":max((a["id"] for a in announcement_items),default=0)+1,"author_id":200,"author":"Registrar Office","course":next((c["title"] for c in courses if c["id"]==values["course_id"]),None),"created_at":datetime.now().strftime("%Y-%m-%d %H:%M"),"updated_at":None})
    announcement_items.append(values);return jsonify(id=values["id"]),201

@app.route("/api/announcements/<int:announcement_id>",methods=["PUT","PATCH","POST"])
def mock_announcement_edit(announcement_id):
    _role,error=announcement_error(admin=True)
    if error: return error
    item=next((a for a in announcement_items if a["id"]==announcement_id),None)
    if not item: return jsonify(error="Announcement not found"),404
    values,message=announcement_values(request.get_json(silent=True) or {})
    if message: return jsonify(error=message),400
    item.update(values);item["course"]=next((c["title"] for c in courses if c["id"]==values["course_id"]),None);item["updated_at"]=datetime.now().strftime("%Y-%m-%d %H:%M")
    return jsonify(ok=True,id=announcement_id)

@app.delete("/api/announcements/<int:announcement_id>")
def mock_announcement_delete(announcement_id):
    _role,error=announcement_error(admin=True)
    if error: return error
    before=len(announcement_items);announcement_items[:]=[a for a in announcement_items if a["id"]!=announcement_id]
    if len(announcement_items)==before: return jsonify(error="Announcement not found"),404
    return jsonify(ok=True)

app.run(port=port)
