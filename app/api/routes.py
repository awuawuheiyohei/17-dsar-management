"""API 路由 —— DSAR 管理平台

设计原则：所有业务规则判定都下沉到 app/models/* 里的纯函数，
路由层只负责 HTTP 语义（参数解析、404/400/409、审计写入）。
这样每条合规规则都能被单测直接覆盖，不必绕一圈 HTTP。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, HTTPException, Query, Request

from ..models import deadlines as dl
from ..models import orchestration as orch
from ..models import redaction as redact
from ..models import rights as rights_mod
from ..models import verification as vf
from ..models import workflow as wf
from ..models.db import db_connection, row_to_dict, rows_to_dicts, write_audit
from ..models.urlform import as_list, first, parse_urlencoded

router = APIRouter(prefix="/api")

TODAY = lambda: date.today()  # noqa: E731 —— 保持可 monkeypatch

ACTOR_DEFAULT = "anonymous"


def _actor(payload: dict | None = None, query_actor: str | None = None) -> str:
    if payload and payload.get("actor"):
        return str(payload["actor"])[:80]
    if query_actor:
        return str(query_actor)[:80]
    return ACTOR_DEFAULT


def _parse_date(value, field: str) -> date:
    if value in (None, ""):
        raise HTTPException(400, f"{field} is required")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise HTTPException(400, f"{field} must be YYYY-MM-DD, got {value!r}") from exc


def _load_request(conn, request_id: str) -> dict:
    row = conn.execute("SELECT * FROM dsar_requests WHERE request_id = ?", (request_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"request {request_id} not found")
    return row_to_dict(row)


def _identity_verified(conn, request_id: str) -> bool:
    """是否已存在 VERIFIED 的核验记录（取最新一条）"""
    row = conn.execute(
        """SELECT status FROM identity_verifications
           WHERE request_id = ? ORDER BY verification_id DESC LIMIT 1""",
        (request_id,),
    ).fetchone()
    return bool(row) and vf.status_permits_disclosure(row["status"])


def _latest_delivery(conn, request_id: str):
    return conn.execute(
        "SELECT delivery_id FROM response_deliveries WHERE request_id = ? ORDER BY delivered_at DESC LIMIT 1",
        (request_id,),
    ).fetchone()


def _deadline_payload(req: dict, today: date | None = None) -> dict:
    """统一从请求行生成期限快照（与 Dashboard 走同一套判定逻辑）"""
    today = today or TODAY()
    view = dl.build_deadline_view(
        received=req["received_date"],
        jurisdiction=req["jurisdiction"],
        due_date=req["due_date"],
        today=today,
        extended_due_date=req.get("extended_due_date"),
        extension_granted=bool(req.get("extension_granted")),
        clock_stopped_days=req.get("clock_stopped_days", 0),
        request_status=req["status"],
        extension_notified_at=req.get("extension_notified_at"),
    )
    return view.as_dict()


# ============================================
# 健康检查 / 元信息
# ============================================
@router.get("/health")
def health():
    with db_connection() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM dsar_requests").fetchone()["c"]
    return {
        "status": "ok",
        "service": "dsar-management-platform",
        "version": "1.0.0",
        "requests": n,
        "data_notice": "synthetic demonstration data only",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================
# 权利目录 / 法域时限策略
# ============================================
@router.get("/rights")
def list_rights():
    """GDPR 条款 ↔ 请求类型 ↔ CCPA 对应机制（UI 下拉框与文档同源）"""
    return {"count": len(rights_mod.REQUEST_TYPES), "rights": rights_mod.list_rights()}


@router.get("/deadlines/policies")
def deadline_policies():
    """各法域法定时限配置 —— 期限是可配置、可测试的，不是硬编码常量"""
    return {"count": len(dl.POLICIES), "policies": [p.as_dict() for p in dl.POLICIES.values()]}


# ============================================
# Dashboard 总览
# ============================================
@router.get("/overview")
def overview():
    today = TODAY()
    with db_connection() as conn:
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) n FROM dsar_requests GROUP BY status")
        }
        by_type = {
            r["request_type"]: r["n"]
            for r in conn.execute("SELECT request_type, COUNT(*) n FROM dsar_requests GROUP BY request_type")
        }
        by_jurisdiction = {
            r["jurisdiction"]: r["n"]
            for r in conn.execute("SELECT jurisdiction, COUNT(*) n FROM dsar_requests GROUP BY jurisdiction")
        }

        total = conn.execute("SELECT COUNT(*) c FROM dsar_requests").fetchone()["c"]
        open_total = conn.execute(
            f"SELECT COUNT(*) c FROM dsar_requests WHERE status IN "
            f"({','.join('?' * len(wf.OPEN_STATUSES))})", wf.OPEN_STATUSES
        ).fetchone()["c"]

        task_status = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) n FROM discovery_tasks GROUP BY status")
        }
        verify_status = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) n FROM identity_verifications GROUP BY status")
        }

    # 期限判定在 Python 侧算，与 deadlines 模块共用同一套逻辑
    with db_connection() as conn:
        rows = conn.execute("SELECT * FROM dsar_requests").fetchall()
    deadline_buckets = {s: 0 for s in ("ON_TRACK", "DUE_SOON", "DUE_TODAY", "OVERDUE", "CLOSED", "EXTENDED")}
    overdue_ids, due_soon_ids = [], []
    for row in rows_to_dicts(rows):
        view = dl.build_deadline_view(
            received=row["received_date"], jurisdiction=row["jurisdiction"],
            due_date=row["due_date"], today=today,
            extended_due_date=row.get("extended_due_date"),
            extension_granted=bool(row.get("extension_granted")),
            clock_stopped_days=row.get("clock_stopped_days", 0),
            request_status=row["status"],
            extension_notified_at=row.get("extension_notified_at"),
        )
        deadline_buckets[view.status] = deadline_buckets.get(view.status, 0) + 1
        if view.status == "OVERDUE":
            overdue_ids.append({"request_id": row["request_id"], "days_overdue": -view.days_remaining})
        elif view.status in ("DUE_SOON", "DUE_TODAY"):
            due_soon_ids.append({"request_id": row["request_id"], "days_remaining": view.days_remaining})

    overdue_ids.sort(key=lambda x: -x["days_overdue"])
    due_soon_ids.sort(key=lambda x: x["days_remaining"])

    # 按时交付率：已关闭请求中，到期日当天或之前完成的比例
    closed = 0
    on_time = 0
    for row in rows_to_dicts(rows):
        if not wf.is_closed(row["status"]):
            continue
        closed += 1
        closed_at = row.get("closed_at") or row.get("updated_at") or ""
        try:
            if str(closed_at)[:10] <= str(row["due_date"])[:10]:
                on_time += 1
        except (TypeError, ValueError):
            pass
    on_time_rate = round(100.0 * on_time / closed, 1) if closed else 100.0

    return {
        "total_requests": total,
        "open_requests": open_total,
        "by_status": by_status,
        "by_request_type": by_type,
        "by_jurisdiction": by_jurisdiction,
        "deadline_buckets": deadline_buckets,
        "overdue": len(overdue_ids),
        "due_soon": len(due_soon_ids),
        "top_overdue": overdue_ids[:5],
        "top_due_soon": due_soon_ids[:5],
        "on_time_delivery_rate": on_time_rate,
        "closed_requests": closed,
        "discovery_tasks": task_status,
        "verification_status": verify_status,
    }


# ============================================
# 参考数据：数据源 / 类别 / 拒绝理由 / 留存策略
# ============================================
@router.get("/sources")
def list_sources(active_only: bool = True):
    sql = "SELECT * FROM data_sources"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY excluded_by_default, source_id"
    with db_connection() as conn:
        rows = rows_to_dicts(conn.execute(sql).fetchall())
    return {"count": len(rows), "sources": rows}


@router.get("/categories")
def list_categories():
    with db_connection() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM data_categories ORDER BY category_id").fetchall())
    return {"count": len(rows), "categories": rows}


@router.get("/denial-reasons")
def list_denial_reasons(request_type: str | None = None):
    sql = "SELECT * FROM denial_reasons WHERE active = 1"
    args: list = []
    if request_type:
        sql += " AND (',' || applicable_types || ',') LIKE ?"
        args.append(f"%,{request_type.upper()}%")
    sql += " ORDER BY reason_code"
    with db_connection() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    return {"count": len(rows), "reasons": rows}


@router.get("/retention/policies")
def list_retention_policies():
    with db_connection() as conn:
        rows = rows_to_dicts(
            conn.execute("SELECT * FROM retention_policies WHERE active = 1 ORDER BY policy_id").fetchall()
        )
    return {"count": len(rows), "policies": rows}


# ============================================
# 请求者
# ============================================
@router.get("/requesters")
def list_requesters(jurisdiction: str | None = None):
    sql = "SELECT * FROM requesters WHERE 1=1"
    args: list = []
    if jurisdiction:
        sql += " AND jurisdiction = ?"
        args.append(jurisdiction.upper())
    sql += " ORDER BY requester_id"
    with db_connection() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    return {"count": len(rows), "requesters": rows}


@router.get("/requesters/{requester_id}")
def get_requester(requester_id: str):
    with db_connection() as conn:
        row = row_to_dict(
            conn.execute("SELECT * FROM requesters WHERE requester_id = ?", (requester_id,)).fetchone()
        )
        if not row:
            raise HTTPException(404, f"requester {requester_id} not found")
        # 该主体的请求历史 + 数据分布
        row["requests"] = rows_to_dicts(
            conn.execute(
                """SELECT request_id, request_type, status, jurisdiction, received_date, due_date
                   FROM dsar_requests WHERE requester_id = ? ORDER BY received_date DESC""",
                (requester_id,),
            ).fetchall()
        )
        row["data_footprint"] = rows_to_dicts(
            conn.execute(
                """SELECT source_id, COUNT(*) records, SUM(third_party_data) third_party_records
                   FROM data_subject_records WHERE requester_id = ?
                   GROUP BY source_id ORDER BY source_id""",
                (requester_id,),
            ).fetchall()
        )
    return row


# ============================================
# DSAR 请求
# ============================================
@router.get("/requests")
def list_requests(status: str | None = None, request_type: str | None = None,
                  jurisdiction: str | None = None, requester_id: str | None = None,
                  deadline_status: str | None = None, overdue_only: bool = False,
                  search: str | None = None,
                  limit: int = Query(100, ge=1, le=500),
                  offset: int = Query(0, ge=0)):
    sql = """
        SELECT r.*, rq.full_name, rq.email,
               (SELECT status FROM identity_verifications v
                 WHERE v.request_id = r.request_id
                 ORDER BY v.verification_id DESC LIMIT 1) AS verification_status,
               (SELECT COUNT(*) FROM discovery_tasks t
                 WHERE t.request_id = r.request_id
                   AND t.status IN ('PENDING','RUNNING','WAIVED')) AS open_tasks,
               (SELECT COUNT(*) FROM discovery_tasks t
                 WHERE t.request_id = r.request_id) AS total_tasks
        FROM dsar_requests r
        JOIN requesters rq ON rq.requester_id = r.requester_id
        WHERE 1=1
    """
    args: list = []
    if status:
        sql += " AND r.status = ?"
        args.append(status.upper())
    if request_type:
        sql += " AND r.request_type = ?"
        args.append(request_type.upper())
    if jurisdiction:
        sql += " AND r.jurisdiction = ?"
        args.append(jurisdiction.upper())
    if requester_id:
        sql += " AND r.requester_id = ?"
        args.append(requester_id)
    if overdue_only:
        sql += " AND r.status NOT IN (%s)" % ",".join("?" * len(wf.CLOSED))
        args += list(wf.CLOSED)
        sql += " AND date(r.due_date) < date(?)"
        args.append(TODAY().isoformat())
    if search:
        sql += " AND (r.request_id LIKE ? OR rq.full_name LIKE ? OR rq.email LIKE ?)"
        args += [f"%{search}%"] * 3

    sql += " ORDER BY r.received_date DESC, r.request_id LIMIT ? OFFSET ?"
    args += [limit, offset]

    with db_connection() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
        total = conn.execute("SELECT COUNT(*) c FROM dsar_requests").fetchone()["c"]

    today = TODAY()
    for r in rows:
        r["right"] = rights_mod.get_right(r["request_type"]).as_dict() \
            if rights_mod.is_valid_request_type(r["request_type"]) else None
        r["deadline"] = _deadline_payload(r, today=today)

    # deadline_status 过滤需要在算完期限后做（SQL 里表达不了扩展/暂停逻辑）
    if deadline_status:
        wanted = deadline_status.upper()
        rows = [r for r in rows if r["deadline"]["status"] == wanted]

    return {"count": len(rows), "total_requests": total, "requests": rows}


@router.post("/requests")
def create_request(payload: dict = Body(...)):
    """
    受理一个新 DSAR 请求。

    受理即起算期限（Art. 12(3) 从「收到」算起），因此 received_date 可显式传入，
    以便历史数据补录与测试。状态机固定从 RECEIVED 起步。
    """
    requester_id = str(payload.get("requester_id") or "").strip()
    request_type = str(payload.get("request_type") or "").strip().upper()

    if not requester_id:
        raise HTTPException(400, "requester_id is required")
    if not rights_mod.is_valid_request_type(request_type):
        raise HTTPException(
            400, f"invalid request_type: {request_type!r}. valid={list(rights_mod.REQUEST_TYPES)}"
        )

    received = _parse_date(payload.get("received_date"), "received_date") \
        if payload.get("received_date") else TODAY()

    with db_connection() as conn:
        requester = conn.execute(
            "SELECT * FROM requesters WHERE requester_id = ?", (requester_id,)
        ).fetchone()
        if not requester:
            raise HTTPException(404, f"requester {requester_id} not found")

        jurisdiction = str(payload.get("jurisdiction") or requester["jurisdiction"]).upper()
        if not dl.is_valid_jurisdiction(jurisdiction):
            raise HTTPException(400, f"invalid jurisdiction: {jurisdiction!r}. valid={list(dl.POLICIES)}")

        policy = dl.get_policy(jurisdiction)
        due = dl.compute_deadline(received, policy)

        # 顺序生成请求号，避免同一秒内并发受理撞号
        n = conn.execute("SELECT COUNT(*) c FROM dsar_requests").fetchone()["c"] + 1
        request_id = f"DSAR-{n:04d}"
        while conn.execute("SELECT 1 FROM dsar_requests WHERE request_id = ?", (request_id,)).fetchone():
            n += 1
            request_id = f"DSAR-{n:04d}"

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO dsar_requests
               (request_id, requester_id, request_type, jurisdiction, channel, status,
                received_date, due_date, priority, assigned_to, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (request_id, requester_id, request_type, jurisdiction,
             str(payload.get("channel") or "WEB_FORM").upper(), wf.RECEIVED,
             received.isoformat(), due.isoformat(),
             str(payload.get("priority") or "NORMAL").upper(),
             payload.get("assigned_to"), now, now),
        )
        write_audit(conn, _actor(payload), "request_received", "dsar_request", request_id,
                    {"request_type": request_type, "jurisdiction": jurisdiction,
                     "channel": payload.get("channel", "WEB_FORM"),
                     "received_date": received.isoformat(),
                     "due_date": due.isoformat(),
                     "citation": policy.citation},
                    actor_role="DATA_SUBJECT")

        req = _load_request(conn, request_id)

    return {"created": True, "request": req, "deadline": _deadline_payload(req)}


@router.post("/requests/intake")
async def intake_form(request: Request):
    """
    受理隐私门户的 urlencoded 表单提交。

    刻意用 request.body() 手写解析而不是 FastAPI 的 Form()，
    以避免 python-multipart 依赖（见 app/models/urlform.py）。
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    form = parse_urlencoded(body)

    requester_id = first(form.get("requester_id"))
    request_type = first(form.get("request_type"))
    if not requester_id or not request_type:
        raise HTTPException(400, "requester_id and request_type are required")

    payload = {
        "requester_id": requester_id,
        "request_type": request_type,
        "channel": first(form.get("channel"), "WEB_FORM"),
        "jurisdiction": first(form.get("jurisdiction")),
        "received_date": first(form.get("received_date")),
        "priority": first(form.get("priority"), "NORMAL"),
        "actor": first(form.get("actor"), "privacy-portal"),
        "extra_fields": as_list(form.get("notes")),
    }
    result = create_request(payload)
    result["intake_form"] = {"parsed_fields": sorted(form.keys())}
    return result


