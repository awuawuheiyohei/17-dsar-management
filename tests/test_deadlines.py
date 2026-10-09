"""法定时限引擎测试 —— 重点是月末截断与延长合法性

这是全项目最容易写错的地方：期限算错 = 超期 = 可被监管处罚的实际违规。
"""
from datetime import date, datetime, timedelta

import pytest

from app.models import deadlines as dl


# ============================================
# add_months：月末截断
# ============================================
class TestAddMonths:
    def test_zero_months_returns_same_day(self):
        d = date(2026, 3, 15)
        assert dl.add_months(d, 0) == d

    def test_jan_31_plus_one_month_clamps_to_feb_28(self):
        """平年 2 月只有 28 天 —— 朴素实现会抛 ValueError 或溢出到 3/3"""
        assert dl.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_jan_31_plus_one_month_leap_year_gives_feb_29(self):
        assert dl.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)

    def test_jan_30_plus_one_month_clamps(self):
        assert dl.add_months(date(2026, 1, 30), 1) == date(2026, 2, 28)

    def test_aug_31_plus_one_month_clamps_to_sep_30(self):
        assert dl.add_months(date(2026, 8, 31), 1) == date(2026, 9, 30)

    def test_mar_31_plus_one_month_clamps_to_apr_30(self):
        assert dl.add_months(date(2026, 3, 31), 1) == date(2026, 4, 30)

    def test_year_rollover_dec_31_plus_two_months(self):
        """跨年：2026-12 + 2M → 2027-02-28"""
        assert dl.add_months(date(2026, 12, 31), 2) == date(2027, 2, 28)

    def test_year_rollover_dec_31_plus_one_month(self):
        assert dl.add_months(date(2026, 12, 31), 1) == date(2027, 1, 31)

    def test_multiple_months_from_month_end(self):
        assert dl.add_months(date(2026, 1, 31), 3) == date(2026, 4, 30)

    def test_negative_months_jan_minus_one(self):
        assert dl.add_months(date(2026, 1, 15), -1) == date(2025, 12, 15)

    def test_negative_months_mar_minus_one(self):
        assert dl.add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)

    def test_leap_day_plus_twelve_months(self):
        """2024-02-29 + 12M → 2025-02-28（非闰年截断）"""
        assert dl.add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)

    def test_leap_day_plus_one_year_stays_leap_day(self):
        assert dl.add_months(date(2024, 2, 29), 48) == date(2028, 2, 29)

    def test_accepts_datetime_and_strips_time(self):
        assert dl.add_months(datetime(2026, 1, 31, 23, 59, 59), 1) == date(2026, 2, 28)

    def test_rejects_non_date(self):
        with pytest.raises(TypeError):
            dl.add_months("2026-01-31", 1)

    def test_month_end_helper(self):
        assert dl.month_end(date(2026, 1, 15)) == date(2026, 1, 31)
        assert dl.month_end(date(2026, 1, 15), 1) == date(2026, 2, 28)

    def test_is_idempotent_roundtrip_for_31st_of_30th_month(self):
        """1/31 → 2/28 → 3/28：截断后不可逆，这是正确语义（不是 bug）"""
        step1 = dl.add_months(date(2026, 1, 31), 1)
        assert dl.add_months(step1, 1) == date(2026, 3, 28)


# ============================================
# 法域策略
# ============================================
class TestPolicies:
    def test_eu_is_one_month(self):
        assert dl.POLICIES["EU"].deadline_months == 1

    def test_eu_extension_is_two_months(self):
        assert dl.POLICIES["EU"].extension_months == 2

    def test_ca_is_45_days(self):
        assert dl.POLICIES["CA"].deadline_days == 45

    def test_cn_is_15_days(self):
        assert dl.POLICIES["CN"].deadline_days == 15

    def test_cn_cannot_be_extended(self):
        assert dl.POLICIES["CN"].max_extensions == 0

    def test_get_policy_defaults_to_eu(self):
        assert dl.get_policy(None).jurisdiction == "EU"

    def test_get_policy_is_case_insensitive(self):
        assert dl.get_policy("eu").jurisdiction == "EU"

    def test_get_policy_rejects_unknown(self):
        with pytest.raises(KeyError):
            dl.get_policy("MARS")

    def test_is_valid_jurisdiction(self):
        assert dl.is_valid_jurisdiction("EU")
        assert dl.is_valid_jurisdiction("ca")
        assert not dl.is_valid_jurisdiction("XX")
        assert not dl.is_valid_jurisdiction(None)

    def test_policy_as_dict_is_serialisable(self):
        d = dl.POLICIES["EU"].as_dict()
        assert d["deadline"] == "1 个月"
        assert d["extension"] == "+2 个月"
        assert "Art. 12(3)" in d["citation"]


