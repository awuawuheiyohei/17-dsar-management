"""身份核验引擎测试 —— 默认拒绝，不给「差一点点」放行"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import verification as vf


class TestMethods:
    def test_every_method_has_a_score_and_label(self):
        for m in vf.METHOD_SCORES:
            assert m in vf.METHOD_LABELS

    def test_every_label_has_a_score(self):
        for m in vf.METHOD_LABELS:
            assert m in vf.METHOD_SCORES

    def test_scores_are_within_range(self):
        for m, s in vf.METHOD_SCORES.items():
            assert 0 <= s <= 100, m

    def test_is_valid_method(self):
        assert vf.is_valid_method("email_otp")
        assert not vf.is_valid_method("TELEPATHY")
        assert not vf.is_valid_method(None)

    def test_unknown_method_raises(self):
        with pytest.raises(KeyError):
            vf.assess_verification("TELEPATHY")

    def test_none_is_a_valid_method_key_but_zero_score(self):
        assert vf.METHOD_SCORES["NONE"] == 0


class TestScoring:
    def test_strong_method_auto_verifies(self):
        a = vf.assess_verification("EMAIL_OTP")
        assert a.score == 90
        assert a.decision == "AUTO_VERIFY"
        assert a.disclosure_permitted

    def test_account_authentication_is_strongest(self):
        assert vf.METHOD_SCORES["ACCOUNT_AUTHENTICATION"] == 100

    def test_none_never_verifies(self):
        a = vf.assess_verification("NONE")
        assert a.decision == "REJECT"
        assert not a.disclosure_permitted

    def test_postal_letter_alone_goes_to_manual_review(self):
        """邮寄信函绑定真实但无法自动化 → 人工复核而非自动放行"""
        a = vf.assess_verification("POSTAL_LETTER")
        assert a.score == 70
        assert a.decision == "MANUAL_REVIEW"
        assert not a.disclosure_permitted

    def test_single_moderate_signal_drops_to_manual_review(self):
        a = vf.assess_verification("EMAIL_OTP", ["email_domain_mismatch"])
        assert a.score == 70
        assert a.decision == "MANUAL_REVIEW"
        assert not a.disclosure_permitted

    def test_signals_accumulate(self):
        a = vf.assess_verification("EMAIL_OTP", ["name_mismatch_on_account",
                                                 "third_party_requesting"])
        assert a.score == 90 - 25 - 30
        assert a.decision == "REJECT"

    def test_score_floors_at_zero(self):
        a = vf.assess_verification("NONE", ["third_party_requesting", "name_mismatch_on_account"])
        assert a.score == 0

    def test_score_capped_at_100(self):
        a = vf.assess_verification("ACCOUNT_AUTHENTICATION", [])
        assert a.score == 100

    def test_unknown_signal_is_ignored_not_fatal(self):
        """调用方拼错字符串时不应静默失效，但也不能让流程崩溃"""
        a = vf.assess_verification("EMAIL_OTP", ["totally_made_up"])
        assert a.score == 90
        assert a.penalties == {}

    def test_negative_signals_cannot_inflate(self):
        a = vf.assess_verification("EMAIL_OTP", ["unknown_positive_bonus"])
        assert a.score == 90

    def test_destructive_request_type_adds_penalty(self):
        a = vf.assess_verification("SMS_OTP", request_type="ERASURE")
        assert a.score == 85 - 10
        assert "high_risk_request_type" in a.penalties

    def test_destructive_penalty_not_double_counted(self):
        a = vf.assess_verification("SMS_OTP", ["high_risk_request_type"], request_type="ERASURE")
        assert a.penalties["high_risk_request_type"] == -10
        assert a.score == 75

    def test_access_request_has_no_destructive_penalty(self):
        assert "high_risk_request_type" not in vf.assess_verification("SMS_OTP").penalties

    def test_manual_review_is_the_only_middle_band(self):
        for method in vf.METHOD_SCORES:
            a = vf.assess_verification(method)
            assert a.decision in ("AUTO_VERIFY", "MANUAL_REVIEW", "REJECT")

    def test_only_auto_verify_permits_disclosure(self):
        for method in vf.METHOD_SCORES:
            a = vf.assess_verification(method)
            assert a.disclosure_permitted == (a.decision == "AUTO_VERIFY")

    def test_reasons_are_human_readable(self):
        a = vf.assess_verification("EMAIL_OTP", ["email_domain_mismatch"])
        assert len(a.reasons) >= 3
        assert any("邮件" in r or "邮箱" in r for r in a.reasons)

    def test_as_dict_is_serialisable(self):
        d = vf.assess_verification("EMAIL_OTP", ["email_domain_mismatch"]).as_dict()
        assert d["method"] == "EMAIL_OTP"
        assert d["decision"] == "MANUAL_REVIEW"
        assert isinstance(d["reasons"], list)

    def test_every_risk_signal_has_a_label(self):
        for sig in vf.RISK_SIGNALS:
            assert sig in vf.RISK_SIGNAL_LABELS

    def test_every_risk_signal_penalty_is_negative(self):
        for sig, pen in vf.RISK_SIGNALS.items():
            assert pen < 0, sig

    def test_none_signals_is_handled(self):
        assert vf.assess_verification("EMAIL_OTP", None).score == 90
        assert vf.assess_verification("EMAIL_OTP", []).score == 90


class TestDisclosureStatus:
    def test_only_verified_permits_disclosure(self):
        assert vf.status_permits_disclosure("VERIFIED")
        assert vf.status_permits_disclosure("verified")

    @pytest.mark.parametrize("status", ["PENDING", "IN_PROGRESS", "MANUAL_REVIEW",
                                        "FAILED", "EXPIRED", "ABANDONED"])
    def test_every_other_status_blocks_disclosure(self, status):
        assert not vf.status_permits_disclosure(status)

    def test_none_blocks_disclosure(self):
        assert not vf.status_permits_disclosure(None)


class TestOtpExpiry:
    def test_expiry_is_ttl_after_start(self):
        start = datetime(2026, 3, 10, 12, 0, 0)
        assert vf.otp_expiry(start, 15) == start + timedelta(minutes=15)

    def test_zero_ttl_returns_start_unchanged(self):
        start = datetime(2026, 3, 10, 12, 0, 0)
        assert vf.otp_expiry(start, 0) == start

    def test_valid_before_expiry(self):
        start = datetime.now(timezone.utc)
        ok, _ = vf.check_otp_valid("IN_PROGRESS", vf.otp_expiry(start), now=start)
        assert ok

    def test_expired_token_rejected(self):
        start = datetime.now(timezone.utc)
        later = start + timedelta(minutes=16)
        ok, msg = vf.check_otp_valid("IN_PROGRESS", vf.otp_expiry(start), now=later)
        assert not ok and "过期" in msg

    def test_missing_expiry_rejected_fail_closed(self):
        """无 expires_at 一律拒绝 —— 防重放默认失败"""
        ok, msg = vf.check_otp_valid("IN_PROGRESS", None)
        assert not ok and "expires_at" in msg

    def test_already_verified_cannot_resubmit(self):
        now = datetime.now(timezone.utc)
        ok, msg = vf.check_otp_valid("VERIFIED", vf.otp_expiry(now), now=now)
        assert not ok and "无需再次" in msg

    def test_failed_cannot_resubmit(self):
        now = datetime.now(timezone.utc)
        ok, msg = vf.check_otp_valid("FAILED", vf.otp_expiry(now, 999), now=now)
        assert not ok and "需重新发起" in msg

    def test_string_expiry_is_parsed(self):
        now = datetime.now(timezone.utc)
        ok, _ = vf.check_otp_valid("IN_PROGRESS", vf.otp_expiry(now).isoformat(), now=now)
        assert ok

    def test_garbage_expiry_rejected(self):
        ok, msg = vf.check_otp_valid("IN_PROGRESS", "not-a-date")
        assert not ok and "格式非法" in msg

    def test_naive_expiry_treated_as_utc(self):
        start = datetime(2026, 3, 10, 12, 0, 0)
        ok, _ = vf.check_otp_valid("IN_PROGRESS", start + timedelta(minutes=15),
                                   now=start)
        assert ok


class TestAttempts:
    def test_can_attempt_below_max(self):
        ok, msg = vf.can_attempt("PENDING", 0, 3)
        assert ok and "3" in msg

    def test_blocked_at_max(self):
        ok, msg = vf.can_attempt("PENDING", 3, 3)
        assert not ok and "最大尝试次数" in msg

    def test_remaining_count_reported(self):
        _, msg = vf.can_attempt("PENDING", 1, 3)
        assert "剩余 2 次" in msg

    def test_verified_cannot_attempt(self):
        ok, _ = vf.can_attempt("VERIFIED", 0, 3)
        assert not ok

    def test_abandoned_cannot_attempt(self):
        ok, _ = vf.can_attempt("ABANDONED", 0, 3)
        assert not ok


class TestManualReview:
    def test_approval_requires_note(self):
        ok, msg = vf.manual_review_outcome(70, True, "")
        assert not ok and "依据说明" in msg

    def test_whitespace_only_note_rejected(self):
        ok, _ = vf.manual_review_outcome(70, True, "   ")
        assert not ok

    def test_approval_with_note_passes(self):
        ok, msg = vf.manual_review_outcome(70, True, "证件与 HR 档案比对一致")
        assert ok and "隐私官复核通过" in msg

    def test_rejection_defaults_to_explicit_failure(self):
        ok, msg = vf.manual_review_outcome(70, False, "")
        assert not ok and "未通过" in msg

    def test_approved_status_is_verified(self):
        assert vf.APPROVED_STATUS == vf.VERIFIED


class TestExplain:
    @pytest.mark.parametrize("status", list(vf.VERIFY_STATUSES))
    def test_every_status_has_explanation(self, status):
        assert vf.explain_status(status)

    def test_manual_review_explanation_warns_against_disclosure(self):
        assert "不得披露" in vf.explain_status("MANUAL_REVIEW")

    def test_failed_explanation_blocks_data_processing(self):
        assert "不得" in vf.explain_status("FAILED")

    def test_unknown_status_falls_back(self):
        assert vf.explain_status("BOGUS") == "未知核验状态"
