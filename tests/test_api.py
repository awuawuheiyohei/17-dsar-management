"""API 集成测试 —— 覆盖端点契约 + 端到端业务流程 + 强制拒绝路径"""
from datetime import date, timedelta


class TestHealthAndMeta:
    def test_health(self, client):
        r = client.get("/api/health").json()
        assert r["status"] == "ok"
        assert r["service"] == "dsar-management-platform"
        assert r["version"] == "1.0.0"

    def test_health_declares_synthetic_data(self, client):
        """健康检查里就写明是合成数据 —— 避免被误当成真实系统"""
        assert "synthetic" in client.get("/api/health").json()["data_notice"]

    def test_rights_catalog(self, client):
        r = client.get("/api/rights").json()
        assert r["count"] == 7
        assert {x["code"] for x in r["rights"]} == {
            "ACCESS", "RECTIFICATION", "ERASURE", "PORTABILITY",
            "RESTRICTION", "OBJECTION", "AUTOMATED_DECISION"}

    def test_every_right_cites_a_gdpr_article(self, client):
        for r in client.get("/api/rights").json()["rights"]:
            assert r["gdpr_article"].startswith("Art.")

    def test_deadline_policies_for_four_jurisdictions(self, client):
        r = client.get("/api/deadlines/policies").json()
        assert r["count"] == 4
        assert {p["jurisdiction"] for p in r["policies"]} == {"EU", "UK", "CA", "CN"}

    def test_eu_policy_is_one_month_plus_two(self, client):
        p = next(p for p in client.get("/api/deadlines/policies").json()["policies"]
                 if p["jurisdiction"] == "EU")
        assert p["deadline"] == "1 个月" and p["extension"] == "+2 个月"

    def test_ca_policy_is_45_days(self, client):
        p = next(p for p in client.get("/api/deadlines/policies").json()["policies"]
                 if p["jurisdiction"] == "CA")
        assert p["deadline"] == "45 天"

    def test_cn_policy_discloses_simplification(self, client):
        """CN 用自然日近似工作日 —— README/接口都必须诚实标注这个简化"""
        p = next(p for p in client.get("/api/deadlines/policies").json()["policies"]
                 if p["jurisdiction"] == "CN")
        assert "简化假设" in p["notes"]

    def test_dashboard_html_served(self, client):
        assert client.get("/").status_code == 200

    def test_openapi_available(self, client):
        assert client.get("/openapi.json").status_code == 200


class TestReferenceData:
    def test_sources_listed(self, client):
        r = client.get("/api/sources").json()
        assert r["count"] == 8

    def test_excluded_source_carries_legal_basis(self, client):
        srcs = client.get("/api/sources").json()["sources"]
        excluded = [s for s in srcs if s["excluded_by_default"]]
        assert excluded
        for s in excluded:
            assert s["exclusion_basis"], f"{s['source_id']} 排除却没有依据"

    def test_categories_listed(self, client):
        assert client.get("/api/categories").json()["count"] == 8

    def test_denial_reasons_all_cite_articles(self, client):
        for r in client.get("/api/denial-reasons").json()["reasons"]:
            assert r["regulation_article"]

    def test_denial_reasons_filtered_by_type(self, client):
        reasons = client.get("/api/denial-reasons?request_type=ERASURE").json()["reasons"]
        codes = {r["reason_code"] for r in reasons}
        assert "ART_17_3_B" in codes
        assert "ART_21_COMPELLED" not in codes

    def test_retention_policies_have_legal_basis(self, client):
        for p in client.get("/api/retention/policies").json()["policies"]:
            assert p["retention_days"] > 0
            assert p["legal_basis"]

    def test_requester_detail_includes_data_footprint(self, client):
        r = client.get("/api/requesters/REQ-001").json()
        assert r["requester_id"] == "REQ-001"
        assert r["data_footprint"]

    def test_unknown_requester_404(self, client):
        assert client.get("/api/requesters/REQ-999").status_code == 404


