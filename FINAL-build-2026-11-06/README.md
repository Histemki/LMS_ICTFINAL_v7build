# LMS университета (редизайн Moodle)

Учебная платформа для студентов, преподавателей и администратора: курсы, задания и сдача работ ссылкой, оценки,
материалы курсов, расписание, объявления, заявки, отметка присутствия (NFC и QR), карта университета.
Flask + PostgreSQL, фронтенд без сборки (`static/index.html`).

## Запуск
```bash
pip install -r requierments.txt
python app_postgresql.py
```
Нужна пустая база PostgreSQL и переменная `DATABASE_URL` (по умолчанию `postgresql://postgres:YOUR_PASSWORD@localhost:5432/lms_db`).
Сайт: <http://127.0.0.1:5000>. Демо-логины и пароль: [docs/PROJECT.md, раздел 1](docs/PROJECT.md#1-быстрый-старт).

Посмотреть интерфейс без базы данных:
```bash
python tools/mock_server.py . 5000
```

## Где что
Подробная документация: **[docs/PROJECT.md](docs/PROJECT.md)** (структура, роли, API, база данных, фронтенд, оформление, тесты, как добавить функцию).

Заметки по отдельным функциям: [CHECKIN.md](CHECKIN.md), [GRADES.md](GRADES.md), [MATERIALS.md](MATERIALS.md), [SUBMISSIONS.md](SUBMISSIONS.md).

## Тесты
```bash
python -m unittest tools.test_grades tools.test_materials tools.test_submissions tools.test_recovery tools.test_announcements tools.test_mock_announcements
```
