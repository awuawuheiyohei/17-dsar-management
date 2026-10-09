"""脱敏引擎测试 —— Art. 5(1)(c) 数据最小化 / Art. 15(4) 第三方 / Art. 9 特殊类别"""
import json

import pytest

from app.models import redaction as rd


# ============================================
# Luhn 校验
# ============================================
class TestLuhn:
    def test_valid_card_number(self):
        assert rd.luhn_check("4539148803436467")

    def test_valid_card_with_spaces(self):
        assert rd.luhn_check("4539 1488 0343 6467")

    def test_invalid_checksum_rejected(self):
        assert not rd.luhn_check("4539148803436468")

    def test_too_short_rejected(self):
        assert not rd.luhn_check("123")

    def test_non_digits_ignored(self):
        assert not rd.luhn_check("abcd")


# ============================================
# 值形态识别
# ============================================
class TestDetectValueKind:
    def test_field_name_wins_over_value_shape(self):
        assert rd.detect_value_kind("email", "not-an-email") == "email"

    def test_detects_email_by_value_when_name_unknown(self):
        assert rd.detect_value_kind("contact", "a.b@x.io") == "email"

    def test_detects_ip_by_value(self):
        assert rd.detect_value_kind("addr", "192.168.1.1") == "ip"

    def test_detects_cn_national_id_by_value(self):
        assert rd.detect_value_kind("x", "11010119900307123X") == "national_id"

    def test_detects_card_only_when_luhn_passes(self):
        assert rd.detect_value_kind("x", "4539148803436467") == "card"
        assert rd.detect_value_kind("x", "1234567890123456") != "card"

    def test_plain_number_not_treated_as_card(self):
        assert rd.detect_value_kind("record_no", "12345678") is None

    def test_none_value_returns_none(self):
        assert rd.detect_value_kind("email", None) is None

    def test_nested_value_returns_none(self):
        assert rd.detect_value_kind("meta", {"a": 1}) is None
        assert rd.detect_value_kind("items", [1, 2]) is None

    def test_empty_string_returns_none(self):
        assert rd.detect_value_kind("note", "   ") is None


# ============================================
# 各类型脱敏
# ============================================
class TestMaskers:
    def test_mask_email_keeps_domain_shape(self):
        masked = rd.mask_email("alice.smith@corp.example.com")
        assert masked == "a***@c***.com"
        assert "alice" not in masked

    def test_mask_email_without_at(self):
        assert rd.mask_email("garbage") == "***"

    def test_mask_email_without_tld_keeps_whole_domain(self):
        """无点域名（localhost/internal）整段保留为域部分，不丢信息"""
        assert rd.mask_email("a@localhost") == "a***@***.localhost"

    def test_mask_phone_keeps_last_four(self):
        assert rd.mask_phone("+8613800001234").endswith("1234")
        assert "1380" not in rd.mask_phone("+8613800001234")

    def test_mask_phone_short(self):
        assert rd.mask_phone("123") == "***"

    def test_mask_name_latin(self):
        assert rd.mask_name("Alice Smith") == "A**** S****"

    def test_mask_name_chinese_keeps_surname(self):
        assert rd.mask_name("张三丰").startswith("张")

    def test_mask_name_empty(self):
        assert rd.mask_name("") == "***"

    def test_mask_national_id_keeps_last_four(self):
        assert rd.mask_national_id("11010119900307123X").endswith("123X")

    def test_mask_national_id_short(self):
        assert rd.mask_national_id("12") == "**"

    def test_card_is_tokenised_not_partially_shown(self):
        masked = rd.tokenize_card("4539148803436467")
        assert masked.startswith("[CARD-TOKEN:")
        assert "4539" not in masked
        assert "4539148803436467" not in masked

    def test_mask_bank(self):
        assert rd.mask_bank("DE89370400440532013000") == "[BANK-ACCOUNT:3000]"

    def test_mask_bank_short(self):
        assert rd.mask_bank("12") == "[BANK-ACCOUNT:***]"

    def test_mask_dob_keeps_year_only(self):
        assert rd.mask_dob("1990-03-07") == "1990-**-**"

    def test_mask_dob_unparseable(self):
        assert rd.mask_dob("unknown") == "****"

    def test_mask_ip_keeps_subnet(self):
        assert rd.mask_ip("192.168.1.55") == "192.168.1.0/24"

    def test_mask_ip_invalid(self):
        assert rd.mask_ip("nope") == "***"

    def test_mask_address_hides_street_numbers(self):
        assert "100" not in rd.mask_address("示例路 100 号 501 室")

    def test_mask_generic(self):
        assert rd.mask_generic("anything") == "[REDACTED]"


class TestThirdPartyDetection:
    @pytest.mark.parametrize("key", ["emergency_contact_name", "other_person_email",
                                     "co_recipient_name", "joint_account_id",
                                     "spouse_phone", "dependent_name"])
    def test_third_party_prefixes_detected(self, key):
        assert rd.is_third_party_field(key)

    def test_own_fields_not_flagged(self):
        assert not rd.is_third_party_field("full_name")
        assert not rd.is_third_party_field("email")


