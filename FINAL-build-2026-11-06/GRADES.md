# Grades: teachers give grades, students see them

Before this change a teacher could not give grades at all and a student saw only one sum per course.

## What changed in the project

| File | Change |
|---|---|
| `gradebook.py` (new) | Schema migration, 4 endpoints for grades, history of changes |
| `app_postgresql.py` | 3 small edits: `import gradebook`, `gradebook.init_schema(cur)` in `init_db()`, `gradebook.register(...)` before `__main__` |
| `static/index.html` | Menu item "Grades" is now for students and teachers (`data-r="st"`); new `showGrades()` (student dashboard / teacher gradebook) |
| `static/ui/theme.css` | Styles of the grade feed, course accordion and gradebook |
| `tools/test_grades.py` (new) | 9 unit tests (no PostgreSQL needed): `python -m unittest tools.test_grades -v` |
| `tools/mock_server.py` | Stubs of the new endpoints (test tool only) |

The old `GET /api/grades` (sum per course) is unchanged and no longer used by the page.

## Database (added only, nothing is deleted)
- `grades`: + `graded_at` (old rows get the task deadline, never a future time), + `graded_by`, + `comment`.
- `grade_history(user_id, course_id, assignment_id, old_score, new_score, max_score, changed_by, changed_at, comment)`:
  `old_score` NULL = first grade, `new_score` NULL = grade removed.
- Unique index `one_grade_per_task` on `grades(user_id, assignment_id)`; if an old database already has duplicates it is skipped with a message (same approach as check-in).

## Endpoints
- `GET /api/courses/<id>/grades` (teacher of the course): tasks, enrolled students, grades given.
- `PUT /api/grades` (teacher): `{"assignment_id", "user_id", "score", "comment"}`. Score 0..max of the task, student must be enrolled, comment up to 300 characters. Same score and comment = no change, no history row.
- `DELETE /api/grades/<id>` (teacher): removes a grade, the removal is written to the history first.
- `GET /api/grades/details` (student): per course sum, percent and every graded task; `recent` = 15 latest grades with `previous_score` when the grade was changed.

## Rules
- Only a teacher of the course (`course_teachers`) can give, change or remove its grades; the check is on the server.
- A changed grade shows to the student as "Changed, was 7/10" with the new date.

## Not done (ask if needed)
- A "new grade" badge on the Grades icon.
- A full history list per grade (the page shows the previous score only).
- This was checked with unit tests and a stub server; run the app once against PostgreSQL to confirm the migration on your real data.
