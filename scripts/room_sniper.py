"""feishu-room-sniper：盯飞书会议室，目标时段一有空房立刻预订，到截止时间自动停。

用法:
  python3 room_sniper.py 2026-10-22T15:00:00+08:00 2026-10-22T16:00:00+08:00 \
      [--keyword 南楼] [--deadline 2026-10-22T14:59:59+08:00] [--summary 会议] \
      [--city 北京] [--building 学清嘉创大厦B座] [--floor F2] \
      [--interval 15] [--dry-run]

默认截止时间 = 时段开始前 1 秒；轮询间隔最小 15 秒。
需要先完成 lark-cli 用户授权（日历域），脚本全程以用户身份操作。
日志写在脚本同目录 room_sniper.log。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "room_sniper.log")
ENV = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 GBK，避免中文输出报错
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg):
    line = f"{datetime.now(TZ):%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def cli(*args):
    p = subprocess.run(["lark-cli", *args, "--as", "user"], capture_output=True, text=True, env=ENV, timeout=60)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "raw": (p.stdout + p.stderr)[-500:]}


def find(obj, key):
    """递归找第一个 key 的值。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find(v, key)
            if r is not None:
                return r
    return None


def find_rooms(slot, kwargs):
    """查一个时段的可用会议室，返回 (rooms, rate_limited)。"""
    args = ["calendar", "+room-find", "--slot", slot]
    for k in ("city", "building", "floor"):
        if kwargs.get(k):
            args += [f"--{k}", kwargs[k]]
    d = cli(*args)
    rooms = []
    for t in (d.get("data") or {}).get("time_slots", []):
        rooms += t.get("meeting_rooms", [])
    kw = kwargs.get("keyword")
    if kw:
        rooms = [r for r in rooms if kw in r.get("room_name", "")]
    return sorted(rooms, key=lambda r: r.get("capacity") or 999), not d.get("ok")


def room_status(event_id, room_id):
    """查会议室 RSVP 状态。"""
    d = cli("calendar", "event.attendees", "list",
            "--params", json.dumps({"calendar_id": "primary", "event_id": event_id}))
    for a in find(d, "items") or []:
        if a.get("room_id") == room_id:
            return a.get("rsvp_status")
    return None


def try_book(room, summary, slot):
    """创建只带会议室的日程并确认回执；被拒则删掉返回 False 继续盯。"""
    d = cli("calendar", "+create", "--summary", summary,
            "--start", slot.split("~")[0], "--end", slot.split("~")[1],
            "--attendee-ids", room["room_id"])
    log(f"create {room['room_name']}: {json.dumps(d, ensure_ascii=False)[:600]}")
    event_id = find(d, "event_id")
    if not d.get("ok") or not event_id:
        return False
    # 会议室接受/拒绝是异步的，等几秒确认
    for _ in range(6):
        time.sleep(3)
        st = room_status(event_id, room["room_id"])
        log(f"  会议室状态: {st}")
        if st == "accept":
            return True
        if st == "decline":
            cli("calendar", "events", "delete", "--params",
                json.dumps({"calendar_id": "primary", "event_id": event_id}))
            log("  被拒，已删除该日程，继续盯")
            return False
    log("  会议室状态未确认（可能需审批），保留日程，停止盯守")
    return True


def parse_dt(s):
    return datetime.fromisoformat(s).astimezone(TZ)


def main():
    ap = argparse.ArgumentParser(description="盯飞书会议室，一有空房立刻预订")
    ap.add_argument("start", help="ISO 8601 开始时间，如 2026-10-22T15:00:00+08:00")
    ap.add_argument("end", help="ISO 8601 结束时间")
    ap.add_argument("--keyword", default="", help="会议室名过滤关键词，如 南楼；空=不过滤")
    ap.add_argument("--deadline", help="盯守截止 ISO 8601 时间；默认时段开始前 1 秒")
    ap.add_argument("--summary", default="会议", help="日程标题")
    ap.add_argument("--city", help="城市，仅当明确时传给 room-find")
    ap.add_argument("--building", help="楼宇，仅当明确时传给 room-find")
    ap.add_argument("--floor", help="楼层，归一化后传给 room-find，如 F2")
    ap.add_argument("--interval", type=int, default=15, help="轮询间隔秒数，默认 15，最小 15")
    ap.add_argument("--dry-run", action="store_true", help="只报告空房，不预订")
    args = ap.parse_args()

    slot = f"{args.start}~{args.end}"
    deadline = parse_dt(args.deadline) if args.deadline else parse_dt(args.start) - timedelta(seconds=1)
    interval = max(args.interval, 15)
    if deadline <= datetime.now(TZ):
        log("❌ 截止时间已过，直接退出")
        return 2

    where = f" 含{args.keyword!r}" if args.keyword else ""
    log(f"开始盯守 {parse_dt(args.start):%m-%d %H:%M}-{parse_dt(args.end):%H:%M}{where}，"
        f"截止 {deadline:%m-%d %H:%M:%S}，间隔 {interval}s{'（dry-run）' if args.dry_run else ''}")
    dry = args.dry_run
    while datetime.now(TZ) < deadline:
        rooms, limited = find_rooms(slot, vars(args))
        if not rooms:
            log("本轮无空房")
        for room in rooms:
            log(f"发现空房: {room['room_name']}")
            if not dry and try_book(room, args.summary, slot):
                log(f"✅ 已订到 {room['room_name']}")
                return 0
        if dry:
            log("dry-run：只查一轮，结束")
            return 0
        # 触发限流（190010）时拉长本轮等待，避免连续撞限
        time.sleep(interval * 3 if limited else interval)
    log("❌ 截止时间到，没抢到")
    return 1


if __name__ == "__main__":
    sys.exit(main())