@router.get("/requests/{request_id}")
def get_request(request_id: str):
    with db_connection() as conn:
        req = _load_request(conn, request_id)
        rq = row_to_dict(
            conn.execute("SELECT * FROM requesters WHERE requester_id = ?",
                         (req["requester_id"],)).fetchone()
        )
        verifications = rows_to_dicts(
            conn.execute(
                "SELECT * FROM identity_verifications WHERE request_id = ? ORDER BY verification_id",
                (request_id,),
            ).fetchall()
        )
        tasks = rows_to_dicts(
            conn.execute(
                """SELECT t.*, s.name source_name, s.system_type, c.name category_name
                   FROM discovery_tasks t
                   JOIN data_sources s ON s.source_id = t.source_id
                   JOIN data_categories c ON c.category_id = t.category_id
                   WHERE t.request_id = ?
                   ORDER BY t.source_id, t.category_id""",
                (request_id,),
            ).fetchall()
        )
        deliveries = rows_to_dicts(
            conn.execute(
                """SELECT delivery_id, format, redaction_mode, record_count, redacted_fields,
                          third_party_masked, recipient, delivered_at, expires_at, status,
                          access_count, purged_at
                   FROM response_deliveries WHERE request_id = ?
                   ORDER BY delivered_at DESC""",
                (request_id,),
            ).fetchall()
        )
        summary = orch.load_summary(conn, request_id)
        audit = rows_to_dicts(
            conn.execute(
                """SELECT audit_id, actor, actor_role, action, details, occurred_at
                   FROM audit_log WHERE entity_id = ? ORDER BY audit_id""",
                (request_id,),
            ).fetchall()
        )

    return {
        "request": req,
        "requester": rq,
        "right": rights_mod.get_right(req["request_type"]).as_dict(),
        "deadline": _deadline_payload(req),
        "status_description": wf.explain_status(req["status"]),
        "allowed_transitions": wf.allowed_transitions(req["status"]),
        "verifications": verifications,
        "discovery_summary": summary,
        "discovery_tasks": tasks,
        "deliveries": deliveries,
        "audit_trail": audit,
    }


