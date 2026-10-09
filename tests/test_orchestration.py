"""检索编排 + 权利目录 + 表单解析测试"""
import pytest

from app.models import orchestration as orch
from app.models import rights
from app.models.urlform import as_list, first, parse_urlencoded


# ============================================
# 权利目录
# ============================================
class TestRights:
    def test_seven_gdpr_rights_covered(self):
        assert len(rights.REQUEST_TYPES) == 7

    @pytest.mark.parametrize("code,article", [
        ("ACCESS", "Art. 15"), ("RECTIFICATION", "Art. 16"), ("ERASURE", "Art. 17"),
        ("PORTABILITY", "Art. 20"), ("RESTRICTION", "Art. 18"),
        ("OBJECTION", "Art. 21"), ("AUTOMATED_DECISION", "Art. 22"),
    ])
    def test_article_mapping(self, code, article):
        assert rights.get_right(code).gdpr_article == article

    def test_all_seven_articles_distinct(self):
        articles = [r.gdpr_article for r in rights.RIGHTS.values()]
        assert len(set(articles)) == len(articles)

    def test_every_right_has_ccpa_equivalent(self):
        for r in rights.RIGHTS.values():
            assert r.ccpa_equivalent

    def test_every_right_requires_verification(self):
        for r in rights.RIGHTS.values():
            assert r.requires_verification, r.code

    def test_erasure_is_destructive(self):
        assert rights.get_right("ERASURE").destructive

    def test_access_is_not_destructive(self):
        assert not rights.get_right("ACCESS").destructive

    def test_all_rights_require_discovery(self):
        assert set(rights.DISCOVERY_TYPES) == set(rights.REQUEST_TYPES)

    def test_is_valid_request_type(self):
        assert rights.is_valid_request_type("access")
        assert not rights.is_valid_request_type("BOGUS")
        assert not rights.is_valid_request_type(None)

    def test_get_right_raises_for_unknown(self):
        with pytest.raises(KeyError):
            rights.get_right("BOGUS")

    def test_list_rights_is_serialisable(self):
        out = rights.list_rights()
        assert len(out) == 7
        assert all("gdpr_article" in r for r in out)

    def test_erasure_carries_legal_basis_note(self):
        assert "17(3)" in rights.get_right("ERASURE").legal_basis_note

    def test_access_notes_machine_readable_format(self):
        assert "machine-readable" in rights.get_right("ACCESS").legal_basis_note


# ============================================
# 任务状态
# ============================================
class TestTaskStatus:
    def test_seven_statuses(self):
        assert len(orch.TASK_STATUSES) == 7

    def test_waived_counts_as_open(self):
        """豁免不等于完成 —— 豁免仍需有人确认，所以继续阻塞关闭"""
        assert orch.is_open(orch.WAIVED)

    def test_error_is_resolved_but_flagged(self):
        assert orch.is_resolved(orch.ERROR)
        assert not orch.is_open(orch.ERROR)

    def test_pending_is_open(self):
        assert orch.is_open(orch.PENDING)

    def test_excluded_is_resolved(self):
        """法定排除算已处理 —— 否则有留存义务的系统会让请求永远关不掉"""
        assert orch.is_resolved(orch.EXCLUDED)

    def test_all_resolved_statuses_are_terminal_or_retryable(self):
        for s in orch.RESOLVED_STATUSES:
            assert s in orch.TASK_STATUSES

    def test_is_valid_task_status(self):
        assert orch.is_valid_task_status("completed")
        assert not orch.is_valid_task_status("BOGUS")


class TestActions:
    @pytest.mark.parametrize("rtype,action", [
        ("ACCESS", "COLLECT"), ("PORTABILITY", "COLLECT"),
        ("ERASURE", "ERASE"), ("RECTIFICATION", "RECTIFY"),
        ("RESTRICTION", "RESTRICT"), ("OBJECTION", "CONFIRM"),
        ("AUTOMATED_DECISION", "CONFIRM"),
    ])
    def test_request_type_maps_to_action(self, rtype, action):
        assert orch.action_for(rtype) == action

    def test_unknown_type_defaults_to_collect(self):
        assert orch.action_for("WHATEVER") == "COLLECT"

    def test_every_action_has_a_label(self):
        for a in orch.ACTIONS.values():
            assert a in orch.ACTION_LABELS

    def test_explain_action(self):
        assert "删除" in orch.explain_action("ERASE")