# ============================================
# 记录脱敏
# ============================================
class TestRedactRecord:
    RECORD = {
        "full_name": "Alice Smith",
        "email": "alice@example.invalid",
        "phone": "+14155550142",
        "order_id": "ORD-1",
        "amount": 99.5,
        "emergency_contact_name": "Bob Relative",
        "emergency_contact_phone": "+14155550199",
        "health_conditions": "示例健康数据",
        "credit_card": "4539148803436467",
    }

    def test_none_mode_returns_untouched(self):
        out, report = rd.redact_record(self.RECORD, mode=rd.NONE)
        assert out == self.RECORD
        assert report["redacted_count"] == 0

    def test_none_mode_still_reports_nothing_removed(self):
        _, report = rd.redact_record(self.RECORD, mode=rd.NONE)
        assert report["third_party_removed"] == []

    def test_standard_masks_sensitive_fields(self):
        out, report = rd.redact_record(self.RECORD, mode=rd.STANDARD)
        assert out["email"] == "a***@e***.invalid"
        assert out["credit_card"].startswith("[CARD-TOKEN:")
        assert report["redacted_count"] >= 4

    def test_standard_strips_third_party_data(self):
        out, report = rd.redact_record(self.RECORD, mode=rd.STANDARD)
        assert "emergency_contact_name" not in out
        assert "emergency_contact_phone" not in out
        assert set(report["third_party_removed"]) == {"emergency_contact_name",
                                                      "emergency_contact_phone"}

    def test_art9_data_is_withheld_not_dumped(self):
        """Art. 9 数据不随响应直接交付 —— 直接给出可能本身违反 Art. 9 限制"""
        out, report = rd.redact_record(self.RECORD, mode=rd.STANDARD)
        assert "health_conditions" not in out
        assert out["health_conditions_presence"] == "WITHHELD_SPECIAL_CATEGORY_ART9"
        assert "health_conditions" in report["special_category_excluded"]

    def test_non_sensitive_fields_untouched(self):
        out, _ = rd.redact_record(self.RECORD, mode=rd.STANDARD)
        assert out["order_id"] == "ORD-1"
        assert out["amount"] == 99.5

    def test_strict_mode_also_works(self):
        out, report = rd.redact_record(self.RECORD, mode=rd.STRICT)
        assert "health_conditions" not in out
        assert report["redacted_count"] >= 4

    def test_unknown_mode_raises(self):
        with pytest.raises(KeyError):
            rd.redact_record(self.RECORD, mode="SUPER_SECURE")

    def test_empty_record(self):
        out, report = rd.redact_record({}, mode=rd.STANDARD)
        assert out == {}
        assert report["redacted_count"] == 0

    def test_report_lists_every_hit_kind(self):
        _, report = rd.redact_record(self.RECORD, mode=rd.STANDARD)
        assert report["fields"]["email"] == "email"
        assert report["fields"]["credit_card"] == "card"


class TestRedactPayload:
    RECORDS = [
        {"full_name": "Alice", "email": "a@x.io"},
        {"full_name": "Alice", "email": "a@x.io", "co_recipient_name": "Bob"},
    ]

    def test_returns_one_output_per_input(self):
        out, manifest = rd.redact_payload(self.RECORDS, mode=rd.STANDARD)
        assert len(out) == len(self.RECORDS)

    def test_manifest_counts_records(self):
        _, manifest = rd.redact_payload(self.RECORDS, mode=rd.STANDARD)
        assert manifest["record_count"] == 2

    def test_manifest_deduplicates_third_party_fields(self):
        _, manifest = rd.redact_payload(self.RECORDS * 3, mode=rd.STANDARD)
        assert manifest["third_party_fields_removed"] == ["co_recipient_name"]
        assert manifest["third_party_removal_count"] == 3

    def test_manifest_lists_withheld_art9_fields(self):
        recs = [{"health_conditions": "x"}]
        _, manifest = rd.redact_payload(recs, mode=rd.STANDARD)
        assert manifest["special_category_fields_withheld"] == ["health_conditions"]

    def test_manifest_includes_legal_note(self):
        _, manifest = rd.redact_payload(self.RECORDS, mode=rd.STANDARD)
        assert "15(4)" in manifest["legal_note"]
        assert "9" in manifest["legal_note"]

    def test_manifest_records_mode(self):
        _, manifest = rd.redact_payload(self.RECORDS, mode=rd.STRICT)
        assert manifest["mode"] == "STRICT"

    def test_empty_payload(self):
        out, manifest = rd.redact_payload([], mode=rd.STANDARD)
        assert out == []
        assert manifest["record_count"] == 0

    def test_none_payload(self):
        out, _ = rd.redact_payload(None, mode=rd.STANDARD)
        assert out == []


class TestBuildPayloadJson:
    def test_output_is_valid_json(self):
        payload, _ = rd.build_payload_json([{"email": "a@x.io"}], mode=rd.STANDARD)
        parsed = json.loads(payload)
        assert isinstance(parsed, list)
        assert parsed[0]["email"] == "a***@x***.io"

    def test_payload_contains_no_raw_sensitive_values(self):
        payload, _ = rd.build_payload_json(
            [{"email": "alice@example.invalid", "national_id": "11010119900307123X"}],
            mode=rd.STANDARD)
        assert "alice@example.invalid" not in payload
        assert "11010119900307123X" not in payload


class TestSummarizeSensitivity:
    def test_reports_sensitive_keys(self):
        s = rd.summarize_sensitivity({"email": "a@x.io", "full_name": "Bob"})
        assert s["email"] == "email"
        assert s["full_name"] == "name"

    def test_bare_name_not_treated_as_sensitive(self):
        """刻意不收录裸 `name`：country_name / product_name 之类会造成过度脱敏"""
        assert rd.detect_value_kind("name", "Bob") is None

    def test_reports_third_party_keys(self):
        s = rd.summarize_sensitivity({"emergency_contact_name": "Bob"})
        assert s["emergency_contact_name"] == "third_party"

    def test_does_not_mutate_input(self):
        rec = {"email": "a@x.io"}
        rd.summarize_sensitivity(rec)
        assert rec == {"email": "a@x.io"}