# ============================================
# 状态流转
# ============================================
@router.patch("/requests/{request_id}/status")
def transition_request(request_id: str, payload: dict = Body(...)):
    """
    状态机唯一入口。非法跳转与前置条件不满足均返回 400，
    并在 detail 中说明是哪一条规则挡住了 —— 而不是笼统的 "bad request"。
    """
    target = str(payload.get("status", "")).upper()
    actor = _actor(payload)
    if not wf.is_valid_status(target):
        raise HTTPException(400, f"invalid status: {target!r}. valid={list(wf.STATUSES)}")

    with db_connection() as conn:
        req = _load_request(conn, request_id)
        current = req["status"]

        tasks_summary = orch.load_summary(conn, request_id)
        delivery = _latest_delivery(conn, request_id)
        context = {
            "identity_verified": _identity_verified(conn, request_id),
            "open_task_count": tasks_summary["open_count"],
            "denial_reason": payload.get("denial_reason") or payload.get("reason_code"),
            "denial_justification": payload.get("denial_justification") or payload.get("justification"),
            "delivery_id": delivery["delivery_id"] if delivery else None,
            "clarification_sent": current == wf.CLARIFICATION_REQUESTED,
        }

        ok, errors = wf.validate_transition(current, target, context)
        if not ok:
            raise HTTPException(400, {"error": "transition_rejected", "from": current,
                                      "to": target, "reasons": errors})

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        closed_at = now if wf.is_closed(target) else None

        conn.execute(
            """UPDATE dsar_requests
               SET status = ?, closed_at = ?, updated_at = ?,
                   outcome_summary = COALESCE(?, outcome_summary)
               WHERE request_id = ?""",
            (target, closed_at, now, payload.get("note"), request_id),
        )
        write_audit(conn, actor, "status_change", "dsar_request", request_id,
                    {"from": current, "to": target, "note": payload.get("note"),
                     "is_reopen": wf.is_reopen(current, target)},
                    actor_role=str(payload.get("actor_role") or "ANALYST"))

        updated = _load_request(conn, request_id)

    return {
        "updated": True,
        "request": updated,
        "deadline": _deadline_payload(updated),
        "allowed_transitions": wf.allowed_transitions(updated["status"]),
    }