# ============================================
# compute_deadline
# ============================================
class TestComputeDeadline:
    def test_eu_mid_month(self):
        assert dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["EU"]) == date(2026, 4, 10)

    def test_eu_month_end_clamp(self):
        assert dl.compute_deadline(date(2026, 1, 31), dl.POLICIES["EU"]) == date(2026, 2, 28)

    def test_eu_extension_adds_two_months_from_base(self):
        assert dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["EU"],
                                   extensions=1) == date(2026, 6, 10)

    def test_extension_is_measured_from_base_not_from_due(self):
        """延长量从「原始期限」起算：1+2=3 个月，而不是 1+1+2=4 个月"""
        result = dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["EU"], extensions=1)
        assert result == date(2026, 6, 10)
        assert result != date(2026, 7, 10), "延长不应从已延长日期再次叠加"

    def test_extensions_clamped_to_max(self):
        """请求传 extensions=99 也不能突破法域上限"""
        assert dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["EU"],
                                   extensions=99) == date(2026, 6, 10)

    def test_cn_cannot_be_extended_even_if_asked(self):
        assert dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["CN"],
                                   extensions=1) == date(2026, 3, 25)

    def test_ca_45_days(self):
        assert dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["CA"]) == date(2026, 4, 24)

    def test_ca_extension_adds_45_days(self):
        assert dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["CA"],
                                   extensions=1) == date(2026, 6, 8)

    def test_clock_stop_extends_deadline(self):
        assert dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["EU"],
                                   clock_stopped_days=15) == date(2026, 4, 25)

    def test_clock_stop_capped_by_policy_max(self):
        """EU 上限 60 天：申请 999 天也只加 60 天"""
        assert dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["EU"],
                                   clock_stopped_days=999) == date(2026, 6, 9)

    def test_clock_stop_ignored_when_policy_disallows(self):
        policy = dl.DeadlinePolicy(jurisdiction="XX", label="x", regulation="x",
                                   deadline_days=10, clock_stop_allowed=False)
        assert dl.compute_deadline(date(2026, 3, 10), policy,
                                   clock_stopped_days=50) == date(2026, 3, 20)

    def test_negative_clock_stop_is_ignored(self):
        assert dl.compute_deadline(date(2026, 3, 10), dl.POLICIES["EU"],
                                   clock_stopped_days=-10) == date(2026, 4, 10)

    def test_original_vs_extended_helper(self):
        received = date(2026, 3, 10)
        assert dl.compute_original_deadline(received, dl.POLICIES["EU"]) == date(2026, 4, 10)
        assert dl.compute_extended_deadline(received, dl.POLICIES["EU"]) == date(2026, 6, 10)

    def test_accepts_datetime_input(self):
        assert dl.compute_deadline(datetime(2026, 1, 31, 10, 0), dl.POLICIES["EU"]) \
            == date(2026, 2, 28)


