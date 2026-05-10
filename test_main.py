"""
main.py の純粋関数に対するユニットテスト。
外部API（Google Sheets / Gemini / LINE）は一切呼び出さない。
"""
import pytest
from datetime import datetime

from utils import (
    _sanitize_cell,
    _sheet_range,
    _is_emoji_only,
    extract_date,
    extract_notes,
    extract_spreadsheet_id,
    is_valid_email,
    _get_week_dates,
)


# ──────────────────────────────────────────
# _sanitize_cell
# ──────────────────────────────────────────
class TestSanitizeCell:
    def test_formula_equals(self):
        assert _sanitize_cell("=CMD()") == "'=CMD()"

    def test_formula_plus(self):
        assert _sanitize_cell("+1+1") == "'+1+1"

    def test_formula_minus(self):
        assert _sanitize_cell("-1+1") == "'-1+1"

    def test_formula_at(self):
        assert _sanitize_cell("@SUM") == "'@SUM"

    def test_tab_prefix(self):
        assert _sanitize_cell("\tDATA") == "'\tDATA"

    def test_safe_japanese(self):
        assert _sanitize_cell("田中太郎") == "田中太郎"

    def test_safe_number_string(self):
        assert _sanitize_cell("12345") == "12345"

    def test_empty_string(self):
        assert _sanitize_cell("") == ""

    def test_none_returns_empty(self):
        assert _sanitize_cell(None) == ""


# ──────────────────────────────────────────
# _sheet_range
# ──────────────────────────────────────────
class TestSheetRange:
    def test_normal_date(self):
        assert _sheet_range("4月10日", "A3") == "'4月10日'!A3"

    def test_december_31(self):
        assert _sheet_range("12月31日", "B6") == "'12月31日'!B6"

    def test_single_digit(self):
        assert _sheet_range("1月1日", "F2") == "'1月1日'!F2"

    def test_injection_single_quote(self):
        with pytest.raises(ValueError):
            _sheet_range("'; DROP TABLE", "A1")

    def test_injection_formula(self):
        with pytest.raises(ValueError):
            _sheet_range("=SUM(1,2)", "A1")

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            _sheet_range("April 10", "A1")


# ──────────────────────────────────────────
# extract_date
# ──────────────────────────────────────────
class TestExtractDate:
    def test_slash_format(self):
        assert extract_date("📅 日付：4/10") == (4, 10)

    def test_japanese_format(self):
        assert extract_date("📅 日付：4月10日") == (4, 10)

    def test_december_31(self):
        assert extract_date("📅 日付：12/31") == (12, 31)

    def test_leading_zero(self):
        assert extract_date("📅 日付：04/09") == (4, 9)

    def test_invalid_month_13_falls_back_to_today(self):
        today = datetime.now()
        assert extract_date("📅 日付：13/5") == (today.month, today.day)

    def test_invalid_day_feb31_falls_back_to_today(self):
        today = datetime.now()
        assert extract_date("📅 日付：2/31") == (today.month, today.day)

    def test_no_date_section_falls_back_to_today(self):
        today = datetime.now()
        assert extract_date("⏰ 活動内容：作業しました") == (today.month, today.day)

    def test_empty_date_falls_back_to_today(self):
        today = datetime.now()
        assert extract_date("📅 日付：") == (today.month, today.day)


# ──────────────────────────────────────────
# is_valid_email
# ──────────────────────────────────────────
class TestIsValidEmail:
    def test_simple(self):
        assert is_valid_email("test@example.com") is True

    def test_subdomain(self):
        assert is_valid_email("test@mail.example.co.jp") is True

    def test_plus_tag(self):
        assert is_valid_email("test+tag@example.com") is True

    def test_no_at_sign(self):
        assert is_valid_email("invalid") is False

    def test_no_tld(self):
        assert is_valid_email("test@example") is False

    def test_empty(self):
        assert is_valid_email("") is False

    def test_at_only(self):
        assert is_valid_email("@example.com") is False

    def test_spaces(self):
        assert is_valid_email("te st@example.com") is False


