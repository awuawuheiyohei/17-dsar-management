"""核心：法定时限计算引擎

GDPR Art. 12(3) 的期限表述是 "in any event within one month of receipt"。
「一个月」在合规实务中不是 30 天，也不是平均 30.44 天，而是**对应日**（corresponding date）
计算，并遵守月末截断（month-end clamping）：

    2026-01-31 + 1 month = 2026-02-28   （2 月无 31 日，截断到月末）
    2024-01-31 + 1 month = 2024-02-29   （闰年 2 月）
    2026-08-31 + 1 month = 2026-09-30

这与 python-dateutil 的 relativedelta 语义一致。本项目**不引入 dateutil**，
自己实现是为了让每个边界行为都可单测覆盖 —— 期限算错就是超期，
超期在 GDPR 下是可被监管处罚的实际违规。

另外两条同样重要的规则：

1. **延长（Art. 12(3) 后半句）**
   请求复杂或数量众多时可延长两个月，但**必须在收到请求后一个月内通知数据主体**。
   没有 extension_notified_at 的延长是非法的，期限不予采信。

2. **时钟暂停（行业实践，非 GDPR 明文条款）**
   控制者向数据主体索取补充信息期间，期限暂停。若不建模这一点，
   所有「已向对方索要信息」的请求都会被系统误判为超期。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

# ============================================
# 月加法（月末截断）—— 本模块最容易写错的地方
# ============================================


def add_months(d, months: int) -> date:
    """
    月加法，带月末截断。

    规则：目标月份没有源日期那么大的天数时，取该月最后一天。
    与 dateutil.relativedelta 行为一致（minus 1 day 再加 1 day 的朴素做法会得到
    2026-03-03 而非 2026-02-28，是常见 bug）。

    支持负数 months（用于撤销延长时回算原始期限）。
    """
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        raise TypeError(f"expected date/datetime, got {type(d).__name__}")
    if months == 0:
        return d

    total = d.month - 1 + months
    year = d.year + total // 12          # Python 的 // 是向下取整，负数同样正确
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_end(d, offset: int = 0) -> date:
    """该月最后一天；offset=1 表示下个月最后一天"""
    last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    return add_months(last, offset)


# ============================================
# 各法域时限策略
# ============================================


@dataclass(frozen=True)
class DeadlinePolicy:
    jurisdiction: str
    label: str
    regulation: str
    deadline_months: int = 0        # 主期限（按月）
    deadline_days: int = 0          # 主期限（按天）
    extension_months: int = 0       # 允许的延长量
    extension_days: int = 0
    max_extensions: int = 1
    clock_stop_allowed: bool = True  # 时钟暂停开关（GDPR 无明文条款，仅 EU/UK 开启）
    max_clock_stop_days: int = 60    # 暂停上限，防止无限期挂起
    citation: str = ""
    clock_stop_citation: str = ""    # 时钟暂停的依据（GDPR/CCPA/PIPL 均无明文条款，须如实标注）
    notes: str = ""

    @property
    def base_description(self) -> str:
        if self.deadline_months:
            return f"{self.deadline_months} 个月"
        return f"{self.deadline_days} 天"

    @property
    def extension_description(self) -> str:
        if not (self.extension_months or self.extension_days):
            return "不可延长"
        if self.extension_months:
            return f"+{self.extension_months} 个月"
        return f"+{self.extension_days} 天"

    def as_dict(self) -> dict:
        return {
            "jurisdiction": self.jurisdiction,
            "label": self.label,
            "regulation": self.regulation,
            "deadline": self.base_description,
            "deadline_months": self.deadline_months,
            "deadline_days": self.deadline_days,
            "extension": self.extension_description,
            "extension_months": self.extension_months,
            "extension_days": self.extension_days,
            "max_extensions": self.max_extensions,
            "clock_stop_allowed": self.clock_stop_allowed,
            "max_clock_stop_days": self.max_clock_stop_days,
            "citation": self.citation,
            "clock_stop_citation": self.clock_stop_citation,
            "notes": self.notes,
        }


# 时限以公开法规文本为准；notes 里写清本实现的简化假设。
#
# 关于「时钟暂停」的引用说明（重要，避免误引）：
#   GDPR Art. 12(6) 的原文是「对请求者身份有合理怀疑时，可要求提供确认身份
#   所需的补充信息」——它授权的是**身份核验**，并未规定可以暂停 Art. 12(3)
#   的答复期限。GDPR 中**没有**明文的「时钟暂停」条款。
#   因此本实现把两者严格区分开：
#     - 身份核验 → Art. 12(6)          （有明文依据）
#     - 时钟暂停 → 标注为「实践做法」，依据是 Art. 12(3) 的 "without undue
#       delay" + EDPB 关于「请求在被完整提出后才起算」的实践口径，
#       属于行业惯例而非成文法条款。
#   这样标注是为了让审计输出可被法务复核——把惯例说成法条会误导决策。
POLICIES: dict[str, DeadlinePolicy] = {
    "EU": DeadlinePolicy(
        jurisdiction="EU",
        label="欧盟 / EEA（GDPR）",
        regulation="GDPR",
        deadline_months=1,
        extension_months=2,
        max_extensions=1,
        citation="GDPR Art. 12(3)",
        clock_stop_citation="行业实践（非 GDPR 明文条款）· 依据 Art. 12(3) "
                            "'without undue delay' + EDPB 口径",
        notes="收到请求后一个月内答复；请求复杂或数量众多可延长两个月，"
              "但须在同一期限内通知数据主体。等待补充信息期间的暂停属行业实践："
              "GDPR 无明文时钟暂停条款，本实现按 Art. 12(3) 的 'without undue delay' "
              "与「请求完整提出后起算」口径处理，并设上限防止无限期挂起。"
              "注意：Art. 12(6) 授权的是身份核验，不是时钟暂停。",
    ),
    "UK": DeadlinePolicy(
        jurisdiction="UK",
        label="英国（UK GDPR + DPA 2018）",
        regulation="UK_GDPR",
        deadline_months=1,
        extension_months=2,
        max_extensions=1,
        citation="UK GDPR Art. 12(3)",
        clock_stop_citation="行业实践（非 UK GDPR 明文条款）· 依据 Art. 12(3) "
                            "'without undue delay'",
        notes="与 EU 一致；DPA 2018 第 52 条为无依据的过度请求提供收费机制。"
              "Art. 12(6) 在 UK GDPR 下同样只授权身份核验。",
    ),
    "CA": DeadlinePolicy(
        jurisdiction="CA",
        label="美国加州（CCPA/CPRA）",
        regulation="CCPA_CPRA",
        deadline_days=45,
        extension_days=45,
        max_extensions=1,
        # CCPA 没有明文「时钟暂停」条款：它给的是「可延长 45 天」的机制，
        # 且延长通常需要消费者主动要求。把暂停当做法条引用会误导审计结论。
        clock_stop_allowed=False,
        max_clock_stop_days=0,
        citation="Cal. Civ. Code §1798.130(a)(2)(B)",
        clock_stop_citation="不适用：CCPA/CPRA 无明文时钟暂停条款，"
                            "仅有 §1798.130(a)(2)(B) 的 45 天延长机制",
        notes="确认收到请求后 45 天内答复，可再延 45 天（须通知）。"
              "CPRA 下「确认收到」的时点为完整受理之时，故本实现以 received_date 起算。"
              "本实现不对 CA 施加时钟暂停——CCPA 无此法定机制。",
    ),
    "CN": DeadlinePolicy(
        jurisdiction="CN",
        label="中国大陆（个人信息保护法）",
        regulation="PIPL",
        deadline_days=15,
        extension_days=15,
        max_extensions=0,
        # PIPL 亦无明文时钟暂停条款。第 50 条讲的是查阅复制权的响应义务，
        # 不是暂停答复期限的授权。这里保留开关仅为演示内部策略配置能力。
        clock_stop_allowed=False,
        max_clock_stop_days=0,
        citation="PIPL 第 50 条 / 第 45-47 条",
        clock_stop_citation="不适用：PIPL 无明文时钟暂停条款",
        notes="【简化假设】实务口径常称「十五个工作日」。本实现按自然日 15 天计算，"
              "未接入法定节假日日历，因此 CN 的到期日仅供演示，不代表真实合规结论。"
              "另：PIPL 第 50 条规定的是查阅、复制请求的响应义务，"
              "并非暂停答复期限的条款依据。",
    ),
}

DEFAULT_JURISDICTION = "EU"


def get_policy(jurisdiction: Optional[str]) -> DeadlinePolicy:
    key = (jurisdiction or DEFAULT_JURISDICTION).upper()
    if key not in POLICIES:
        raise KeyError(key)
    return POLICIES[key]


def is_valid_jurisdiction(jurisdiction: Optional[str]) -> bool:
    return bool(jurisdiction) and jurisdiction.upper() in POLICIES


# ============================================
# 期限计算
# ============================================


def compute_deadline(received, policy: DeadlinePolicy, extensions: int = 0,
                     clock_stopped_days: int = 0) -> date:
    """
    计算到期日。

    base = 收到日 + 期限（按对应日加月，或按天）
    ext  = base + 延长量（仅在合法延长时传入 extensions=1）
    最终 = ext + 时钟暂停天数（行业实践，受 max_clock_stop_days 上限约束）

    extensions 会被夹到 [0, max_extensions]，防止请求方任意拉长期限。
    """
    if isinstance(received, datetime):
        received = received.date()
    extensions = max(0, min(int(extensions or 0), policy.max_extensions))

    if policy.deadline_months:
        due = add_months(received, policy.deadline_months)
    else:
        due = received + timedelta(days=policy.deadline_days)

    if extensions:
        if policy.extension_months:
            due = add_months(due, policy.extension_months)
        elif policy.extension_days:
            due = due + timedelta(days=policy.extension_days)

    if clock_stopped_days:
        if not policy.clock_stop_allowed:
            stop = 0
        else:
            stop = max(0, min(int(clock_stopped_days), policy.max_clock_stop_days))
        if stop:
            due = due + timedelta(days=stop)

    return due


def compute_original_deadline(received, policy: DeadlinePolicy) -> date:
    return compute_deadline(received, policy, extensions=0, clock_stopped_days=0)


def compute_extended_deadline(received, policy: DeadlinePolicy,
                               clock_stopped_days: int = 0) -> date:
    return compute_deadline(received, policy, extensions=1, clock_stopped_days=clock_stopped_days)


# ============================================
# 延长的合法性 —— Art. 12(3) 要求「一个月内通知」
# ============================================


def extension_is_valid(received, policy: DeadlinePolicy, notified_at,
                       reason: str = "", extensions: int = 1,
                       require_reason: bool = True) -> tuple[bool, list[str]]:
    """
    校验一次延长是否符合 Art. 12(3)。

    返回 (是否合法, 理由列表)。非法延长不予采信 —— 到期日仍按原始期限算。
    这是刻意设计的：系统不因为「我已经决定延期」就自动给请求宽限，
    必须留下合规的通知证据，否则超期就是超期。

    require_reason=False 用于「回看快照」场景：此时库里已有已记录的延长，
    只校验通知证据是否在法定期限内，不重复要求填写理由。
    """
    errors: list[str] = []
    if isinstance(received, datetime):
        received = received.date()

    if extensions < 1:
        return False, ["未申请延长"]
    if extensions > policy.max_extensions:
        return False, [f"{policy.jurisdiction} 法域最多允许延长 {policy.max_extensions} 次"]
    if not (policy.extension_months or policy.extension_days):
        return False, [f"{policy.citation} 未规定可延期"]
    if require_reason and not str(reason or "").strip():
        errors.append("Art. 12(3) 要求延期必须说明「请求复杂或数量众多」的具体情形")

    if notified_at is None:
        errors.append(f"{policy.citation} 要求在原始期限（{policy.base_description}）届满前通知数据主体，"
                      "缺少 extension_notified_at，延长不予采信")
        return False, errors

    if isinstance(notified_at, datetime):
        notified_at = notified_at.date()
    original_due = compute_original_deadline(received, policy)
    # 边界口径：通知日 == 原始期限当日 → 仍算「在期限内」（Art. 12(3) 的期限届满于该日结束），
    # 晚一天即无效。实践上应更早通知以留出对方接收时间，但法律文本允许到期限当日。
    if notified_at > original_due:
        errors.append(
            f"延长通知日期 {notified_at.isoformat()} 晚于原始期限 "
            f"{original_due.isoformat()}，{policy.citation} 下的延长无效"
        )
        return False, errors

    return (not errors), errors


# ============================================
# 时钟暂停（行业实践 —— GDPR 无明文条款，Art. 12(6) 只授权身份核验）
# ============================================


def clock_stop_is_valid(policy: DeadlinePolicy, already_stopped_days: int,
                        requested_days: int) -> tuple[bool, str]:
    if not policy.clock_stop_allowed:
        return False, f"{policy.jurisdiction} 法域未在本策略下启用时钟暂停"
    remaining = policy.max_clock_stop_days - max(0, already_stopped_days)
    if remaining <= 0:
        return False, f"累计暂停已达上限 {policy.max_clock_stop_days} 天，不得继续挂起"
    if requested_days > remaining:
        return False, (f"本次申请暂停 {requested_days} 天超出剩余额度 {remaining} 天"
                       f"（上限 {policy.max_clock_stop_days} 天）")
    return True, "时钟暂停符合法域策略上限（行业实践，非 GDPR 明文条款）"


# ============================================
# 期限状态
# ============================================

TERMINAL_STATUSES = ("COMPLETED", "PARTIALLY_COMPLETED", "DENIED", "WITHDRAWN", "EXPIRED")

# 全部会停止倒计时的状态
CLOSED_STATUSES = TERMINAL_STATUSES


@dataclass(frozen=True)
class DeadlineView:
    """对单个请求的期限快照，供 API / Dashboard / 测试共用同一套判定逻辑"""
    jurisdiction: str
    regulation: str
    citation: str
    received_date: date
    due_date: date
    extended_due_date: Optional[date]
    effective_due_date: date
    extension_granted: bool
    clock_stopped_days: int
    days_remaining: int
    status: str
    explanation: str
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "jurisdiction": self.jurisdiction,
            "regulation": self.regulation,
            "citation": self.citation,
            "received_date": self.received_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "extended_due_date": self.extended_due_date.isoformat() if self.extended_due_date else None,
            "effective_due_date": self.effective_due_date.isoformat(),
            "extension_granted": self.extension_granted,
            "clock_stopped_days": self.clock_stopped_days,
            "days_remaining": self.days_remaining,
            "status": self.status,
            "explanation": self.explanation,
            "warnings": self.warnings,
        }


def deadline_status(due_date, today, request_status: str = "",
                    extension_granted: bool = False,
                    warning_days: int = 7) -> str:
    """
    ON_TRACK / DUE_SOON / DUE_TODAY / OVERDUE / CLOSED

    顺序很关键：已关闭的请求不再倒计时；超期优先于「即将到期」。
    """
    if isinstance(today, datetime):
        today = today.date()
    if isinstance(due_date, datetime):
        due_date = due_date.date()

    if request_status in CLOSED_STATUSES:
        return "CLOSED"

    remaining = (due_date - today).days
    if remaining < 0:
        return "OVERDUE"
    if remaining == 0:
        return "DUE_TODAY"
    if remaining <= warning_days:
        return "DUE_SOON"
    if extension_granted:
        return "EXTENDED"
    return "ON_TRACK"


def _as_date(value):
    """统一 date / datetime / ISO 字符串 → date；None 原样返回"""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"cannot interpret {value!r} as a date")


def build_deadline_view(received, jurisdiction: str, due_date, today,
                        extended_due_date=None, extension_granted: bool = False,
                        clock_stopped_days: int = 0, request_status: str = "",
                        warning_days: int = 7,
                        extension_notified_at=None) -> DeadlineView:
    """
    组装期限快照。effective_due_date 优先取延长后的日期；
    若延长存在但通知证据不合法（extension_notified_at 缺失/逾期），
    则回落到原始期限，并在 warnings 中说明 —— 见 extension_is_valid。
    """
    policy = get_policy(jurisdiction)
    received = _as_date(received)
    today = _as_date(today)
    due_date = _as_date(due_date)
    extended_due_date = _as_date(extended_due_date)
    extension_notified_at = _as_date(extension_notified_at)

    warnings: list[str] = []
    effective_granted = bool(extension_granted)

    if extension_granted:
        if extension_notified_at is None:
            effective_granted = False
            warnings.append(
                f"已记录延期但缺少通知证据（{policy.citation}），有效到期日按原始期限计算"
            )
        else:
            ok, errors = extension_is_valid(received, policy, extension_notified_at,
                                            extensions=1, require_reason=False)
            if not ok:
                effective_granted = False
                warnings.extend(errors)
                warnings.append(
                    f"延长不满足 {policy.citation}，有效到期日回落至原始期限 {due_date.isoformat()}"
                )

    effective = extended_due_date if (effective_granted and extended_due_date) else due_date

    # 时钟暂停天数要叠加到有效到期日上。
    # 入库时的 due_date 是「受理即起算」算出来的原始期限，不含之后累积的暂停天数，
    # 所以快照必须在这里补上 —— 否则「已向对方索要信息」的请求会被误判为超期。
    if clock_stopped_days and policy.clock_stop_allowed:
        stop = max(0, min(int(clock_stopped_days), policy.max_clock_stop_days))
        if stop:
            effective = effective + timedelta(days=stop)

    status = deadline_status(effective, today, request_status,
                             extension_granted=effective_granted,
                             warning_days=warning_days)
    days_remaining = (effective - today).days

    return DeadlineView(
        jurisdiction=policy.jurisdiction,
        regulation=policy.regulation,
        citation=policy.citation,
        received_date=received,
        due_date=due_date,
        extended_due_date=extended_due_date,
        effective_due_date=effective,
        extension_granted=effective_granted,
        clock_stopped_days=int(clock_stopped_days or 0),
        days_remaining=days_remaining,
        status=status,
        explanation=explain_deadline(policy, received, effective, today, status,
                                     days_remaining, effective_granted,
                                     int(clock_stopped_days or 0)),
        warnings=warnings,
    )


def explain_deadline(policy: DeadlinePolicy, received, due, today, status: str,
                     days_remaining: int, extension_granted: bool,
                     clock_stopped_days: int) -> str:
    """人类可读解释 —— 让合规/业务方知道系统为什么这么判"""
    parts = [
        f"{policy.label}（{policy.citation}）：收到日 {received.isoformat()} → 期限 {policy.base_description}",
    ]
    if extension_granted:
        parts.append(f"已依法延长 {policy.extension_description}")
    if clock_stopped_days:
        stop_cite = policy.clock_stop_citation or policy.citation
        parts.append(f"时钟暂停累计 {clock_stopped_days} 天（{stop_cite}）")
    parts.append(f"有效到期日 {due.isoformat()}")

    if status == "OVERDUE":
        parts.append(f"⚠️ 已超期 {-days_remaining} 天 —— 属于可被监管问责的时限违规")
    elif status == "DUE_TODAY":
        parts.append("⚠️ 今日到期")
    elif status == "DUE_SOON":
        parts.append(f"剩 {days_remaining} 天，需尽快推进")
    elif status == "CLOSED":
        parts.append("请求已关闭，倒计时停止")
    elif status == "EXTENDED":
        parts.append(f"剩 {days_remaining} 天（延长后）")
    else:
        parts.append(f"剩 {days_remaining} 天")
    return " · ".join(parts)
