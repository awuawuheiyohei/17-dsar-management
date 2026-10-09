"""DSAR 请求状态机

真实平台里状态机是防止「未核验就导出数据」「检索没跑完就标记完成」
这类事故的最后一道闸门 —— 不是 UI 的问题，是数据泄露的问题。

    RECEIVED
       │
       ▼
    IDENTITY_VERIFICATION ──(核验未通过)──► DENIED
       │  (VERIFIED)
       ▼
    IN_PROGRESS ◄──────────────┐
       │                       │
       ├──► CLARIFICATION_REQUESTED ──► CLARIFICATION_ANSWERED ──┘
       │
       ├──► COMPLETED          (需: 核验通过 + 检索任务全部收敛)
       ├──► PARTIALLY_COMPLETED
       ├──► DENIED             (需: 法定拒绝理由 + 说明 + 审批)
       └──► WITHDRAWN
    (任何未关闭状态) ──► EXPIRED   (长期无响应，按内部老化策略关闭；非 GDPR 条款)

注意：Art. 12(6) 授权的是「身份核验」，与「关闭请求」无关。

转换规则集中在本文件，API 只调用 `validate_transition`，
不自己拼字符串 —— 这样非法跳转不可能绕过校验。
"""
from __future__ import annotations

from dataclasses import dataclass

# 状态定义
RECEIVED = "RECEIVED"
IDENTITY_VERIFICATION = "IDENTITY_VERIFICATION"
IN_PROGRESS = "IN_PROGRESS"
CLARIFICATION_REQUESTED = "CLARIFICATION_REQUESTED"
CLARIFICATION_ANSWERED = "CLARIFICATION_ANSWERED"
COMPLETED = "COMPLETED"
PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
DENIED = "DENIED"
WITHDRAWN = "WITHDRAWN"
EXPIRED = "EXPIRED"

STATUSES = (
    RECEIVED, IDENTITY_VERIFICATION, IN_PROGRESS,
    CLARIFICATION_REQUESTED, CLARIFICATION_ANSWERED,
    COMPLETED, PARTIALLY_COMPLETED, DENIED, WITHDRAWN, EXPIRED,
)

# 终态：不可再流转（除显式重开）
TERMINAL = (COMPLETED, WITHDRAWN)
# 关闭态：停止倒计时，但允许重新开启
CLOSED = (COMPLETED, PARTIALLY_COMPLETED, DENIED, WITHDRAWN, EXPIRED)

# 仍需团队推进的状态（Dashboard 关注这几类）
OPEN_STATUSES = (RECEIVED, IDENTITY_VERIFICATION, IN_PROGRESS,
                 CLARIFICATION_REQUESTED, CLARIFICATION_ANSWERED)

# 禁止流转（重开仅允许走显式接口）
_FROZEN = frozenset()

TRANSITIONS: dict[str, frozenset[str]] = {
    RECEIVED: frozenset({IDENTITY_VERIFICATION, WITHDRAWN, DENIED}),
    IDENTITY_VERIFICATION: frozenset({IN_PROGRESS, DENIED, WITHDRAWN, EXPIRED}),
    IN_PROGRESS: frozenset({CLARIFICATION_REQUESTED, COMPLETED,
                            PARTIALLY_COMPLETED, DENIED, WITHDRAWN, EXPIRED}),
    CLARIFICATION_REQUESTED: frozenset({CLARIFICATION_ANSWERED, DENIED,
                                         WITHDRAWN, EXPIRED}),
    CLARIFICATION_ANSWERED: frozenset({IN_PROGRESS, CLARIFICATION_REQUESTED,
                                        COMPLETED, PARTIALLY_COMPLETED, DENIED,
                                        WITHDRAWN}),
    COMPLETED: _FROZEN,
    PARTIALLY_COMPLETED: _FROZEN,
    # 拒绝后若数据主体申诉，可重新进入流程
    DENIED: frozenset({IDENTITY_VERIFICATION, IN_PROGRESS}),
    WITHDRAWN: _FROZEN,
    # 长期无响应过期后，数据主体可重新唤起（通常视为新请求）
    EXPIRED: frozenset({RECEIVED, IDENTITY_VERIFICATION}),
}

