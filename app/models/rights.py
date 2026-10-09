"""数据主体权利目录 —— GDPR Art.12-22 + CCPA/CPRA 对应机制

这张表是整个平台的事实来源：请求类型能选什么、需不需要检索数据、
响应格式是什么、能不能拒绝，全部由此派生。UI 的下拉框、API 的校验、
测试的断言都读同一份，避免「文档写一套、代码跑另一套」。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Right:
    code: str                       # 内部请求类型代码
    name_zh: str
    name_en: str
    gdpr_article: str
    ccpa_equivalent: str
    description: str
    requires_verification: bool = True   # 几乎所有权利都必须先核验身份
    requires_discovery: bool = True      # 是否需要跨系统检索数据
    requires_delivery: bool = True       # 是否需要产出并留存响应
    default_format: str = "JSON"
    destructive: bool = False           # 是否不可逆（如删除）
    legal_basis_note: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name_zh": self.name_zh,
            "name_en": self.name_en,
            "gdpr_article": self.gdpr_article,
            "ccpa_equivalent": self.ccpa_equivalent,
            "description": self.description,
            "requires_verification": self.requires_verification,
            "requires_discovery": self.requires_discovery,
            "requires_delivery": self.requires_delivery,
            "default_format": self.default_format,
            "destructive": self.destructive,
            "legal_basis_note": self.legal_basis_note,
        }


RIGHTS: dict[str, Right] = {
    r.code: r for r in [
        Right(
            code="ACCESS", name_zh="访问权", name_en="Right of Access",
            gdpr_article="Art. 15",
            ccpa_equivalent="CCPA §1798.100 / §1798.110 (Know)",
            description="确认控制者是否处理本人个人数据、获取数据副本及处理目的。",
            default_format="JSON",
            legal_basis_note="副本须以commonly used machine-readable 形式提供（Art.15(3)）。",
        ),
        Right(
            code="RECTIFICATION", name_zh="更正权", name_en="Right to Rectification",
            gdpr_article="Art. 16",
            ccpa_equivalent="CCPA §1798.106 (Correct)",
            description="要求更正不准确或不完整的个人数据。",
            requires_delivery=False,
        ),
        Right(
            code="ERASURE", name_zh="删除权", name_en="Right to Erasure",
            gdpr_article="Art. 17",
            ccpa_equivalent="CCPA §1798.105 (Delete)",
            description="要求删除本人个人数据；但存在 Art.17(3) 除外情形时可被拒绝。",
            destructive=True,
            legal_basis_note="Art.17(3) 例外：履行法律义务、法庭主张、公共利益、科学研究等。",
        ),
        Right(
            code="PORTABILITY", name_zh="可携权", name_en="Right to Data Portability",
            gdpr_article="Art. 20",
            ccpa_equivalent="—（CPRA 无直接对应，见 §1798.140 定义）",
            description="以结构化、通用、机器可读格式获取本人提供的个人数据并转移给他人。",
            default_format="JSON",
        ),
        Right(
            code="RESTRICTION", name_zh="限制处理权", name_en="Right to Restriction of Processing",
            gdpr_article="Art. 18",
            ccpa_equivalent="CCPA §1798.121 (Limit Use of Sensitive PI)",
            description="要求暂停对本人数据的处理（数据仍可存储）。",
            destructive=True,
        ),
        Right(
            code="OBJECTION", name_zh="反对处理权", name_en="Right to Object",
            gdpr_article="Art. 21",
            ccpa_equivalent="CCPA §1798.120 (Opt-out of Sale/Sharing)",
            description="反对基于特定依据（含直接营销、剖析）的处理。",
            destructive=True,
        ),
        Right(
            code="AUTOMATED_DECISION", name_zh="自动化决策人工介入",
            name_en="Right to Human Intervention in Automated Decisions",
            gdpr_article="Art. 22",
            ccpa_equivalent="CCPA ADMT 提案（CPRA 范围内）",
            description="要求对仅基于自动化处理（含画像）作出的决定获得人工介入、表达观点、质疑决定。",
        ),
    ]
}

REQUEST_TYPES = tuple(RIGHTS.keys())

# 需要跨系统检索的请求类型（决定是否生成 discovery_tasks）
DISCOVERY_TYPES = tuple(c for c, r in RIGHTS.items() if r.requires_discovery)


def get_right(code: str) -> Right:
    key = (code or "").upper()
    if key not in RIGHTS:
        raise KeyError(key)
    return RIGHTS[key]


def is_valid_request_type(code: str) -> bool:
    return bool(code) and code.upper() in RIGHTS


def requires_discovery(code: str) -> bool:
    return is_valid_request_type(code) and RIGHTS[code.upper()].requires_discovery


def requires_verification(code: str) -> bool:
    return is_valid_request_type(code) and RIGHTS[code.upper()].requires_verification


def list_rights() -> list[dict]:
    return [RIGHTS[c].as_dict() for c in REQUEST_TYPES]
