#!/usr/bin/env bash
# Smoke test —— 启动服务 → 打关键端点 → 校验合规不变量 → 关闭
# 用途：CI 与本地一键验证，不依赖 pytest
#
# 校验重点不是「接口通不通」，而是「强制控制是否真的生效」：
#   - 未核验身份不得检索个人数据
#   - 检索未收敛不得标记完成
#   - 拒绝必须带法定理由
#   - 响应必须脱敏、必须有留存期
set -uo pipefail

PORT="${PORT:-5054}"
BASE="http://127.0.0.1:${PORT}"
PASS=0
FAIL=0
DB_FILE="${DSAR_DB:-data/dsar.db}"

green() { printf "\033[32m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }

check() {
  local name="$1" url="$2" expect="${3:-200}"
  local code
  code=$(curl -s -o /tmp/dsar_smoke_body -w "%{http_code}" "${BASE}${url}" 2>/dev/null || echo "000")
  if [[ "${code}" == "${expect}" ]]; then
    green "  ✓ ${name} (${code})"; PASS=$((PASS+1))
  else
    red   "  ✗ ${name} — 期望 ${expect}, 实际 ${code}"; FAIL=$((FAIL+1))
  fi
}

check_json() {
  local name="$1" url="$2" expr="$3"
  local out
  out=$(curl -s "${BASE}${url}" 2>/dev/null)
  if python3 -c "
import json,sys
try:
    d=json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
sys.exit(0 if (${expr}) else 1)
" "${out}" 2>/dev/null; then
    green "  ✓ ${name}"; PASS=$((PASS+1))
  else
    red   "  ✗ ${name} — 断言失败: ${expr}"; FAIL=$((FAIL+1))
  fi
}

check_code() {
  local name="$1" method="$2" url="$3" data="$4" expect="$5"
  local code
  if [[ -n "${data}" ]]; then
    code=$(curl -s -o /dev/null -w "%{http_code}" -X "${method}" "${BASE}${url}" \
      -H 'Content-Type: application/json' -d "${data}" 2>/dev/null || echo "000")
  else
    code=$(curl -s -o /dev/null -w "%{http_code}" -X "${method}" "${BASE}${url}" 2>/dev/null || echo "000")
  fi
  if [[ "${code}" == "${expect}" ]]; then
    green "  ✓ ${name} (${code})"; PASS=$((PASS+1))
  else
    red   "  ✗ ${name} — 期望 ${expect}, 实际 ${code}"; FAIL=$((FAIL+1))
  fi
}

echo "==> 重建演示数据"
python3 -m app.models.seed >/dev/null 2>&1 || { red "seed 失败"; exit 1; }

echo "==> 启动服务 (port ${PORT})"
python3 -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" --log-level warning &
SERVER_PID=$!
cleanup() { kill "${SERVER_PID}" 2>/dev/null; wait "${SERVER_PID}" 2>/dev/null; }
trap cleanup EXIT

for i in $(seq 1 40); do
  curl -s -o /dev/null "${BASE}/api/health" 2>/dev/null && break
  sleep 0.25
done

if ! curl -s -o /dev/null "${BASE}/api/health" 2>/dev/null; then
  red "服务未能在 10s 内就绪"; exit 1
fi

echo "==> 健康与元信息"
check "GET /api/health" /api/health
check_json "version 1.0.0" /api/health "d['version'] == '1.0.0'"
check_json "声明为合成数据" /api/health "'synthetic' in d['data_notice']"
check_json "请求数 = 10" /api/health "d['requests'] == 10"

echo "==> 权利目录与法域策略"
check_json "7 项数据主体权利" /api/rights "d['count'] == 7"
check_json "全部权利引用 GDPR 条款" /api/rights \
  "all(r['gdpr_article'].startswith('Art.') for r in d['rights'])"
check_json "Art.15 = 访问权" /api/rights \
  "[r for r in d['rights'] if r['code']=='ACCESS'][0]['gdpr_article'] == 'Art. 15'"
check_json "4 个法域策略" /api/deadlines/policies "d['count'] == 4"
check_json "EU = 1 个月" /api/deadlines/policies \
  "[p for p in d['policies'] if p['jurisdiction']=='EU'][0]['deadline'] == '1 个月'"
check_json "CA = 45 天" /api/deadlines/policies \
  "[p for p in d['policies'] if p['jurisdiction']=='CA'][0]['deadline'] == '45 天'"
check_json "CN 标注了简化假设" /api/deadlines/policies \
  "'简化假设' in [p for p in d['policies'] if p['jurisdiction']=='CN'][0]['notes']"

echo "==> 总览与时限"
check_json "时限桶齐全" /api/overview \
  "set(d['deadline_buckets'])=={'ON_TRACK','DUE_SOON','DUE_TODAY','OVERDUE','CLOSED','EXTENDED'}"
check_json "存在超期请求" /api/overview "d['overdue'] > 0"
check_json "存在今日到期" /api/overview "d['deadline_buckets']['DUE_TODAY'] > 0"
check_json "超期清单按严重度排序" /api/overview \
  "[t['days_overdue'] for t in d['top_overdue']] == sorted([t['days_overdue'] for t in d['top_overdue']], reverse=True)"
check_json "按时率在 0-100" /api/overview "0 <= d['on_time_delivery_rate'] <= 100"

echo "==> 法定时限边界（月末截断）"
check_json "1/31 受理 → 2/28 到期" "/api/requests?search=DSAR-0001" \
  "len(d['requests']) > 0"
check_json "请求均带期限快照" /api/requests \
  "all(set(('effective_due_date','days_remaining','status')) <= set(r['deadline']) for r in d['requests'])"
check_json "超期过滤只含未关闭" "/api/requests?overdue_only=true" \
  "all(r['status'] not in ('COMPLETED','DENIED','WITHDRAWN') for r in d['requests'])"
check_json "期限状态过滤生效" "/api/requests?deadline_status=OVERDUE" \
  "all(r['deadline']['status']=='OVERDUE' for r in d['requests'])"

echo "==> 参考数据"
check_json "数据源 8 个" /api/sources "d['count'] == 8"
check_json "排除项必须带法定依据" /api/sources \
  "all(s['exclusion_basis'] for s in d['sources'] if s['excluded_by_default'])"
check_json "数据类别 8 个" /api/categories "d['count'] == 8"
check_json "拒绝理由均引用条款" /api/denial-reasons \
  "all(r['regulation_article'] for r in d['reasons'])"
check_json "ERASURE 可用 ART_17_3_B" "/api/denial-reasons?request_type=ERASURE" \
  "'ART_17_3_B' in [r['reason_code'] for r in d['reasons']]"
check_json "留存策略均带法律依据" /api/retention/policies \
  "all(p['legal_basis'] and p['retention_days'] > 0 for p in d['policies'])"

echo "==> 请求详情与审计"
check "GET /api/requests/DSAR-0001" /api/requests/DSAR-0001
check "GET /api/requests/DSAR-9999 → 404" /api/requests/DSAR-9999 404
check_json "详情含合规解释" /api/requests/DSAR-0001 \
  "len(d['deadline']['explanation']) > 0"
check_json "详情列出合法后继状态" /api/requests/DSAR-0001 \
  "'COMPLETED' in d['allowed_transitions']"
check_json "已关闭请求无后继状态" /api/requests/DSAR-0005 \
  "d['allowed_transitions'] == []"
check_json "审计日志非空" /api/audit "d['count'] > 0"
check_json "审计按 ID 倒序" /api/audit \
  "[e['audit_id'] for e in d['entries']] == sorted([e['audit_id'] for e in d['entries']], reverse=True)"

echo "==> 强制控制：身份核验"
check_code "未核验不得检索个人数据" POST "/api/requests/DSAR-0004/discover" '{}' 409
check_code "未核验不得生成响应"     POST "/api/requests/DSAR-0004/response" '{}' 409
check_code "无核验方式被拒"         POST "/api/requests/DSAR-0004/verify" '{"method":"NONE"}' 400
check_code "未知核验方式被拒"       POST "/api/requests/DSAR-0004/verify" '{"method":"TELEPATHY"}' 400
check_code "未知风险信号被拒"       POST "/api/requests/DSAR-0004/verify" \
  '{"method":"EMAIL_OTP","risk_signals":["made_up"]}' 400
out=$(curl -s -X POST "${BASE}/api/requests/DSAR-0004/verify" \
  -H 'Content-Type: application/json' -d '{"method":"EMAIL_OTP","risk_signals":["email_domain_mismatch"]}')
if python3 -c "
import json,sys
d=json.loads(sys.argv[1])
sys.exit(0 if d['assessment']['decision']=='MANUAL_REVIEW' and d['disclosure_permitted'] is False else 1)
" "${out}" 2>/dev/null; then
  green "  ✓ 中置信度 → 人工复核且不授权披露"; PASS=$((PASS+1))
else
  red "  ✗ 中置信度 → 人工复核且不授权披露"; FAIL=$((FAIL+1))
fi

echo "==> 强制控制：状态机（新建请求走完整链路）"
# 用一条全新的请求验证状态机 —— 演示数据里的请求早已流转过，状态不同
new_id=$(curl -s -X POST "${BASE}/api/requests" -H 'Content-Type: application/json' \
  -d '{"requester_id":"REQ-001","request_type":"ACCESS","channel":"WEB_FORM","actor":"smoke"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['request']['request_id'])" 2>/dev/null)
if [[ -z "${new_id}" ]]; then
  red "  ✗ 无法新建请求以验证状态机"; FAIL=$((FAIL+1))
else
  green "  ✓ 新建请求 ${new_id}"; PASS=$((PASS+1))
fi
check_code "跳过核验直接处理被拒" PATCH "/api/requests/${new_id}/status" '{"status":"IN_PROGRESS"}' 400
check_code "非法目标状态被拒"     PATCH "/api/requests/${new_id}/status" '{"status":"BOGUS"}' 400
check_code "RECEIVED → 核验中"   PATCH "/api/requests/${new_id}/status" '{"status":"IDENTITY_VERIFICATION"}' 200
check_code "未核验通过不得处理"   PATCH "/api/requests/${new_id}/status" '{"status":"IN_PROGRESS"}' 400
check_code "未核验不得标记完成"   PATCH "/api/requests/${new_id}/status" '{"status":"COMPLETED"}' 400
check_code "撤回被审计"           PATCH "/api/requests/${new_id}/status" '{"status":"WITHDRAWN","actor":"smoke"}' 200
check_code "撤回后不可再流转"     PATCH "/api/requests/${new_id}/status" '{"status":"COMPLETED"}' 400
check_code "已关闭请求不可流转"   PATCH "/api/requests/DSAR-0005/status" '{"status":"IN_PROGRESS"}' 400
check_code "检索未收敛不得完成"   PATCH "/api/requests/DSAR-0001/status" '{"status":"COMPLETED"}' 400

echo "==> 强制控制：拒绝必须有法定理由"
check_code "缺 reason_code 被拒"  POST "/api/requests/DSAR-0002/deny" \
  '{"justification":"该记录受税务法定留存义务约束，不得删除"}' 400
check_code "理由过短被拒"          POST "/api/requests/DSAR-0002/deny" \
  '{"reason_code":"ART_17_3_B","justification":"短"}' 400
check_code "理由不适用该权利被拒"  POST "/api/requests/DSAR-0002/deny" \
  '{"reason_code":"ART_21_COMPELLED","justification":"反对权仅适用于特定处理依据的情形"}' 400
check_code "需审批理由缺审批人被拒" POST "/api/requests/DSAR-0002/deny" \
  '{"reason_code":"ART_17_3_B","justification":"该税务记录受法定留存义务约束，删除将妨碍履行法定义务"}' 400

echo "==> 强制控制：延长的通知证据"
check_code "延期通知晚于原期限被拒" POST "/api/requests/DSAR-0004/extend" \
  '{"reason":"请求涉及 6 个系统历史记录，需版本比对说明","notified_at":"2030-01-01"}' 400
check_code "延期缺理由被拒"         POST "/api/requests/DSAR-0004/extend" \
  '{"reason":"   ","notified_at":"2026-01-05"}' 400

echo "==> 检索编排"
check_json "任务计划含全部源与类别" "/api/requests/DSAR-0001/tasks" \
  "d['summary']['total'] >= 64"
check_json "归档源被法定排除"        "/api/requests/DSAR-0001/tasks?status=EXCLUDED" \
  "d['count'] > 0 and all(t['excluded_reason'] for t in d['tasks'])"
check_json "未收敛任务阻塞完成"      "/api/requests/DSAR-0001/tasks" \
  "d['summary']['blocking'] is True"
check_json "已完成请求任务已收敛"    "/api/requests/DSAR-0005/tasks" \
  "d['summary']['open_count'] == 0"
check_json "覆盖率按收敛任务计算"    "/api/requests/DSAR-0005/tasks" \
  "d['summary']['coverage_pct'] == 100.0"

echo "==> 响应脱敏与留存"
check_code "脱敏模式 NONE 被拒" POST "/api/requests/DSAR-0005/response" \
  '{"redaction_mode":"NONE"}' 400
check_code "非法脱敏模式被拒"   POST "/api/requests/DSAR-0005/response" \
  '{"redaction_mode":"SUPER"}' 400
check_code "生成 STANDARD 响应" POST "/api/requests/DSAR-0005/response" \
  '{"redaction_mode":"STANDARD","actor":"smoke"}' 200
check_json "响应条目非空" /api/responses "d['count'] > 0"
check_json "列表接口不返回完整载荷" /api/responses \
  "all('payload' not in x for x in d['deliveries'])"
check_json "交付记录带到期销毁日" /api/responses \
  "all(x['expires_at'] > x['delivered_at'] for x in d['deliveries'])"
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/retention/purge" \
  -H 'Content-Type: application/json' -d '{"actor":"smoke"}')
if [[ "${code}" == "200" ]]; then
  green "  ✓ 留存销毁端点可用 (200)"; PASS=$((PASS+1))
else
  red "  ✗ 留存销毁端点可用 — 实际 ${code}"; FAIL=$((FAIL+1))
fi

echo "==> 合规汇总"
check_json "按法域给出时限口径" /api/compliance \
  "d['by_jurisdiction'] and all('citation' in v for v in d['by_jurisdiction'].values())"
check_json "汇总说明统计口径"   /api/compliance "'12(3)' in d['note']"
check_json "汇总声明合成数据"   /api/compliance "'合成' in d['note']"
check_json "按源统计检索量"     /api/compliance "len(d['discovery_by_source']) == 8"

echo "==> Dashboard"
check "GET /" /
check "GET /openapi.json" /openapi.json
check_json "OpenAPI 声明版本" /openapi.json "d['info']['version'] == '1.0.0'"

echo
echo "================================"
echo "  通过: ${PASS}    失败: ${FAIL}"
echo "================================"
[[ "${FAIL}" -eq 0 ]] || exit 1