# 人工可重开的流转（需额外记录理由）
REOPEN_TRANSITIONS = frozenset({
    (DENIED, IDENTITY_VERIFICATION), (DENIED, IN_PROGRESS),
    (EXPIRED, RECEIVED), (EXPIRED, IDENTITY_VERIFICATION),
})


@dataclass(frozen=True)
class Guard:
    """流转前置条件。context 由调用方（API 层）从数据库组装。"""
    requires_verified_identity: bool = False
    requires_no_open_tasks: bool = False
    requires_denial_reason: bool = False
    requires_delivery: bool = False
    requires_clarification: bool = False


GUARDS: dict[tuple[str, str], Guard] = {
    # 未核验身份，绝不允许进入数据处理阶段
    (IDENTITY_VERIFICATION, IN_PROGRESS): Guard(requires_verified_identity=True),
    (CLARIFICATION_ANSWERED, IN_PROGRESS): Guard(requires_verified_identity=True),
    (RECEIVED, IDENTITY_VERIFICATION): Guard(),
    (RECEIVED, DENIED): Guard(requires_denial_reason=True),
    (IDENTITY_VERIFICATION, DENIED): Guard(requires_denial_reason=True),
    # 标记完成前必须确认跨系统检索已经收敛
    (IN_PROGRESS, COMPLETED): Guard(requires_verified_identity=True,
                                    requires_no_open_tasks=True,
                                    requires_delivery=True),
    (IN_PROGRESS, PARTIALLY_COMPLETED): Guard(requires_verified_identity=True,
                                              requires_no_open_tasks=True),
    (CLARIFICATION_ANSWERED, COMPLETED): Guard(requires_verified_identity=True,
                                                requires_no_open_tasks=True,
                                                requires_delivery=True),
    (CLARIFICATION_ANSWERED, PARTIALLY_COMPLETED): Guard(requires_verified_identity=True,
                                                        requires_no_open_tasks=True),
    (IN_PROGRESS, DENIED): Guard(requires_denial_reason=True),
    (CLARIFICATION_REQUESTED, DENIED): Guard(requires_denial_reason=True),
    (CLARIFICATION_ANSWERED, DENIED): Guard(requires_denial_reason=True),
    (IN_PROGRESS, CLARIFICATION_REQUESTED): Guard(requires_verified_identity=True),
    (CLARIFICATION_ANSWERED, CLARIFICATION_REQUESTED): Guard(requires_verified_identity=True),
    # 必须先发出澄清请求才能记录澄清答复
    (CLARIFICATION_REQUESTED, CLARIFICATION_ANSWERED): Guard(requires_clarification=True),
}

# 每种阻塞条件对应的中文说明（返回给 API 的 detail）
GUARD_LABELS = {
    "requires_verified_identity": "身份核验尚未通过（Art. 12(6) 要求处理数据前先确认请求者身份）",
    "requires_no_open_tasks": "仍有跨系统检索任务未收敛（不允许在检索完成前标记请求完成）",
    "requires_denial_reason": "拒绝必须附带法定理由与说明（Art. 12(4)）",
    "requires_delivery": "尚未生成并留存响应（Art. 15(3) 要求提供数据副本）",
    "requires_clarification": "尚未发出澄清请求",
}

VALID_STATUSES = STATUSES


def is_valid_status(status: str) -> bool:
    return bool(status) and status.upper() in STATUSES


def is_terminal(status: str) -> bool:
    return (status or "").upper() in TERMINAL


def is_closed(status: str) -> bool:
    return (status or "").upper() in CLOSED


def is_open(status: str) -> bool:
    return (status or "").upper() in OPEN_STATUSES


def can_transition(current: str, target: str) -> bool:
    """纯状态图层面的合法性（不考虑数据条件）"""
    current, target = (current or "").upper(), (target or "").upper()
    return target in TRANSITIONS.get(current, frozenset())