class TestRequestIntake:
    def test_create_request(self, api):
        req = api.new_request("ACCESS", "EU")
        assert req["status"] == "RECEIVED"
        assert req["request_type"] == "ACCESS"

    def test_deadline_derived_on_create(self, api):
        req = api.new_request("ACCESS", "EU")
        assert req["due_date"] > req["received_date"]

    def test_cn_deadline_is_15_days(self, api):
        req = api.new_request("ACCESS", "CN")
        received = date.fromisoformat(req["received_date"])
        due = date.fromisoformat(req["due_date"])
        assert (due - received).days == 15

    def test_month_end_clamp_end_to_end(self, api):
        """端到端验证月末截断：1/31 受理 → 到期 2/28"""
        req = api.new_request("ACCESS", "EU", received_date="2026-01-31")
        assert req["due_date"] == "2026-02-28"

    def test_leap_year_clamp_end_to_end(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2024-01-31")
        assert req["due_date"] == "2024-02-29"

    def test_invalid_request_type_rejected(self, api):
        rid = api.new_requester()
        r = api.c.post("/api/requests", json={"requester_id": rid, "request_type": "TELEPORT"})
        assert r.status_code == 400
        assert "request_type" in str(r.json()["detail"])

    def test_missing_requester_rejected(self, client):
        assert client.post("/api/requests", json={"request_type": "ACCESS"}).status_code == 400

    def test_unknown_requester_404(self, client):
        r = client.post("/api/requests", json={"requester_id": "REQ-NOPE",
                                                "request_type": "ACCESS"})
        assert r.status_code == 404

    def test_invalid_jurisdiction_rejected(self, api):
        rid = api.new_requester()
        r = api.c.post("/api/requests", json={"requester_id": rid, "request_type": "ACCESS",
                                              "jurisdiction": "MARS"})
        assert r.status_code == 400

    def test_malformed_received_date_rejected(self, api):
        rid = api.new_requester()
        r = api.c.post("/api/requests", json={"requester_id": rid, "request_type": "ACCESS",
                                              "received_date": "31/12/2026"})
        assert r.status_code == 400

    def test_intake_receives_are_audited(self, api):
        req = api.new_request("ACCESS", "EU")
        entries = api.c.get(f"/api/audit?entity_id={req['request_id']}").json()["entries"]
        assert any(e["action"] == "request_received" for e in entries)

    def test_intake_audit_names_the_actor(self, api):
        req = api.new_request("ACCESS", "EU")
        entries = api.c.get(f"/api/audit?entity_id={req['request_id']}").json()["entries"]
        assert any(e["actor"] == "pytest" for e in entries)

    def test_urlencoded_form_intake(self, api):
        rid = api.new_requester()
        r = api.c.post("/api/requests/intake",
                       content=f"requester_id={rid}&request_type=ACCESS&channel=WEB_FORM&notes=a&notes=b",
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200
        assert r.json()["request"]["requester_id"] == rid
        assert "notes" in r.json()["intake_form"]["parsed_fields"]

    def test_form_intake_requires_fields(self, api):
        r = api.c.post("/api/requests/intake", content="request_type=ACCESS",
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 400


class TestRequestListing:
    def test_list_returns_seeded_requests(self, client):
        assert client.get("/api/requests").json()["total_requests"] >= 10

    def test_each_row_carries_deadline_view(self, client):
        row = client.get("/api/requests").json()["requests"][0]
        assert set(row["deadline"]) >= {"effective_due_date", "days_remaining", "status"}

    def test_each_row_carries_right_metadata(self, client):
        row = client.get("/api/requests").json()["requests"][0]
        assert row["right"]["gdpr_article"].startswith("Art.")

    def test_filter_by_status(self, client):
        rows = client.get("/api/requests?status=COMPLETED").json()["requests"]
        assert rows and all(r["status"] == "COMPLETED" for r in rows)

    def test_filter_by_type(self, client):
        rows = client.get("/api/requests?request_type=ERASURE").json()["requests"]
        assert all(r["request_type"] == "ERASURE" for r in rows)

    def test_filter_by_jurisdiction(self, client):
        rows = client.get("/api/requests?jurisdiction=CA").json()["requests"]
        assert all(r["jurisdiction"] == "CA" for r in rows)

    def test_deadline_status_filter(self, client):
        rows = client.get("/api/requests?deadline_status=OVERDUE").json()["requests"]
        assert all(r["deadline"]["status"] == "OVERDUE" for r in rows)

    def test_overdue_only_filter_excludes_closed(self, client):
        rows = client.get("/api/requests?overdue_only=true").json()["requests"]
        assert all(r["status"] not in ("COMPLETED", "DENIED", "WITHDRAWN") for r in rows)

    def test_search_by_id(self, client):
        rows = client.get("/api/requests?search=DSAR-0001").json()["requests"]
        assert any(r["request_id"] == "DSAR-0001" for r in rows)

    def test_open_tasks_count_present(self, client):
        row = next(r for r in client.get("/api/requests").json()["requests"]
                   if r["request_id"] == "DSAR-0001")
        assert row["open_tasks"] > 0


class TestRequestDetail:
    def test_detail_shape(self, client):
        d = client.get("/api/requests/DSAR-0001").json()
        assert set(d) >= {"request", "requester", "right", "deadline",
                          "verifications", "discovery_tasks", "deliveries", "audit_trail"}

    def test_detail_explains_status(self, client):
        assert client.get("/api/requests/DSAR-0001").json()["status_description"]

    def test_detail_lists_allowed_transitions(self, client):
        d = client.get("/api/requests/DSAR-0001").json()
        assert "COMPLETED" in d["allowed_transitions"]

    def test_completed_request_has_no_transitions(self, client):
        assert client.get("/api/requests/DSAR-0005").json()["allowed_transitions"] == []

    def test_unknown_request_404(self, client):
        assert client.get("/api/requests/DSAR-9999").status_code == 404


class TestStateMachineEnforcement:
    def test_cannot_skip_verification(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.patch(f"/api/requests/{req['request_id']}/status",
                        json={"status": "IN_PROGRESS"})
        assert r.status_code == 400

    def test_cannot_complete_without_verification(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.patch(f"/api/requests/{req['request_id']}/status",
                        json={"status": "COMPLETED"})
        assert r.status_code == 400

    def test_invalid_target_status_rejected(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.patch(f"/api/requests/{req['request_id']}/status",
                        json={"status": "BOGUS"})
        assert r.status_code == 400

    def test_illegal_jump_error_is_structured(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.patch(f"/api/requests/{req['request_id']}/status",
                        json={"status": "COMPLETED"})
        detail = r.json()["detail"]
        assert detail["error"] == "transition_rejected"
        assert detail["reasons"]

    def test_illegal_jump_names_the_rule(self, api):
        req = api.new_request("ACCESS", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        detail = api.c.patch(f"/api/requests/{req['request_id']}/status",
                             json={"status": "COMPLETED"}).json()["detail"]
        assert any("非法状态跳转" in x for x in detail["reasons"])

    def test_unknown_request_transition_404(self, client):
        assert client.patch("/api/requests/DSAR-9999/status",
                            json={"status": "WITHDRAWN"}).status_code == 404

    def test_status_change_is_audited(self, api):
        req = api.new_request("ACCESS", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION", "actor": "auditor"})
        entries = api.c.get(f"/api/audit?entity_id={req['request_id']}&action=status_change").json()
        assert any(e["actor"] == "auditor" for e in entries["entries"])

    def test_closed_request_cannot_move(self, client):
        r = client.patch("/api/requests/DSAR-0005/status", json={"status": "IN_PROGRESS"})
        assert r.status_code == 400


class TestIdentityVerification:
    def test_discovery_blocked_before_verification(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.post(f"/api/requests/{req['request_id']}/discover", json={})
        assert r.status_code == 409
        assert "12(6)" in str(r.json()["detail"])

    def test_response_blocked_before_verification(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.post(f"/api/requests/{req['request_id']}/response", json={})
        assert r.status_code == 409

    def test_start_verification_returns_assessment(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.post(f"/api/requests/{req['request_id']}/verify",
                       json={"method": "EMAIL_OTP"}).json()
        assert r["assessment"]["decision"] == "AUTO_VERIFY"
        assert r["disclosure_permitted"] is True

    def test_none_method_rejected(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.post(f"/api/requests/{req['request_id']}/verify", json={"method": "NONE"})
        assert r.status_code == 400

    def test_unknown_method_rejected(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.post(f"/api/requests/{req['request_id']}/verify",
                       json={"method": "TELEPATHY"})
        assert r.status_code == 400

    def test_unknown_risk_signal_rejected(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.post(f"/api/requests/{req['request_id']}/verify",
                       json={"method": "EMAIL_OTP", "risk_signals": ["made_up"]})
        assert r.status_code == 400

    def test_medium_confidence_goes_to_manual_review(self, api):
        req = api.new_request("ACCESS", "EU")
        r = api.c.post(f"/api/requests/{req['request_id']}/verify",
                       json={"method": "EMAIL_OTP",
                             "risk_signals": ["email_domain_mismatch"]}).json()
        assert r["assessment"]["decision"] == "MANUAL_REVIEW"
        assert r["disclosure_permitted"] is False

    def test_manual_review_still_blocks_discovery(self, api):
        req = api.new_request("ACCESS", "EU")
        api.c.post(f"/api/requests/{req['request_id']}/verify",
                   json={"method": "EMAIL_OTP", "risk_signals": ["email_domain_mismatch"]})
        assert api.c.post(f"/api/requests/{req['request_id']}/discover", json={}).status_code == 409

    def test_approval_requires_note(self, api):
        req = api.new_request("ACCESS", "EU")
        vid = api.c.post(f"/api/requests/{req['request_id']}/verify",
                         json={"method": "POSTAL_LETTER"}).json()["verification"]["verification_id"]
        assert api.c.post(f"/api/verify/{vid}/approve", json={"note": ""}).status_code == 400

    def test_approval_unlocks_discovery(self, api):
        req = api.new_request("ACCESS", "EU")
        vid = api.c.post(f"/api/requests/{req['request_id']}/verify",
                         json={"method": "POSTAL_LETTER"}).json()["verification"]["verification_id"]
        api.c.post(f"/api/verify/{vid}/approve",
                   json={"note": "证件与档案比对一致", "verified_by": "po"})
        assert api.c.post(f"/api/requests/{req['request_id']}/discover", json={}).status_code == 200

    def test_approve_wrong_state_rejected(self, api):
        req = api.new_request("ACCESS", "EU")
        vid = api.c.post(f"/api/requests/{req['request_id']}/verify",
                         json={"method": "EMAIL_OTP"}).json()["verification"]["verification_id"]
        api.c.post(f"/api/verify/{vid}/submit", json={"evidence_ref": "EV-1"})
        r = api.c.post(f"/api/verify/{vid}/approve", json={"note": "x"})
        assert r.status_code == 409

    def test_approve_unknown_id_404(self, client):
        assert client.post("/api/verify/999999/approve", json={"note": "x"}).status_code == 404

    def test_submit_without_evidence_fails(self, api):
        req = api.new_request("ACCESS", "EU")
        vid = api.c.post(f"/api/requests/{req['request_id']}/verify",
                         json={"method": "EMAIL_OTP"}).json()["verification"]["verification_id"]
        r = api.c.post(f"/api/verify/{vid}/submit", json={}).json()
        assert r["verification"]["status"] == "FAILED"

    def test_destructive_request_gets_extra_penalty(self, api):
        req = api.new_request("ERASURE", "EU")
        a = api.c.post(f"/api/requests/{req['request_id']}/verify",
                       json={"method": "SMS_OTP"}).json()["assessment"]
        assert a["score"] == 75

    def test_verification_is_audited(self, api):
        req = api.new_request("ACCESS", "EU")
        api.c.post(f"/api/requests/{req['request_id']}/verify",
                   json={"method": "EMAIL_OTP", "actor": "sec-analyst"})
        entries = api.c.get(f"/api/audit?entity_id={req['request_id']}"
                            f"&action=verification_started").json()["entries"]
        assert any(e["actor"] == "sec-analyst" for e in entries)


class TestExtension:
    def test_valid_extension_extends_due_date(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        r = api.c.post(f"/api/requests/{req['request_id']}/extend",
                       json={"reason": "涉及 6 个系统历史记录，需版本比对", "notified_at": "2026-02-01"})
        assert r.status_code == 200
        assert r.json()["request"]["extended_due_date"] == "2026-04-15"

    def test_late_notice_rejected(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        r = api.c.post(f"/api/requests/{req['request_id']}/extend",
                       json={"reason": "复杂请求需要跨系统比对说明", "notified_at": "2026-03-01"})
        assert r.status_code == 400
        assert "晚于原始期限" in str(r.json()["detail"])

    def test_missing_reason_rejected(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        r = api.c.post(f"/api/requests/{req['request_id']}/extend",
                       json={"reason": "  ", "notified_at": "2026-02-01"})
        assert r.status_code == 400

    def test_missing_notified_at_rejected(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        r = api.c.post(f"/api/requests/{req['request_id']}/extend", json={"reason": "复杂请求"})
        assert r.status_code == 400

    def test_double_extension_conflicts(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        body = {"reason": "复杂请求", "notified_at": "2026-02-01"}
        api.c.post(f"/api/requests/{req['request_id']}/extend", json=body)
        assert api.c.post(f"/api/requests/{req['request_id']}/extend", json=body).status_code == 409

    def test_cn_cannot_be_extended(self, api):
        req = api.new_request("ACCESS", "CN", received_date="2026-01-15")
        r = api.c.post(f"/api/requests/{req['request_id']}/extend",
                       json={"reason": "复杂请求", "notified_at": "2026-01-20"})
        assert r.status_code == 400

    def test_rejected_extension_is_audited(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        api.c.post(f"/api/requests/{req['request_id']}/extend",
                   json={"reason": "复杂请求", "notified_at": "2026-06-01", "actor": "bad-actor"})
        entries = api.c.get(f"/api/audit?entity_id={req['request_id']}"
                            f"&action=extension_rejected").json()
        assert entries["count"] >= 1

    def test_extension_audited_on_success(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        api.c.post(f"/api/requests/{req['request_id']}/extend",
                   json={"reason": "复杂请求", "notified_at": "2026-02-01", "actor": "po"})
        entries = api.c.get(f"/api/audit?entity_id={req['request_id']}"
                            f"&action=extension_granted").json()
        assert entries["count"] == 1


class TestClarificationAndClockStop:
    def test_clarification_requires_verification_first(self, api):
        req = api.new_request("ACCESS", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        r = api.c.post(f"/api/requests/{req['request_id']}/clarify",
                       json={"question": "请确认您要处理的账号是哪一个"})
        assert r.status_code == 400

    def test_clarification_stops_the_clock(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/clarify",
                       json={"question": "请确认您要处理的账号是哪一个", "days": 20}).json()
        assert r["request"]["clock_stopped_days"] == 20
        assert r["request"]["clarification_rounds"] == 1

    def test_clock_stop_extends_effective_due_date(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/clarify",
                       json={"question": "请确认您要处理的账号是哪一个", "days": 20}).json()
        # 原始 2/15 + 20 天暂停 = 3/7
        assert r["deadline"]["effective_due_date"] == "2026-03-07"

    def test_clock_stop_capped(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/clarify",
                       json={"question": "请确认您要处理的账号是哪一个", "days": 900})
        assert r.status_code == 400

    def test_short_question_rejected(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/clarify",
                       json={"question": "who?"})
        assert r.status_code == 400

    def test_clarification_answer_flow(self, api):
        req = api.new_request("ACCESS", "EU", received_date="2026-01-15")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status", json={"status": "IN_PROGRESS"})
        assert api.c.post(f"/api/requests/{req['request_id']}/clarify",
                          json={"question": "请确认您要处理的账号是哪一个",
                                "days": 10}).status_code == 200
        r = api.c.patch(f"/api/requests/{req['request_id']}/status",
                        json={"status": "CLARIFICATION_ANSWERED"})
        assert r.status_code == 200


class TestDiscoveryOrchestration:
    def test_orchestration_creates_cartesian_tasks(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        r = api.c.post(f"/api/requests/{req['request_id']}/discover", json={}).json()
        assert r["created"] == 64  # 8 源 × 8 类别

    def test_excluded_source_pre_marked(self, api):
        req = api.new_request("ERASURE", "EU")
        api.verify_ok(req["request_id"])
        api.c.post(f"/api/requests/{req['request_id']}/discover", json={})
        tasks = api.c.get(f"/api/requests/{req['request_id']}/tasks"
                          f"?status=EXCLUDED").json()["tasks"]
        assert tasks
        assert all(t["excluded_reason"] for t in tasks)

    def test_action_matches_request_type(self, api):
        req = api.new_request("ERASURE", "EU")
        api.verify_ok(req["request_id"])
        r = api.c.post(f"/api/requests/{req['request_id']}/discover", json={}).json()
        assert r["action"] == "ERASE"

    def test_orchestration_is_idempotent(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        api.c.post(f"/api/requests/{req['request_id']}/discover", json={})
        r = api.c.post(f"/api/requests/{req['request_id']}/discover", json={}).json()
        assert r["created"] == 0 and r["skipped"] == 64

    def test_category_subset_filter(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        r = api.c.post(f"/api/requests/{req['request_id']}/discover",
                       json={"category_ids": ["CAT-IDENTITY", "CAT-CONTACT"]}).json()
        assert r["created"] == 16

    def test_unknown_category_404(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        r = api.c.post(f"/api/requests/{req['request_id']}/discover",
                       json={"category_ids": ["CAT-NOPE"]})
        assert r.status_code == 404

    def test_tasks_for_unknown_request_404(self, client):
        assert client.get("/api/requests/DSAR-9999/tasks").status_code == 404

    def test_summary_reports_blocking(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        api.c.post(f"/api/requests/{req['request_id']}/discover", json={})
        s = api.c.get(f"/api/requests/{req['request_id']}/tasks").json()["summary"]
        assert s["blocking"] and s["open_count"] > 0

    def test_task_completion_without_records_rejected(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        api.c.post(f"/api/requests/{req['request_id']}/discover", json={})
        tid = api.c.get(f"/api/requests/{req['request_id']}/tasks").json()["tasks"][0]["task_id"]
        r = api.c.patch(f"/api/tasks/{tid}", json={"status": "COMPLETED", "record_count": 0})
        assert r.status_code == 400

    def test_task_flow_pending_to_running_to_completed(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        api.c.post(f"/api/requests/{req['request_id']}/discover", json={})
        tid = api.c.get(f"/api/requests/{req['request_id']}/tasks").json()["tasks"][0]["task_id"]
        assert api.c.patch(f"/api/tasks/{tid}", json={"status": "RUNNING"}).status_code == 200
        assert api.c.patch(f"/api/tasks/{tid}",
                           json={"status": "COMPLETED", "record_count": 3}).status_code == 200

    def test_completed_task_is_frozen(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        api.c.post(f"/api/requests/{req['request_id']}/discover", json={})
        tid = api.c.get(f"/api/requests/{req['request_id']}/tasks").json()["tasks"][0]["task_id"]
        # 任务也不能从 PENDING 直接跳 COMPLETED：检索动作必须先真正开始
        assert api.c.patch(f"/api/tasks/{tid}",
                           json={"status": "COMPLETED", "record_count": 3}).status_code == 400
        api.c.patch(f"/api/tasks/{tid}", json={"status": "RUNNING"})
        api.c.patch(f"/api/tasks/{tid}", json={"status": "COMPLETED", "record_count": 3})
        r = api.c.patch(f"/api/tasks/{tid}", json={"status": "RUNNING"})
        assert r.status_code == 400

    def test_unknown_task_404(self, client):
        assert client.patch("/api/tasks/TSK-NOPE", json={"status": "RUNNING"}).status_code == 404

    def test_task_update_audited(self, api):
        req = api.new_request("ACCESS", "EU")
        api.verify_ok(req["request_id"])
        api.c.post(f"/api/requests/{req['request_id']}/discover", json={})
        tid = api.c.get(f"/api/requests/{req['request_id']}/tasks").json()["tasks"][0]["task_id"]
        api.c.patch(f"/api/tasks/{tid}", json={"status": "RUNNING", "actor": "data-eng-1"})
        api.c.patch(f"/api/tasks/{tid}", json={"status": "COMPLETED", "record_count": 1,
                                                "actor": "data-eng-1"})
        entries = api.c.get("/api/audit?action=task_updated").json()["entries"]
        assert any(e["actor"] == "data-eng-1" for e in entries)


class TestDenial:
    def test_denial_requires_reason_code(self, api):
        req = api.new_request("ACCESS", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status", json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/deny",
                       json={"justification": "该记录受法定留存义务约束，不得删除"})
        assert r.status_code == 400

    def test_denial_requires_substantive_justification(self, api):
        req = api.new_request("ERASURE", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status", json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/deny",
                       json={"reason_code": "ART_17_3_B", "justification": "短"})
        assert r.status_code == 400

    def test_reason_must_apply_to_request_type(self, api):
        req = api.new_request("ACCESS", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status", json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/deny", json={
            "reason_code": "ART_17_3_B",
            "justification": "该记录受税务法定留存义务约束，不得删除",
            "approved_by": "po"})
        assert r.status_code == 400
        assert "不适用于" in str(r.json()["detail"])

    def test_reason_requiring_approval_needs_approver(self, api):
        req = api.new_request("ERASURE", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status", json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/deny", json={
            "reason_code": "ART_17_3_B",
            "justification": "该记录受税务法定留存义务约束，不得删除"})
        assert r.status_code == 400
        assert "approved_by" in str(r.json()["detail"])

    def test_valid_denial_succeeds_and_cites_article(self, api):
        req = api.new_request("ERASURE", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status", json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/deny", json={
            "reason_code": "ART_17_3_B",
            "justification": "该税务记录受法定留存义务约束，删除将妨碍履行法定义务",
            "approved_by": "privacy-officer-pytest"}).json()
        assert r["request"]["status"] == "DENIED"
        assert r["article"] == "Art. 17(3)(b)"
        assert "Art. 17(3)(b)" in r["outcome_summary"]

    def test_denial_closes_open_tasks(self, api):
        req = api.new_request("ERASURE", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status", json={"status": "IN_PROGRESS"})
        api.c.post(f"/api/requests/{req['request_id']}/discover", json={})
        api.c.post(f"/api/requests/{req['request_id']}/deny", json={
            "reason_code": "ART_17_3_B",
            "justification": "该税务记录受法定留存义务约束，删除将妨碍履行法定义务",
            "approved_by": "po"})
        s = api.c.get(f"/api/requests/{req['request_id']}/tasks").json()["summary"]
        assert s["open_count"] == 0

    def test_denial_audited_with_article(self, api):
        req = api.new_request("ERASURE", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status", json={"status": "IN_PROGRESS"})
        api.c.post(f"/api/requests/{req['request_id']}/deny", json={
            "reason_code": "ART_17_3_B",
            "justification": "该税务记录受法定留存义务约束，删除将妨碍履行法定义务",
            "approved_by": "po"})
        entries = api.c.get(f"/api/audit?entity_id={req['request_id']}"
                            f"&action=request_denied").json()["entries"]
        assert entries[0]["details"]["article"] == "Art. 17(3)(b)"

    def test_unknown_reason_code_404(self, api):
        req = api.new_request("ERASURE", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(req["request_id"])
        api.c.patch(f"/api/requests/{req['request_id']}/status", json={"status": "IN_PROGRESS"})
        r = api.c.post(f"/api/requests/{req['request_id']}/deny", json={
            "reason_code": "MADE_UP", "justification": "随便写一个足够长的拒绝理由说明"})
        assert r.status_code == 404

    def test_identity_failure_is_a_valid_denial_reason(self, api):
        req = api.new_request("ACCESS", "EU")
        api.c.patch(f"/api/requests/{req['request_id']}/status",
                    json={"status": "IDENTITY_VERIFICATION"})
        r = api.c.post(f"/api/requests/{req['request_id']}/deny", json={
            "reason_code": "IDENTITY_FAILED",
            "justification": "多次提交的身份材料无法确认请求者与账户主体一致"})
        assert r.status_code == 200


class TestResponseAndRetention:
    def test_response_generated_after_verification(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        api.verify_ok(req["request_id"])
        r = api.c.post(f"/api/requests/{req['request_id']}/response", json={}).json()
        assert r["delivery"]["record_count"] > 0
        assert r["delivery"]["redaction_mode"] == "STANDARD"

    def test_art9_data_withheld(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-002")
        api.verify_ok(req["request_id"])
        m = api.c.post(f"/api/requests/{req['request_id']}/response", json={}).json()["manifest"]
        assert "health_conditions" in m["special_category_fields_withheld"]

    def test_third_party_fields_removed(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        api.verify_ok(req["request_id"])
        m = api.c.post(f"/api/requests/{req['request_id']}/response", json={}).json()["manifest"]
        assert m["third_party_removal_count"] > 0
        assert "emergency_contact_name" in m["third_party_fields_removed"]

    def test_none_mode_refused(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        api.verify_ok(req["request_id"])
        r = api.c.post(f"/api/requests/{req['request_id']}/response",
                       json={"redaction_mode": "NONE"})
        assert r.status_code == 400
        assert "数据最小化" in str(r.json()["detail"])

    def test_invalid_mode_refused(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        api.verify_ok(req["request_id"])
        r = api.c.post(f"/api/requests/{req['request_id']}/response",
                       json={"redaction_mode": "SUPER"})
        assert r.status_code == 400

    def test_invalid_format_refused(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        api.verify_ok(req["request_id"])
        r = api.c.post(f"/api/requests/{req['request_id']}/response", json={"format": "PAPER"})
        assert r.status_code == 400

    def test_delivery_gets_expiry(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        api.verify_ok(req["request_id"])
        d = api.c.post(f"/api/requests/{req['request_id']}/response", json={}).json()["delivery"]
        assert d["expires_at"] > d["delivered_at"]

    def test_list_endpoint_hides_payload(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        api.verify_ok(req["request_id"])
        api.c.post(f"/api/requests/{req['request_id']}/response", json={})
        rows = api.c.get("/api/responses").json()["deliveries"]
        assert rows and all("payload" not in r for r in rows)

    def test_delivery_audited(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        api.verify_ok(req["request_id"])
        api.c.post(f"/api/requests/{req['request_id']}/response", json={})
        assert api.c.get("/api/audit?action=response_delivered").json()["count"] >= 1

    def test_purge_destroys_expired_payloads(self, client):
        from app.models.db import db_connection
        with db_connection() as conn:
            conn.execute("UPDATE response_deliveries SET expires_at = '2000-01-01T00:00:00'")
            conn.commit()
        r = client.post("/api/retention/purge", json={"actor": "system"}).json()
        assert r["purged_count"] >= 1
        rows = client.get("/api/responses").json()["deliveries"]
        assert all(x["status"] == "PURGED" for x in rows)

    def test_purge_is_audited(self, client):
        client.post("/api/retention/purge", json={"actor": "system"})
        entries = client.get("/api/audit?action=response_purged").json()["entries"]
        assert all(e["actor_role"] == "SYSTEM" for e in entries)

    def test_purge_leaves_active_deliveries_alone(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        api.verify_ok(req["request_id"])
        did = api.c.post(f"/api/requests/{req['request_id']}/response",
                         json={}).json()["delivery"]["delivery_id"]
        api.c.post("/api/retention/purge", json={"actor": "system"})
        row = next(x for x in api.c.get("/api/responses").json()["deliveries"]
                   if x["delivery_id"] == did)
        assert row["status"] == "ACTIVE"


class TestEndToEndHappyPath:
    def test_full_access_request_lifecycle(self, api):
        """受理 → 核验 → 编排 → 全部检索完成 → 生成响应 → 标记完成"""
        req = api.new_request("ACCESS", "EU", received_date="2026-08-01", requester_id="REQ-001")
        rid = req["request_id"]

        api.c.patch(f"/api/requests/{rid}/status", json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(rid)
        api.c.patch(f"/api/requests/{rid}/status", json={"status": "IN_PROGRESS"})

        api.c.post(f"/api/requests/{rid}/discover", json={})
        assert api.c.patch(f"/api/requests/{rid}/status",
                           json={"status": "COMPLETED"}).status_code == 400

        api.resolve_all_tasks(rid)
        r = api.c.post(f"/api/requests/{rid}/response", json={})
        assert r.status_code == 200

        done = api.c.patch(f"/api/requests/{rid}/status", json={"status": "COMPLETED"})
        assert done.status_code == 200
        assert done.json()["request"]["status"] == "COMPLETED"
        assert done.json()["request"]["closed_at"]

    def test_cannot_complete_before_response_exists(self, api):
        """任务全部收敛但没生成响应 → 仍不得标记完成（Art. 15(3)）"""
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        rid = req["request_id"]
        api.c.patch(f"/api/requests/{rid}/status", json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(rid)
        api.c.patch(f"/api/requests/{rid}/status", json={"status": "IN_PROGRESS"})
        api.c.post(f"/api/requests/{rid}/discover", json={})
        api.resolve_all_tasks(rid)
        detail = api.c.patch(f"/api/requests/{rid}/status",
                             json={"status": "COMPLETED"}).json()["detail"]
        assert any("尚未生成" in r for r in detail["reasons"])

    def test_erasure_end_to_end_with_partial_completion(self, api):
        req = api.new_request("ERASURE", "EU", requester_id="REQ-001")
        rid = req["request_id"]
        api.c.patch(f"/api/requests/{rid}/status", json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(rid)
        api.c.patch(f"/api/requests/{rid}/status", json={"status": "IN_PROGRESS"})
        api.c.post(f"/api/requests/{rid}/discover", json={})
        api.resolve_all_tasks(rid, status="EXCLUDED", records=0)
        r = api.c.patch(f"/api/requests/{rid}/status", json={"status": "PARTIALLY_COMPLETED"})
        assert r.status_code == 200

    def test_full_audit_trail_present(self, api):
        req = api.new_request("ACCESS", "EU", requester_id="REQ-001")
        rid = req["request_id"]
        api.c.patch(f"/api/requests/{rid}/status", json={"status": "IDENTITY_VERIFICATION"})
        api.verify_ok(rid)
        api.c.patch(f"/api/requests/{rid}/status", json={"status": "IN_PROGRESS"})
        api.c.post(f"/api/requests/{rid}/discover", json={})
        api.resolve_all_tasks(rid)
        api.c.post(f"/api/requests/{rid}/response", json={})
        api.c.patch(f"/api/requests/{rid}/status", json={"status": "COMPLETED"})

        actions = {e["action"] for e in api.c.get("/api/audit").json()["entries"]
                   if e["entity_id"] == rid or e.get("details", {}).get("request_id") == rid}
        for expected in ("request_received", "verification_started", "verification_submitted",
                         "discovery_orchestrated", "status_change", "response_delivered"):
            assert expected in actions, f"审计链缺少 {expected}"


class TestOverviewAndCompliance:
    def test_overview_buckets_cover_all_outcomes(self, client):
        buckets = client.get("/api/overview").json()["deadline_buckets"]
        assert set(buckets) == {"ON_TRACK", "DUE_SOON", "DUE_TODAY", "OVERDUE", "CLOSED", "EXTENDED"}

    def test_overview_has_overdue_and_due_soon(self, client):
        d = client.get("/api/overview").json()
        assert d["overdue"] > 0 and d["due_soon"] > 0

    def test_overdue_list_sorted_worst_first(self, client):
        top = client.get("/api/overview").json()["top_overdue"]
        assert [t["days_overdue"] for t in top] == sorted([t["days_overdue"] for t in top],
                                                           reverse=True)

    def test_on_time_rate_in_range(self, client):
        rate = client.get("/api/overview").json()["on_time_delivery_rate"]
        assert 0 <= rate <= 100

    def test_compliance_breakdown_by_jurisdiction(self, client):
        by_j = client.get("/api/compliance").json()["by_jurisdiction"]
        assert by_j and all("citation" in v for v in by_j.values())

    def test_compliance_states_the_basis(self, client):
        assert "12(3)" in client.get("/api/compliance").json()["note"]

    def test_compliance_discloses_synthetic_data(self, client):
        assert "合成" in client.get("/api/compliance").json()["note"]

    def test_compliance_lists_denial_reasons(self, client):
        assert client.get("/api/compliance").json()["available_denial_reasons"]

    def test_audit_descending_order(self, client):
        ids = [e["audit_id"] for e in client.get("/api/audit").json()["entries"]]
        assert ids == sorted(ids, reverse=True)

    def test_audit_details_parsed_to_object(self, client):
        assert isinstance(client.get("/api/audit").json()["entries"][0]["details"], dict)

    def test_audit_limit_respected(self, client):
        assert client.get("/api/audit?limit=3").json()["count"] <= 3