@router.post("/requests/{request_id}/extend")
def extend_request(request_id: str, payload: dict = Body(...)):
    """
    Art. 12(3) 延长。必须提供理由 + 通知日期，且通知日期必须早于原始期限，
    否则不予采信（deadline 仍按原始期限判定）。
    """
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "Art. 12(3) requires a reason for the extension "
                                 "(请求复杂或数量众多)")
    notified_at = _parse_date(payload.get("notified_at"), "notified_at")

    with db_connection() as conn:
        req = _load_request(conn, request_id)
        if req["extension_granted"]:
            raise HTTPException(409, f"{request_id} 已经延长过，{req['jurisdiction']} 法域不支持二次延长")

        policy = dl.get_policy(req["jurisdiction"])
        received = dl._as_date(req["received_date"])
        ok, errors = dl.extension_is_valid(received, policy, notified_at, reason=reason)
        if not ok:
            write_audit(conn, _actor(payload), "extension_rejected", "dsar_request", request_id,
                        {"errors": errors}, actor_role="ANALYST")
            raise HTTPException(400, {"error": "extension_invalid", "reasons": errors,
                                      "citation": policy.citation})

        extended = dl.compute_deadline(received, policy, extensions=1)
        conn.execute(
            """UPDATE dsar_requests
               SET extension_granted = 1, extended_due_date = ?,
                   extension_notified_at = ?, extension_reason = ?, updated_at = datetime('now')
               WHERE request_id = ?""",
            (extended.isoformat(), notified_at.isoformat(), reason, request_id),
        )
        write_audit(conn, _actor(payload), "extension_granted", "dsar_request", request_id,
                    {"reason": reason, "notified_at": notified_at.isoformat(),
                     "new_due_date": extended.isoformat(), "citation": policy.citation},
                    actor_role=str(payload.get("actor_role") or "PRIVACY_OFFICER"))
        updated = _load_request(conn, request_id)

    return {"extended": True, "request": updated,
            "deadline": _deadline_payload(updated)}


@router.post("/requests/{request_id}/clarify")
def request_clarification(request_id: str, payload: dict = Body(...)):
    """
    Art. 12(6) 向数据主体索取补充信息 —— 时钟暂停。

    暂停天数受法域上限约束（EU 默认 60 天），防止无限期挂起请求。
    """
    question = str(payload.get("question") or "").strip()
    if len(question) < 10:
        raise HTTPException(400, "clarification question too short (min 10 chars)")
    days = int(payload.get("days") or 0)

    with db_connection() as conn:
        req = _load_request(conn, request_id)
        current = req["status"]
        ok, errors = wf.validate_transition(current, wf.CLARIFICATION_REQUESTED, {
            "identity_verified": _identity_verified(conn, request_id),
        })
        if not ok:
            raise HTTPException(400, {"error": "clarification_not_allowed",
                                      "from": current, "reasons": errors})

        policy = dl.get_policy(req["jurisdiction"])
        stop_days = days or 30
        valid, msg = dl.clock_stop_is_valid(policy, req.get("clock_stopped_days", 0), stop_days)
        if not valid:
            raise HTTPException(400, {"error": "clock_stop_rejected", "reason": msg})

        conn.execute(
            """UPDATE dsar_requests
               SET status = ?, clock_stopped_days = clock_stopped_days + ?,
                   clarification_rounds = clarification_rounds + 1,
                   updated_at = datetime('now')
               WHERE request_id = ?""",
            (wf.CLARIFICATION_REQUESTED, stop_days, request_id),
        )
        write_audit(conn, _actor(payload), "clarification_requested", "dsar_request", request_id,
                    {"question": question, "clock_stop_days": stop_days,
                     "citation": "GDPR Art. 12(6)"},
                    actor_role=str(payload.get("actor_role") or "ANALYST"))
        updated = _load_request(conn, request_id)

    return {"clarification_requested": True, "question": question,
            "clock_stop_days": stop_days, "request": updated,
            "deadline": _deadline_payload(updated)}