def allowed_transitions(current: str) -> list[str]:
    return sorted(TRANSITIONS.get((current or "").upper(), frozenset()))


def guard_for(current: str, target: str) -> Guard:
    return GUARDS.get(((current or "").upper(), (target or "").upper()), Guard())


def is_reopen(current: str, target: str) -> bool:
    return ((current or "").upper(), (target or "").upper()) in REOPEN_TRANSITIONS


def validate_transition(current: str, target: str, context: dict | None = None) -> tuple[bool, list[str]]:
    """
    完整校验一次流转：状态图合法性 + 全部 Guard 前置条件。

    context 约定键（缺失视为不满足，保守拒绝）：
        identity_verified : bool
        open_task_count   : int   (PENDING / RUNNING / WAIVED 的检索任务数)
        denial_reason     : str   (reason_code)
        denial_justification : str
        delivery_id       : str   已生成的响应
        clarification_sent: bool

    返回 (是否允许, 阻塞原因列表)。
    """
    context = context or {}
    current_u, target_u = (current or "").upper(), (target or "").upper()
    errors: list[str] = []

    if not is_valid_status(current_u):
        errors.append(f"未知的当前状态: {current!r}")
    if not is_valid_status(target_u):
        return False, [f"非法目标状态: {target!r}，合法值={list(STATUSES)}"]
    if current_u == target_u:
        errors.append(f"状态未变化（{current_u} → {target_u}）")
    if errors:
        return False, errors

    if not can_transition(current_u, target_u):
        if is_closed(current_u):
            errors.append(f"{current_u} 是关闭态，不能直接流转到 {target_u}"
                          f"（可重开的状态：DENIED / EXPIRED）")
        else:
            errors.append(f"非法状态跳转 {current_u} → {target_u}，"
                          f"当前合法后继为 {allowed_transitions(current_u) or '无'}")

    guard = guard_for(current_u, target_u)

    if guard.requires_verified_identity and not context.get("identity_verified"):
        errors.append(GUARD_LABELS["requires_verified_identity"])

    if guard.requires_no_open_tasks and int(context.get("open_task_count") or 0) > 0:
        errors.append(
            f"{GUARD_LABELS['requires_no_open_tasks']}（当前 {context['open_task_count']} 个）"
        )

    if guard.requires_denial_reason:
        if not str(context.get("denial_reason") or "").strip():
            errors.append(GUARD_LABELS["requires_denial_reason"] + "：缺少 reason_code")
        if len(str(context.get("denial_justification") or "").strip()) < 10:
            errors.append("拒绝说明过短：必须向数据主体说明为何无法满足其权利（至少 10 个字符）")

    if guard.requires_delivery and not context.get("delivery_id"):
        errors.append(GUARD_LABELS["requires_delivery"])

    if guard.requires_clarification and not context.get("clarification_sent"):
        errors.append(GUARD_LABELS["requires_clarification"])

    return (not errors), errors


def explain_status(status: str) -> str:
    """给人看的状态说明"""
    return {
        RECEIVED: "已受理，尚未开始身份核验",
        IDENTITY_VERIFICATION: "正在核验请求者身份（Art. 12(6)）",
        IN_PROGRESS: "核验通过，正在跨系统检索数据",
        CLARIFICATION_REQUESTED: "已向数据主体索取补充信息，期限按行业实践暂停（非 GDPR 明文条款）",
        CLARIFICATION_ANSWERED: "已收到补充信息，等待恢复处理",
        COMPLETED: "已完整履行该权利并交付响应",
        PARTIALLY_COMPLETED: "部分履行（部分系统被法定例外排除）",
        DENIED: "已按法定理由拒绝（Art. 12(4) 要求告知理由）",
        WITHDRAWN: "数据主体主动撤回",
        EXPIRED: "长期无响应，按内部老化策略关闭（非 GDPR 条款）",
    }.get((status or "").upper(), "未知状态")
