#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发抢座演示：多个线程同时抢同一个座位的同一时段，验证只有一个人成功。

用法（先启动服务端）：

    python server.py                # 另开一个窗口
    python tools/race_demo.py       # 默认抢 16 次，指向 http://127.0.0.1:8000

可选参数：

    python tools/race_demo.py --base http://127.0.0.1:8000 --threads 16 --room 3
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta


def call(base: str, path: str, payload=None, method: str = "GET"):
    url = base.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="并发抢座演示")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="服务端地址")
    parser.add_argument("--threads", type=int, default=16, help="并发线程数，默认 16")
    parser.add_argument("--room", type=int, default=3, help="自习室 id，默认 3")
    parser.add_argument("--slot", type=int, default=8, help="时段序号（0=08:00），默认 8")
    args = parser.parse_args(argv)

    target_day = (date.today() + timedelta(days=1)).isoformat()
    status, seats = call(args.base, "/api/rooms/%d/seats?date=%s" % (args.room, target_day))
    if status != 200:
        print("读取座位失败：%s %s" % (status, seats))
        return 1

    free = [s for s in seats["seats"] if s["state"] == "free"]
    if not free:
        print("该自习室明天没有完全空闲的座位，换个 --room 试试")
        return 1
    seat = free[0]

    slot = seats["slots"][args.slot]
    print("目标：%s 自习室 %s 座位，%s 的 %s-%s" % (
        seats["room"]["name"], seat["label"], target_day, slot["start"], slot["end"]))
    print("并发：%d 个线程同时提交预约\n" % args.threads)

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(args.threads)

    def worker(index: int):
        payload = {
            "userId": "2024%03d" % index,
            "userName": "抢座同学%02d" % index,
            "seatId": seat["id"],
            "date": target_day,
            "startSlot": args.slot,
            "endSlot": args.slot + 1,
        }
        barrier.wait()
        status, body = call(args.base, "/api/reservations", payload, "POST")
        with lock:
            results.append(status)
            code = body.get("code", "OK") if isinstance(body, dict) else "?"
            print("  线程 %02d → %s %s" % (index, status, code))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    success = results.count(201)
    conflict = results.count(409)
    print("\n结果：成功 %d 个，冲突被拒 %d 个，其他 %d 个" % (
        success, conflict, len(results) - success - conflict))

    status, after = call(args.base, "/api/rooms/%d/seats?date=%s" % (args.room, target_day))
    row = next(s for s in after["seats"] if s["id"] == seat["id"])
    booked = args.slot in row["booked"]
    print("核对数据库：该时段%s被占用" % ("已" if booked else "未"))

    ok = success == 1 and conflict == args.threads - 1 and booked
    print("结论：%s" % ("并发控制正确，只有一个同学预约成功" if ok else "出现异常，请检查"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    sys.exit(main())
