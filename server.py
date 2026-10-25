#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园自习座位预约系统 —— 零依赖服务端。

只使用 Python 标准库（http.server + sqlite3），不需要安装任何第三方包。

启动：
    python server.py                  # http://127.0.0.1:8000
    python server.py --port 8080      # 换端口
    python server.py --db data/x.db   # 换数据库文件

并发安全由数据库唯一约束保证，而不是靠前端判断：
同一个座位 + 同一天 + 同一个时段，在 reservation_slots 表里只能存在一行，
因此多人同时抢同一座位时，只有一个人的事务能提交成功，其余会收到 409。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import uuid
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DB_PATH = BASE_DIR / "data" / "seats.db"

# ---- 业务常量 -------------------------------------------------------------
DAY_START_HOUR = 8          # 每天 08:00 开放
DAY_END_HOUR = 22           # 每天 22:00 闭馆
SLOT_MINUTES = 60           # 1 个时段 = 1 小时
SLOT_COUNT = (DAY_END_HOUR - DAY_START_HOUR) * 60 // SLOT_MINUTES   # 14 个时段
BOOKING_DAYS = 7            # 可预约今天起 7 天
MAX_BODY = 64 * 1024

USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 进程内串行化写入事务；正确的兜底仍然是对数据库本身的唯一约束
BOOKING_LOCK = threading.Lock()

DB_PATH = DEFAULT_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    building    TEXT    NOT NULL,
    floor       INTEGER NOT NULL DEFAULT 1,
    description TEXT    NOT NULL DEFAULT '',
    rows        INTEGER NOT NULL,
    cols        INTEGER NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seats (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    label   TEXT    NOT NULL,
    row     INTEGER NOT NULL,
    col     INTEGER NOT NULL,
    kind    TEXT    NOT NULL DEFAULT 'normal',
    UNIQUE(room_id, label)
);

CREATE TABLE IF NOT EXISTS reservations (
    id           TEXT    PRIMARY KEY,
    user_id      TEXT    NOT NULL,
    user_name    TEXT    NOT NULL,
    seat_id      INTEGER NOT NULL REFERENCES seats(id),
    date         TEXT    NOT NULL,
    start_slot   INTEGER NOT NULL,
    end_slot     INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'confirmed',
    created_at   TEXT    NOT NULL,
    cancelled_at TEXT
);

-- 一个座位在同一天同一时段只能被一条预约占用：这就是防冲突的核心约束
CREATE TABLE IF NOT EXISTS reservation_slots (
    seat_id        INTEGER NOT NULL REFERENCES seats(id),
    date           TEXT    NOT NULL,
    slot           INTEGER NOT NULL,
    reservation_id TEXT    NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    user_id        TEXT    NOT NULL,
    PRIMARY KEY (seat_id, date, slot)
);

-- 同一个人在同一时段也不能占用两个座位
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_slot
    ON reservation_slots(user_id, date, slot);
CREATE INDEX IF NOT EXISTS idx_slots_reservation
    ON reservation_slots(reservation_id);
CREATE INDEX IF NOT EXISTS idx_reservations_user
    ON reservations(user_id, date);
