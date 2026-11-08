# Classroom check-in (NFC + QR) - D2 extension

New IoT feature: students are marked present by tapping a student card on an NFC door reader (ESP32 + PN532),
or by scanning a short-lived QR code from the teacher's screen. Both go through the same server check.

## What changed in the project

| File | Change |
|---|---|
| `checkin.py` (new) | All logic: tables, `POST /api/checkin`, QR code, reader/card admin, purge of old rows |
| `app_postgresql.py` | 3 small edits: `import checkin`, `checkin.init_schema(cur)` in `init_db()`, `checkin.register(...)` before `__main__` |
| `static/index.html` | Menu items "Check-in" (student, teacher) and "Check-in readers" (admin); `?checkin=<code>` link handling |
| `tools/reader_sim.py` (new) | Simulator of the reader: `setup`, `tap`, `class`, `heartbeat` |
| `requierments.txt` | + `segno` (draws the QR image; without it the teacher still sees the text code) |

## Works with the admin course/schedule module
- Lessons are matched by date: a one-off lesson (`schedule.class_date` set, created in "Courses & schedule") counts only on its date, a weekly demo lesson (`class_date` NULL) on its weekday.
- A course can have several teachers (`course_teachers`): each of them can open the QR code of the course's class.
- A new course starts working with check-in as soon as it has a lesson with a room and enrolled students, and the room has a reader (admin: "Check-in readers"). Deleting a course keeps its check-in log (`schedule_id` becomes NULL); its attendance rows are deleted by the existing cascade.
- Not covered yet: there is no screen or endpoint to enrol students into a new course (only the demo seed does it), so students of a new course must be added to `enrollments` first.

## New database objects
`devices(id, room UNIQUE, name, key_hash, created_at, last_seen)`, `checkins(user_id, device_id, schedule_id, method, result, created_at)`,
`checkin_audit(admin_id, action, created_at)`, column `users.nfc_token_hash`, unique index `one_attendance_per_day` on `attendance`
(skipped with a message if an old database already contains duplicate rows).

## Endpoints
- `POST /api/checkin` - `{"method":"nfc","card_uid":"04000002"}` with headers `X-Device-Id`, `X-Device-Key` (reader), or `{"method":"qr","code":"..."}` with the student's login token
- `GET /api/checkin/qr` (teacher) - code + QR image for the class that is open for check-in
- `GET /api/checkins` (student) - own history, last 30 days
- `POST /api/device/heartbeat` (reader) - updates `last_seen` only
- Admin: `GET/POST /api/devices`, `DELETE /api/devices/<id>`, `POST /api/cards`, `DELETE /api/cards/<user_id>`, `GET /api/admin/checkins` (every call is written to `checkin_audit`)

## Rules
- Check-in is open from 10 minutes before the class starts to 15 minutes after (`EARLY_MIN`, `LATE_MIN`).
- The lesson is found from the reader's room (NFC) or from the signed code (QR), then `enrollments` is checked, then one `attendance` row is written.
- A QR code lives 60 seconds and contains only the lesson id, an expiry time and a signature.
- Cards are stored as a keyed hash (HMAC-SHA256 with `CHECKIN_SECRET`), reader keys as SHA-256. Raw check-ins are deleted after 30 days.
- Rate limit: 60 requests per minute per reader and per student.

## Settings (environment variables)
`CHECKIN_SECRET` (set a long random value before real use), `CHECKIN_TZ` (default `Asia/Almaty`; the timetable has no time zone).
`CHECKIN_TEST_NOW=2026-11-02T09:05:00` fixes the clock for tests only.

## Demo
```
pip install -r requierments.txt
python app_postgresql.py                       # terminal 1
python tools/reader_sim.py setup               # terminal 2: 20 readers (rooms C1.100-C1.119) + a card for each demo student
python tools/reader_sim.py class --room C1.100 --users 2-60
```
(Do this during the class time of the room, e.g. C1.100 on Monday 09:00-10:30, or set `CHECKIN_TEST_NOW`.)
`setup` writes the reader keys to `tools/demo_devices.json` - do not commit that file.
