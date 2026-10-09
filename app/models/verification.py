"""身份核验引擎 —— DSAR 平台最重要的安全控制

如果这一步可以被绕过，任何人都能以「我是某某」为由索取他人的全部个人数据。
因此本模块的立场是**默认拒绝**：核验未达到阈值，系统不允许进入数据检索阶段
（由 workflow.GUARDS 强制），而不是「先放行再说」。

三段式判定：
1. 核验方式本身有强度（EMAIL_OTP 90 分 > 短信 85 分 > 邮寄信函 70 分 > 无 0 分）
2. 风险信号扣分（姓名与账户不符、邮箱域不符、第三方代为请求……）
3. 阈值分档：
     >= 80  AUTO_VERIFY      自动通过
     60-79  MANUAL_REVIEW    需隐私官人工复核（防「差一点点」被自动放行）
     < 60   REJECT           拒绝，且请求方只能走拒绝/补件流程

另外强制：
- OTP 有时效（默认 15 分钟），过期即失效，防止重放
- 最多尝试 3 次，超出后强制 FAILED，不能无限试
- 核验记录本身按留存策略到期销毁（留存的是结论，不是原始身份材料）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# ============================================
# 核验方式强度（0-100）
# ============================================
METHOD_SCORES: dict[str, int] = {
    "ACCOUNT_AUTHENTICATION": 100,  # 已登录账户内发起 + 二次认证，绑定关系最强
    "IN_PERSON": 98,                # 带证件当面核验
    "IDENTITY_DOCUMENT": 95,        # 政府证件 + 人脸/活体比对
    "EMAIL_OTP": 90,                # 邮箱一次性口令（需邮箱本身已绑定该主体）
    "SMS_OTP": 85,                  # 短信验证码
    "POSTAL_LETTER": 70,            # 邮寄到已登记地址（慢但绑定真实）
    "MANUAL_REVIEW": 65,            # 无自助方式，转人工证据审查
    "NONE": 0,                      # 未核验
}

METHOD_LABELS = {
    "ACCOUNT_AUTHENTICATION": "账户内二次认证",
    "IN_PERSON": "线下当面核验",
    "IDENTITY_DOCUMENT": "证件核验（政府签发件）",
    "EMAIL_OTP": "邮箱一次性口令",
    "SMS_OTP": "短信验证码",
    "POSTAL_LETTER": "邮寄信函核验",
    "MANUAL_REVIEW": "人工证据复核",
    "NONE": "未核验",
}

# ============================================
# 风险信号扣分
# ============================================
RISK_SIGNALS: dict[str, int] = {
    "name_mismatch_on_account": -25,     # 请求姓名与账户登记姓名不符
    "email_domain_mismatch": -20,        # 请求邮箱域与账户绑定邮箱域不同
    "third_party_requesting": -30,       # 由第三方代为请求（须额外授权文件）
    "unverified_contact_channel": -15,   # 使用了从未验证过的联系方式
    "high_risk_request_type": -10,       # 删除/限制等不可逆请求
    "new_account_recently_created": -10, # 新建账户即发起大规模数据请求
    "prior_failed_attempt": -15,         # 之前已有核验失败记录
    "inconsistent_locale": -5,           # 声称所在地与请求来源不一致
}

RISK_SIGNAL_LABELS = {
    "name_mismatch_on_account": "请求姓名与账户登记信息不符",
    "email_domain_mismatch": "请求邮箱域与账户绑定邮箱域不一致",
    "third_party_requesting": "由第三方代为提出请求（缺授权文件）",
    "unverified_contact_channel": "使用了此前从未验证的联系方式",
    "high_risk_request_type": "请求类型为不可逆操作（删除/限制/反对）",
    "new_account_recently_created": "账户创建时间过短",
    "prior_failed_attempt": "存在历史核验失败记录",
    "inconsistent_locale": "主体所在地与请求来源不一致",
}

# 不可逆请求类型：核验阈值相应提高
DESTRUCTIVE_TYPES = ("ERASURE", "RESTRICTION", "OBJECTION")

# ============================================
# 阈值
# ============================================
AUTO_VERIFY_THRESHOLD = 80
MANUAL_REVIEW_THRESHOLD = 60

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_OTP_TTL_MINUTES = 15


@dataclass(frozen=True)
class VerificationAssessment:
    method: str
    method_label: str
    base_score: int
    penalties: dict
    score: int
    decision: str            # AUTO_VERIFY / MANUAL_REVIEW / REJECT
    reasons: list[str]
    disclosure_permitted: bool

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "method_label": self.method_label,
            "base_score": self.base_score,
            "penalties": self.penalties,
            "score": self.score,
            "decision": self.decision,
            "reasons": self.reasons,
            "disclosure_permitted": self.disclosure_permitted,
        }


def valid_methods() -> tuple[str, ...]:
    return tuple(METHOD_SCORES.keys())


def is_valid_method(method: str) -> bool:
    return bool(method) and method.upper() in METHOD_SCORES


def assess_verification(method: str, risk_signals=None,
                        request_type: str = "ACCESS") -> VerificationAssessment:
    """
    核验评估：方式强度 − 风险扣分 → 三段式决策。

    返回的 disclosure_permitted 只有在 AUTO_VERIFY 时为 True —— 
    即「系统认为可以向对方披露数据」。workflow 的 Guard 还会再查一次
    数据库里实际的核验状态，双重保险。
    """
    method_u = (method or "NONE").upper()
    if method_u not in METHOD_SCORES:
        raise KeyError(method_u)

    base = METHOD_SCORES[method_u]
    signals = [s for s in (risk_signals or []) if s]
    penalties: dict[str, int] = {}
    for sig in signals:
        if sig not in RISK_SIGNALS:
            continue  # 未知信号忽略，避免调用方拼错字符串导致静默失效
        penalties[sig] = RISK_SIGNALS[sig]

    if (request_type or "").upper() in DESTRUCTIVE_TYPES:
        penalties.setdefault("high_risk_request_type", RISK_SIGNALS["high_risk_request_type"])

    score = base + sum(penalties.values())
    score = max(0, min(100, score))

    if score >= AUTO_VERIFY_THRESHOLD:
        decision = "AUTO_VERIFY"
    elif score >= MANUAL_REVIEW_THRESHOLD:
        decision = "MANUAL_REVIEW"
    else:
        decision = "REJECT"

    reasons = [f"核验方式「{METHOD_LABELS[method_u]}」基准强度 {base}"]
    for sig, pen in penalties.items():
        reasons.append(f"{RISK_SIGNAL_LABELS.get(sig, sig)}（{pen:+d}）")

    if decision == "AUTO_VERIFY":
        reasons.append(f"合计 {score} ≥ {AUTO_VERIFY_THRESHOLD}，自动通过，可开始数据检索")
    elif decision == "MANUAL_REVIEW":
        reasons.append(
            f"合计 {score} 落在 [{MANUAL_REVIEW_THRESHOLD}, {AUTO_VERIFY_THRESHOLD})，"
            "需隐私官人工复核后方可处理"
        )
    else:
        reasons.append(
            f"合计 {score} < {MANUAL_REVIEW_THRESHOLD}，核验不通过，"
            "不得据此披露任何个人数据"
        )

    return VerificationAssessment(
        method=method_u,
        method_label=METHOD_LABELS[method_u],
        base_score=base,
        penalties=penalties,
        score=score,
        decision=decision,
        reasons=reasons,
        disclosure_permitted=(decision == "AUTO_VERIFY"),
    )


# ============================================
# 生命周期
# ============================================

# 允许进入 VERIFIED 的状态来源（人工复核可从 MANUAL_REVIEW 放行）
PENDING = "PENDING"
IN_PROGRESS = "IN_PROGRESS"
VERIFIED = "VERIFIED"
MANUAL_REVIEW = "MANUAL_REVIEW"
FAILED = "FAILED"
EXPIRED = "EXPIRED"
ABANDONED = "ABANDONED"

VERIFY_STATUSES = (PENDING, IN_PROGRESS, VERIFIED, MANUAL_REVIEW, FAILED, EXPIRED, ABANDONED)

# 唯一允许披露数据的状态
DISCLOSURE_STATUS = VERIFIED

# 人工复核通过后落到的状态
APPROVED_STATUS = VERIFIED


def status_permits_disclosure(status: str) -> bool:
    """只有 VERIFIED 允许进入数据检索/响应阶段"""
    return (status or "").upper() == DISCLOSURE_STATUS


def otp_expiry(started_at=None, ttl_minutes: int = DEFAULT_OTP_TTL_MINUTES) -> datetime:
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    if ttl_minutes <= 0:
        return started_at
    return started_at + timedelta(minutes=ttl_minutes)


def check_otp_valid(status: str, expires_at, now=None) -> tuple[bool, str]:
    """OTP 时效校验 —— 过期后即使口令正确也不可用"""
    if now is None:
        now = datetime.now(timezone.utc)
    if (status or "").upper() in (VERIFIED,):
        return False, "核验已完成，无需再次提交口令"
    if (status or "").upper() in (FAILED, ABANDONED):
        return False, f"核验已{'失败' if status.upper() == FAILED else '放弃'}，需重新发起"
    if expires_at is None:
        return False, "核验记录缺少 expires_at，口令不可用（防重放默认失败）"
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return False, "expires_at 格式非法"
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if now > expires_at:
        return False, "一次性口令已过期（默认有效期 15 分钟），需重新发送"
    return True, "口令有效"


def can_attempt(status: str, attempts: int, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> tuple[bool, str]:
    """尝试次数限制 —— 超出后锁定，防止暴力猜解"""
    if (status or "").upper() in (VERIFIED,):
        return False, "核验已通过"
    if (status or "").upper() == ABANDONED:
        return False, "核验已放弃"
    if attempts >= max_attempts:
        return False, (f"已达最大尝试次数 {max_attempts}，核验锁定为 FAILED，"
                       "需重新发起核验流程")
    return True, f"可继续尝试（剩余 {max_attempts - attempts} 次）"


def manual_review_outcome(score: int, approved: bool, note: str = "") -> tuple[bool, str]:
    """
    人工复核结论。reviewer 必须在 note 中写明依据 —— 
    「人工放行」本身就是一个需要留痕的审计事件。
    """
    if not approved:
        return False, note or "隐私官复核未通过：核验证据不足"
    if not str(note or "").strip():
        return False, "人工复核通过必须填写依据说明（审计要求）"
    return True, f"隐私官复核通过（置信度 {score}）：{note}"


def explain_status(status: str) -> str:
    return {
        PENDING: "已发起核验，等待数据主体提交凭证",
        IN_PROGRESS: "凭证已提交，核验中",
        VERIFIED: "身份核验通过 —— 可进行跨系统检索",
        MANUAL_REVIEW: "置信度不足，已转隐私官人工复核（此时不得披露数据）",
        FAILED: "核验失败（尝试次数耗尽），不得据此处理数据",
        EXPIRED: "核验凭证过期，需重新发起",
        ABANDONED: "数据主体放弃核验",
    }.get((status or "").upper(), "未知核验状态")
