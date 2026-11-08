# Work submission by link

Students hand in work as a **link** (Google Drive, OneDrive, GitHub, any web address). No files are uploaded:
the teacher opens the link and downloads the work from there.

## How it works
- **Student, Assignments:** the status column has "Submit work". It opens a form (link + optional comment).
  After sending: "Submitted", the date, buttons "Open", "Change", "Withdraw". The Dashboard timeline also shows a "Submitted" badge.
- **Late work** is accepted after the deadline and marked "Late" (the teacher sees the mark).
- **Closed after grading:** once the teacher has given a grade, the student can no longer change or withdraw the link
  (the teacher can remove the grade, which opens the task again).
- **Teacher, Assignments:** every task shows "Submitted 12/25" and a "Grade" button that opens Grades with this task selected.
- **Teacher, Grades:** a new column "Work (link)": "Open work" (new tab), submission time, "Late" mark, student's comment, or "Not submitted".
  A grade can be given without a submission (for example for an oral answer).

## What changed in the project
| File | Change |
|---|---|
| `submissions.py` (new) | table `submissions`, `PUT /api/submissions`, `DELETE /api/submissions/<assignment_id>` |
| `app_postgresql.py` | `import submissions`, `submissions.init_schema(cur)`, `submissions.register(...)`; `GET /api/assignments` returns submission fields (student) and counters (teacher) |
| `gradebook.py` | `GET /api/courses/<id>/grades` also returns `submissions` |
| `static/index.html` | submit form in Assignments, "Grade" shortcut, "Work (link)" column in Grades |
| `tools/test_submissions.py` (new) | 7 unit tests: `python -m unittest tools.test_submissions -v` |
| `tools/mock_server.py` | stubs (test tool only) |

## Also fixed
The teacher's task list and "delete task" used `courses.teacher_id` (the first teacher only). They now use `course_teachers`,
so every teacher of a course sees and manages its tasks.

## Rules checked on the server
- Only a student enrolled in the course can submit; teachers and admin cannot.
- Link: `http://` or `https://`, up to 500 characters, no spaces / quotes / `<>`; `javascript:`, `file:` and similar are refused (same check as course materials).
- Comment up to 500 characters. One link per student and task (sending again replaces it).

## Not done (ask if needed)
- Several links per task (a Drive folder works instead).
- A notification for the teacher about a new submission (the counter and the Grades column show it).
- Checked with unit tests and the stub server; run the app once on PostgreSQL to confirm the new table and queries.
