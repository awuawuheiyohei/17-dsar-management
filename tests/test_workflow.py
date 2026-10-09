"""状态机测试 —— 非法跳转与业务前置条件必须被拒绝"""
import pytest

from app.models import workflow as wf


class TestStatusBasics:
    def test_all_ten_statuses_exist(self):
        assert len(wf.STATUSES) == 10

    def test_is_valid_status(self):
        assert wf.is_valid_status("IN_PROGRESS")
        assert wf.is_valid_status("completed")
        assert not wf.is_valid_status("BOGUS")

    def test_is_valid_status_rejects_none(self):
        assert not wf.is_valid_status(None)

    def test_terminal_statuses(self):
        assert wf.is_terminal("COMPLETED")
        assert wf.is_terminal("WITHDRAWN")
        assert not wf.is_terminal("IN_PROGRESS")

    def test_closed_includes_denied_and_expired(self):
        assert wf.is_closed("DENIED")
        assert wf.is_closed("EXPIRED")
        assert wf.is_closed("PARTIALLY_COMPLETED")

    def test_open_statuses_exclude_closed(self):
        assert wf.is_open("RECEIVED")
        assert wf.is_open("CLARIFICATION_REQUESTED")
        assert not wf.is_open("COMPLETED")

    def test_every_status_is_reachable_in_graph(self):
        for status in wf.STATUSES:
            assert status in wf.TRANSITIONS, f"{status} 未在状态图中定义"


class TestTransitionGraph:
    def test_happy_path_received_to_completed(self):
        assert wf.can_transition("RECEIVED", "IDENTITY_VERIFICATION")
        assert wf.can_transition("IDENTITY_VERIFICATION", "IN_PROGRESS")
        assert wf.can_transition("IN_PROGRESS", "COMPLETED")

    def test_cannot_skip_verification(self):
        assert not wf.can_transition("RECEIVED", "IN_PROGRESS")

    def test_cannot_skip_to_completed_directly(self):
        assert not wf.can_transition("RECEIVED", "COMPLETED")

    def test_clarification_round_trip(self):
        assert wf.can_transition("IN_PROGRESS", "CLARIFICATION_REQUESTED")
        assert wf.can_transition("CLARIFICATION_REQUESTED", "CLARIFICATION_ANSWERED")
        assert wf.can_transition("CLARIFICATION_ANSWERED", "IN_PROGRESS")

    def test_cannot_answer_clarification_without_asking(self):
        assert not wf.can_transition("IN_PROGRESS", "CLARIFICATION_ANSWERED")

    def test_completed_is_frozen(self):
        assert wf.allowed_transitions("COMPLETED") == []

    def test_withdrawn_is_frozen(self):
        assert wf.allowed_transitions("WITHDRAWN") == []

    def test_denied_can_be_reopened(self):
        assert wf.can_transition("DENIED", "IDENTITY_VERIFICATION")
        assert wf.is_reopen("DENIED", "IDENTITY_VERIFICATION")

    def test_expired_can_be_reopened(self):
        assert wf.can_transition("EXPIRED", "RECEIVED")
        assert wf.is_reopen("EXPIRED", "RECEIVED")

    def test_partially_completed_is_frozen(self):
        assert wf.allowed_transitions("PARTIALLY_COMPLETED") == []

    def test_withdraw_allowed_from_any_open_state(self):
        for s in wf.OPEN_STATUSES:
            assert wf.can_transition(s, "WITHDRAWN"), f"{s} 应允许撤回"

    def test_unknown_status_has_no_transitions(self):
        assert wf.allowed_transitions("BOGUS") == []


