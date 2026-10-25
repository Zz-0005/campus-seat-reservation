#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：起一个临时数据库的服务端，把核心流程跑一遍。

用法：
    python tests/test_booking.py

覆盖：自习室/座位结构、预约成功、重复预约被拒、跨座位时段互斥、
本人取消释放座位、越权取消、非法参数、以及 16 人同时抢同一座位只成功 1 人。
"""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASSED.append(name)
        print("  PASS  %s" % name)
    else:
        FAILED.append((name, detail))
        print("  FAIL  %s  %s" % (name, detail))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(base: str, path: str, payload=None, method: str = "GET"):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def wait_health(base: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = call(base, "/api/health")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    tmpdir = tempfile.mkdtemp(prefix="seat-test-")
    db = str(Path(tmpdir) / "test.db")

    print("启动测试服务器 %s（数据库 %s）" % (base, db))
    server = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port), "--db", db],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if not wait_health(base):
            print("服务端启动失败")
            return 1

        print("\n[1] 基础结构与页面")
        status, meta = call(base, "/api/meta")
        check("GET /api/meta 返回 200", status == 200, str(status))
        check("至少 3 个自习室", len(meta["rooms"]) >= 3, str(len(meta["rooms"])))
        check("每天 14 个时段（08:00-22:00）", meta["slotCount"] == 14, str(meta["slotCount"]))
        check("可选日期 7 天", len(meta["dates"]) == 7, str(len(meta["dates"])))

        room = meta["rooms"][0]
        status, seats = call(base, "/api/rooms/%d/seats?date=%s" % (room["id"], meta["today"]))
        check("座位图接口返回 200", status == 200, str(status))
        check("座位数 = 行 × 列",
              len(seats["seats"]) == room["rows"] * room["cols"],
              "%d vs %d" % (len(seats["seats"]), room["rows"] * room["cols"]))
        check("每个座位带 booked/mine 字段",
              all("booked" in s and "mine" in s and "state" in s for s in seats["seats"]))

        status, page = call(base, "/")
        check("首页可访问且含标题", status == 200 and "校园自习座位预约系统" in page)
        status, _ = call(base, "/app.js")
        check("静态资源 app.js 可访问", status == 200, str(status))
        status, _ = call(base, "/favicon.ico")
        check("favicon.ico 不再 404（Issue #5）", status == 200, str(status))
        status, _ = call(base, "/favicon.svg")
        check("favicon.svg 可访问", status == 200, str(status))
        check("首页声明了页面图标", 'rel="icon"' in page)
        status, _ = call(base, "/../server.py")
        check("禁止目录穿越", status in (403, 404), str(status))

        print("\n[2] 正常预约")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        user_a = {"userId": "2024001", "userName": "李明"}
        user_b = {"userId": "2024002", "userName": "王芳"}
        seat_a = seats["seats"][10]["id"]
        seat_b = seats["seats"][11]["id"]

        status, created = call(base, "/api/reservations", {
            **user_a, "seatId": seat_a, "date": tomorrow, "startSlot": 10, "endSlot": 12,
        }, "POST")
        check("预约成功返回 201", status == 201, "%s %s" % (status, created))
        reservation_a = created.get("reservation", {}) if isinstance(created, dict) else {}
        check("预约状态为未开始", reservation_a.get("status") == "upcoming", str(reservation_a.get("status")))
        check("时段文案正确", reservation_a.get("timeRange") == "18:00 - 20:00", str(reservation_a.get("timeRange")))

        status, seats_now = call(base, "/api/rooms/%d/seats?date=%s&userId=2024001" % (room["id"], tomorrow))
        seat_row = next(s for s in seats_now["seats"] if s["id"] == seat_a)
        check("座位已显示为被占", seat_row["booked"] == [10, 11], str(seat_row["booked"]))
        check("本人的占用标记为 mine", seat_row["mine"] == [10, 11], str(seat_row["mine"]))

        print("\n[3] 冲突与互斥")
        status, conflict = call(base, "/api/reservations", {
            **user_b, "seatId": seat_a, "date": tomorrow, "startSlot": 11, "endSlot": 13,
        }, "POST")
        check("他人抢同一座位被拒（409）", status == 409, str(status))
        check("错误码为 SLOT_TAKEN", isinstance(conflict, dict) and conflict.get("code") == "SLOT_TAKEN", str(conflict))

        status, overlap = call(base, "/api/reservations", {
            **user_a, "seatId": seat_b, "date": tomorrow, "startSlot": 11, "endSlot": 13,
        }, "POST")
        check("同一人同时段约另一座位被拒（409）", status == 409, str(status))
        check("错误码为 USER_OVERLAP", isinstance(overlap, dict) and overlap.get("code") == "USER_OVERLAP", str(overlap))

        status, second = call(base, "/api/reservations", {
            **user_b, "seatId": seat_b, "date": tomorrow, "startSlot": 10, "endSlot": 12,
        }, "POST")
        check("他人约其他空闲座位成功", status == 201, str(status))

        status, adjacent = call(base, "/api/reservations", {
            **user_b, "seatId": seat_a, "date": tomorrow, "startSlot": 12, "endSlot": 13,
        }, "POST")
        check("相邻但不相交的时段可以续约", status == 201, str(status))

        print("\n[4] 取消规则")
        status, forbidden = call(base, "/api/reservations/%s/cancel" % reservation_a["id"],
                                 {"userId": "2024002"}, "POST")
        check("不能取消别人的预约（403）", status == 403, str(status))

        status, cancelled = call(base, "/api/reservations/%s/cancel" % reservation_a["id"],
                                 {"userId": "2024001"}, "POST")
        check("取消自己的未开始预约成功", status == 200, str(status))
        check("状态变为 cancelled", cancelled.get("reservation", {}).get("status") == "cancelled")
        check("取消后不可再次取消", call(base, "/api/reservations/%s/cancel" % reservation_a["id"],
                                        {"userId": "2024001"}, "POST")[0] == 400)

        status, seats_after = call(base, "/api/rooms/%d/seats?date=%s" % (room["id"], tomorrow))
        seat_row = next(s for s in seats_after["seats"] if s["id"] == seat_a)
        check("取消后座位时段回到空闲", 10 not in seat_row["booked"], str(seat_row["booked"]))

        status, _ = call(base, "/api/reservations", {
            **user_a, "seatId": seat_a, "date": tomorrow, "startSlot": 10, "endSlot": 11,
        }, "POST")
        check("取消释放的时段可被别人预约", status == 201, str(status))

        print("\n[5] 非法参数")
        cases = [
            ("时段倒序", {**user_a, "seatId": seat_a, "date": tomorrow, "startSlot": 5, "endSlot": 5}),
            ("时段越界", {**user_a, "seatId": seat_a, "date": tomorrow, "startSlot": 0, "endSlot": 15}),
            ("过去日期", {**user_a, "seatId": seat_a, "date": "2020-01-01", "startSlot": 0, "endSlot": 1}),
            ("超出预约窗口", {**user_a, "seatId": seat_a,
                          "date": (date.today() + timedelta(days=30)).isoformat(), "startSlot": 0, "endSlot": 1}),
            ("学号非法", {"userId": "a!", "userName": "张三", "seatId": seat_a, "date": tomorrow,
                       "startSlot": 0, "endSlot": 1}),
            ("座位不存在", {**user_a, "seatId": 999999, "date": tomorrow, "startSlot": 0, "endSlot": 1}),
        ]
        for label, payload in cases:
            status, _ = call(base, "/api/reservations", payload, "POST")
            check("拒绝%s（4xx）" % label, 400 <= status < 500, str(status))

        print("\n[6] 并发抢座：16 个请求同时抢同一座位同一时段")
        race_seat = seats["seats"][20]["id"]
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def worker(index: int):
            payload = {
                "userId": "race%03d" % index,
                "userName": "抢座同学%d" % index,
                "seatId": race_seat,
                "date": tomorrow,
                "startSlot": 8,
                "endSlot": 9,
            }
            barrier.wait()
            status, _ = call(base, "/api/reservations", payload, "POST")
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        success = results.count(201)
        conflicts = results.count(409)
        check("只有 1 个请求成功", success == 1, "成功 %d 次，状态分布 %s" % (success, sorted(results)))
        check("其余 15 个请求被拒", conflicts == 15, "409 数量 %d" % conflicts)

        status, seats_final = call(base, "/api/rooms/%d/seats?date=%s" % (room["id"], tomorrow))
        seat_row = next(s for s in seats_final["seats"] if s["id"] == race_seat)
        check("数据库里该时段只有 1 条占用", seat_row["booked"] == [8], str(seat_row["booked"]))

        print("\n[7] 个人预约列表")
        status, mine = call(base, "/api/reservations?userId=2024001")
        check("查询本人预约列表成功", status == 200, str(status))
        check("列表里能区分状态",
              all(r["status"] in ("upcoming", "ongoing", "finished", "cancelled")
                  for r in mine["reservations"]))

        print("\n[8] 客户端异常断开")
        # 发出半截请求后强制 RST，模拟用户直接关页面 / 切网络（Issue #4）
        for _ in range(2):
            sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            time.sleep(0.1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()
        time.sleep(0.5)
        status, health = call(base, "/api/health")
        check("异常断开后服务仍然可用", status == 200 and health.get("ok") is True, str(status))
        status, seats_after = call(base, "/api/rooms/%d/seats?date=%s" % (room["id"], tomorrow))
        check("异常断开后数据仍然可读", status == 200 and len(seats_after["seats"]) > 0, str(status))

        total = len(PASSED) + len(FAILED)
        print("\n" + "=" * 56)
        print("通过 %d / %d" % (len(PASSED), total))
        if FAILED:
            for name, detail in FAILED:
                print("  失败：%s %s" % (name, detail))
            return 1
        print("全部通过")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