# ============================================
# 延长合法性（Art. 12(3)）
# ============================================
class TestExtensionValidity:
    def test_valid_when_notified_on_time(self):
        ok, errors = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["EU"],
                                            date(2026, 4, 1), reason="请求涉及多系统历史数据")
        assert ok and not errors

    def test_valid_on_the_deadline_day_itself(self):
        """期限届满于该日结束 —— 当日通知仍在期限内"""
        ok, _ = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["EU"],
                                      date(2026, 4, 10), reason="复杂请求")
        assert ok

    def test_invalid_one_day_after_deadline(self):
        ok, errors = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["EU"],
                                           date(2026, 4, 11), reason="复杂请求")
        assert not ok
        assert any("晚于原始期限" in e for e in errors)

    def test_invalid_without_notification(self):
        ok, errors = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["EU"],
                                           None, reason="复杂请求")
        assert not ok
        assert any("extension_notified_at" in e for e in errors)

    def test_invalid_without_reason(self):
        ok, errors = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["EU"],
                                           date(2026, 4, 1), reason="")
        assert not ok
        assert any("复杂或数量众多" in e for e in errors)

    def test_require_reason_false_skips_reason_check(self):
        ok, _ = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["EU"],
                                      date(2026, 4, 1), reason="",
                                      require_reason=False)
        assert ok

    def test_rejects_double_extension(self):
        ok, errors = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["EU"],
                                           date(2026, 4, 1), reason="x", extensions=2)
        assert not ok
        assert any("最多允许延长" in e for e in errors)

    def test_rejects_extension_when_policy_forbids(self):
        ok, errors = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["CN"],
                                           date(2026, 3, 15), reason="x")
        assert not ok

    def test_rejects_zero_extensions(self):
        ok, errors = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["EU"],
                                           date(2026, 4, 1), reason="x", extensions=0)
        assert not ok

    def test_accepts_datetime_notification(self):
        ok, _ = dl.extension_is_valid(date(2026, 3, 10), dl.POLICIES["EU"],
                                      datetime(2026, 4, 1, 15, 30), reason="x")
        assert ok


# ============================================
# 时钟暂停（Art. 12(6)）
# ============================================
class TestClockStop:
    def test_valid_within_quota(self):
        ok, _ = dl.clock_stop_is_valid(dl.POLICIES["EU"], already_stopped_days=0, requested_days=30)
        assert ok

    def test_rejected_when_exhausted(self):
        ok, msg = dl.clock_stop_is_valid(dl.POLICIES["EU"], already_stopped_days=60, requested_days=10)
        assert not ok and "上限" in msg

    def test_rejected_when_exceeding_remaining_quota(self):
        ok, msg = dl.clock_stop_is_valid(dl.POLICIES["EU"], already_stopped_days=50, requested_days=30)
        assert not ok and "剩余额度" in msg

    def test_rejected_when_policy_disallows(self):
        policy = dl.DeadlinePolicy(jurisdiction="XX", label="x", regulation="x",
                                   deadline_days=10, clock_stop_allowed=False)
        ok, _ = dl.clock_stop_is_valid(policy, 0, 10)
        assert not ok


# ============================================
# 期限状态
# ============================================
class TestDeadlineStatus:
    def test_on_track(self):
        assert dl.deadline_status(date(2026, 4, 30), date(2026, 4, 1)) == "ON_TRACK"

    def test_due_soon_within_warning_window(self):
        assert dl.deadline_status(date(2026, 4, 5), date(2026, 4, 1)) == "DUE_SOON"

    def test_due_today(self):
        assert dl.deadline_status(date(2026, 4, 10), date(2026, 4, 10)) == "DUE_TODAY"

    def test_overdue(self):
        assert dl.deadline_status(date(2026, 4, 1), date(2026, 4, 10)) == "OVERDUE"

    def test_closed_status_wins_over_overdue(self):
        assert dl.deadline_status(date(2026, 4, 1), date(2026, 4, 10),
                                  request_status="COMPLETED") == "CLOSED"

    def test_extended_flag_shown_when_far_from_due(self):
        assert dl.deadline_status(date(2026, 6, 1), date(2026, 4, 1),
                                  extension_granted=True) == "EXTENDED"

    def test_overdue_wins_over_extended_flag(self):
        assert dl.deadline_status(date(2026, 4, 1), date(2026, 4, 10),
                                  extension_granted=True) == "OVERDUE"

    def test_boundary_at_warning_days(self):
        assert dl.deadline_status(date(2026, 4, 8), date(2026, 4, 1),
                                  warning_days=7) == "DUE_SOON"

    def test_one_day_outside_warning_window(self):
        assert dl.deadline_status(date(2026, 4, 9), date(2026, 4, 1),
                                  warning_days=7) == "ON_TRACK"


