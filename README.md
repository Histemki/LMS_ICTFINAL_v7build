# University LMS (Moodle redesign)

A learning platform for students, teachers and the Registrar Office: courses, assignments and link submissions, grades, course materials, timetable, announcements, applications, classroom check-in (NFC and QR) and a campus map.

**Stack:** Python (Flask) + PostgreSQL. The frontend is a single page (`static/index.html`) with no build step.

---

## 1. Requirements

| Tool | Version | Notes |
|---|---|---|
| Python | 3.10 or newer | `python --version` |
| PostgreSQL | 14 or newer (tested on 18) | the `psql` command-line tool must be available |
| Git | any | to clone the repository |

The commands below are for **Windows PowerShell**. macOS / Linux equivalents are given in section 7.

---

## 2. Get the code and install dependencies

```powershell
git clone <REPOSITORY_URL>
cd <REPOSITORY_FOLDER>
pip install -r requierments.txt
```

> The file name really is `requierments.txt` (with a typo). It installs `flask`, `psycopg2-binary`, `bcrypt` and `segno`.

---

## 3. Create a clean database

The server creates all tables and demo data by itself, but **the database itself must exist and be empty**.

If `psql` is not on your PATH, use its full path. For PostgreSQL 18 on Windows it is:

```powershell
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
```

(Change `18` to your PostgreSQL version.)

**See which databases exist:**

```powershell
& $psql -U postgres -h localhost -c "\l"
```

**Create a clean `lms_db`** (drops an old `lms_db` if there is one, then creates it again):

```powershell
& $psql -U postgres -h localhost -c "DROP DATABASE IF EXISTS lms_db WITH (FORCE);" -c "CREATE DATABASE lms_db;"
```

`psql` asks for the password of the `postgres` user, the one you chose when installing PostgreSQL.

> Demo data is created only on the **first** run against an **empty** database. If you change `DEMO_PASSWORD` later, or the login does not work, recreate the database with the command above and start the server again.

---

## 4. Set environment variables

Run these in the **same PowerShell window** you will start the server from (they last only for that window).

```powershell
# Connection string: replace 1234 with your postgres password
$env:DATABASE_URL = "postgresql://postgres:1234@localhost:5432/lms_db"

# Password that all demo accounts will get
$env:DEMO_PASSWORD = "Demo12345"

# Random secret for check-in (card hashes and QR signatures)
$env:CHECKIN_SECRET = -join ((48..57)+(97..122) | Get-Random -Count 32 | % {[char]$_})
```

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:YOUR_PASSWORD@localhost:5432/lms_db` | PostgreSQL connection |
| `DEMO_PASSWORD` | `Lms#2026demo` | password of every demo account |
| `CHECKIN_SECRET` | `dev-only-change-me` | secret for check-in; set a long random value |
| `CHECKIN_TZ` | `Asia/Almaty` | time zone of the timetable for check-in |
| `APP_ENV` | `development` | `production` turns demo mode off |
| `DEMO_MODE` | `1` (`0` in production) | demo inbox `/demo-inbox` for password recovery |
| `FLASK_DEBUG` | off | `1` turns on Flask debug mode |

---

## 5. Run the server

```powershell
python app_postgresql.py
```

Open **<http://127.0.0.1:5000>** in a browser.

On the first start the server creates the tables, 10 courses, the timetable, grades, attendance, announcements and all demo users. Stop it with `Ctrl + C`.

> **Port 5000 must be free.** Do not run `tools/mock_server.py` at the same time: it also uses port 5000 and accepts any password. Check the port with:
> ```powershell
> Get-NetTCPConnection -LocalPort 5000 -State Listen
> ```

---

## 6. Test accounts

All accounts use the same password: the value of `DEMO_PASSWORD`.
With the settings above that is **`Demo12345`**. If you did not set `DEMO_PASSWORD`, it is **`Lms#2026demo`**.

| Role | Login (email) | Notes |
|---|---|---|
| **Student** | `260002@astanait.edu.kz` | any number from `260002` to `260101` works (100 students, each enrolled in 5 courses) |
| **Teacher** | `teacher@astanait.edu.kz` | Diana Kazbekova, teaches course 1 (ICT, `CSE101`) |
| **Administrator** | `admin@astanait.edu.kz` | Registrar Office |

Other teacher accounts (same password), one per course:

