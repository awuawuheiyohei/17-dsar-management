-- DSAR Management Platform - SQLite schema (v1.0)
-- 数据主体请求（Data Subject Access Request）全生命周期管理
--
-- 对应 GDPR Art.12-22 数据主体权利 + CCPA/CPRA 对应机制
-- 全部表均为演示用合成数据，不含任何真实个人信息

-- ============================================
-- 请求者（数据主体）
-- ============================================
CREATE TABLE IF NOT EXISTS requesters (
    requester_id      TEXT PRIMARY KEY,                    -- REQ-001
    full_name         TEXT NOT NULL,
    email             TEXT NOT NULL UNIQUE,
    phone             TEXT,
    subject_type      TEXT NOT NULL DEFAULT 'CUSTOMER',    -- EMPLOYEE / CUSTOMER / POTENTIAL_CUSTOMER / FORMER_USER
    jurisdiction      TEXT NOT NULL DEFAULT 'EU',          -- EU / UK / CA / CN
    account_id        TEXT,
    locale            TEXT DEFAULT 'en-US',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_requesters_jurisdiction ON requesters(jurisdiction);

-- ============================================
-- 数据源（一个数据主体可能散落在多个系统）
-- ============================================
CREATE TABLE IF NOT EXISTS data_sources (
    source_id         TEXT PRIMARY KEY,                    -- SRC-CRM
    name              TEXT NOT NULL,
    system_type       TEXT NOT NULL,                       -- CRM / HRIS / ORDER / ANALYTICS / MARKETING / SUPPORT / DWH / BACKUP
    owner_team        TEXT NOT NULL DEFAULT 'unassigned',
    has_sensitive_pi  INTEGER NOT NULL DEFAULT 0,          -- 0/1 是否含敏感个人信息（GDPR Art.9 / CCPA CPRA）
    sla_hours         INTEGER NOT NULL DEFAULT 72,          -- 单系统检索回复时限
    avg_records       INTEGER NOT NULL DEFAULT 0,          -- 演示用：该源典型命中条数
    excluded_by_default INTEGER NOT NULL DEFAULT 0,        -- 1 = 默认不检索（如备份归档，需法律依据）
    exclusion_basis   TEXT,                                -- 排除的法定依据（如 Art.17(3)(b) 留存义务）
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================
-- 数据类别（按 GDPR 处理目的划分）
-- ============================================
CREATE TABLE IF NOT EXISTS data_categories (
    category_id       TEXT PRIMARY KEY,                    -- CAT-IDENTITY
    name              TEXT NOT NULL,
    gdpr_basis        TEXT,                                -- Art.6(1)(b) 等处理依据
    sensitivity       TEXT NOT NULL DEFAULT 'STANDARD',    -- STANDARD / SENSITIVE / HIGHLY_SENSITIVE
    third_party_risk  INTEGER NOT NULL DEFAULT 0,          -- 0/1 命中记录是否常含第三方数据（Art.15(4) 风险）
    description       TEXT
);

-- ============================================
-- DSAR 请求（核心实体）
-- ============================================
CREATE TABLE IF NOT EXISTS dsar_requests (
    request_id            TEXT PRIMARY KEY,                -- DSAR-0001
    requester_id          TEXT NOT NULL,
    request_type          TEXT NOT NULL,                   -- ACCESS / RECTIFICATION / ERASURE / PORTABILITY / RESTRICTION / OBJECTION / AUTOMATED_DECISION
    regulation            TEXT NOT NULL DEFAULT 'GDPR',    -- GDPR / UK_GDPR / CCPA_CPRA / PIPL
    jurisdiction          TEXT NOT NULL DEFAULT 'EU',      -- EU / UK / CA / CN（决定法定时限）
    channel               TEXT NOT NULL DEFAULT 'WEB_FORM',-- WEB_FORM / EMAIL / PHONE / IN_PERSON / REGULATOR
    status                TEXT NOT NULL DEFAULT 'RECEIVED',
    received_date         TEXT NOT NULL,                   -- YYYY-MM-DD，期限起算日（Art.12(3)）
    due_date              TEXT NOT NULL,                   -- 原始到期日
    extension_granted     INTEGER NOT NULL DEFAULT 0,
    extended_due_date     TEXT,                            -- 延长后到期日
    extension_notified_at TEXT,                            -- Art.12(3)：必须在一个月的期限届满前通知
    extension_reason      TEXT,
    clock_stopped_days    INTEGER NOT NULL DEFAULT 0,      -- 时钟暂停天数（行业实践；Art.12(6) 只授权身份核验）
    clarification_rounds  INTEGER NOT NULL DEFAULT 0,
    assigned_to           TEXT,
    priority              TEXT NOT NULL DEFAULT 'NORMAL',  -- LOW / NORMAL / HIGH / CRITICAL
    closed_at             TEXT,
    outcome_summary       TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (requester_id) REFERENCES requesters(requester_id)
);

CREATE INDEX IF NOT EXISTS idx_requests_status ON dsar_requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_due ON dsar_requests(due_date);
CREATE INDEX IF NOT EXISTS idx_requests_jurisdiction ON dsar_requests(jurisdiction);
CREATE INDEX IF NOT EXISTS idx_requests_type ON dsar_requests(request_type);
CREATE INDEX IF NOT EXISTS idx_requests_requester ON dsar_requests(requester_id);

-- ============================================
-- 身份核验（Art.12(6) —— 核实身份后方可处理）
-- ============================================
CREATE TABLE IF NOT EXISTS identity_verifications (
    verification_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT NOT NULL,
    method            TEXT NOT NULL,                       -- EMAIL_OTP / SMS_OTP / IDENTITY_DOCUMENT / IN_PERSON / ACCOUNT_AUTHENTICATION / POSTAL_LETTER / MANUAL_REVIEW / NONE
    status            TEXT NOT NULL DEFAULT 'PENDING',     -- PENDING / IN_PROGRESS / VERIFIED / MANUAL_REVIEW / FAILED / EXPIRED / ABANDONED
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    confidence_score  REAL NOT NULL DEFAULT 0,             -- 0-100，方法强度经风险信号调整后的置信度
    decision          TEXT,                                -- AUTO_VERIFY / MANUAL_REVIEW / REJECT
    risk_signals      TEXT,                                -- JSON list
    justification     TEXT,
    started_at        TEXT,
    expires_at        TEXT,                                -- OTP 有效期（防重放）
    completed_at      TEXT,
    verified_by       TEXT,
    evidence_ref      TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (request_id) REFERENCES dsar_requests(request_id)
);

CREATE INDEX IF NOT EXISTS idx_verify_request ON identity_verifications(request_id);
CREATE INDEX IF NOT EXISTS idx_verify_status ON identity_verifications(status);

-- ============================================
-- 数据主体在各系统中的记录（检索结果落地，用于生成响应）
-- ============================================
CREATE TABLE IF NOT EXISTS data_subject_records (
    record_id         TEXT PRIMARY KEY,                    -- REC-00001
    requester_id      TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    category_id       TEXT NOT NULL,
    subject_key       TEXT,                                -- 该源内用于匹配的稳定标识
    field_count       INTEGER NOT NULL DEFAULT 0,
    sensitive_fields  TEXT,                                -- JSON list（命中脱敏规则的字段名）
    third_party_data  INTEGER NOT NULL DEFAULT 0,          -- 0/1 记录内含他人数据 → Art.15(4) 需脱敏
    contains_special_categories INTEGER NOT NULL DEFAULT 0,-- 0/1 Art.9 特殊类别数据（种族/健康/政治/宗教）
    payload           TEXT NOT NULL,                       -- JSON，合成数据
    collected_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (requester_id) REFERENCES requesters(requester_id),
    FOREIGN KEY (source_id)    REFERENCES data_sources(source_id),
    FOREIGN KEY (category_id)  REFERENCES data_categories(category_id)
);

CREATE INDEX IF NOT EXISTS idx_dsr_rec_requester ON data_subject_records(requester_id);
CREATE INDEX IF NOT EXISTS idx_dsr_rec_source ON data_subject_records(source_id);

-- ============================================
-- 检索任务（跨系统编排：request × source × category）
-- ============================================
CREATE TABLE IF NOT EXISTS discovery_tasks (
    task_id           TEXT PRIMARY KEY,                    -- TSK-000001
    request_id        TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    category_id       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'PENDING',    -- PENDING / RUNNING / COMPLETED / NOT_FOUND / ERROR / EXCLUDED / WAIVED
    action            TEXT,                                -- COLLECT / ERASE / RECTIFY / RESTRICT / CONFIRM
    record_count      INTEGER NOT NULL DEFAULT 0,
    owner             TEXT,
    sla_due_at        TEXT,                                -- 该子任务的响应时限（源级 SLA）
    started_at        TEXT,
    completed_at      TEXT,
    duration_ms       INTEGER,
    error_note        TEXT,
    excluded_reason   TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (request_id) REFERENCES dsar_requests(request_id),
    FOREIGN KEY (source_id)  REFERENCES data_sources(source_id),
    FOREIGN KEY (category_id) REFERENCES data_categories(category_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_request ON discovery_tasks(request_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON discovery_tasks(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_unique ON discovery_tasks(request_id, source_id, category_id);

-- ============================================
-- 响应交付（响应本身也含个人信息 → 必须有留存期与销毁）
-- ============================================
CREATE TABLE IF NOT EXISTS response_deliveries (
    delivery_id       TEXT PRIMARY KEY,                    -- DEL-0001
    request_id        TEXT NOT NULL,
    format            TEXT NOT NULL DEFAULT 'JSON',       -- JSON / CSV / SECURE_LINK
    redaction_mode    TEXT NOT NULL DEFAULT 'STANDARD',   -- NONE / STANDARD / STRICT
    record_count      INTEGER NOT NULL DEFAULT 0,
    redacted_fields   INTEGER NOT NULL DEFAULT 0,
    third_party_masked INTEGER NOT NULL DEFAULT 0,
    payload           TEXT NOT NULL,                       -- JSON（已脱敏）
    manifest          TEXT,                                -- JSON：每个源的命中/脱敏摘要
    recipient         TEXT,
    delivered_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at        TEXT NOT NULL,                       -- 到期必须销毁
    status            TEXT NOT NULL DEFAULT 'ACTIVE',     -- ACTIVE / REVOKED / PURGED
    access_count      INTEGER NOT NULL DEFAULT 0,
    purged_at         TEXT,
    FOREIGN KEY (request_id) REFERENCES dsar_requests(request_id)
);

CREATE INDEX IF NOT EXISTS idx_delivery_request ON response_deliveries(request_id);
CREATE INDEX IF NOT EXISTS idx_delivery_expires ON response_deliveries(expires_at);

-- ============================================
-- 拒绝理由（Art.12(4) 必须告知理由，Art.17(3) 等除外情形）
-- ============================================
CREATE TABLE IF NOT EXISTS denial_reasons (
    reason_code       TEXT PRIMARY KEY,                    -- ART_17_3_B
    regulation_article TEXT NOT NULL,                      -- Art. 17(3)(b)
    title             TEXT NOT NULL,
    description       TEXT NOT NULL,
    requires_legal_basis INTEGER NOT NULL DEFAULT 1,       -- 1 = 必须填写法律依据说明
    requires_approval INTEGER NOT NULL DEFAULT 1,          -- 1 = 必须由隐私官/DPO 批准
    applicable_types  TEXT NOT NULL DEFAULT 'ERASURE',     -- 逗号分隔的 request_type
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================
-- 留存策略（数据最小化：响应本身也是 PI）
-- ============================================
CREATE TABLE IF NOT EXISTS retention_policies (
    policy_id         TEXT PRIMARY KEY,                    -- RET-RESPONSE
    artifact_type     TEXT NOT NULL,                       -- RESPONSE_PAYLOAD / IDENTITY_EVIDENCE / DISCOVERY_LOG / DENIAL_RECORD
    retention_days    INTEGER NOT NULL,
    legal_basis       TEXT NOT NULL,
    description       TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_retention_type ON retention_policies(artifact_type);

-- ============================================
-- 审计日志（谁请求 / 谁核验 / 谁批准 / 检索了什么 / 返回了什么）
-- ============================================
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor             TEXT,
    actor_role        TEXT,                                -- DATA_SUBJECT / ANALYST / PRIVACY_OFFICER / SYSTEM
    action            TEXT NOT NULL,
    entity_type       TEXT,
    entity_id         TEXT,
    details           TEXT,                                -- JSON
    occurred_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(occurred_at);
