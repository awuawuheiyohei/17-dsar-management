"""跨系统数据检索编排

一个数据主体不会只存在于一个系统里。真实场景下「删除」这一个动作，
往往要同时落到 CRM、HRIS、订单、分析仓库、营销平台等 6-8 个系统，
其中 1-2 个可能因为留存义务被法定排除。本模块把这些固化成任务编排：

    plan = build_plan(sources, categories, request_type)
    → 每个 (source, category) 生成一个 discovery_task，带源级 SLA

关键约束（也是 workflow.GUARDS 的依据）：
    open_task_count == 0 才允许把请求标记为 COMPLETED
    —— 检索没跑完就说「已完成」，在审计和监管视角都是虚假陈述。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# ============================================
# 任务状态
# ============================================
PENDING = "PENDING"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
NOT_FOUND = "NOT_FOUND"
ERROR = "ERROR"
EXCLUDED = "EXCLUDED"
WAIVED = "WAIVED"

TASK_STATUSES = (PENDING, RUNNING, COMPLETED, NOT_FOUND, ERROR, EXCLUDED, WAIVED)

# 收敛态：不再阻塞请求完成
RESOLVED_STATUSES = (COMPLETED, NOT_FOUND, ERROR, EXCLUDED, WAIVED)
# 阻塞态：必须处理完才能关闭请求
OPEN_STATUSES = (PENDING, RUNNING, WAIVED)

TERMINAL_STATUSES = RESOLVED_STATUSES

# 任务终态里「真正完成了业务动作」的状态
SUCCESSFUL_STATUSES = (COMPLETED, NOT_FOUND)

# ============================================
# 各请求类型在子任务上的动作
# ============================================
ACTIONS = {
    "ACCESS": "COLLECT",
    "PORTABILITY": "COLLECT",
    "RECTIFICATION": "RECTIFY",
    "ERASURE": "ERASE",
    "RESTRICTION": "RESTRICT",
    "OBJECTION": "CONFIRM",
    "AUTOMATED_DECISION": "CONFIRM",
}

ACTION_LABELS = {
    "COLLECT": "采集并导出",
    "ERASE": "执行删除",
    "RECTIFY": "执行更正",
    "RESTRICT": "标记为限制处理",
    "CONFIRM": "确认已停止/豁免该处理",
}


def action_for(request_type: str) -> str:
    return ACTIONS.get((request_type or "").upper(), "COLLECT")


def is_resolved(status: str) -> bool:
    return (status or "").upper() in RESOLVED_STATUSES


def is_open(status: str) -> bool:
    return (status or "").upper() in OPEN_STATUSES


def is_valid_task_status(status: str) -> bool:
    return bool(status) and status.upper() in TASK_STATUSES


# ============================================
# 任务 ID 生成
# ============================================
def make_task_id(request_id: str, source_id: str, category_id: str) -> str:
    """
    确定性任务 ID：同一 (request, source, category) 永远得到同一个 ID。
    配合 schema 里的 UNIQUE(request_id, source_id, category_id) 索引做幂等去重，
    重复触发编排不会产生重复任务。
    """
    digest = hashlib.sha1(f"{request_id}|{source_id}|{category_id}".encode()).hexdigest()[:8]
    return f"TSK-{digest.upper()}"


def make_delivery_id(request_id: str, seq: int = 1) -> str:
    return f"DEL-{str(request_id).replace('DSAR-', '')}-{seq:02d}"


# ============================================
# 计划构建
# ============================================
@dataclass(frozen=True)
class PlannedTask:
    task_id: str
    request_id: str
    source_id: str
    category_id: str
    status: str
    action: str
    owner: str | None = None
    excluded_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "request_id": self.request_id,
            "source_id": self.source_id,
            "category_id": self.category_id,
            "status": self.status,
            "action": self.action,
            "owner": self.owner,
            "excluded_reason": self.excluded_reason,
        }


def build_plan(request_id: str, sources: list, categories: list,
               request_type: str = "ACCESS") -> list[PlannedTask]:
    """
    生成检索任务计划。

    sources / categories 接受 dict 列表，需含：
        source_id, owner_team, active, excluded_by_default, exclusion_basis
        category_id
    默认排除的源直接落成 EXCLUDED 并带上法定依据 —— 不是静默跳过，
    排除本身必须可解释、可审计（Art. 12(4)/15(4)）。
    """
    action = action_for(request_type)
    tasks: list[PlannedTask] = []

    for src in sources or []:
        if not src.get("active", 1):
            continue
        source_id = src["source_id"]
        excluded = bool(src.get("excluded_by_default", 0))
        basis = src.get("exclusion_basis")

        for cat in categories or []:
            category_id = cat["category_id"] if isinstance(cat, dict) else cat
            tasks.append(PlannedTask(
                task_id=make_task_id(request_id, source_id, category_id),
                request_id=request_id,
                source_id=source_id,
                category_id=category_id,
                status=EXCLUDED if excluded else PENDING,
                action=action,
                owner=src.get("owner_team"),
                excluded_reason=basis if excluded else None,
            ))
    return tasks


def upsert_tasks(conn, request_id: str, sources: list, categories: list,
                 request_type: str = "ACCESS") -> dict:
    """
    幂等写入任务计划到数据库。已存在的任务不覆盖（保留其运行状态）。
    返回 {"created": n, "skipped": n, "total": n, "excluded": n}
    """
    planned = build_plan(request_id, sources, categories, request_type)
    created = skipped = excluded = 0

    for task in planned:
        if task.status == EXCLUDED:
            excluded += 1
        exists = conn.execute(
            "SELECT task_id FROM discovery_tasks WHERE task_id = ?", (task.task_id,)
        ).fetchone()
        if exists:
            skipped += 1
            continue
        conn.execute(
            """INSERT INTO discovery_tasks
               (task_id, request_id, source_id, category_id, status, action,
                owner, excluded_reason)
               VALUES (?,?,?,?,?,?,?,?)""",
            (task.task_id, task.request_id, task.source_id, task.category_id,
             task.status, task.action, task.owner, task.excluded_reason),
        )
        created += 1

    conn.commit()
    return {"created": created, "skipped": skipped,
            "total": len(planned), "excluded": excluded}


# ============================================
# 完成度汇总
# ============================================

def summarize(by_status: dict) -> dict:
    """
    任务集合的收敛情况 —— workflow 用 open_count 做完成前校验。
    入参是 {status: count} 计数表（由 summarize_tasks / load_summary 归一）。
    """
    by_status = dict(by_status or {})
    total = sum(by_status.values())
    open_count = sum(by_status.get(s, 0) for s in OPEN_STATUSES)
    resolved_count = sum(by_status.get(s, 0) for s in RESOLVED_STATUSES)

    return {
        "total": total,
        "by_status": by_status,
        "open_count": open_count,
        "resolved_count": resolved_count,
        "completed": by_status.get(COMPLETED, 0),
        "not_found": by_status.get(NOT_FOUND, 0),
        "errors": by_status.get(ERROR, 0),
        "excluded": by_status.get(EXCLUDED, 0),
        # 覆盖率只统计「已收敛」的任务，合法排除项算已处理
        "coverage_pct": round(100.0 * resolved_count / total, 1) if total else 100.0,
        "blocking": open_count > 0,
        "blocking_reason": (f"仍有 {open_count} 个任务未收敛，不得标记请求完成"
                            if open_count else "全部检索任务已收敛"),
    }


def summarize_tasks(tasks: list) -> dict:
    """把任务列表归一为计数表后汇总"""
    counts: dict[str, int] = {}
    for t in tasks or []:
        if isinstance(t, dict):
            st = t.get("status", "")
        else:
            st = getattr(t, "status", "")
        st = (st or PENDING).upper()
        counts[st] = counts.get(st, 0) + 1
    return summarize(counts)


def load_summary(conn, request_id: str) -> dict:
    rows = conn.execute(
        """SELECT status, COUNT(*) n FROM discovery_tasks
           WHERE request_id = ? GROUP BY status""", (request_id,)
    ).fetchall()
    return summarize({r["status"]: r["n"] for r in rows})


# ============================================
# 允许的状态流转（任务级）
# ============================================
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({RUNNING, EXCLUDED, WAIVED, ERROR, NOT_FOUND}),
    RUNNING: frozenset({COMPLETED, NOT_FOUND, ERROR}),
    # 终态不可回退 —— 误标只能新建任务，保证审计链不被覆盖
    COMPLETED: frozenset(),
    NOT_FOUND: frozenset(),
    ERROR: frozenset({RUNNING}),          # 允许重跑
    EXCLUDED: frozenset(),
    WAIVED: frozenset({RUNNING}),         # 允许撤销豁免
}


def can_task_transition(current: str, target: str) -> bool:
    return (target or "").upper() in TASK_TRANSITIONS.get((current or "").upper(), frozenset())


def validate_task_transition(current: str, target: str, record_count: int = 0,
                             excluded_reason: str = "") -> tuple[bool, list[str]]:
    errors: list[str] = []
    current_u, target_u = (current or "").upper(), (target or "").upper()
    if not is_valid_task_status(target_u):
        return False, [f"非法任务状态: {target!r}，合法值={list(TASK_STATUSES)}"]
    if current_u == target_u:
        return False, [f"任务状态未变化（{current_u}）"]
    if not can_task_transition(current_u, target_u):
        errors.append(f"非法任务流转 {current_u} → {target_u}，"
                      f"当前合法后继为 {sorted(TASK_TRANSITIONS.get(current_u, frozenset())) or '无（终态）'}")
    if target_u == COMPLETED and int(record_count or 0) <= 0:
        errors.append("COMPLETED 必须报告命中条数（record_count > 0）；无数据请标记 NOT_FOUND")
    if target_u == NOT_FOUND and int(record_count or 0) > 0:
        errors.append("NOT_FOUND 与 record_count > 0 矛盾，请改用 COMPLETED")
    if target_u == EXCLUDED and not str(excluded_reason or "").strip():
        errors.append("EXCLUDED 必须记录法定排除依据（如 Art. 17(3)(b) 留存义务），不得静默排除")
    return (not errors), errors


def explain_action(action: str) -> str:
    return ACTION_LABELS.get((action or "").upper(), "未知动作")