| Course | Teacher | Login |
|---|---|---|
| CSE101 ICT | Diana Kazbekova | `teacher@astanait.edu.kz` |
| CSE102 Databases | Aidar Sarsenov | `a.sarsenov@astanait.edu.kz` |
| CSE103 Calculus | Madina Omarova | `m.omarova@astanait.edu.kz` |
| CSE104 Physics | Erlan Tulegenov | `e.tulegenov@astanait.edu.kz` |
| CSE105 English | Aigerim Nurpeisova | `a.nurpeisova@astanait.edu.kz` |
| CSE106 Algorithms | Bauyrzhan Zhaksylykov | `b.zhaksylykov@astanait.edu.kz` |
| CSE107 Web Development | Saltanat Abenova | `s.abenova@astanait.edu.kz` |
| CSE108 Statistics | Daniyar Mukhamedov | `d.mukhamedov@astanait.edu.kz` |
| CSE109 Discrete Math | Gulnara Kenzhebayeva | `g.kenzhebayeva@astanait.edu.kz` |
| CSE110 Networks | Timur Alimov | `t.alimov@astanait.edu.kz` |

After 5 wrong passwords an account is locked for 20 minutes (restarting the server clears the lock).

**Password recovery in demo mode:** recovery emails are not really sent. Open <http://127.0.0.1:5000/demo-inbox> to see the recovery link.

---

## 7. macOS / Linux

Same steps, different shell syntax:

```bash
pip install -r requierments.txt

psql -U postgres -h localhost -c "DROP DATABASE IF EXISTS lms_db WITH (FORCE);" -c "CREATE DATABASE lms_db;"

export DATABASE_URL="postgresql://postgres:1234@localhost:5432/lms_db"
export DEMO_PASSWORD="Demo12345"
export CHECKIN_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"

python3 app_postgresql.py
```

---

## 8. Optional: try NFC / QR check-in

With the server running, open a **second** terminal in the project folder, set the same `DEMO_PASSWORD`, and run the reader simulator:

```powershell
$env:DEMO_PASSWORD = "Demo12345"
python tools/reader_sim.py setup                                 # registers 20 readers (rooms C1.100-C1.119) and a card for every demo student
python tools/reader_sim.py class --room C1.100 --users 2-60      # many students tap their cards
```

Check-in only works from 10 minutes before a class to 15 minutes after it starts (for example room C1.100 on Monday 09:00-10:30). To test at any time, start the **server** with a fixed clock:

```powershell
$env:CHECKIN_TEST_NOW = "2026-11-02T09:05:00"
python app_postgresql.py
```

`setup` writes reader keys to `tools/demo_devices.json`. Do not commit that file. Details: [CHECKIN.md](CHECKIN.md).

---

## 9. Optional: share a temporary public link

To show the locally running site to someone else, use a Cloudflare quick tunnel (no account needed):

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:5000
```

It prints a `https://....trycloudflare.com` address that works while both the server and the tunnel are running.

---

## 10. View the interface without PostgreSQL

A mock server with fake in-memory data, for looking at the UI only:

```powershell
python tools/mock_server.py . 5000
```

Logins: `260002@astanait.edu.kz`, `teacher@astanait.edu.kz`, `admin@astanait.edu.kz`, **any password**. It does not run the real rules or SQL. Never run it together with `app_postgresql.py`.

---

## 11. Run the tests

Unit tests do not need PostgreSQL:

```powershell
python -m unittest tools.test_grades tools.test_materials tools.test_submissions tools.test_recovery tools.test_announcements tools.test_mock_announcements
```

---

## 12. Troubleshooting

| Problem | Fix |
|---|---|
| `psql` is not recognized | use the full path, e.g. `& "C:\Program Files\PostgreSQL\18\bin\psql.exe" ...` |
| `password authentication failed for user "postgres"` | the password in `DATABASE_URL` is wrong |
| `database "lms_db" does not exist` | run the create command in section 3 |
| Demo login says the password is wrong | the database was seeded with a different `DEMO_PASSWORD`; recreate `lms_db` (section 3) and restart |
| Any password is accepted | the mock server is running on port 5000; stop it |
| QR code is shown only as text | `segno` is not installed: `pip install segno` |

---

## 13. Project structure

```
app_postgresql.py   main server: database, schema, demo data, login, courses, assignments, timetable, admin
checkin.py          NFC / QR classroom check-in
gradebook.py        grades and grade history
materials.py        course materials (video and resource links)
submissions.py      student submissions as links
static/             frontend (index.html, styles, icons, campus map)
tools/              tests, mock server, reader simulator, asset scripts
docs/PROJECT.md     full project documentation
```

Feature notes: [CHECKIN.md](CHECKIN.md), [GRADES.md](GRADES.md), [MATERIALS.md](MATERIALS.md), [SUBMISSIONS.md](SUBMISSIONS.md).