class TestValidateTransition:
    def test_rejects_unknown_target(self):
        ok, errors = wf.validate_transition("IN_PROGRESS", "NOPE")
        assert not ok
        assert any("非法目标状态" in e for e in errors)

    def test_rejects_unknown_current(self):
        ok, errors = wf.validate_transition("NOPE", "IN_PROGRESS")
        assert not ok
        assert any("未知的当前状态" in e for e in errors)

    def test_rejects_no_op_transition(self):
        ok, errors = wf.validate_transition("IN_PROGRESS", "IN_PROGRESS")
        assert not ok
        assert any("状态未变化" in e for e in errors)

    def test_rejects_illegal_jump(self):
        ok, errors = wf.validate_transition("RECEIVED", "COMPLETED")
        assert not ok
        assert any("非法状态跳转" in e for e in errors)

    def test_closed_state_rejection_mentions_reopen(self):
        ok, errors = wf.validate_transition("COMPLETED", "IN_PROGRESS")
        assert not ok
        assert any("关闭态" in e for e in errors)

    def test_in_progress_requires_verified_identity(self):
        ok, errors = wf.validate_transition("IDENTITY_VERIFICATION", "IN_PROGRESS",
                                            {"identity_verified": False})
        assert not ok
        assert any("Art. 12(6)" in e for e in errors)

    def test_in_progress_allowed_after_verification(self):
        ok, errors = wf.validate_transition("IDENTITY_VERIFICATION", "IN_PROGRESS",
                                            {"identity_verified": True})
        assert ok, errors

    def test_context_missing_is_treated_as_unverified(self):
        """保守默认：不传 context 视为未核验"""
        ok, errors = wf.validate_transition("IDENTITY_VERIFICATION", "IN_PROGRESS")
        assert not ok

    def test_completed_requires_no_open_tasks(self):
        ok, errors = wf.validate_transition("IN_PROGRESS", "COMPLETED", {
            "identity_verified": True, "open_task_count": 3, "delivery_id": "DEL-1",
        })
        assert not ok
        assert any("未收敛" in e for e in errors)

    def test_completed_requires_delivery(self):
        ok, errors = wf.validate_transition("IN_PROGRESS", "COMPLETED", {
            "identity_verified": True, "open_task_count": 0, "delivery_id": None,
        })
        assert not ok
        assert any("尚未生成" in e for e in errors)

    def test_completed_succeeds_with_all_conditions(self):
        ok, errors = wf.validate_transition("IN_PROGRESS", "COMPLETED", {
            "identity_verified": True, "open_task_count": 0, "delivery_id": "DEL-1",
        })
        assert ok, errors

    def test_partially_completed_does_not_require_delivery(self):
        ok, errors = wf.validate_transition("IN_PROGRESS", "PARTIALLY_COMPLETED", {
            "identity_verified": True, "open_task_count": 0,
        })
        assert ok, errors

    def test_denial_requires_reason_code(self):
        ok, errors = wf.validate_transition("IN_PROGRESS", "DENIED",
                                            {"denial_justification": "符合 Art.17(3)(b)"})
        assert not ok
        assert any("reason_code" in e for e in errors)

    def test_denial_requires_substantive_justification(self):
        ok, errors = wf.validate_transition("IN_PROGRESS", "DENIED",
                                            {"denial_reason": "ART_17_3_B",
                                             "denial_justification": "太短"})
        assert not ok
        assert any("至少 10 个字符" in e for e in errors)

    def test_denial_succeeds_with_full_context(self):
        ok, errors = wf.validate_transition("IN_PROGRESS", "DENIED", {
            "denial_reason": "ART_17_3_B",
            "denial_justification": "该记录受税务法定留存义务约束，不得删除",
        })
        assert ok, errors

    def test_denial_from_received_allowed(self):
        """受理阶段即可因无法定位主体而拒绝（NO_MATCHING_DATA 场景）"""
        ok, _ = wf.validate_transition("RECEIVED", "DENIED", {
            "denial_reason": "NO_MATCHING_DATA",
            "denial_justification": "已完成跨系统检索，未发现匹配记录",
        })
        assert ok

    def test_cannot_answer_clarification_while_in_progress(self):
        """没发过澄清请求就直接进 CLARIFICATION_ANSWERED，在状态图层面即非法"""
        ok, errors = wf.validate_transition("IN_PROGRESS", "CLARIFICATION_ANSWERED",
                                            {"identity_verified": True,
                                             "clarification_sent": False})
        assert not ok
        assert any("非法状态跳转" in e for e in errors)

    def test_clarification_guard_defends_its_own_edge(self):
        """
        requires_clarification 挂在 CLARIFICATION_REQUESTED → CLARIFICATION_ANSWERED 上。
        正常流程下当前状态已隐含「发过澄清」，这里显式传 False 仍应被守卫拦住 ——
        守卫是纵深防御，不依赖调用方一定传对 context。
        """
        ok, errors = wf.validate_transition("CLARIFICATION_REQUESTED", "CLARIFICATION_ANSWERED",
                                            {"clarification_sent": False})
        assert not ok
        assert any("尚未发出澄清请求" in e for e in errors)

    def test_clarification_answered_allowed_after_ask(self):
        ok, errors = wf.validate_transition("CLARIFICATION_REQUESTED", "CLARIFICATION_ANSWERED",
                                            {"clarification_sent": True})
        assert ok, errors

    def test_clarification_request_requires_verification(self):
        ok, errors = wf.validate_transition("RECEIVED", "CLARIFICATION_REQUESTED",
                                            {"identity_verified": False})
        assert not ok


class TestExplain:
    @pytest.mark.parametrize("status", list(wf.STATUSES))
    def test_every_status_has_an_explanation(self, status):
        assert wf.explain_status(status)

    def test_unknown_status_falls_back(self):
        assert wf.explain_status("BOGUS") == "未知状态"

    def test_completed_explanation_mentions_delivery(self):
        assert "交付" in wf.explain_status("COMPLETED")

    def test_denied_explanation_cites_article(self):
        assert "12(4)" in wf.explain_status("DENIED")


class TestGuardsTable:
    def test_all_guard_keys_reference_valid_statuses(self):
        for (src, dst) in wf.GUARDS:
            assert src in wf.STATUSES, f"{src} 不在状态列表"
            assert dst in wf.STATUSES, f"{dst} 不在状态列表"

    def test_all_guard_keys_are_legal_transitions(self):
        for (src, dst) in wf.GUARDS:
            assert dst in wf.TRANSITIONS[src], f"{src} → {dst} 在状态图上非法，不该出现在 Guard 表"

    def test_reopen_pairs_are_legal_transitions(self):
        for (src, dst) in wf.REOPEN_TRANSITIONS:
            assert wf.can_transition(src, dst)

    def test_guard_labels_cover_all_guard_flags(self):
        for guard in wf.GUARDS.values():
            for flag, value in vars(guard).items():
                if value:
                    assert flag in wf.GUARD_LABELS, f"缺少 {flag} 的中文说明"