# ============================================
# 计划构建
# ============================================
class TestBuildPlan:
    SOURCES = [
        {"source_id": "SRC-A", "owner_team": "t1", "active": 1,
         "excluded_by_default": 0, "exclusion_basis": None},
        {"source_id": "SRC-B", "owner_team": "t2", "active": 1,
         "excluded_by_default": 0, "exclusion_basis": None},
        {"source_id": "SRC-ARCH", "owner_team": "t3", "active": 1,
         "excluded_by_default": 1, "exclusion_basis": "Art. 17(3)(b) 留存义务"},
    ]
    CATEGORIES = [{"category_id": "CAT-X"}, {"category_id": "CAT-Y"}]

    def test_cartesian_product_size(self):
        plan = orch.build_plan("DSAR-0001", self.SOURCES, self.CATEGORIES, "ACCESS")
        assert len(plan) == 6

    def test_inactive_source_skipped(self):
        sources = self.SOURCES + [{"source_id": "SRC-OFF", "active": 0,
                                   "owner_team": "t", "excluded_by_default": 0}]
        plan = orch.build_plan("DSAR-0001", sources, self.CATEGORIES, "ACCESS")
        assert len(plan) == 6
        assert all(t.source_id != "SRC-OFF" for t in plan)

    def test_excluded_source_marked_with_basis(self):
        plan = orch.build_plan("DSAR-0001", self.SOURCES, self.CATEGORIES, "ERASURE")
        arch = [t for t in plan if t.source_id == "SRC-ARCH"]
        assert all(t.status == orch.EXCLUDED for t in arch)
        assert all("17(3)(b)" in t.excluded_reason for t in arch)

    def test_action_applied_to_all_tasks(self):
        plan = orch.build_plan("DSAR-0001", self.SOURCES, self.CATEGORIES, "ERASURE")
        assert all(t.action == "ERASE" for t in plan)

    def test_owner_taken_from_source(self):
        plan = orch.build_plan("DSAR-0001", self.SOURCES, self.CATEGORIES, "ACCESS")
        assert {t.owner for t in plan if t.source_id == "SRC-A"} == {"t1"}

    def test_categories_accept_plain_strings(self):
        plan = orch.build_plan("DSAR-0001", self.SOURCES[:1], ["CAT-X", "CAT-Y"], "ACCESS")
        assert len(plan) == 2

    def test_empty_sources_yields_empty_plan(self):
        assert orch.build_plan("DSAR-0001", [], self.CATEGORIES, "ACCESS") == []

    def test_plan_is_deterministic(self):
        a = orch.build_plan("DSAR-0001", self.SOURCES, self.CATEGORIES, "ACCESS")
        b = orch.build_plan("DSAR-0001", self.SOURCES, self.CATEGORIES, "ACCESS")
        assert [t.task_id for t in a] == [t.task_id for t in b]

    def test_task_id_differs_per_request(self):
        a = orch.make_task_id("DSAR-0001", "SRC-A", "CAT-X")
        b = orch.make_task_id("DSAR-0002", "SRC-A", "CAT-X")
        assert a != b

    def test_task_id_differs_per_category(self):
        a = orch.make_task_id("DSAR-0001", "SRC-A", "CAT-X")
        b = orch.make_task_id("DSAR-0001", "SRC-A", "CAT-Y")
        assert a != b

    def test_planned_task_as_dict(self):
        t = orch.build_plan("DSAR-0001", self.SOURCES[:1], ["CAT-X"], "ACCESS")[0]
        assert set(t.as_dict()) >= {"task_id", "status", "action"}


class TestTaskTransitions:
    def test_pending_to_running(self):
        assert orch.can_task_transition(orch.PENDING, orch.RUNNING)

    def test_completed_is_frozen(self):
        assert orch.TASK_TRANSITIONS[orch.COMPLETED] == frozenset()

    def test_completed_cannot_be_overwritten(self):
        ok, errors = orch.validate_task_transition(orch.COMPLETED, orch.RUNNING)
        assert not ok and any("非法任务流转" in e for e in errors)

    def test_error_can_be_retried(self):
        assert orch.can_task_transition(orch.ERROR, orch.RUNNING)

    def test_waived_can_be_reverted(self):
        assert orch.can_task_transition(orch.WAIVED, orch.RUNNING)

    def test_not_found_is_frozen(self):
        ok, _ = orch.validate_task_transition(orch.NOT_FOUND, orch.COMPLETED, record_count=5)
        assert not ok

    def test_completed_requires_record_count(self):
        ok, errors = orch.validate_task_transition(orch.RUNNING, orch.COMPLETED, record_count=0)
        assert not ok and any("record_count > 0" in e for e in errors)

    def test_completed_ok_with_records(self):
        ok, errors = orch.validate_task_transition(orch.RUNNING, orch.COMPLETED, record_count=3)
        assert ok, errors

    def test_not_found_with_records_is_contradictory(self):
        ok, errors = orch.validate_task_transition(orch.RUNNING, orch.NOT_FOUND, record_count=3)
        assert not ok and any("矛盾" in e for e in errors)

    def test_excluded_requires_legal_basis(self):
        ok, errors = orch.validate_task_transition(orch.PENDING, orch.EXCLUDED,
                                                   excluded_reason="")
        assert not ok and any("法定排除依据" in e for e in errors)

    def test_excluded_ok_with_basis(self):
        ok, errors = orch.validate_task_transition(
            orch.PENDING, orch.EXCLUDED, excluded_reason="Art. 17(3)(b) 留存义务")
        assert ok, errors

    def test_invalid_status_rejected(self):
        ok, errors = orch.validate_task_transition(orch.PENDING, "BOGUS")
        assert not ok and any("非法任务状态" in e for e in errors)

    def test_no_op_rejected(self):
        ok, errors = orch.validate_task_transition(orch.PENDING, orch.PENDING)
        assert not ok and any("状态未变化" in e for e in errors)


