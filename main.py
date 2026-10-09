"""CLI 入口

用法：
    python main.py serve              # 启动 FastAPI（端口 5054）
    python main.py seed               # 仅重置演示数据
    python main.py stats              # 打印请求分布 + 时限桶统计
    python main.py deadline DSAR-0001 # 解释某条请求的法定时限判定
    python main.py rights             # 打印 GDPR 条款 ↔ 请求类型对照表
"""
from __future__ import annotations

import argparse
import json
from datetime import date


def cmd_serve(args):
    from app.main import run
    print(f"[+] Dashboard: http://{args.host}:{args.port}/")
    print(f"[+] API docs : http://{args.host}:{args.port}/docs")
    run(host=args.host, port=args.port, reload=args.reload)


def cmd_seed(args):
    from app.models.seed import seed_all
    print(json.dumps(seed_all(verbose=True), ensure_ascii=False, indent=2))


def cmd_stats(args):
    from app.models import deadlines as dl
    from app.models import workflow as wf
    from app.models.db import db_connection
    with db_connection() as conn:
        rows = conn.execute("SELECT * FROM dsar_requests ORDER BY request_id").fetchall()

    buckets: dict[str, int] = {}
    today = date.today()
    print(f"{'REQUEST':<12}{'TYPE':<20}{'JURIS':<6}{'STATUS':<14}{'DUE':<12}{'LEFT':>6}  DEADLINE")
    print("-" * 92)
    for r in rows:
        view = dl.build_deadline_view(
            received=r["received_date"], jurisdiction=r["jurisdiction"],
            due_date=r["due_date"], today=today,
            extended_due_date=r["extended_due_date"],
            extension_granted=bool(r["extension_granted"]),
            clock_stopped_days=r["clock_stopped_days"],
            request_status=r["status"],
            extension_notified_at=r["extension_notified_at"],
        )
        buckets[view.status] = buckets.get(view.status, 0) + 1
        left = "—" if view.status == "CLOSED" else f"{view.days_remaining}"
        print(f"{r['request_id']:<12}{r['request_type']:<20}{r['jurisdiction']:<6}"
              f"{r['status']:<14}{view.effective_due_date.isoformat():<12}{left:>6}  {view.status}")

    print("\n时限分布:", json.dumps(buckets, ensure_ascii=False))
    total = sum(buckets.values())
    if total:
        closed = sum(buckets.get(s, 0) for s in wf.CLOSED)
        print(f"请求总数 {total} · 已关闭 {closed} · 进行中 {total - closed}")


def cmd_deadline(args):
    from app.models import deadlines as dl
    from app.models.db import db_connection
    with db_connection() as conn:
        r = conn.execute(
            "SELECT * FROM dsar_requests WHERE request_id = ?", (args.request_id,)
        ).fetchone()
    if not r:
        print(f"[!] request {args.request_id} not found")
        return
    view = dl.build_deadline_view(
        received=r["received_date"], jurisdiction=r["jurisdiction"],
        due_date=r["due_date"], today=date.today(),
        extended_due_date=r["extended_due_date"],
        extension_granted=bool(r["extension_granted"]),
        clock_stopped_days=r["clock_stopped_days"],
        request_status=r["status"],
        extension_notified_at=r["extension_notified_at"],
    )
    print(json.dumps(view.as_dict(), ensure_ascii=False, indent=2))
    print("\n" + view.explanation)
    for w in view.warnings:
        print(f"[!] {w}")


def cmd_rights(args):
    from app.models.rights import list_rights
    print(f"{'CODE':<22}{'GDPR':<10}{'CCPA 对应':<40}NAME")
    print("-" * 110)
    for r in list_rights():
        print(f"{r['code']:<22}{r['gdpr_article']:<10}{r['ccpa_equivalent'][:38]:<40}{r['name_zh']}")


def main():
    parser = argparse.ArgumentParser(description="DSAR Management Platform")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="启动 FastAPI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5054)
    p.add_argument("--reload", action="store_true")

    sub.add_parser("seed", help="重置演示数据")
    sub.add_parser("stats", help="请求分布与时限统计")
    sub.add_parser("rights", help="GDPR 条款对照表")

    p = sub.add_parser("deadline", help="解释某条请求的法定时限判定")
    p.add_argument("request_id")

    args = parser.parse_args()
    dispatch = {
        "serve": cmd_serve, "seed": cmd_seed,
        "deadline": cmd_deadline, "stats": cmd_stats, "rights": cmd_rights,
    }
    dispatch.get(getattr(args, "command", None) or "stats", cmd_stats)(args)


if __name__ == "__main__":
    main()