# ============================================
# build_deadline_view
# ============================================
class TestDeadlineView:
    def _base(self, **kw):
        params = dict(received="2026-03-10", jurisdiction="EU", due_date="2026-04-10",
                      today=date(2026, 4, 1))
        params.update(kw)
        return dl.build_deadline_view(**params)

    def test_accepts_iso_strings(self):
        v = self._base()
        assert v.received_date == date(2026, 3, 10)
        assert v.due_date == date(2026, 4, 10)

    def test_as_dict_is_json_friendly(self):
        d = self._base().as_dict()
        assert d["effective_due_date"] == "2026-04-10"
        assert d["days_remaining"] == 9
        assert d["jurisdiction"] == "EU"

    def test_extension_honoured_with_valid_notice(self):
        v = self._base(extension_granted=True, extended_due_date="2026-06-10",
                       extension_notified_at="2026-04-01")
        assert v.effective_due_date == date(2026, 6, 10)
        assert v.extension_granted
        assert not v.warnings

    def test_extension_ignored_without_notice_evidence(self):
        v = self._base(extension_granted=True, extended_due_date="2026-06-10")
        assert v.effective_due_date == date(2026, 4, 10)
        assert not v.extension_granted
        assert any("缺少通知证据" in w for w in v.warnings)

    def test_extension_ignored_when_notice_is_late(self):
        v = self._base(extension_granted=True, extended_due_date="2026-06-10",
                       extension_notified_at="2026-04-20")
        assert v.effective_due_date == date(2026, 4, 10)
        assert any("晚于原始期限" in w for w in v.warnings)

    def test_clock_stopped_days_shown(self):
        v = self._base(due_date="2026-04-25", clock_stopped_days=15)
        assert v.clock_stopped_days == 15
        assert "Art. 12(6)" in v.explanation

    def test_clock_stop_citation_is_jurisdiction_specific(self):
        """CA 请求不该引用 GDPR 的 Art.12(6)"""
        v = dl.build_deadline_view(received="2026-08-21", jurisdiction="CA",
                                   due_date="2026-10-05", today=date(2026, 10, 1),
                                   clock_stopped_days=7, request_status="IN_PROGRESS")
        assert "Cal. Civ. Code" in v.explanation
        assert "Art. 12(6)" not in v.explanation

    def test_clock_stop_cash_extends_effective_due_date(self):
        """入库 due_date 不含暂停天数，快照必须补上，否则会被误判超期"""
        v = dl.build_deadline_view(received="2026-01-15", jurisdiction="EU",
                                   due_date="2026-02-15", today=date(2026, 2, 1),
                                   clock_stopped_days=20, request_status="IN_PROGRESS")
        assert v.effective_due_date == date(2026, 3, 7)
        assert v.days_remaining == 34

    def test_clock_stop_is_capped_in_view(self):
        v = dl.build_deadline_view(received="2026-01-15", jurisdiction="EU",
                                   due_date="2026-02-15", today=date(2026, 2, 1),
                                   clock_stopped_days=999, request_status="IN_PROGRESS")
        # EU 上限 60 天：2/15 + 60 = 4/16
        assert v.effective_due_date == date(2026, 4, 16)

    def test_every_policy_declares_a_clock_stop_citation(self):
        for p in dl.POLICIES.values():
            assert p.clock_stop_citation, f"{p.jurisdiction} 缺少时钟暂停依据"

    def test_overdue_explanation_flags_violation(self):
        v = self._base(today=date(2026, 5, 1))
        assert v.status == "OVERDUE"
        assert v.days_remaining == -21
        assert "监管" in v.explanation

    def test_explanation_contains_citation(self):
        assert "Art. 12(3)" in self._base().explanation

    def test_closed_request_stops_countdown(self):
        v = self._base(request_status="COMPLETED")
        assert v.status == "CLOSED"
        assert "已关闭" in v.explanation