class TestSummarize:
    def test_empty_summary_is_not_blocking(self):
        s = orch.summarize({})
        assert s["total"] == 0
        assert s["coverage_pct"] == 100.0
        assert not s["blocking"]

    def test_all_pending_blocks(self):
        s = orch.summarize({orch.PENDING: 5})
        assert s["blocking"] and s["open_count"] == 5

    def test_all_resolved_does_not_block(self):
        s = orch.summarize({orch.COMPLETED: 3, orch.EXCLUDED: 1})
        assert not s["blocking"]
        assert s["coverage_pct"] == 100.0

    def test_excluded_counts_as_resolved(self):
        s = orch.summarize({orch.EXCLUDED: 2})
        assert s["coverage_pct"] == 100.0

    def test_partial_coverage(self):
        s = orch.summarize({orch.COMPLETED: 2, orch.PENDING: 2})
        assert s["coverage_pct"] == 50.0

    def test_errors_counted(self):
        s = orch.summarize({orch.ERROR: 1, orch.COMPLETED: 1})
        assert s["errors"] == 1

    def test_summarize_tasks_accepts_object_list(self):
        s = orch.summarize_tasks([{"status": "PENDING"}, {"status": "COMPLETED"}])
        assert s["total"] == 2 and s["open_count"] == 1

    def test_summarize_tasks_defaults_missing_status(self):
        s = orch.summarize_tasks([{}])
        assert s["open_count"] == 1

    def test_blocking_reason_is_explicit(self):
        s = orch.summarize({orch.PENDING: 3})
        assert "3" in s["blocking_reason"]


# ============================================
# 表单解析（手写，替代 python-multipart）
# ============================================
class TestUrlForm:
    def test_simple_pair(self):
        assert parse_urlencoded("a=1") == {"a": "1"}

    def test_plus_becomes_space(self):
        """urlencoded 里 `+` 是空格 —— 漏掉会导致 John+Doe 原样保留"""
        assert parse_urlencoded("name=John+Doe") == {"name": "John Doe"}

    def test_percent_decoding(self):
        assert parse_urlencoded("name=%E5%BC%A0%E4%B8%89") == {"name": "张三"}

    def test_percent_encoded_plus(self):
        assert parse_urlencoded("name=a%2Bb") == {"name": "a+b"}

    def test_empty_value_preserved(self):
        """`x=` 与「未提供」语义不同，必须区分"""
        assert parse_urlencoded("x=") == {"x": ""}
        assert parse_urlencoded("x") == {"x": ""}

    def test_repeated_key_becomes_list(self):
        assert parse_urlencoded("c=A&c=B&c=C") == {"c": ["A", "B", "C"]}

    def test_two_keys_then_repeat(self):
        assert parse_urlencoded("a=1&b=2&a=3") == {"a": ["1", "3"], "b": "2"}

    def test_empty_body(self):
        assert parse_urlencoded("") == {}

    def test_none_body(self):
        assert parse_urlencoded(None) == {}

    def test_whitespace_only_body(self):
        assert parse_urlencoded("   ") == {}

    def test_bytes_input(self):
        assert parse_urlencoded(b"a=1") == {"a": "1"}

    def test_malformed_bytes_do_not_crash(self):
        assert parse_urlencoded(b"a=\xff\xfe") == {"a": "\ufffd\ufffd"}

    def test_double_ampersands_ignored(self):
        assert parse_urlencoded("a=1&&b=2") == {"a": "1", "b": "2"}

    def test_rejects_wrong_type(self):
        with pytest.raises(TypeError):
            parse_urlencoded(123)

    def test_equals_inside_value(self):
        assert parse_urlencoded("token=a=b=c") == {"token": "a=b=c"}

    def test_empty_key_allowed(self):
        assert parse_urlencoded("=v") == {"": "v"}


class TestFormHelpers:
    def test_first_scalar(self):
        assert first("a") == "a"

    def test_first_of_list(self):
        assert first(["a", "b"]) == "a"

    def test_first_default(self):
        assert first(None, "d") == "d"
        assert first([], "d") == "d"

    def test_as_list_from_scalar(self):
        assert as_list("a") == ["a"]

    def test_as_list_splits_commas(self):
        assert as_list("A,B,C") == ["A", "B", "C"]

    def test_as_list_handles_list_of_comma_strings(self):
        assert as_list(["A,B", "C"]) == ["A", "B", "C"]

    def test_as_list_strips_blanks(self):
        assert as_list("A,,B,") == ["A", "B"]

    def test_as_list_none(self):
        assert as_list(None) == []