# ──────────────────────────────────────────
# extract_spreadsheet_id
# ──────────────────────────────────────────
class TestExtractSpreadsheetId:
    def test_full_url(self):
        url = "https://docs.google.com/spreadsheets/d/abc123xyz-_ABCDEF/edit"
        assert extract_spreadsheet_id(url) == "abc123xyz-_ABCDEF"

    def test_bare_id_passthrough(self):
        assert extract_spreadsheet_id("abc123") == "abc123"

    def test_empty_returns_empty(self):
        assert extract_spreadsheet_id("") == ""

    def test_none_returns_empty(self):
        assert extract_spreadsheet_id(None) == ""

    def test_url_with_surrounding_whitespace(self):
        url = "  https://docs.google.com/spreadsheets/d/abc123xyz-_ABCDEF/edit  "
        assert extract_spreadsheet_id(url) == "abc123xyz-_ABCDEF"

    def test_bare_id_with_whitespace(self):
        assert extract_spreadsheet_id("  abc123  ") == "abc123"


# ──────────────────────────────────────────
# _is_emoji_only
# ──────────────────────────────────────────
class TestIsEmojiOnly:
    def test_single_emoji(self):
        assert _is_emoji_only("😊") is True

    def test_multiple_emoji(self):
        assert _is_emoji_only("👍🎉") is True

    def test_japanese_hiragana(self):
        assert _is_emoji_only("おはよう") is False

    def test_japanese_katakana(self):
        assert _is_emoji_only("テスト") is False

    def test_kanji(self):
        assert _is_emoji_only("日本語") is False

    def test_english(self):
        assert _is_emoji_only("hello") is False

    def test_emoji_with_japanese(self):
        assert _is_emoji_only("😊おはよう") is False

    def test_numbers_only(self):
        # 数字はアルファベット扱いなので日誌テキストとみなす
        assert _is_emoji_only("123") is False


# ──────────────────────────────────────────
# extract_notes
# ──────────────────────────────────────────
class TestExtractNotes:
    def test_with_notes(self):
        text = "⏰ 活動内容：\n・作業\n📣 共有事項：明日は休みます"
        assert extract_notes(text) == "明日は休みます"

    def test_multiline_notes(self):
        text = "📣 共有事項：1行目\n2行目"
        result = extract_notes(text)
        assert "1行目" in result
        assert "2行目" in result

    def test_no_notes_section(self):
        assert extract_notes("⏰ 活動内容：作業") == ""

    def test_notes_nashi(self):
        text = "📣 共有事項：なし"
        assert extract_notes(text) == "なし"


# ──────────────────────────────────────────
# _get_week_dates
# ──────────────────────────────────────────
class TestGetWeekDates:
    def test_returns_five_days(self):
        ref = datetime(2026, 5, 6)  # Wednesday
        dates = _get_week_dates(ref)
        assert len(dates) == 5

    def test_starts_on_monday(self):
        ref = datetime(2026, 5, 6)  # Wednesday
        dates = _get_week_dates(ref)
        assert dates[0].weekday() == 0  # Monday

    def test_ends_on_friday(self):
        ref = datetime(2026, 5, 6)  # Wednesday
        dates = _get_week_dates(ref)
        assert dates[-1].weekday() == 4  # Friday

    def test_monday_ref_returns_same_week(self):
        ref = datetime(2026, 5, 4)  # Monday
        dates = _get_week_dates(ref)
        assert dates[0] == datetime(2026, 5, 4)
        assert dates[4] == datetime(2026, 5, 8)

    def test_friday_ref_returns_same_week(self):
        ref = datetime(2026, 5, 8)  # Friday
        dates = _get_week_dates(ref)
        assert dates[0] == datetime(2026, 5, 4)
        assert dates[4] == datetime(2026, 5, 8)

    def test_none_ref_returns_current_week(self):
        dates = _get_week_dates(None)
        assert len(dates) == 5
        assert dates[0].weekday() == 0