@router.post("/requests/{request_id}/deny")
def deny_request(request_id: str, payload: dict = Body(...)):
    """
    拒绝请求 —— Art. 12(4) 要求告知理由与依据条款。
    拒绝理由必须存在于 denial_reasons 表中（受控词表，避免自由发挥）。
    """
    reason_code = str(payload.get("reason_code") or "").strip()
    justification = str(payload.get("justification") or "").strip()
    approver = str(payload.get("approved_by") or "").strip()

    if not reason_code:
        raise HTTPException(400, "reason_code is required (Art. 12(4) 拒绝必须告知理由)")
    if len(justification) < 10:
        raise HTTPException(400, "justification too short: 必须向数据主体说明拒绝原因（至少 10 字符）")

    with db_connection() as conn:
        req = _load_request(conn, request_id)
        reason = conn.execute(
            "SELECT * FROM denial_reasons WHERE reason_code = ? AND active = 1", (reason_code,)
        ).fetchone()
        if not reason:
            raise HTTPException(404, f"denial reason {reason_code} not found")

        applicable = [t.strip() for t in (reason["applicable_types"] or "").split(",")]
        if applicable and req["request_type"] not in applicable:
            raise HTTPException(400, f"denial reason {reason_code} 不适用于请求类型 "
                                     f"{req['request_type']}（适用：{applicable}）")
        if reason["requires_approval"] and not approver:
            raise HTTPException(400, f"{reason_code} 需要隐私官审批（approved_by 必填）")
        if reason["requires_legal_basis"] and len(justification) < 20:
            raise HTTPException(400, f"{reason_code} 需要更完整的法律依据说明（至少 20 字符）")

        ok, errors = wf.validate_transition(req["status"], wf.DENIED, {
            "denial_reason": reason_code,
            "denial_justification": justification,
        })
        if not ok:
            raise HTTPException(400, {"error": "denial_not_allowed",
                                      "from": req["status"], "reasons": errors})

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary = (f"依据 {reason['regulation_article']}（{reason['title']}）拒绝：{justification}")
        conn.execute(
            """UPDATE dsar_requests
               SET status = ?, closed_at = ?, outcome_summary = ?, updated_at = ?
               WHERE request_id = ?""",
            (wf.DENIED, now, summary, now, request_id),
        )
        # 拒绝后未收敛的检索任务终止。落 EXCLUDED（收敛态）而不是 WAIVED ——
        # WAIVED 在本模型里表示「待人确认的豁免」，属于阻塞态，用它会导致
        # 请求被拒绝后反而永远关不掉。
        conn.execute(
            """UPDATE discovery_tasks SET status = 'EXCLUDED', excluded_reason = ?
               WHERE request_id = ? AND status IN ('PENDING','RUNNING')""",
            (f"请求已被拒绝（{reason['regulation_article']}：{reason['title']}）", request_id),
        )
        write_audit(conn, _actor(payload), "request_denied", "dsar_request", request_id,
                    {"reason_code": reason_code, "article": reason["regulation_article"],
                     "justification": justification, "approved_by": approver or None},
                    actor_role=str(payload.get("actor_role") or "PRIVACY_OFFICER"))
        updated = _load_request(conn, request_id)

    return {"denied": True, "reason_code": reason_code,
            "article": reason["regulation_article"], "request": updated,
            "outcome_summary": summary}


# ============================================
# 身份核验（Art. 12(6)）
# ============================================
@router.post("/requests/{request_id}/verify")
def start_verification(request_id: str, payload: dict = Body(...)):
    """
    发起身份核验。核验方式强度 + 风险信号 → 置信度 → 三段式决策。

    注意：MANUAL_REVIEW 状态**不允许**披露数据，
    必须由隐私官显式复核放行（见 /verify/{verification_id}/approve）。
    """
    method = str(payload.get("method") or "").upper()
    if not vf.is_valid_method(method):
        raise HTTPException(400, f"invalid method: {method!r}. valid={list(vf.METHOD_SCORES)}")
    if method == "NONE":
        raise HTTPException(400, "method 'NONE' 不是有效核验方式")

    signals = payload.get("risk_signals") or []
    if isinstance(signals, str):
        signals = [s.strip() for s in signals.split(",") if s.strip()]
    unknown = [s for s in signals if s not in vf.RISK_SIGNALS]
    if unknown:
        raise HTTPException(400, f"unknown risk signals: {unknown}. valid={list(vf.RISK_SIGNALS)}")

    with db_connection() as conn:
        req = _load_request(conn, request_id)
        assessment = vf.assess_verification(method, signals, req["request_type"])

        started = datetime.now(timezone.utc)
        expires = vf.otp_expiry(started)
        initial = vf.MANUAL_REVIEW if assessment.decision == "MANUAL_REVIEW" else vf.IN_PROGRESS

        cur = conn.execute(
            """INSERT INTO identity_verifications
               (request_id, method, status, attempts, max_attempts, confidence_score,
                decision, risk_signals, justification, started_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (request_id, method, initial, 0, vf.DEFAULT_MAX_ATTEMPTS, assessment.score,
             assessment.decision, json.dumps(signals, ensure_ascii=False),
             "；".join(assessment.reasons), started.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds")),
        )
        verification_id = cur.lastrowid
        write_audit(conn, _actor(payload), "verification_started", "dsar_request", request_id,
                    {"verification_id": verification_id, "method": method,
                     "decision": assessment.decision, "score": assessment.score,
                     "risk_signals": signals},
                    actor_role=str(payload.get("actor_role") or "ANALYST"))
        row = row_to_dict(
            conn.execute("SELECT * FROM identity_verifications WHERE verification_id = ?",
                         (verification_id,)).fetchone()
        )

    return {
        "verification": row,
        "assessment": assessment.as_dict(),
        "explanation": assessment.reasons,
        "disclosure_permitted": assessment.disclosure_permitted,
        "note": ("置信度不足，已转人工复核 —— 在隐私官放行前不得披露任何个人数据"
                 if assessment.decision == "MANUAL_REVIEW" else None),
    }


@router.post("/verify/{verification_id}/submit")
def submit_verification(verification_id: int, payload: dict = Body(...)):
    """提交凭证（OTP / 证件号 / 人工结论）"""
    method_status_note = str(payload.get("note") or "")
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM identity_verifications WHERE verification_id = ?", (verification_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"verification {verification_id} not found")

        attempts = int(row["attempts"] or 0)
        can_try, why = vf.can_attempt(row["status"], attempts, row["max_attempts"] or vf.DEFAULT_MAX_ATTEMPTS)
        if not can_try:
            raise HTTPException(409, why)

        ok, why = vf.check_otp_valid(row["status"], row["expires_at"])
        if not ok:
            raise HTTPException(409, why)

        provided = str(payload.get("evidence_ref") or "").strip()
        approved = bool(payload.get("approved"))

        if row["decision"] == "MANUAL_REVIEW":
            passed, message = vf.manual_review_outcome(row["confidence_score"], approved, method_status_note)
            new_status = vf.APPROVED_STATUS if passed else vf.FAILED
        else:
            # 自动核验：只要凭证提交且时效有效即通过
            new_status = vf.VERIFIED if provided else vf.FAILED
            message = "凭证有效，核验通过" if provided else "未提供凭证，核验失败"

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE identity_verifications
               SET status = ?, attempts = attempts + 1, completed_at = ?,
                   verified_by = COALESCE(?, verified_by), evidence_ref = COALESCE(?, evidence_ref),
                   justification = ?
               WHERE verification_id = ?""",
            (new_status, now, payload.get("verified_by"),
             provided or row["evidence_ref"], message, verification_id),
        )
        write_audit(conn, _actor(payload), "verification_submitted", "dsar_request",
                    row["request_id"],
                    {"verification_id": verification_id, "new_status": new_status,
                     "attempts": attempts + 1, "note": message},
                    actor_role=str(payload.get("actor_role") or "ANALYST"))
        updated = row_to_dict(
            conn.execute("SELECT * FROM identity_verifications WHERE verification_id = ?",
                         (verification_id,)).fetchone()
        )

    return {"verification": updated, "message": message,
            "disclosure_permitted": vf.status_permits_disclosure(updated["status"])}


