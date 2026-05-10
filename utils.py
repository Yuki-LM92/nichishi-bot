"""Pure utility functions for nichishi-bot. No external state or API calls."""
import re
import calendar
from datetime import datetime, timedelta

_SAFE_SHEET_TITLE_RE = re.compile(r'^\d{1,2}月\d{1,2}日$')


def _sanitize_cell(value: str | None) -> str:
    """スプレッドシートインジェクション防止: 数式起動文字を無効化する。"""
    s = str(value) if value else ''
    return ("'" + s) if s and s[0] in ('=', '+', '-', '@', '\t', '\r') else s


def _sheet_range(sheet_title: str, cell: str) -> str:
    """シート名とセル位置から安全なA1記法を返す。不正なシート名は ValueError。"""
    if not _SAFE_SHEET_TITLE_RE.match(sheet_title):
        raise ValueError(f"Invalid sheet title: {sheet_title!r}")
    escaped = sheet_title.replace("'", "''")
    return f"'{escaped}'!{cell}"


def extract_spreadsheet_id(value: str | None) -> str:
    """URLからスプレッドシートIDを抽出する。URLでない場合はそのまま返す。"""
    if not value:
        return ''
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', value)
    return (m.group(1) if m else value).strip()


def extract_date(structured_text: str) -> tuple[int, int]:
    """構造化テキストから (month, day) を抽出する。見つからなければ今日の日付。"""
    for line in structured_text.split('\n'):
        line = line.strip()
        if line.startswith('📅'):
            date_str = line.replace('📅 日付：', '').strip()
            m = re.search(r'(\d{1,2})[/月](\d{1,2})', date_str)
            if m:
                mo, dy = int(m.group(1)), int(m.group(2))
                max_day = calendar.monthrange(datetime.now().year, mo)[1] if 1 <= mo <= 12 else 0
                if 1 <= mo <= 12 and 1 <= dy <= max_day:
                    return mo, dy
    today = datetime.now()
    return today.month, today.day


def extract_notes(structured_text: str) -> str:
    """構造化テキストから 📣 共有事項 以降の文字列を返す。"""
    notes = ''
    mode = None
    for line in structured_text.split('\n'):
        line = line.strip()
        if line.startswith('📣'):
            mode = 'notes'
            notes = line.replace('📣 共有事項：', '').strip()
        elif mode == 'notes' and line:
            notes += '\n' + line
    return notes


def _is_emoji_only(text: str) -> bool:
    """日本語・英数字を含まない（絵文字・記号のみ）場合 True を返す。"""
    return not re.search(r'[ぁ-んァ-ン一-龯一-鿿a-zA-Z0-9]', text)


def is_valid_email(email: str) -> bool:
    """RFC準拠の簡易メールアドレス検証。"""
    return bool(re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email))


def _get_week_dates(ref_date: datetime | None = None) -> list[datetime]:
    """ref_date を含む週の月〜金の日付リストを返す。"""
    if ref_date is None:
        ref_date = datetime.now()
    monday = ref_date - timedelta(days=ref_date.weekday())
    return [monday + timedelta(days=i) for i in range(5)]
