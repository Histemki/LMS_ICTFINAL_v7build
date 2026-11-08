"""Simulator of the NFC door reader (stands in for the ESP32 + PN532 in D2). Standard library only.

  python tools/reader_sim.py setup      # admin: register a reader per demo room + link a card to every demo student
  python tools/reader_sim.py tap --room C1.100 --user 2          # one student taps the card on the reader of C1.100
  python tools/reader_sim.py class --room C1.100 --users 2-60    # many students tap, some do not come
  python tools/reader_sim.py heartbeat --room C1.100             # the 5-minute "I am alive" message

Demo card rule: the student with user id N has card UID "04" + N as 6 hex digits (user 2 -> 04000002).
Reader keys are written to tools/demo_devices.json by `setup` (shown by the server only once).
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS_FILE = os.path.join(HERE, "demo_devices.json")
DEMO_ROOMS = [f"C1.{100 + n}" for n in range(20)]  # rooms used by the demo timetable in app_postgresql.py


def card_for(user_id):
    return f"04{user_id:06X}"


def call(server, path, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server + path, data=data, method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def load_keys():
    if not os.path.exists(KEYS_FILE):
        sys.exit("No tools/demo_devices.json - run `setup` first")
    with open(KEYS_FILE) as f:
        return json.load(f)


def reader_headers(keys, room):
    if room not in keys:
        sys.exit(f"No reader key for room {room}")
    return {"X-Device-Id": str(keys[room]["id"]), "X-Device-Key": keys[room]["key"]}


def tap(server, keys, room, user_id):
    return call(server, "/api/checkin", {"method": "nfc", "card_uid": card_for(user_id)}, reader_headers(keys, room))


def cmd_setup(a):
    code, d = call(a.server, "/api/login", {"email": a.admin_email, "password": a.password})
    if code != 200:
        sys.exit(f"Admin login failed: {d.get('error')}")
    auth = {"Authorization": d["token"]}
    keys = json.load(open(KEYS_FILE)) if os.path.exists(KEYS_FILE) else {}
    for room in DEMO_ROOMS:
        code, d = call(a.server, "/api/devices", {"room": room, "name": "Door reader " + room}, auth)
        if code == 201:
            keys[room] = {"id": d["id"], "key": d["key"]}
            print("reader created:", room, "id", d["id"])
        elif code == 409:
            print("reader exists (key is not recoverable):", room)
        else:
            sys.exit(f"{room}: {d.get('error')}")
    with open(KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)
    linked = 0
    for uid in range(2, 2 + a.students):
        code, d = call(a.server, "/api/cards", {"email": f"26{uid:04d}@astanait.edu.kz", "card_uid": card_for(uid)}, auth)
        linked += code == 201
    print(f"cards linked: {linked}; keys saved to {KEYS_FILE}")


def cmd_tap(a):
    code, d = tap(a.server, load_keys(), a.room, a.user)
    print(code, d)


def cmd_heartbeat(a):
    keys = load_keys()
    code, d = call(a.server, "/api/device/heartbeat", {}, reader_headers(keys, a.room))
    print(code, d)


def cmd_class(a):
    keys = load_keys()
    lo, hi = (int(x) for x in a.users.split("-"))
    random.seed(a.seed)
    counts = {}
    for uid in range(lo, hi + 1):
        if random.random() < a.absent:
            continue  # this student does not come
        code, d = tap(a.server, keys, a.room, uid)
        key = d.get("result") or d.get("error", "?")
        counts[key] = counts.get(key, 0) + 1
        time.sleep(a.delay)
    print(counts)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="http://127.0.0.1:5000")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--admin-email", default="admin@astanait.edu.kz")
    s.add_argument("--password", default=os.getenv("DEMO_PASSWORD", "Lms#2026demo"))
    s.add_argument("--students", type=int, default=100)
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("tap")
    s.add_argument("--room", required=True)
    s.add_argument("--user", type=int, required=True)
    s.set_defaults(fn=cmd_tap)
    s = sub.add_parser("heartbeat")
    s.add_argument("--room", required=True)
    s.set_defaults(fn=cmd_heartbeat)
    s = sub.add_parser("class")
    s.add_argument("--room", required=True)
    s.add_argument("--users", default="2-60", help="user id range, e.g. 2-60")
    s.add_argument("--absent", type=float, default=0.12, help="share of students who do not come")
    s.add_argument("--delay", type=float, default=0.0)
    s.add_argument("--seed", type=int, default=1)
    s.set_defaults(fn=cmd_class)
    args = p.parse_args()
    args.fn(args)
