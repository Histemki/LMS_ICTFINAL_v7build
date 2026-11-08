# Course materials (links to videos and resources)

Materials are **links**, not files: a title, a web address, an optional description. YouTube and Vimeo links
can be played inside the page; any other link opens in a new tab.

## Who can do what
| Role | Sees | Adds / removes |
|---|---|---|
| Student | materials of the courses he is enrolled in | no |
| Teacher | materials of the courses he teaches | yes, only for his courses |
| Admin | all courses | yes, for any course (so materials can be attached to a new course in advance) |

## Where it is in the interface
- Menu item **Media** (all roles): materials grouped by course. Teacher and admin get an "Add material" form under each course.
- **My courses**: each course card is now a link (mouse or Tab + Enter) that opens the materials of that course, with buttons "My courses" and "All courses".

## What changed in the project
| File | Change |
|---|---|
| `materials.py` (new) | table `course_materials`, 3 endpoints, link validation, embed builder |
| `app_postgresql.py` | 3 small edits: `import materials`, `materials.init_schema(cur)`, `materials.register(...)` |
| `static/index.html` | menu item Media, `showMedia()`, clickable course cards |
| `static/ui/theme.css`, `static/icons/sprite.svg` | styles, icons `i-play`, `i-link` (sprite version `?v=6`) |
| `tools/test_materials.py` (new) | 10 unit tests: `python -m unittest tools.test_materials -v` |
| `tools/mock_server.py` | stubs of the new endpoints (test tool only) |

## Endpoints
- `GET /api/materials` - `{can_edit, courses:[{id, code, title, teacher, materials:[...]}]}` for the signed-in user's courses.
- `POST /api/materials` - `{course_id, title, url, description}` (teacher of the course or admin). 409 if the same link is already in the course.
- `DELETE /api/materials/<id>` - teacher of the course or admin.

## Safety rules
- A link must be `http://` or `https://` with a real host, up to 500 characters, no spaces, quotes or `<>`; `javascript:`, `data:` and similar are refused on the server.
- The player address is built from the extracted video id only (`youtube-nocookie.com/embed/<id>` or `player.vimeo.com/video/<id>`), the original link never goes into the frame. The frame is sandboxed.
- Nothing is loaded from YouTube / Vimeo until the user presses Play (no preview images).
- Links open with `rel="noopener noreferrer"`.

## Not done (ask if needed)
- Editing a material (remove and add again instead).
- Ordering by hand; new materials are shown first.
- Checked with unit tests and the stub server; run the app once on PostgreSQL to confirm the table is created.