"""


# ---- 时间工具 -------------------------------------------------------------
def now_local() -> datetime:
    return datetime.now().replace(microsecond=0)


def slot_start(day: str, slot: int) -> datetime:
    base = datetime.combine(date.fromisoformat(day), time(DAY_START_HOUR, 0))
    return base + timedelta(minutes=SLOT_MINUTES * slot)


def slot_text(slot: int) -> tuple[str, str]:
    start = slot_start("2000-01-01", slot)
    end = start + timedelta(minutes=SLOT_MINUTES)
    return start.strftime("%H:%M"), end.strftime("%H:%M")


def first_bookable_slot(day: str, moment: datetime):
    """返回该日期第一个还能预约的时段下标；已过期或已闭馆返回 None。"""
    today = moment.date()
    target = date.fromisoformat(day)
    if target < today:
        return None
    if target > today:
        return 0
    opening = datetime.combine(today, time(DAY_START_HOUR, 0))
    if moment <= opening:
        return 0
    closing = datetime.combine(today, time(DAY_END_HOUR, 0))
    if moment >= closing:
        return None
    elapsed = (moment - opening).total_seconds() / (SLOT_MINUTES * 60)
    slot = int(elapsed)
    if elapsed > slot:          # 当前时段已经开始，只能约下一个整点
        slot += 1
    return slot if slot < SLOT_COUNT else None


def day_slot_states(day: str, moment: datetime):
    first = first_bookable_slot(day, moment)
    states = []
    for i in range(SLOT_COUNT):
        start, end = slot_text(i)
        states.append({
            "index": i,
            "start": start,
            "end": end,
            "bookable": first is not None and i >= first,
        })
    return states


# ---- 数据库 ---------------------------------------------------------------
def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def seat_label(row: int, col: int) -> str:
    return "%s-%02d" % (chr(ord("A") + row - 1), col)


def seed_rooms(conn: sqlite3.Connection) -> None:
    rooms = [
        # 名称, 楼栋, 楼层, 说明, 行, 列, 靠窗列, 有电源列
        ("A 区自习室 · 静音", "图书馆", 3, "全程静音，适合深度学习", 6, 8, (1, 8), (4, 5)),
        ("B 区自习室 · 研讨", "图书馆", 3, "可低声交流，允许使用键盘", 6, 8, (1, 8), (3, 6)),
        ("A201 自习室", "第一教学楼", 2, "课程间隙首选，座位紧凑", 5, 6, (1,), (2, 3)),
        ("共享自习室", "东区宿舍 1 号楼", 1, "开放到 22:00，离宿舍最近", 4, 5, (5,), (1,)),
    ]
    for order, (name, building, floor, desc, rows, cols, windows, powers) in enumerate(rooms):
        cur = conn.execute(
            "INSERT INTO rooms (name, building, floor, description, rows, cols, sort_order)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, building, floor, desc, rows, cols, order),
        )
        room_id = cur.lastrowid
        for r in range(1, rows + 1):
            for c in range(1, cols + 1):
                if c in windows:
                    kind = "window"
                elif c in powers:
                    kind = "power"
                else:
                    kind = "normal"
                conn.execute(
                    "INSERT INTO seats (room_id, label, row, col, kind) VALUES (?, ?, ?, ?, ?)",
                    (room_id, seat_label(r, c), r, c, kind),
                )


def seed_demo_reservations(conn: sqlite3.Connection) -> None:
    """放两条演示预约，方便打开页面就能看到"已预约"的红色座位。"""
    moment = now_local()
    today = moment.date()
    plans = []
    first_today = first_bookable_slot(today.isoformat(), moment)
    if first_today is not None and first_today + 2 <= SLOT_COUNT:
        plans.append((today.isoformat(), first_today, first_today + 2, "演示同学·李"))
    tomorrow = (today + timedelta(days=1)).isoformat()
    plans.append((tomorrow, 1, 4, "演示同学·王"))
    plans.append((tomorrow, 5, 7, "演示同学·赵"))

    for order, (day, start, end, name) in enumerate(plans):
        seat = conn.execute(
            "SELECT id FROM seats WHERE room_id = (SELECT MIN(id) FROM rooms)"
            " ORDER BY row, col LIMIT 1 OFFSET ?",
            (2 + order * 7,),
        ).fetchone()
        if seat is None:
            continue
        rid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO reservations (id, user_id, user_name, seat_id, date, start_slot, end_slot, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)",
            (rid, "demo-2024001", name, seat["id"], day, start, end, moment.isoformat()),
        )
        for slot in range(start, end):
            conn.execute(
                "INSERT INTO reservation_slots (seat_id, date, slot, reservation_id, user_id) VALUES (?, ?, ?, ?, ?)",
                (seat["id"], day, slot, rid, "demo-2024001"),
            )


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        empty = conn.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()["n"] == 0
        if empty:
            conn.execute("BEGIN IMMEDIATE")
            try:
                seed_rooms(conn)
                seed_demo_reservations(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


# ---- 业务逻辑 -------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "ERROR", extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.extra = extra or {}


def build_meta(moment: datetime) -> dict:
    conn = connect(DB_PATH)
    try:
        rooms = conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM seats s WHERE s.room_id = r.id) AS seat_count"
            " FROM rooms r ORDER BY r.sort_order, r.id"
        ).fetchall()
    finally:
        conn.close()

    today = moment.date()
    dates = []
    for offset in range(BOOKING_DAYS):
        d = today + timedelta(days=offset)
        dates.append({
            "date": d.isoformat(),
            "label": "%d 月 %d 日" % (d.month, d.day),
            "weekday": "周" + "一二三四五六日"[d.weekday()],
            "isToday": offset == 0,
        })

    return {
        "serverTime": moment.isoformat(),
        "today": today.isoformat(),
        "slotCount": SLOT_COUNT,
        "dayStartHour": DAY_START_HOUR,
        "dayEndHour": DAY_END_HOUR,
        "slotMinutes": SLOT_MINUTES,
        "bookingDays": BOOKING_DAYS,
        "dates": dates,
        "rooms": [
            {
                "id": r["id"],
                "name": r["name"],
                "building": r["building"],
                "floor": r["floor"],
                "description": r["description"],
                "rows": r["rows"],
                "cols": r["cols"],
                "seatCount": r["seat_count"],
            }
            for r in rooms
        ],
    }


def room_seats_payload(room_id: int, day: str, user_id: str, moment: datetime) -> dict:
    conn = connect(DB_PATH)
    try:
        room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if room is None:
            raise ApiError(404, "自习室不存在", "ROOM_NOT_FOUND")
        seats = conn.execute(
            "SELECT * FROM seats WHERE room_id = ? ORDER BY row, col", (room_id,)
        ).fetchall()
        rows = conn.execute(
            "SELECT seat_id, slot, user_id FROM reservation_slots WHERE date = ? AND seat_id IN"
            " (SELECT id FROM seats WHERE room_id = ?)",
            (day, room_id),
        ).fetchall()
    finally:
        conn.close()

    booked: dict[int, list[int]] = {}
    mine: dict[int, list[int]] = {}
    for row in rows:
        booked.setdefault(row["seat_id"], []).append(row["slot"])
        if user_id and row["user_id"] == user_id:
            mine.setdefault(row["seat_id"], []).append(row["slot"])

    slot_states = day_slot_states(day, moment)
    bookable = {s["index"] for s in slot_states if s["bookable"]}

    seat_payload = []
    for seat in seats:
        seat_booked = sorted(booked.get(seat["id"], []))
        seat_mine = sorted(mine.get(seat["id"], []))
        free_bookable = [s for s in range(SLOT_COUNT) if s in bookable and s not in set(seat_booked)]
        if not bookable:
            state = "closed"
        elif not free_bookable:
            state = "full"
        elif len(free_bookable) == len(bookable):
            state = "free"
        else:
            state = "partial"
        seat_payload.append({
            "id": seat["id"],
            "label": seat["label"],
            "row": seat["row"],
            "col": seat["col"],
            "kind": seat["kind"],
            "state": state,
            "booked": seat_booked,
            "mine": seat_mine,
            "freeCount": len(free_bookable),
        })

    return {
        "room": {
            "id": room["id"],
            "name": room["name"],
            "building": room["building"],
            "floor": room["floor"],
            "description": room["description"],
            "rows": room["rows"],
            "cols": room["cols"],
        },
        "date": day,
        "serverTime": moment.isoformat(),
        "slots": slot_states,
        "seats": seat_payload,
    }


def reservation_status(row, moment: datetime) -> str:
    if row["status"] == "cancelled":
        return "cancelled"
    start = slot_start(row["date"], row["start_slot"])
    end = slot_start(row["date"], row["end_slot"])
    if moment < start:
        return "upcoming"
    if moment < end:
        return "ongoing"
    return "finished"


def reservation_payload(row, moment: datetime) -> dict:
    status = reservation_status(row, moment)
    start = slot_start(row["date"], row["start_slot"])
    end = slot_start(row["date"], row["end_slot"])
    return {
        "id": row["id"],
        "userId": row["user_id"],
        "userName": row["user_name"],
        "seatId": row["seat_id"],
        "seatLabel": row["seat_label"],
        "roomId": row["room_id"],
        "roomName": row["room_name"],
        "building": row["building"],
        "date": row["date"],
        "startSlot": row["start_slot"],
        "endSlot": row["end_slot"],
        "timeRange": "%s - %s" % (start.strftime("%H:%M"), end.strftime("%H:%M")),
        "status": status,
        "createdAt": row["created_at"],
        "cancelledAt": row["cancelled_at"],
        "cancellable": status == "upcoming",
    }


def list_reservations(user_id: str, moment: datetime) -> list[dict]:
    conn = connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT res.*, s.label AS seat_label, s.room_id AS room_id,"
            "       r.name AS room_name, r.building AS building"
            " FROM reservations res"
            " JOIN seats s ON s.id = res.seat_id"
            " JOIN rooms r ON r.id = s.room_id"
            " WHERE res.user_id = ?"
            " ORDER BY res.date, res.start_slot",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    items = [reservation_payload(row, moment) for row in rows]
    order = {"ongoing": 0, "upcoming": 1, "finished": 2, "cancelled": 3}
    items.sort(key=lambda it: (order[it["status"]], it["date"], it["startSlot"]))
    return items


def validate_booking(payload: dict, moment: datetime) -> dict:
    user_id = str(payload.get("userId") or "").strip()
    user_name = str(payload.get("userName") or "").strip()
    if not USER_ID_RE.match(user_id):
        raise ApiError(400, "学号格式不正确（3-20 位字母、数字、下划线或连字符）", "BAD_USER")
    if not user_name:
        raise ApiError(400, "请填写姓名", "BAD_USER")
    if len(user_name) > 20:
        raise ApiError(400, "姓名过长", "BAD_USER")

    try:
        seat_id = int(payload.get("seatId"))
    except (TypeError, ValueError):
        raise ApiError(400, "座位参数不正确", "BAD_SEAT")

    day = str(payload.get("date") or "").strip()
    if not DATE_RE.match(day):
        raise ApiError(400, "日期格式应为 YYYY-MM-DD", "BAD_DATE")
    try:
        target = date.fromisoformat(day)
    except ValueError:
        raise ApiError(400, "日期不存在", "BAD_DATE")
    if target < moment.date():
        raise ApiError(400, "不能预约过去的日期", "BAD_DATE")
    if target > moment.date() + timedelta(days=BOOKING_DAYS - 1):
        raise ApiError(400, "最多只能预约未来 %d 天" % BOOKING_DAYS, "BAD_DATE")

    try:
        start_slot = int(payload.get("startSlot"))
        end_slot = int(payload.get("endSlot"))
    except (TypeError, ValueError):
        raise ApiError(400, "时段参数不正确", "BAD_SLOT")
    if not (0 <= start_slot < end_slot <= SLOT_COUNT):
        raise ApiError(400, "时段范围不合法", "BAD_SLOT")

    first = first_bookable_slot(day, moment)
    if first is None:
        raise ApiError(400, "当天已闭馆，无法预约", "DAY_CLOSED")
    if start_slot < first:
        raise ApiError(400, "该时段已开始，请选择更晚的时段", "SLOT_STARTED")

    return {
        "user_id": user_id,
        "user_name": user_name,
        "seat_id": seat_id,
        "date": day,
        "start_slot": start_slot,
        "end_slot": end_slot,
    }


def create_reservation(payload: dict, moment: datetime) -> dict:
    data = validate_booking(payload, moment)
    conn = connect(DB_PATH)
    try:
        seat = conn.execute(
            "SELECT s.*, r.name AS room_name FROM seats s JOIN rooms r ON r.id = s.room_id WHERE s.id = ?",
            (data["seat_id"],),
        ).fetchone()
        if seat is None:
            raise ApiError(404, "座位不存在", "SEAT_NOT_FOUND")

        with BOOKING_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            try:
                taken = conn.execute(
                    "SELECT slot FROM reservation_slots WHERE seat_id = ? AND date = ?"
                    " AND slot >= ? AND slot < ? ORDER BY slot",
                    (data["seat_id"], data["date"], data["start_slot"], data["end_slot"]),
                ).fetchall()
                if taken:
                    raise ApiError(
                        409,
                        "该时段已被他人预约，请换一个时段或座位",
                        "SLOT_TAKEN",
                        {"conflicts": [r["slot"] for r in taken]},
                    )

                overlap = conn.execute(
                    "SELECT slot FROM reservation_slots WHERE user_id = ? AND date = ?"
                    " AND slot >= ? AND slot < ? ORDER BY slot",
                    (data["user_id"], data["date"], data["start_slot"], data["end_slot"]),
                ).fetchall()
                if overlap:
                    raise ApiError(
                        409,
                        "你在该时段已经有其他座位的预约",
                        "USER_OVERLAP",
                        {"conflicts": [r["slot"] for r in overlap]},
                    )

                rid = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO reservations (id, user_id, user_name, seat_id, date, start_slot,"
                    " end_slot, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)",
                    (rid, data["user_id"], data["user_name"], data["seat_id"], data["date"],
                     data["start_slot"], data["end_slot"], moment.isoformat()),
                )
                conn.executemany(
                    "INSERT INTO reservation_slots (seat_id, date, slot, reservation_id, user_id)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [(data["seat_id"], data["date"], slot, rid, data["user_id"])
                     for slot in range(data["start_slot"], data["end_slot"])],
                )
                conn.execute("COMMIT")
            except ApiError:
                conn.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise ApiError(409, "该时段刚被他人抢占，请重新选择", "SLOT_TAKEN", {"detail": str(exc)})
            except Exception:
                conn.execute("ROLLBACK")
                raise

        row = conn.execute(
            "SELECT res.*, s.label AS seat_label, s.room_id AS room_id,"
            "       r.name AS room_name, r.building AS building"
            " FROM reservations res JOIN seats s ON s.id = res.seat_id"
            " JOIN rooms r ON r.id = s.room_id WHERE res.id = ?",
            (rid,),
        ).fetchone()
        return reservation_payload(row, moment)
    finally:
        conn.close()


def cancel_reservation(reservation_id: str, user_id: str, moment: datetime) -> dict:
    if not USER_ID_RE.match(user_id or ""):
        raise ApiError(400, "缺少用户标识", "BAD_USER")
    conn = connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT res.*, s.label AS seat_label, s.room_id AS room_id,"
            "       r.name AS room_name, r.building AS building"
            " FROM reservations res JOIN seats s ON s.id = res.seat_id"
            " JOIN rooms r ON r.id = s.room_id WHERE res.id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ApiError(404, "预约不存在", "RESERVATION_NOT_FOUND")
        if row["user_id"] != user_id:
            raise ApiError(403, "只能取消自己的预约", "NOT_OWNER")
        if row["status"] == "cancelled":
            raise ApiError(400, "该预约已取消", "ALREADY_CANCELLED")
        if moment >= slot_start(row["date"], row["start_slot"]):
            raise ApiError(400, "预约已开始或已结束，无法取消", "ALREADY_STARTED")

        with BOOKING_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE reservations SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                    (moment.isoformat(), reservation_id),
                )
                conn.execute(
                    "DELETE FROM reservation_slots WHERE reservation_id = ?", (reservation_id,)
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        updated = conn.execute(
            "SELECT res.*, s.label AS seat_label, s.room_id AS room_id,"
            "       r.name AS room_name, r.building AS building"
            " FROM reservations res JOIN seats s ON s.id = res.seat_id"
            " JOIN rooms r ON r.id = s.room_id WHERE res.id = ?",
            (reservation_id,),
        ).fetchone()
        return reservation_payload(updated, moment)
    finally:
        conn.close()


# ---- HTTP 层 --------------------------------------------------------------
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class SeatHandler(BaseHTTPRequestHandler):
    server_version = "CampusSeat/1.0"
    protocol_version = "HTTP/1.1"

    # -- 工具 --
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str, code: str = "ERROR", extra=None):
        payload = {"error": message, "code": code}
        if extra:
            payload.update(extra)
        self._send_json(payload, status)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "请求体长度不合法", "BAD_REQUEST")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError(413, "请求体过大", "TOO_LARGE")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "请求体不是合法 JSON", "BAD_JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体格式不正确", "BAD_JSON")
        return data

    def serve_static(self, path: str):
        if path in ("/", ""):
            rel = "index.html"
        elif path == "/favicon.ico":
            # 浏览器默认会请求 /favicon.ico，直接复用自带图标，避免控制台 404
            rel = "favicon.svg"
        else:
            rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self._send_error_json(404, "文件不存在", "NOT_FOUND")
            return
        if not target.is_file():
            self._send_error_json(404, "文件不存在", "NOT_FOUND")
            return
        data = target.read_bytes()
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # -- 路由 --
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if not parsed.path.startswith("/api/"):
                self.serve_static(parsed.path)
                return

            moment = now_local()
            if parsed.path == "/api/health":
                self._send_json({"ok": True, "serverTime": moment.isoformat()})
                return
            if parsed.path == "/api/meta":
                self._send_json(build_meta(moment))
                return
            if parsed.path == "/api/reservations":
                user_id = (query.get("userId") or [""])[0]
                if not USER_ID_RE.match(user_id):
                    raise ApiError(400, "缺少或非法的用户标识", "BAD_USER")
                self._send_json({"reservations": list_reservations(user_id, moment)})
                return

            match = re.fullmatch(r"/api/rooms/(\d+)/seats", parsed.path)
            if match:
                day = (query.get("date") or [moment.date().isoformat()])[0]
                if not DATE_RE.match(day):
                    raise ApiError(400, "日期格式应为 YYYY-MM-DD", "BAD_DATE")
                user_id = (query.get("userId") or [""])[0]
                payload = room_seats_payload(int(match.group(1)), day, user_id, moment)
                self._send_json(payload)
                return

            raise ApiError(404, "接口不存在", "NOT_FOUND")
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message, exc.code, exc.extra)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # 客户端提前断开（关页面 / 切网络 / 刷新），不必回写也不再记一条 500
            pass
        except Exception as exc:                      # noqa: BLE001 - 兜底，避免连接悬挂
            if os.environ.get("APP_DEBUG"):
                sys.stderr.write("DEBUG 未预期异常 %s: %r\n" % (type(exc).__name__, exc))
            self._send_error_json(500, "服务器内部错误：%s" % exc, "INTERNAL")

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if not parsed.path.startswith("/api/"):
                raise ApiError(404, "接口不存在", "NOT_FOUND")
            moment = now_local()
            payload = self._read_json()

            if parsed.path == "/api/reservations":
                created = create_reservation(payload, moment)
                self._send_json({"reservation": created}, 201)
                return

            match = re.fullmatch(r"/api/reservations/([0-9a-fA-F]{32})/cancel", parsed.path)
            if match:
                cancelled = cancel_reservation(
                    match.group(1).lower(), str(payload.get("userId") or "").strip(), moment
                )
                self._send_json({"reservation": cancelled})
                return

            raise ApiError(404, "接口不存在", "NOT_FOUND")
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message, exc.code, exc.extra)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # 客户端提前断开（关页面 / 切网络 / 刷新），不必回写也不再记一条 500
            pass
        except Exception as exc:                      # noqa: BLE001
            if os.environ.get("APP_DEBUG"):
                sys.stderr.write("DEBUG 未预期异常 %s: %r\n" % (type(exc).__name__, exc))
            self._send_error_json(500, "服务器内部错误：%s" % exc, "INTERNAL")


class SeatServer(ThreadingHTTPServer):
    """连接断开是常态（用户关页面、切网络），不打印整段堆栈吓到使用者。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            sys.stderr.write("[%s] 客户端已断开连接：%s\n" % (
                datetime.now().strftime("%H:%M:%S"), client_address[0]))
            return
        super().handle_error(request, client_address)


def main(argv=None) -> int:
    global DB_PATH
    parser = argparse.ArgumentParser(description="校园自习座位预约系统")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", 8000)))
    parser.add_argument("--db", default=os.environ.get("APP_DB", str(DEFAULT_DB_PATH)))
    args = parser.parse_args(argv)

    DB_PATH = Path(args.db).expanduser().resolve()
    init_db(DB_PATH)

    server = SeatServer((args.host, args.port), SeatHandler)
    print("校园自习座位预约系统已启动")
    print("  访问地址： http://%s:%d" % (args.host, args.port))
    print("  数据库：   %s" % DB_PATH)
    print("  按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
