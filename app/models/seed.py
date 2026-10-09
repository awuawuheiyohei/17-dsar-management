"""Seed data —— 合成演示数据

⚠️ 数据诚实性声明 ⚠️
以下全部为**虚构的合成数据**：
- 所有姓名、邮箱、手机号、证件号、卡号均为构造值，不对应任何真实个人
- 所有企业名、系统名（CRM / HRIS 等）为通用占位，不代表任何真实企业的内部系统
- 所有处理记录、处理目的、保留期限为演示假设，不构成任何合规结论
- 本项目**未对接**任何真实的 HR / CRM / 客服 / 分析系统

GDPR / CCPA 条款引用为公开法规文本，用于说明流程设计依据。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from .db import init_db, db_connection
from .deadlines import POLICIES, add_months, compute_deadline, get_policy

# ============================================
# 数据源（一个数据主体散落在这些系统里）
# ============================================
DATA_SOURCES = [
    # (source_id, name, system_type, owner_team, sensitive_pi, sla_hours, avg_records, excluded, basis)
    ("SRC-CRM", "客户关系管理系统", "CRM", "sales-ops", 0, 48, 4, 0, None),
    ("SRC-HRIS", "人力资源信息系统", "HRIS", "people-ops", 1, 72, 3, 0, None),
    ("SRC-ORDER", "订单与支付系统", "ORDER", "commerce-eng", 1, 48, 6, 0, None),
    ("SRC-ANALYTICS", "用户行为分析平台", "ANALYTICS", "data-eng", 1, 96, 3, 0, None),
    ("SRC-MARKETING", "营销自动化平台", "MARKETING", "growth-ops", 0, 72, 2, 0, None),
    ("SRC-SUPPORT", "客服工单系统", "SUPPORT", "customer-success", 0, 48, 5, 0, None),
    ("SRC-DWH", "企业数据仓库", "DWH", "data-eng", 1, 120, 4, 0, None),
    ("SRC-ARCHIVE", "冷备份归档系统", "BACKUP", "infra-ops", 1, 240, 1, 1,
     "Art. 17(3)(b) —— 履行法律规定的留存义务，备份副本在轮转周期内不可单独删除"),
]

# ============================================
# 数据类别
# ============================================
DATA_CATEGORIES = [
    # (category_id, name, gdpr_basis, sensitivity, third_party_risk, description)
    ("CAT-IDENTITY", "身份信息", "Art. 6(1)(b)", "STANDARD", 0, "姓名、生日、证件号"),
    ("CAT-CONTACT", "联系方式", "Art. 6(1)(b)", "STANDARD", 0, "邮箱、电话、地址"),
    ("CAT-EMPLOYMENT", "雇佣信息", "Art. 6(1)(b)", "STANDARD", 1, "职位、薪资、工号（常含经理等第三方信息）"),
    ("CAT-FINANCIAL", "财务与支付", "Art. 6(1)(b)", "HIGHLY_SENSITIVE", 0, "卡号、银行账户、交易记录"),
    ("CAT-TRANSACTION", "交易记录", "Art. 6(1)(b)", "STANDARD", 1, "订单明细（可能含收货人等他人信息）"),
    ("CAT-ONLINE", "在线行为数据", "Art. 6(1)(f) 合法利益", "SENSITIVE", 0, "浏览、搜索、App 事件"),
    ("CAT-PROFILING", "画像与推断", "Art. 22 / Art. 6(1)(f)", "SENSITIVE", 0, "推断兴趣、风控评分、信用评分"),
    ("CAT-LOCATION", "位置数据", "Art. 6(1)(f)", "SENSITIVE", 0, "IP 推断位置、最后已知位置"),
]

# ============================================
# 拒绝理由（Art. 12(4) 必须告知理由）
# ============================================
DENIAL_REASONS = [
    ("ART_15_4", "Art. 15(4)", "涉及他人权利与自由",
     "响应将涉及其他数据主体的个人数据，披露将损害其权利与自由（GDPR 明示的拒绝依据）",
     1, 1, "ACCESS,PORTABILITY"),
    ("ART_17_3_A", "Art. 17(3)(a)", "履行法律规定的义务",
     "删除将妨碍控制者履行法律法规规定的法定义务", 1, 1, "ERASURE"),
    ("ART_17_3_B", "Art. 17(3)(b)", "为提起、行使或抗辩法律主张所必需",
     "删除将妨碍提起、行使或抗辩法律主张所必需的留存", 1, 1, "ERASURE"),
    ("ART_17_3_E", "Art. 17(3)(e)", "公共利益或科学研究",
     "为公共利益任务或科学研究/统计目的，且数据已去标识化", 1, 1, "ERASURE"),
    ("ART_19_ABSENT", "Art. 19", "通知接收方的义务无法履行",
     "向下游接收方传播删除/更正指令的义务无法履行", 1, 1, "ERASURE,RECTIFICATION"),
    ("ART_12_5_REPEATED", "Art. 12(5)", "重复或无依据的过度请求",
     "请求明显无依据或过度，且属于重复请求；可收取合理费用或拒绝处理", 1, 0,
     "ACCESS,PORTABILITY,ERASURE"),
    ("IDENTITY_FAILED", "Art. 12(6)", "身份核验未通过",
     "无法确认请求者身份，出于防止第三方冒充数据主体的安全考虑不予处理", 0, 0,
     "ACCESS,RECTIFICATION,ERASURE,PORTABILITY,RESTRICTION,OBJECTION,AUTOMATED_DECISION"),
    ("NO_MATCHING_DATA", "Art. 12(4)", "未检索到匹配个人数据",
     "已完成跨系统检索，未发现与该主体匹配的个人数据；依据 Art. 12(4) 予以告知", 0, 0,
     "ACCESS,PORTABILITY,ERASURE,RECTIFICATION,RESTRICTION,OBJECTION,AUTOMATED_DECISION"),
    ("OUT_OF_SCOPE", "Art. 12(4)", "请求超出控制者管辖范围",
     "所涉数据并非由本控制者处理，或该处理已不再进行", 0, 0,
     "ACCESS,RECTIFICATION,ERASURE,PORTABILITY,RESTRICTION,OBJECTION,AUTOMATED_DECISION"),
    ("ART_21_COMPELLED", "Art. 21(1)", "存在强制性法律依据",
     "反对权仅适用于基于 Art. 6(1)(e)/(f) 的处理；本处理基于 Art. 6(1)(c) 强制义务", 0, 1,
     "OBJECTION"),
]

# ============================================
# 留存策略（数据最小化：响应本身也是 PI）
# ============================================
RETENTION_POLICIES = [
    ("RET-RESPONSE", "RESPONSE_PAYLOAD", 30,
     "Art. 5(1)(e) 存储限制",
     "已交付的响应副本含个人数据，仅保留 30 天用于数据主体下载，之后自动销毁并留审计记录"),
    ("RET-VERIFICATION", "IDENTITY_EVIDENCE", 180,
     "Art. 5(1)(e) + 举证责任",
     "身份核验的结论与证据引用保留 180 天，用于应对监管问询时的举证；原始证件材料不留存"),
    ("RET-DISCOVERY", "DISCOVERY_LOG", 365,
     "Art. 5(2) 问责制",
     "检索任务的执行留痕保留 365 天，构成 Art. 5(2) 问责原则的证据链"),
    ("RET-DENIAL", "DENIAL_RECORD", 1095,
     "Art. 12(4) + 举证责任",
     "拒绝记录保留 3 年，用于证明拒绝依据的合法性（举证责任在控制者）"),
    ("RET-AUDIT", "AUDIT_LOG", 1095,
     "Art. 5(2) 问责制",
     "全链路审计日志保留 3 年，不可修改（应用层只追加不更新）"),
]

# ============================================
# 请求者（合成）
# ============================================
REQUESTERS = [
    # (id, name, email, phone, type, jurisdiction, account, locale)
    ("REQ-001", "Wei Zhang", "wei.zhang@example.invalid", "+8613800000001",
     "CUSTOMER", "EU", "ACC-88001", "zh-CN"),
    ("REQ-002", "Anna Kowalski", "anna.kowalski@example.invalid", "+48501234567",
     "EMPLOYEE", "EU", "EMP-10432", "pl-PL"),
    ("REQ-003", "Jordan Blake", "jordan.blake@example.invalid", "+14155550142",
     "CUSTOMER", "CA", "ACC-91077", "en-US"),
    ("REQ-004", "Mei Lin", "mei.lin@example.invalid", "+8613900000009",
     "EMPLOYEE", "CN", "EMP-10880", "zh-CN"),
    ("REQ-005", "Sam Whitfield", "sam.whitfield@example.invalid", "+447700900123",
     "CUSTOMER", "UK", "ACC-76543", "en-GB"),
    ("REQ-006", "Priya Nair", "priya.nair@example.invalid", "+919812345678",
     "FORMER_USER", "EU", "ACC-60218", "en-IN"),
]


def _synth_record(requester_id: str, source_id: str, category_id: str,
                  base_email: str, full_name: str) -> dict:
    """
    为某个 (主体, 源, 类别) 生成一条合成记录。
    刻意在 EMPLOYMENT / TRANSACTION 类别里塞入第三方字段和 Art.9 字段，
    以便演示 Art. 15(4) 与 Art. 9 的脱敏处理。
    """
    common = {
        "record_source": source_id,
        "category": category_id,
        "full_name": full_name,
    }
    if category_id == "CAT-IDENTITY":
        return {**common, "national_id": "11010119900307123X",
                "date_of_birth": "1990-03-07", "account_id": requester_id}
    if category_id == "CAT-CONTACT":
        return {**common, "email": base_email, "phone": "+8613800000001",
                "home_address": "示例路 100 号 3 单元 501 室（虚构地址）"}
    if category_id == "CAT-EMPLOYMENT":
        # 第三方：经理姓名；且含健康数据触发 Art. 9
        return {**common, "job_title": "Software Engineer", "employee_id": requester_id,
                "manager_name": "Alex Supervisor", "emergency_contact_name": "Family Contact",
                "emergency_contact_phone": "+8613700000000",
                "health_conditions": "示例：既往过敏史（合成数据）"}
    if category_id == "CAT-FINANCIAL":
        # 4539 1488 0343 6467 —— 通过 Luhn 校验，用于演示卡号识别
        return {**common, "credit_card": "4539148803436467",
                "bank_account": "DE89370400440532013000"}
    if category_id == "CAT-TRANSACTION":
        # 第三方：收货人姓名
        return {**common, "order_id": "ORD-2026-0001", "amount": 129.9,
                "other_person_name": "Co Recipient", "co_recipient_email": "other@example.invalid",
                "purchase_history": "12 笔订单（合成）"}
    if category_id == "CAT-ONLINE":
        return {**common, "browsing_history": "合成事件流 90 条",
                "search_queries": "合成查询 14 条", "app_events": "合成事件 220 条"}
    if category_id == "CAT-PROFILING":
        return {**common, "inferred_interests": "合成标签：户外、数码",
                "risk_score": 0.21, "segment": "high_value_consumer"}
    if category_id == "CAT-LOCATION":
        return {**common, "ip_address": "203.0.113.42",
                "last_known_location": "合成：上海市（虚构）"}
    return {**common, "note": "合成占位记录"}


# 每条记录的字段数（用于 field_count）
def _count_fields(payload: dict) -> int:
    return len(payload)


# ============================================
# 演示请求（覆盖各种状态/期限/边界）
# ============================================
def _demo_requests(today: date) -> list[dict]:
    """
    刻意构造 5 种期限形态，让 Dashboard 一眼看出倒计时逻辑：
      - 严重超期（收到于 3 个月前）
      - 即将到期
      - 今日到期
      - 已合法延长
      - 正常在途
    """
    def mk(rid, rq, rtype, juris, days_ago, status, received_override=None, **kw):
        received = received_override or (today - timedelta(days=days_ago))
        policy = get_policy(juris)
        due = compute_deadline(received, policy)
        ext_due = kw.get("extended_due_date")
        return {
            "request_id": rid, "requester_id": rq, "request_type": rtype,
            "jurisdiction": juris, "received_date": received, "due_date": due,
            "extended_due_date": ext_due, "status": status, **kw,
        }

    return [
        # 严重超期：90 天前收到的访问请求，仍在处理中 → 明确超期
        mk("DSAR-0001", "REQ-001", "ACCESS", "EU", 90, "IN_PROGRESS",
           channel="WEB_FORM", priority="CRITICAL", assigned_to="analyst-a",
           outcome_summary=None),
        # 即将到期：25 天前收到的删除请求
        mk("DSAR-0002", "REQ-002", "ERASURE", "EU", 25, "IN_PROGRESS",
           channel="EMAIL", priority="HIGH", assigned_to="analyst-b"),
        # 已合法延长：Art.12(3) 两个月延长，通知在原期限内发出
        mk("DSAR-0003", "REQ-003", "ACCESS", "CA", 50, "CLARIFICATION_REQUESTED",
           channel="WEB_FORM", priority="NORMAL", assigned_to="analyst-a",
           extension_granted=True,
           extended_due_date=compute_deadline(today - timedelta(days=50), get_policy("CA"), extensions=1),
           extension_notified_at=(today - timedelta(days=50) + timedelta(days=30)).isoformat(),
           extension_reason="涉及 6 个业务系统的历史记录，需历史版本比对，"
                            "属于 CCPA §1798.130(a)(2) 允许的复杂情形",
           clock_stopped_days=7, clarification_rounds=1),
        # 正常在途：10 天前收到的可携请求
        mk("DSAR-0004", "REQ-005", "PORTABILITY", "UK", 10, "IDENTITY_VERIFICATION",
           channel="EMAIL", priority="NORMAL", assigned_to=None),
        # 已完成 + 已交付
        mk("DSAR-0005", "REQ-006", "ACCESS", "EU", 40, "COMPLETED",
           channel="IN_PERSON", priority="NORMAL", assigned_to="analyst-c",
           closed_at=(today - timedelta(days=5)).isoformat(),
           outcome_summary="已交付 6 个系统的脱敏数据副本，含第三方数据剥离说明"),
        # 拒绝：Art. 17(3)(b) 法律留存义务
        mk("DSAR-0006", "REQ-004", "ERASURE", "CN", 20, "DENIED",
           channel="IN_PERSON", priority="NORMAL", assigned_to="analyst-b",
           closed_at=(today - timedelta(days=3)).isoformat(),
           outcome_summary="税务与薪酬记录受法定留存义务约束，未予删除"),
        # 撤回
        mk("DSAR-0007", "REQ-001", "OBJECTION", "EU", 15, "WITHDRAWN",
           channel="WEB_FORM", priority="LOW",
           closed_at=(today - timedelta(days=2)).isoformat(),
           outcome_summary="数据主体主动撤回，检索任务全部终止"),
        # 今日到期：接收日 = 今日往前推一个月的对应日 → 到期日恒等于 today
        # （写死 days_ago 的话只有「恰好是某一天」才落到 DUE_TODAY，演示会随日期漂移）
        mk("DSAR-0008", "REQ-005", "ACCESS", "EU", 0, "IN_PROGRESS",
           received_override=add_months(today, -1),
           channel="PHONE", priority="HIGH", assigned_to="analyst-a"),
        # 过期关闭
        mk("DSAR-0009", "REQ-006", "ACCESS", "EU", 120, "EXPIRED",
           channel="WEB_FORM", priority="LOW",
           closed_at=(today - timedelta(days=40)).isoformat(),
           outcome_summary="身份核验多次未通过且长期无响应，按 Art. 12(6) 实践关闭"),
        # 部分完成：部分系统因留存义务排除
        mk("DSAR-0010", "REQ-003", "ERASURE", "CA", 35, "PARTIALLY_COMPLETED",
           channel="WEB_FORM", priority="NORMAL", assigned_to="analyst-c",
           closed_at=(today - timedelta(days=1)).isoformat(),
           outcome_summary="6 个系统已删除，冷备份归档因 Art. 17(3)(b) 留存义务排除"),
    ]


# ============================================
# 各演示请求的身份核验状态
# ============================================
VERIFICATIONS = {
    # request_id: (method, status, score, attempts, risk_signals)
    "DSAR-0001": ("EMAIL_OTP", "VERIFIED", 90, 1, []),
    "DSAR-0002": ("IDENTITY_DOCUMENT", "VERIFIED", 95, 1, []),
    "DSAR-0003": ("EMAIL_OTP", "VERIFIED", 80, 2, ["unverified_contact_channel"]),
    "DSAR-0004": ("SMS_OTP", "MANUAL_REVIEW", 75, 1, ["name_mismatch_on_account"]),
    "DSAR-0005": ("IN_PERSON", "VERIFIED", 98, 1, []),
    "DSAR-0006": ("IDENTITY_DOCUMENT", "VERIFIED", 95, 1, []),
    "DSAR-0007": ("EMAIL_OTP", "VERIFIED", 90, 1, []),
    "DSAR-0008": ("POSTAL_LETTER", "VERIFIED", 70, 1, []),
    "DSAR-0009": ("MANUAL_REVIEW", "FAILED", 50, 3, ["name_mismatch_on_account",
                                                     "prior_failed_attempt"]),
    "DSAR-0010": ("ACCOUNT_AUTHENTICATION", "VERIFIED", 100, 1, []),
}


def seed_all(verbose: bool = False) -> dict:
    """重建演示数据（先清空所有表再写入）"""
    init_db()
    today = date.today()
    now_iso = datetime.now().isoformat(timespec="seconds")

    with db_connection() as conn:
        for table in ("audit_log", "response_deliveries", "discovery_tasks",
                      "data_subject_records", "identity_verifications",
                      "dsar_requests", "data_categories", "data_sources",
                      "denial_reasons", "retention_policies", "requesters"):
            conn.execute(f"DELETE FROM {table}")

        # --- 请求者 ---
        for r in REQUESTERS:
            conn.execute(
                """INSERT INTO requesters
                   (requester_id, full_name, email, phone, subject_type,
                    jurisdiction, account_id, locale)
                   VALUES (?,?,?,?,?,?,?,?)""", r)

        # --- 数据源 ---
        for s in DATA_SOURCES:
            conn.execute(
                """INSERT INTO data_sources
                   (source_id, name, system_type, owner_team, has_sensitive_pi,
                    sla_hours, avg_records, excluded_by_default, exclusion_basis)
                   VALUES (?,?,?,?,?,?,?,?,?)""", s)

        # --- 数据类别 ---
        for c in DATA_CATEGORIES:
            conn.execute(
                """INSERT INTO data_categories
                   (category_id, name, gdpr_basis, sensitivity, third_party_risk,
                    description)
                   VALUES (?,?,?,?,?,?)""", c)

        # --- 拒绝理由 ---
        for d in DENIAL_REASONS:
            conn.execute(
                """INSERT INTO denial_reasons
                   (reason_code, regulation_article, title, description,
                    requires_legal_basis, requires_approval, applicable_types)
                   VALUES (?,?,?,?,?,?,?)""", d)

        # --- 留存策略 ---
        for p in RETENTION_POLICIES:
            conn.execute(
                """INSERT INTO retention_policies
                   (policy_id, artifact_type, retention_days, legal_basis, description)
                   VALUES (?,?,?,?,?)""", p)

        # --- 请求 ---
        requester_email = {r[0]: r[2] for r in REQUESTERS}
        requester_name = {r[0]: r[1] for r in REQUESTERS}

        for req in _demo_requests(today):
            ext = req.get("extended_due_date")
            conn.execute(
                """INSERT INTO dsar_requests
                   (request_id, requester_id, request_type, jurisdiction, channel,
                    status, received_date, due_date, extension_granted,
                    extended_due_date, extension_notified_at, extension_reason,
                    clock_stopped_days, clarification_rounds, assigned_to,
                    priority, closed_at, outcome_summary, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (req["request_id"], req["requester_id"], req["request_type"],
                 req["jurisdiction"], req.get("channel", "WEB_FORM"), req["status"],
                 req["received_date"].isoformat(), req["due_date"].isoformat(),
                 1 if req.get("extension_granted") else 0,
                 ext.isoformat() if ext else None,
                 req.get("extension_notified_at"),
                 req.get("extension_reason"),
                 req.get("clock_stopped_days", 0),
                 req.get("clarification_rounds", 0),
                 req.get("assigned_to"),
                 req.get("priority", "NORMAL"),
                 req.get("closed_at"),
                 req.get("outcome_summary"),
                 now_iso, now_iso),
            )

        # --- 身份核验 ---
        from .verification import assess_verification
        for rid, (method, status, score, attempts, signals) in VERIFICATIONS.items():
            req_row = conn.execute(
                "SELECT request_type FROM dsar_requests WHERE request_id = ?", (rid,)
            ).fetchone()
            req_type = req_row["request_type"] if req_row else "ACCESS"
            assessment = assess_verification(method, signals, req_type)
            started = now_iso
            conn.execute(
                """INSERT INTO identity_verifications
                   (request_id, method, status, attempts, max_attempts,
                    confidence_score, decision, risk_signals, justification,
                    started_at, completed_at, verified_by, evidence_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, method, status, attempts, 3, score,
                 assessment.decision, json.dumps(signals, ensure_ascii=False),
                 f"合成演示核验记录（{assessment.decision}）",
                 started, started if status in ("VERIFIED", "FAILED", "MANUAL_REVIEW") else None,
                 "privacy-officer-demo" if status == "VERIFIED" else None,
                 "EVIDENCE-DEMO-REF"),
            )

        # --- 数据主体记录（用于生成响应） ---
        rec_no = 0
        seeded_requesters: set[str] = set()
        for rid, rq, rtype, *_ in [(d["request_id"], d["requester_id"], d["request_type"])
                                   for d in _demo_requests(today)]:
            # data_subject_records 挂在 requester 上：同一个人的记录只生成一套，
            # 多个请求引用同一 requester 时不重复灌数据
            if rq in seeded_requesters:
                continue
            seeded_requesters.add(rq)
            base_email = requester_email.get(rq, "demo@example.invalid")
            name = requester_name.get(rq, "Demo Subject")
            # 每个主体在 6 个源 × 6 个类别上的记录
            for source_id in ("SRC-CRM", "SRC-HRIS", "SRC-ORDER",
                              "SRC-ANALYTICS", "SRC-MARKETING", "SRC-SUPPORT"):
                for category_id in ("CAT-IDENTITY", "CAT-CONTACT",
                                    "CAT-EMPLOYMENT", "CAT-FINANCIAL",
                                    "CAT-TRANSACTION", "CAT-ONLINE"):
                    payload = _synth_record(rq, source_id, category_id, base_email, name)
                    sensitive = [k for k in payload
                                 if k in ("national_id", "credit_card", "bank_account",
                                          "health_conditions", "other_person_name",
                                          "co_recipient_email", "emergency_contact_phone",
                                          "manager_name")]
                    third_party = int(any(
                        k.startswith(("other_person", "emergency_contact", "co_", "joint_"))
                        for k in payload))
                    special = int("health_conditions" in payload)
                    rec_no += 1
                    conn.execute(
                        """INSERT INTO data_subject_records
                           (record_id, requester_id, source_id, category_id,
                            subject_key, field_count, sensitive_fields,
                            third_party_data, contains_special_categories, payload)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (f"REC-{rec_no:05d}", rq, source_id, category_id,
                         f"{source_id}:{rq}", len(payload),
                         json.dumps(sensitive, ensure_ascii=False),
                         third_party, special,
                         json.dumps(payload, ensure_ascii=False)),
                    )

        conn.commit()

        counts = {
            t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in ("requesters", "data_sources", "data_categories",
                      "dsar_requests", "identity_verifications",
                      "data_subject_records", "denial_reasons",
                      "retention_policies")
        }

    # --- 检索任务：为「在途」请求生成计划（由 orchestration 幂等写入）---
    from .orchestration import upsert_tasks
    with db_connection() as conn:
        sources = [{"source_id": r["source_id"], "owner_team": r["owner_team"],
                    "active": r["active"], "excluded_by_default": r["excluded_by_default"],
                    "exclusion_basis": r["exclusion_basis"]}
                   for r in conn.execute("SELECT * FROM data_sources")]
        categories = [{"category_id": r["category_id"]}
                      for r in conn.execute("SELECT * FROM data_categories")]

        active = ("DSAR-0001", "DSAR-0002", "DSAR-0004", "DSAR-0008", "DSAR-0003")
        for rid in active:
            row = conn.execute(
                "SELECT request_type FROM dsar_requests WHERE request_id = ?", (rid,)
            ).fetchone()
            upsert_tasks(conn, rid, sources, categories, row["request_type"])

        # 部分收敛：让 completed 的请求能通过 workflow 的 no_open_tasks 校验
        for rid in ("DSAR-0005", "DSAR-0010", "DSAR-0006"):
            row = conn.execute(
                "SELECT request_type FROM dsar_requests WHERE request_id = ?", (rid,)
            ).fetchone()
            upsert_tasks(conn, rid, sources, categories, row["request_type"])
            conn.execute(
                """UPDATE discovery_tasks SET status = 'COMPLETED', record_count = 1,
                   started_at = datetime('now', '-3 days'), completed_at = datetime('now', '-2 days')
                   WHERE request_id = ? AND status = 'PENDING'""", (rid,))
        conn.commit()

        # --- 响应交付（给已完成的请求留一份，并设置到期日以演示留存销毁）---
        retention = conn.execute(
            "SELECT retention_days FROM retention_policies WHERE artifact_type = 'RESPONSE_PAYLOAD'"
        ).fetchone()
        keep_days = retention["retention_days"] if retention else 30
        for rid in ("DSAR-0005", "DSAR-0010"):
            recs = conn.execute(
                """SELECT payload FROM data_subject_records
                   WHERE requester_id = (SELECT requester_id FROM dsar_requests
                                         WHERE request_id = ?)
                     LIMIT 12""", (rid,)
            ).fetchall()
            records = [json.loads(r["payload"]) for r in recs]
            from .redaction import build_payload_json
            payload_json, manifest = build_payload_json(records, mode="STANDARD")
            delivered = datetime.now()
            expires = delivered + timedelta(days=keep_days)
            conn.execute(
                """INSERT INTO response_deliveries
                   (delivery_id, request_id, format, redaction_mode, record_count,
                    redacted_fields, third_party_masked, payload, manifest,
                    recipient, delivered_at, expires_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"DEL-{rid.replace('DSAR-', '')}-01", rid, "JSON", "STANDARD",
                 manifest["record_count"], manifest["redacted_field_count"],
                 manifest["third_party_removal_count"], payload_json,
                 json.dumps(manifest, ensure_ascii=False),
                 "requester@example.invalid",
                 delivered.isoformat(timespec="seconds"),
                 expires.isoformat(timespec="seconds"), "ACTIVE"),
            )

        # --- 审计留痕（演示已关闭请求的全链路记录）---
        audit_rows = [
            ("system", "SYSTEM", "request_received", "dsar_request", "DSAR-0001",
             {"channel": "WEB_FORM", "note": "合成演示：受理请求并起算 1 个月期限"}),
            ("analyst-a", "ANALYST", "identity_verified", "dsar_request", "DSAR-0001",
             {"method": "EMAIL_OTP", "score": 90, "decision": "AUTO_VERIFY"}),
            ("analyst-a", "ANALYST", "discovery_started", "dsar_request", "DSAR-0001",
             {"sources": 8, "categories": 8, "tasks_created": 64}),
            ("privacy-officer-demo", "PRIVACY_OFFICER", "extension_granted",
             "dsar_request", "DSAR-0003",
             {"reason": "复杂请求", "citation": "CCPA §1798.130(a)(2)"}),
            ("analyst-c", "ANALYST", "response_delivered", "response_delivery",
             "DEL-0005-01", {"records": 12, "redacted_fields": 40}),
            ("privacy-officer-demo", "PRIVACY_OFFICER", "request_denied",
             "dsar_request", "DSAR-0006",
             {"reason_code": "ART_17_3_B", "article": "Art. 17(3)(b)"}),
            ("data-subject", "DATA_SUBJECT", "request_withdrawn", "dsar_request",
             "DSAR-0007", {"note": "合成演示：数据主体撤回"}),
        ]
        for actor, role, action, etype, eid, details in audit_rows:
            conn.execute(
                """INSERT INTO audit_log (actor, actor_role, action, entity_type,
                                          entity_id, details)
                   VALUES (?,?,?,?,?,?)""",
                (actor, role, action, etype, eid,
                 json.dumps(details, ensure_ascii=False)),
            )
        conn.commit()

        counts["discovery_tasks"] = conn.execute(
            "SELECT COUNT(*) c FROM discovery_tasks").fetchone()["c"]
        counts["response_deliveries"] = conn.execute(
            "SELECT COUNT(*) c FROM response_deliveries").fetchone()["c"]
        counts["audit_log"] = conn.execute(
            "SELECT COUNT(*) c FROM audit_log").fetchone()["c"]

    if verbose:
        print(json.dumps(counts, ensure_ascii=False, indent=2))
    return counts


if __name__ == "__main__":
    seed_all(verbose=True)