@router.post("/verify/{verification_id}/approve")
def approve_verification(verification_id: int, payload: dict = Body(...)):
    """隐私官人工复核放行 —— 必须填写依据说明（审计要求）"""
    note = str(payload.get("note") or "").strip()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM identity_verifications WHERE verification_id = ?", (verification_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"verification {verification_id} not found")
        if row["status"] != vf.MANUAL_REVIEW:
            raise HTTPException(409, f"只有 MANUAL_REVIEW 可复核，当前状态 {row['status']}")

        passed, message = vf.manual_review_outcome(row["confidence_score"], True, note)
        if not passed:
            raise HTTPException(400, message)

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE identity_verifications
               SET status = ?, completed_at = ?, verified_by = ?, justification = ?
               WHERE verification_id = ?""",
            (vf.VERIFIED, now, payload.get("verified_by") or "privacy-officer", message,
             verification_id),
        )
        write_audit(conn, _actor(payload), "verification_approved", "dsar_request",
                    row["request_id"],
                    {"verification_id": verification_id, "note": note, "message": message},
                    actor_role="PRIVACY_OFFICER")
        updated = row_to_dict(
            conn.execute("SELECT * FROM identity_verifications WHERE verification_id = ?",
                         (verification_id,)).fetchone()
        )

    return {"verification": updated, "message": message,
            "disclosure_permitted": vf.status_permits_disclosure(updated["status"])}


# ============================================
# 跨系统检索编排
# ============================================
@router.post("/requests/{request_id}/discover")
def orchestrate_discovery(request_id: str, payload: dict = Body(...)):
    """
    按 request × source × category 生成检索任务计划（幂等）。

    重复调用不会产生重复任务 —— 幂等性由 schema 里的
    UNIQUE(request_id, source_id, category_id) + 确定性 task_id 保证。
    """
    category_ids = payload.get("category_ids") or []
    if isinstance(category_ids, str):
        category_ids = [c.strip() for c in category_ids.split(",") if c.strip()]

    with db_connection() as conn:
        req = _load_request(conn, request_id)
        if not _identity_verified(conn, request_id):
            raise HTTPException(409, "身份核验未通过（Art. 12(6)）：在核验 VERIFIED 前不得检索个人数据")

        if category_ids:
            placeholders = ",".join("?" * len(category_ids))
            categories = rows_to_dicts(
                conn.execute(
                    f"SELECT * FROM data_categories WHERE category_id IN ({placeholders})",
                    category_ids,
                ).fetchall()
            )
            found = {c["category_id"] for c in categories}
            missing = [c for c in category_ids if c not in found]
            if missing:
                raise HTTPException(404, f"unknown categories: {missing}")
        else:
            categories = rows_to_dicts(
                conn.execute("SELECT * FROM data_categories ORDER BY category_id").fetchall()
            )

        sources = rows_to_dicts(
            conn.execute("SELECT * FROM data_sources WHERE active = 1").fetchall()
        )
        result = orch.upsert_tasks(conn, request_id, sources, categories, req["request_type"])
        summary = orch.load_summary(conn, request_id)
        write_audit(conn, _actor(payload), "discovery_orchestrated", "dsar_request", request_id,
                    {**result, "request_type": req["request_type"],
                     "action": orch.action_for(req["request_type"])},
                    actor_role=str(payload.get("actor_role") or "ANALYST"))

    return {"orchestrated": True, **result, "summary": summary,
            "action": orch.action_for(req["request_type"]),
            "action_label": orch.explain_action(orch.action_for(req["request_type"]))}


@router.get("/requests/{request_id}/tasks")
def list_tasks(request_id: str, status: str | None = None):
    sql = """
        SELECT t.*, s.name source_name, s.system_type, s.sla_hours, s.owner_team,
               c.name category_name, c.sensitivity
        FROM discovery_tasks t
        JOIN data_sources s ON s.source_id = t.source_id
        JOIN data_categories c ON c.category_id = t.category_id
        WHERE t.request_id = ?
    """
    args: list = [request_id]
    if status:
        sql += " AND t.status = ?"
        args.append(status.upper())
    sql += " ORDER BY t.source_id, t.category_id"

    with db_connection() as conn:
        if not conn.execute("SELECT 1 FROM dsar_requests WHERE request_id = ?", (request_id,)).fetchone():
            raise HTTPException(404, f"request {request_id} not found")
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
        summary = orch.load_summary(conn, request_id)

    return {"count": len(rows), "tasks": rows, "summary": summary}


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: dict = Body(...)):
    """更新单个检索任务的状态（幂等性外的唯一变更入口，全程审计）"""
    target = str(payload.get("status") or "").upper()
    record_count = int(payload.get("record_count") or 0)
    excluded_reason = str(payload.get("excluded_reason") or "")

    with db_connection() as conn:
        row = conn.execute("SELECT * FROM discovery_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"task {task_id} not found")

        ok, errors = orch.validate_task_transition(row["status"], target, record_count, excluded_reason)
        if not ok:
            raise HTTPException(400, {"error": "task_transition_rejected",
                                      "from": row["status"], "to": target, "reasons": errors})

        now = datetime.now(timezone.utc)
        duration = None
        if row["started_at"]:
            try:
                started = datetime.fromisoformat(row["started_at"])
                duration = int((now - started).total_seconds() * 1000)
            except ValueError:
                duration = None

        conn.execute(
            """UPDATE discovery_tasks
               SET status = ?, record_count = ?,
                   started_at = COALESCE(started_at, ?),
                   completed_at = ?, duration_ms = ?,
                   error_note = ?, excluded_reason = COALESCE(?, excluded_reason)
               WHERE task_id = ?""",
            (target, record_count, now.isoformat(timespec="seconds"),
             now.isoformat(timespec="seconds") if orch.is_resolved(target) else None,
             duration, payload.get("error_note"), excluded_reason or None, task_id),
        )
        write_audit(conn, _actor(payload), "task_updated", "discovery_task", task_id,
                    {"request_id": row["request_id"], "from": row["status"], "to": target,
                     "record_count": record_count, "excluded_reason": excluded_reason or None},
                    actor_role=str(payload.get("actor_role") or "SYSTEM"))
        updated = row_to_dict(conn.execute("SELECT * FROM discovery_tasks WHERE task_id = ?",
                                           (task_id,)).fetchone())
        summary = orch.load_summary(conn, row["request_id"])

    return {"updated": True, "task": updated, "summary": summary}


# ============================================
# 响应交付（脱敏 + 留存）
# ============================================
@router.post("/requests/{request_id}/response")
def build_response(request_id: str, payload: dict = Body(...)):
    """
    生成响应副本：跨源取记录 → 脱敏 → 落库并设置到期销毁日。

    必须已核验身份。响应本身含个人数据，所以必须有留存期限 ——
    这是数据最小化在 DSAR 场景里最容易被忽略的一环。
    """
    mode = str(payload.get("redaction_mode") or "STANDARD").upper()
    if mode not in redact.REDACTION_MODES:
        raise HTTPException(400, f"invalid redaction_mode: {mode!r}. valid={list(redact.REDACTION_MODES)}")
    if mode == redact.NONE:
        raise HTTPException(400, "redaction_mode=NONE 不允许对外交付（Art. 5(1)(c) 数据最小化）")

    fmt = str(payload.get("format") or "JSON").upper()
    if fmt not in ("JSON", "CSV", "SECURE_LINK"):
        raise HTTPException(400, f"invalid format: {fmt!r}. valid=['JSON','CSV','SECURE_LINK']")

    with db_connection() as conn:
        req = _load_request(conn, request_id)
        if not _identity_verified(conn, request_id):
            raise HTTPException(409, "身份核验未通过（Art. 12(6)）：不得生成个人数据副本")

        summary = orch.load_summary(conn, request_id)
        if summary["blocking"] and summary["open_count"] > 0:
            # 允许带警告地生成（响应里注明尚未收敛），但不阻断 —— 部分场景下先给已有数据
            pass

        rows = conn.execute(
            """SELECT r.*, s.name source_name
               FROM data_subject_records r
               JOIN data_sources s ON s.source_id = r.source_id
               WHERE r.requester_id = ? ORDER BY r.source_id, r.category_id""",
            (req["requester_id"],),
        ).fetchall()
        records = [json.loads(x["payload"]) for x in rows]
        payload_json, manifest = build_manifest(records, rows, mode)

        policy_row = conn.execute(
            "SELECT retention_days FROM retention_policies WHERE artifact_type = 'RESPONSE_PAYLOAD'"
        ).fetchone()
        keep_days = policy_row["retention_days"] if policy_row else 30
        delivered = datetime.now(timezone.utc)
        expires = delivered + timedelta(days=keep_days)

        seq = conn.execute("SELECT COUNT(*) c FROM response_deliveries WHERE request_id = ?",
                           (request_id,)).fetchone()["c"] + 1
        delivery_id = orch.make_delivery_id(request_id, seq)

        conn.execute(
            """INSERT INTO response_deliveries
               (delivery_id, request_id, format, redaction_mode, record_count,
                redacted_fields, third_party_masked, payload, manifest, recipient,
                delivered_at, expires_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (delivery_id, request_id, fmt, mode, manifest["record_count"],
             manifest["redacted_field_count"], manifest["third_party_removal_count"],
             payload_json, json.dumps(manifest, ensure_ascii=False),
             payload.get("recipient"), delivered.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds"), "ACTIVE"),
        )
        write_audit(conn, _actor(payload), "response_delivered", "response_delivery", delivery_id,
                    {"request_id": request_id, "records": manifest["record_count"],
                     "redacted_fields": manifest["redacted_field_count"],
                     "third_party_removed": manifest["third_party_removal_count"],
                     "retention_days": keep_days, "mode": mode},
                    actor_role=str(payload.get("actor_role") or "ANALYST"))
        row = row_to_dict(
            conn.execute("SELECT * FROM response_deliveries WHERE delivery_id = ?",
                         (delivery_id,)).fetchone()
        )

    return {"delivery": {k: v for k, v in row.items() if k != "payload"},
            "manifest": manifest,
            "discovery_summary": summary,
            "note": (f"响应含个人数据，将在 {expires.date().isoformat()} "
                     f"（+{keep_days} 天，{manifest.get('retention_basis', '')}）后自动销毁")}


def build_manifest(records: list, rows, mode: str) -> tuple[str, dict]:
    """脱敏 + 生成交付摘要"""
    payload_json, manifest = redact.build_payload_json(records, mode=mode)
    by_source: dict[str, int] = {}
    for x in rows:
        sid = x["source_id"]
        by_source[sid] = by_source.get(sid, 0) + 1
    manifest["by_source"] = by_source
    manifest["retention_basis"] = "Art. 5(1)(e) 存储限制"
    return payload_json, manifest


@router.get("/responses")
def list_responses(status: str | None = None, active_only: bool = False):
    sql = """SELECT d.*, r.request_type, r.jurisdiction
             FROM response_deliveries d
             JOIN dsar_requests r ON r.request_id = d.request_id WHERE 1=1"""
    args: list = []
    if status:
        sql += " AND d.status = ?"
        args.append(status.upper())
    if active_only:
        sql += " AND d.status = 'ACTIVE'"
    sql += " ORDER BY d.delivered_at DESC"
    with db_connection() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    for r in rows:
        r.pop("payload", None)  # 列表接口不返回完整载荷（Art. 5(1)(c)）
    return {"count": len(rows), "deliveries": rows}


@router.post("/retention/purge")
def purge_expired(actor: str = Body("system", embed=True)):
    """
    销毁到期响应 —— 数据最小化的执行点。

    不做物理删除式的内容留白：payload 置空 + 状态置 PURGED + 记录 purged_at，
    这样审计链仍然能证明「这份数据曾经交付过、已按期销毁」。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db_connection() as conn:
        due = conn.execute(
            """SELECT delivery_id, request_id, record_count FROM response_deliveries
               WHERE status = 'ACTIVE' AND expires_at IS NOT NULL AND expires_at < ?""",
            (now,),
        ).fetchall()
        purged = []
        for row in due:
            conn.execute(
                """UPDATE response_deliveries
                   SET status = 'PURGED', payload = '', purged_at = ?, access_count = access_count
                   WHERE delivery_id = ?""",
                (now, row["delivery_id"]),
            )
            write_audit(conn, actor, "response_purged", "response_delivery", row["delivery_id"],
                        {"request_id": row["request_id"], "records": row["record_count"],
                         "reason": "retention period elapsed (Art. 5(1)(e))"},
                        actor_role="SYSTEM")
            purged.append(row["delivery_id"])
        conn.commit()

    return {"purged_count": len(purged), "purged": purged}


# ============================================
# 审计
# ============================================
@router.get("/audit")
def list_audit(action: str | None = None, entity_id: str | None = None,
               limit: int = Query(200, ge=1, le=1000)):
    sql = "SELECT * FROM audit_log WHERE 1=1"
    args: list = []
    if action:
        sql += " AND action = ?"
        args.append(action)
    if entity_id:
        sql += " AND entity_id = ?"
        args.append(entity_id)
    sql += " ORDER BY audit_id DESC LIMIT ?"
    args.append(limit)
    with db_connection() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    for r in rows:
        try:
            r["details"] = json.loads(r["details"]) if r.get("details") else {}
        except (TypeError, json.JSONDecodeError):
            r["details"] = {"raw": r.get("details")}
    return {"count": len(rows), "entries": rows}


# ============================================
# 合规汇总（管理层视角）
# ============================================
@router.get("/compliance")
def compliance_summary():
    """
    时限达成情况 —— 这是隐私团队对管理层最常被问的一组数字。
    口径：已关闭请求在 due_date 当日或之前关闭的占比。
    """
    today = TODAY()
    with db_connection() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM dsar_requests").fetchall())
        by_source = rows_to_dicts(
            conn.execute(
                """SELECT s.source_id, s.name, COUNT(t.task_id) tasks,
                          SUM(CASE WHEN t.status='COMPLETED' THEN 1 ELSE 0 END) completed,
                          SUM(CASE WHEN t.status='EXCLUDED' THEN 1 ELSE 0 END) excluded
                   FROM discovery_tasks t JOIN data_sources s ON s.source_id = t.source_id
                   GROUP BY s.source_id ORDER BY s.source_id"""
            ).fetchall()
        )
        reason_rows = rows_to_dicts(
            conn.execute(
                "SELECT reason_code, regulation_article, title FROM denial_reasons WHERE active = 1"
            ).fetchall()
        )
        denied = conn.execute(
            "SELECT COUNT(*) c FROM dsar_requests WHERE status = 'DENIED'"
        ).fetchone()["c"]

    per_jurisdiction: dict[str, dict] = {}
    for r in rows:
        view = dl.build_deadline_view(
            received=r["received_date"], jurisdiction=r["jurisdiction"],
            due_date=r["due_date"], today=today,
            extended_due_date=r.get("extended_due_date"),
            extension_granted=bool(r.get("extension_granted")),
            clock_stopped_days=r.get("clock_stopped_days", 0),
            request_status=r["status"],
            extension_notified_at=r.get("extension_notified_at"),
        )
        bucket = per_jurisdiction.setdefault(r["jurisdiction"], {
            "citation": view.citation, "total": 0, "closed": 0, "on_time": 0, "overdue": 0,
        })
        bucket["total"] += 1
        if view.status == "OVERDUE":
            bucket["overdue"] += 1
        if wf.is_closed(r["status"]):
            bucket["closed"] += 1
            closed_at = str(r.get("closed_at") or r.get("updated_at") or "")[:10]
            if closed_at and closed_at <= str(r["due_date"])[:10]:
                bucket["on_time"] += 1

    for bucket in per_jurisdiction.values():
        bucket["on_time_rate"] = round(100.0 * bucket["on_time"] / bucket["closed"], 1) \
            if bucket["closed"] else 100.0

    return {
        "as_of": today.isoformat(),
        "by_jurisdiction": per_jurisdiction,
        "denied_requests": denied,
        "available_denial_reasons": reason_rows,
        "discovery_by_source": by_source,
        "note": ("时限口径：Art. 12(3) 自收到日起算；延长须在原期限内通知数据主体，"
                 "否则不予采信。统计仅基于本平台内的合成演示数据。"),
    }
