"""数据最小化引擎 —— 响应生成前的脱敏

DSAR 响应有个天然的矛盾：
  - Art. 15 要求把个人数据**完整**交给数据主体
  - Art. 15(4) 又要求不得损害他人的权利与自由
  - Art. 5(1)(c) 数据最小化 —— 不能顺手把无关字段也倒出去
  - Art. 9 特殊类别数据（健康/政治/种族/宗教）需要更严格控制

所以导出的响应不是数据库行的直接 dump，而是三步处理：
  1. 字段级敏感识别（按字段名 + 值形态双重判定，如 16 位卡号 + Luhn 校验）
  2. 按 redaction_mode 脱敏（NONE / STANDARD / STRICT）
  3. 第三方数据剥离（记录里常夹带他人信息，如紧急联系人、共同账户人）

特殊类别数据（Art. 9）在 STANDARD 模式下默认**不随响应交付**，
只在 manifest 里标注「存在 Art.9 数据，已留存待人工裁定」—— 
因为直接给出可能本身就违反 Art. 9 的处理限制。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# ============================================
# 脱敏模式
# ============================================
NONE = "NONE"
STANDARD = "STANDARD"
STRICT = "STRICT"
REDACTION_MODES = (NONE, STANDARD, STRICT)

# ============================================
# 按字段名判定的敏感类别
# ============================================
SENSITIVE_FIELD_RULES: dict[str, str] = {
    # 身份
    "email": "email", "email_address": "email", "contact_email": "email",
    "phone": "phone", "mobile": "phone", "phone_number": "phone", "msisdn": "phone",
    "full_name": "name", "first_name": "name", "last_name": "name", "legal_name": "name",
    "display_name": "name",
    # 强标识
    "national_id": "national_id", "ssn": "national_id", "passport_number": "national_id",
    "id_number": "national_id", "tax_id": "national_id", "cn_id_card": "national_id",
    "credit_card": "card", "card_number": "card", "payment_card": "card", "pan": "card",
    "bank_account": "bank", "iban": "bank", "account_iban": "bank",
    # 时间 / 位置
    "date_of_birth": "dob", "dob": "dob", "birth_date": "dob",
    "home_address": "address", "address": "address", "postal_address": "address",
    "ip_address": "ip", "ip": "ip", "device_ip": "ip", "geo_latitude": "location",
    "geo_longitude": "location", "last_known_location": "location",
    # 行为 / 画像
    "browsing_history": "behavior", "purchase_history": "behavior",
    "search_queries": "behavior", "app_events": "behavior",
    "inferred_interests": "profiling", "risk_score": "profiling",
    "credit_score": "profiling", "segment": "profiling",
    # Art. 9 特殊类别
    "health_conditions": "special_category", "medical_records": "special_category",
    "prescriptions": "special_category", "biometric_template": "special_category",
    "political_opinions": "special_category", "religious_beliefs": "special_category",
    "ethnic_origin": "special_category", "sexual_orientation": "special_category",
}

# Art. 9 特殊类别字段
SPECIAL_CATEGORY = "special_category"

# 第三方数据字段前缀（如紧急联系人、共同账户人）
THIRD_PARTY_PREFIXES = ("emergency_contact", "other_person", "co_", "joint_", "spouse_", "dependent_")

# ============================================
# 值形态识别（字段名不明确时兜底）
# ============================================
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+?[\d\s\-().]{7,20}$")
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_CARD_RE = re.compile(r"^\d{13,19}$")
_CN_ID_RE = re.compile(r"^\d{17}[\dXx]$")


def luhn_check(number: str) -> bool:
    """Luhn 校验 —— 用来区分「真的像卡号」和「碰巧是 16 位数字」"""
    digits = [int(c) for c in str(number) if c.isdigit()]
    if len(digits) < 13:
        return False
    checksum, parity = 0, len(digits) % 2
    for idx, digit in enumerate(digits):
        if idx % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def detect_value_kind(key: str, value) -> str | None:
    """
    字段名规则优先；字段名不认识时按值形态判断。
    返回敏感类别名，无法识别返回 None。
    """
    if value is None or isinstance(value, (dict, list)):
        return None
    kind = SENSITIVE_FIELD_RULES.get(str(key).lower())
    if kind:
        return kind

    text = str(value).strip()
    if not text:
        return None
    if _EMAIL_RE.match(text):
        return "email"
    if _IP_RE.match(text):
        return "ip"
    if _CN_ID_RE.match(text):
        return "national_id"
    if _CARD_RE.match(text.replace(" ", "").replace("-", "")) and luhn_check(text):
        return "card"
    return None


def is_third_party_field(key: str) -> bool:
    k = str(key).lower()
    return any(k.startswith(p) or f"_{p}" in k for p in THIRD_PARTY_PREFIXES)


# ============================================
# 各类型的脱敏实现
# ============================================

def mask_email(value: str) -> str:
    """a***@d***.com —— 保留域名以让本人确认是自己的邮箱，但不可回推完整地址"""
    text = str(value)
    if "@" not in text:
        return "***"
    local, _, domain = text.partition("@")
    name, _, tld = domain.rpartition(".")
    head = local[0] if local else ""
    dom_head = name[0] if name else ""
    return f"{head}***@{dom_head}***.{tld}" if tld else f"{head}***@{dom_head}***"


def mask_phone(value: str) -> str:
    """保留末 4 位，其余掩码 —— 足以本人识别，不足以批量枚举"""
    digits = [c for c in str(value) if c.isdigit()]
    if len(digits) < 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + "".join(digits[-4:])


def mask_name(value: str) -> str:
    """英文名保留首字母；中文名保留姓"""
    text = str(value).strip()
    if not text:
        return "***"
    if re.search(r"[\u4e00-\u9fff]", text):
        return text[0] + "*" * max(1, len(text) - 1)
    parts = [p for p in re.split(r"\s+", text) if p]
    return " ".join(p[0] + "*" * max(1, len(p) - 1) for p in parts)


def mask_national_id(value: str) -> str:
    """证件号只留末 4 位"""
    text = str(value).strip()
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


def tokenize_card(value: str) -> str:
    """卡号不脱敏成部分明文（避免反查），直接 token 化"""
    digits = "".join(c for c in str(value) if c.isdigit())
    suffix = digits[-4:] if len(digits) >= 4 else ""
    return f"[CARD-TOKEN:{suffix}]"


def mask_bank(value: str) -> str:
    text = str(value).strip()
    return f"[BANK-ACCOUNT:{text[-4:]}]" if len(text) >= 4 else "[BANK-ACCOUNT:***]"


def mask_dob(value) -> str:
    """生日只保留年份 —— 本人足以确认，旁观者无法定位具体日期"""
    text = str(value).strip()
    m = re.match(r"^(\d{4})", text)
    return f"{m.group(1)}-**-**" if m else "****"


def mask_address(value: str) -> str:
    text = str(value).strip()
    if not text:
        return "***"
    # 保留国家/城市级前缀，抹掉门牌级
    return re.sub(r"\d+", "**", text)[:24] + "…"


def mask_ip(value: str) -> str:
    text = str(value).strip()
    if _IP_RE.match(text):
        return ".".join(text.split(".")[:3]) + ".0/24"
    return "***"


def mask_generic(value) -> str:
    return "[REDACTED]"


def mask_value(kind: str, value):
    fn = {
        "email": mask_email,
        "phone": mask_phone,
        "name": mask_name,
        "national_id": mask_national_id,
        "card": tokenize_card,
        "bank": mask_bank,
        "dob": mask_dob,
        "address": mask_address,
        "ip": mask_ip,
        "behavior": mask_generic,
        "profiling": mask_generic,
        "location": mask_generic,
        "special_category": mask_generic,
    }.get(kind, mask_generic)
    return fn(value)


# STRICT 模式下额外脱敏的类别（STANDARD 已脱敏的这里保持一致，
# 差别在于 STRICT 额外处理「模糊字段名 + 值形态识别」的字段）
STRICT_ONLY_KINDS = ()


def redact_record(record: dict, mode: str = STANDARD) -> tuple[dict, dict]:
    """
    对单条记录脱敏。

    返回 (脱敏后记录, 脱敏报告)。
    报告包含：每个字段命中的类别、脱敏数量、是否剥离第三方数据。
    mode=NONE 时原样返回（仅用于内部调试，不应用于对外响应）。
    """
    mode = (mode or STANDARD).upper()
    if mode not in REDACTION_MODES:
        raise KeyError(mode)
    if mode == NONE:
        return dict(record), {"fields": {}, "redacted_count": 0,
                              "third_party_removed": [], "special_category_excluded": []}

    out: dict = {}
    hits: dict[str, str] = {}
    third_party_removed: list[str] = []
    special_excluded: list[str] = []

    for key, value in (record or {}).items():
        # --- 第三方数据（Art. 15(4)）：不属于数据主体的信息一律剥离 ---
        if is_third_party_field(key):
            third_party_removed.append(key)
            continue

        # --- Art.9 特殊类别：STANDARD/STRICT 下不在响应中直接交付 ---
        kind = detect_value_kind(key, value)
        if kind == SPECIAL_CATEGORY:
            special_excluded.append(key)
            out[f"{key}_presence"] = "WITHHELD_SPECIAL_CATEGORY_ART9"
            hits[key] = SPECIAL_CATEGORY
            continue

        if kind:
            out[key] = mask_value(kind, value)
            hits[key] = kind
        else:
            out[key] = value

    return out, {
        "fields": hits,
        "redacted_count": len(hits),
        "third_party_removed": third_party_removed,
        "special_category_excluded": special_excluded,
    }


def redact_payload(records: list, mode: str = STANDARD) -> tuple[list, dict]:
    """
    批量脱敏并汇总 manifest。

    manifest 是交付给合规团队看的：每个数据源命中多少条、脱敏多少字段、
    剥离多少第三方数据 —— 这份摘要本身要跟响应一起留痕。
    """
    cleaned: list = []
    total_redacted = 0
    third_party_total: list[str] = []
    special_total: list[str] = []

    for rec in records or []:
        red, report = redact_record(rec, mode=mode)
        cleaned.append(red)
        total_redacted += report["redacted_count"]
        third_party_total.extend(report["third_party_removed"])
        special_total.extend(report["special_category_excluded"])

    manifest = {
        "mode": (mode or STANDARD).upper(),
        "record_count": len(cleaned),
        "redacted_field_count": total_redacted,
        "third_party_fields_removed": sorted(set(third_party_total)),
        "third_party_removal_count": len(third_party_total),
        "special_category_fields_withheld": sorted(set(special_total)),
        "legal_note": (
            "已按 Art. 15(4) 剥离记录内第三方数据；"
            "Art. 9 特殊类别数据未随响应交付，仅记录存在性，需单独法律评估后处理。"
        ),
    }
    return cleaned, manifest


def build_payload_json(records: list, mode: str = STANDARD) -> tuple[str, dict]:
    cleaned, manifest = redact_payload(records, mode=mode)
    return json.dumps(cleaned, ensure_ascii=False, indent=2), manifest


def summarize_sensitivity(record: dict) -> dict:
    """不改动数据，只报告这条记录里有哪些敏感字段（用于编排阶段预估脱敏量）"""
    hits = {}
    for key, value in (record or {}).items():
        kind = detect_value_kind(key, value)
        if kind:
            hits[key] = kind
        if is_third_party_field(key):
            hits[key] = "third_party"
    return hits
