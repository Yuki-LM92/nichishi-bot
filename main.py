import os
import re
import json
import base64
import time
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, PushMessageRequest, TextMessage,
    QuickReply, QuickReplyItem, PostbackAction
)
from linebot.v3.webhooks import (
    MessageEvent, AudioMessageContent, PostbackEvent,
    TextMessageContent, FollowEvent, ImageMessageContent
)
import google.auth
import google.auth.transport.requests

app = Flask(__name__)

LINE_CHANNEL_SECRET         = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN   = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
GEMINI_API_KEY              = os.environ['GEMINI_API_KEY']
MASTER_SPREADSHEET_ID       = os.environ['MASTER_SPREADSHEET_ID']
SLACK_WEBHOOK_URL           = os.environ.get('SLACK_WEBHOOK_URL', '')
RICHMENU_REGISTERED         = os.environ.get('RICHMENU_REGISTERED', '')
RICHMENU_UNREGISTERED       = os.environ.get('RICHMENU_UNREGISTERED', '')
TEMPLATE_SPREADSHEET_ID     = os.environ.get('TEMPLATE_SPREADSHEET_ID', '')
ADMIN_EMAIL                 = os.environ.get('ADMIN_EMAIL', '')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ---- 状態管理 ----
# 確認待ちの構造化テキスト - メモリ+スプシ永続化
pending: dict = {}
# セッション状態キャッシュ - メモリ+スプシ永続化
_session_cache: dict = {}
# 処理中キャンセルフラグ（一時的・永続化不要）
pending_cancel: set = set()
# トークンキャッシュ
_token_cache: dict = {'token': None, 'expires_at': 0.0}
# 初回セットアップ済みフラグ
_setup_done: bool = False

TEMPLATE_SHEET_NAME = '●月●日（テンプレート）'
LIFF_URL            = 'https://liff.line.me/2009693703-ONMSHAXr'
PENDING_SHEET       = 'pending_states'
SESSION_SHEET       = 'session_states'
SESSION_TTL         = 30 * 60  # 30分

_REPORT_FORMAT = """
📅 日付：（言及があれば。なければ空欄）
⏰ 活動内容：
・[HH:MM] 活動内容
・[HH:MM] 活動内容
（複数ある場合はすべて列挙する）
📣 共有事項：（上司や仲間にSlackで伝えたいことがあれば記載。なければ「なし」）

時間のルール：
- 時間は必ず [HH:MM] 形式で角括弧に入れる（例：[08:00]、[10:30]）
- 「8時ごろ」→ [08:00]、「10時から11時」→ [10:00]、「11:15ごろ」→ [11:15]
- 時間の言及がない活動は [--:--] とする
- 途中で終わった記録も省略せずすべて記載する
"""

PROMPT = f"""
あなたは地域おこし協力隊の業務日報の記録係です。
送られてきた音声は、協力隊員が今日の業務を振り返って話したものです。

以下のフォーマットだけで出力してください（余計な説明は不要）：
{_REPORT_FORMAT}
音声に含まれる情報だけを使い、推測で補わないでください。
"""

TEXT_PROMPT = f"""
あなたは地域おこし協力隊の業務日報の記録係です。
以下のテキストは、協力隊員が今日の業務内容を伝えたものです。

以下のフォーマットだけで出力してください（余計な説明は不要）：
{_REPORT_FORMAT}
テキストに含まれる情報だけを使い、推測で補わないでください。

テキスト：
"""

CORRECTION_PROMPT = f"""
あなたは地域おこし協力隊の業務日報の記録係です。
以下の「現在の日報」に対して「修正指示」を適用し、修正後の日報を出力してください。

ルール：
- 修正指示に含まれる変更のみを反映し、それ以外の内容はそのまま維持する
- 以下のフォーマットだけで出力すること（余計な説明は不要）：
{_REPORT_FORMAT}
現在の日報：
{{original}}

修正指示：
{{correction}}
"""

# ========== Token ==========

def get_sheets_token() -> str:
    """Google APIトークンをキャッシュ付きで取得する。"""
    if _token_cache['token'] and time.time() < _token_cache['expires_at'] - 60:
        return _token_cache['token']
    creds, _ = google.auth.default(
        scopes=[
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive',
        ]
    )
    creds.refresh(google.auth.transport.requests.Request())
    _token_cache['token'] = creds.token
    _token_cache['expires_at'] = time.time() + 3600
    return creds.token

# ========== Helpers ==========

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

def _json_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ========== Session state (メモリ+スプシ永続化) ==========

def _session_rows(token: str) -> list:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}/values/{SESSION_SHEET}!A:D"
    resp = requests.get(url, headers=_auth_headers(token), timeout=15)
    if resp.status_code != 200:
        return []
    return resp.json().get('values', [])

def session_get(user_id: str, token: str) -> tuple:
    """セッション状態を返す。キャッシュ優先、TTL切れならNone。"""
    cached = _session_cache.get(user_id)
    if cached and time.time() - cached['ts'] <= SESSION_TTL:
        return cached['type'], cached['data']
    _session_cache.pop(user_id, None)

    for row in _session_rows(token):
        if len(row) >= 2 and row[0] == user_id:
            state_type = row[1]
            if not state_type:
                return None, {}
            data = json.loads(row[2]) if len(row) > 2 and row[2] else {}
            ts = float(row[3]) if len(row) > 3 and row[3] else 0.0
            if time.time() - ts > SESSION_TTL:
                return None, {}
            _session_cache[user_id] = {'type': state_type, 'data': data, 'ts': ts}
            return state_type, data
    return None, {}

def session_set(user_id: str, state_type: str, data: dict, token: str) -> None:
    """セッション状態を保存（メモリ+スプシ）。"""
    ts = time.time()
    _session_cache[user_id] = {'type': state_type, 'data': data, 'ts': ts}
    values = [[user_id, state_type, json.dumps(data), str(ts)]]
    rows = _session_rows(token)
    for i, row in enumerate(rows):
        if row and row[0] == user_id:
            row_num = i + 1
            url = (f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
                   f"/values/{SESSION_SHEET}!A{row_num}:D{row_num}?valueInputOption=RAW")
            requests.put(url, json={"values": values}, headers=_json_headers(token), timeout=15)
            return
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
           f"/values/{SESSION_SHEET}!A:D:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
    requests.post(url, json={"values": values}, headers=_json_headers(token), timeout=15)

def session_del(user_id: str, token: str) -> None:
    """セッション状態を削除（メモリ+スプシ）。"""
    _session_cache.pop(user_id, None)
    rows = _session_rows(token)
    for i, row in enumerate(rows):
        if row and row[0] == user_id:
            row_num = i + 1
            url = (f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
                   f"/values/{SESSION_SHEET}!A{row_num}:D{row_num}?valueInputOption=RAW")
            requests.put(url, json={"values": [['', '', '', '']]},
                        headers=_json_headers(token), timeout=15)
            return

def ensure_session_sheet(token: str) -> None:
    """session_statesシートが存在しない場合は作成する。"""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
    resp = requests.get(url, headers=_auth_headers(token), timeout=15)
    if not resp.ok:
        return
    existing = [s['properties']['title'] for s in resp.json().get('sheets', [])]
    if SESSION_SHEET not in existing:
        url2 = f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}:batchUpdate"
        requests.post(url2,
            json={"requests": [{"addSheet": {"properties": {"title": SESSION_SHEET}}}]},
            headers=_json_headers(token), timeout=15)

# ========== Sheets API ==========

def _pending_rows(token: str) -> list:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}/values/{PENDING_SHEET}!A:B"
    resp = requests.get(url, headers=_auth_headers(token), timeout=15)
    if resp.status_code != 200:
        return []
    return resp.json().get('values', [])

def pending_get(user_id: str, token: str) -> str:
    if user_id in pending:
        return pending[user_id]
    for row in _pending_rows(token):
        if len(row) >= 2 and row[0] == user_id and row[1]:
            pending[user_id] = row[1]
            return row[1]
    return ''

def pending_set(user_id: str, text: str, token: str) -> None:
    pending[user_id] = text
    rows = _pending_rows(token)
    for i, row in enumerate(rows):
        if row and row[0] == user_id:
            row_num = i + 1
            url = (f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
                   f"/values/{PENDING_SHEET}!A{row_num}:B{row_num}?valueInputOption=RAW")
            requests.put(url, json={"values": [[user_id, text]]},
                        headers=_json_headers(token), timeout=15)
            return
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
           f"/values/{PENDING_SHEET}!A:B:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
    requests.post(url, json={"values": [[user_id, text]]},
                 headers=_json_headers(token), timeout=15)

def pending_del(user_id: str, token: str) -> None:
    pending.pop(user_id, None)
    rows = _pending_rows(token)
    for i, row in enumerate(rows):
        if row and row[0] == user_id:
            row_num = i + 1
            url = (f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
                   f"/values/{PENDING_SHEET}!A{row_num}:B{row_num}?valueInputOption=RAW")
            requests.put(url, json={"values": [['', '']]},
                        headers=_json_headers(token), timeout=15)
            return

def get_all_members(token: str) -> list:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}/values/メンバー!A2:E"
    resp = requests.get(url, headers=_auth_headers(token), timeout=15)
    resp.raise_for_status()
    return resp.json().get('values', [])

def extract_spreadsheet_id(value: str) -> str:
    if not value:
        return ''
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', value)
    return m.group(1) if m else value

def get_member(user_id: str, token: str) -> dict | None:
    for row in get_all_members(token):
        if len(row) > 2 and row[2] == user_id:
            raw = row[3] if len(row) > 3 else ''
            return {
                'name': row[0] if len(row) > 0 else '',
                'spreadsheet_id': extract_spreadsheet_id(raw)
            }
    return None

def is_duplicate(user_id: str, name: str, email: str, token: str) -> bool:
    for row in get_all_members(token):
        row_line_id = row[2] if len(row) > 2 else ''
        row_name    = row[0] if len(row) > 0 else ''
        row_email   = row[1] if len(row) > 1 else ''
        if (user_id and row_line_id == user_id) or \
           (row_name == name and row_email == email):
            return True
    return False

def append_member(user_id: str, name: str, email: str, token: str, spreadsheet_url: str = '') -> None:
    now = datetime.now().strftime('%Y/%m/%d %H:%M')
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
        f"/values/メンバー!A:E:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    payload = {"values": [[name, email, user_id, spreadsheet_url, now]]}
    resp = requests.post(url, json=payload, headers=_json_headers(token), timeout=15)
    resp.raise_for_status()

def create_user_spreadsheet(name: str, email: str, token: str) -> tuple:
    if not TEMPLATE_SPREADSHEET_ID:
        return None, None
    resp = requests.post(
        f'https://www.googleapis.com/drive/v3/files/{TEMPLATE_SPREADSHEET_ID}/copy',
        json={'name': f'{name}さんの業務日誌', 'mimeType': 'application/vnd.google-apps.spreadsheet'},
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        timeout=30
    )
    resp.raise_for_status()
    file_id = resp.json()['id']
    perm_url = f'https://www.googleapis.com/drive/v3/files/{file_id}/permissions'
    for share_email in filter(None, [ADMIN_EMAIL, email]):
        try:
            requests.post(perm_url,
                json={'type': 'user', 'role': 'writer', 'emailAddress': share_email},
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                timeout=15
            )
        except Exception:
            pass
    spreadsheet_url = f'https://docs.google.com/spreadsheets/d/{file_id}/edit'
    return file_id, spreadsheet_url

def get_template_sheet_id(spreadsheet_id: str, token: str) -> int | None:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    resp = requests.get(url, headers=_auth_headers(token), timeout=15)
    if not resp.ok:
        raise ValueError(f"スプシ取得失敗 ID={repr(spreadsheet_id)} status={resp.status_code} body={resp.text[:200]}")
    for sheet in resp.json().get('sheets', []):
        if sheet['properties']['title'] == TEMPLATE_SHEET_NAME:
            return sheet['properties']['sheetId']
    return None

def copy_template(spreadsheet_id: str, template_id: int, new_title: str, token: str) -> None:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
    payload = {"requests": [{"duplicateSheet": {
        "sourceSheetId": template_id,
        "newSheetName": new_title,
        "insertSheetIndex": 1
    }}]}
    resp = requests.post(url, json=payload, headers=_json_headers(token), timeout=15)
    if resp.status_code == 400 and 'already exists' in resp.text:
        return
    resp.raise_for_status()

def write_to_sheet(spreadsheet_id: str, sheet_title: str, name: str,
                   structured_text: str, month: int, day: int, token: str) -> None:
    year = datetime.now().year
    reiwa_year = year - 2018

    activities = []
    notes = 'なし'
    mode = None
    for line in structured_text.split('\n'):
        line = line.strip()
        if line.startswith('⏰'):
            mode = 'act'
        elif line.startswith('📣'):
            mode = 'notes'
            notes = line.replace('📣 共有事項：', '').strip()
        elif mode == 'act' and line.startswith('・'):
            item = line.lstrip('・').strip()
            m = re.match(r'\[(.+?)\]\s*(.*)', item)
            if m:
                activities.append((m.group(1), m.group(2)))
            else:
                activities.append(('', item))
        elif mode == 'notes' and line:
            notes += '\n' + line

    data = [
        {"range": f"'{sheet_title}'!A3", "values": [[f"令和{reiwa_year}年{month}月{day}日"]]},
        {"range": f"'{sheet_title}'!B6", "values": [[name]]},
    ]
    for i, (time_str, content) in enumerate(activities[:7]):
        row = 10 + i
        data.append({"range": f"'{sheet_title}'!A{row}", "values": [[time_str]]})
        data.append({"range": f"'{sheet_title}'!B{row}", "values": [[content]]})
    overflow = activities[7:]
    notes_parts = []
    if notes and notes != 'なし':
        notes_parts.append(notes)
    if overflow:
        overflow_lines = '\n'.join(f"{t} {c}".strip() for t, c in overflow)
        notes_parts.append(f"【続き】\n{overflow_lines}")
    if notes_parts:
        data.append({"range": f"'{sheet_title}'!B17", "values": [['\n'.join(notes_parts)]]})

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate"
    resp = requests.post(url, json={"valueInputOption": "USER_ENTERED", "data": data},
                        headers=_json_headers(token), timeout=15)
    resp.raise_for_status()

PHOTO_CELL = 'F2'

def upload_photo_to_drive(image_bytes: bytes, filename: str, token: str) -> str:
    metadata = json.dumps({'name': filename, 'mimeType': 'image/jpeg'}).encode()
    boundary = b'nishishi_boundary_2025'
    body = (
        b'--' + boundary + b'\r\n'
        b'Content-Type: application/json; charset=UTF-8\r\n\r\n' +
        metadata + b'\r\n'
        b'--' + boundary + b'\r\n'
        b'Content-Type: image/jpeg\r\n\r\n' +
        image_bytes + b'\r\n'
        b'--' + boundary + b'--'
    )
    resp = requests.post(
        'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart',
        data=body,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': f'multipart/related; boundary={boundary.decode()}',
        },
        timeout=30
    )
    resp.raise_for_status()
    file_id = resp.json()['id']
    requests.post(
        f'https://www.googleapis.com/drive/v3/files/{file_id}/permissions',
        json={'type': 'anyone', 'role': 'reader'},
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        timeout=15
    )
    return file_id

def extract_date(structured_text: str) -> tuple:
    for line in structured_text.split('\n'):
        line = line.strip()
        if line.startswith('📅'):
            date_str = line.replace('📅 日付：', '').strip()
            m = re.search(r'(\d{1,2})[/月](\d{1,2})', date_str)
            if m:
                return int(m.group(1)), int(m.group(2))
    today = datetime.now()
    return today.month, today.day

def record_to_sheet(user_id: str, structured_text: str) -> tuple:
    token = get_sheets_token()
    member = get_member(user_id, token)
    if not member or not member.get('spreadsheet_id'):
        return None, None
    spreadsheet_id = member['spreadsheet_id']
    name = member['name']
    month, day = extract_date(structured_text)
    sheet_title = f"{month}月{day}日"
    template_id = get_template_sheet_id(spreadsheet_id, token)
    if template_id is None:
        return None, None
    copy_template(spreadsheet_id, template_id, sheet_title, token)
    write_to_sheet(spreadsheet_id, sheet_title, name, structured_text, month, day, token)
    return sheet_title, name

# ========== Slack ==========

def extract_notes(structured_text: str) -> str:
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

def send_to_slack(member_name: str, sheet_title: str, structured_text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    notes = extract_notes(structured_text)
    if not notes or notes == 'なし':
        return
    text = (
        f"📋 *{member_name}さんの日報（{sheet_title}）*\n\n"
        f"📣 共有事項：\n{notes}"
    )
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    except Exception:
        pass

# ========== Rich Menu ==========

def link_rich_menu(user_id: str, menu_id: str) -> None:
    if not menu_id:
        return
    try:
        requests.post(
            f'https://api.line.me/v2/bot/user/{user_id}/richmenu/{menu_id}',
            headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'},
            timeout=10
        )
    except Exception:
        pass

# ========== Feedback ==========

def save_feedback(user_id: str, category: str, message: str, token: str) -> None:
    member = get_member(user_id, token)
    name = member['name'] if member else '不明'
    now = datetime.now().strftime('%Y/%m/%d %H:%M')
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
        f"/values/フィードバック!A:D:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    payload = {"values": [[now, name, category, message]]}
    resp = requests.post(url, json=payload, headers=_json_headers(token), timeout=15)
    resp.raise_for_status()

# ========== Gemini ==========

def call_gemini_audio(audio_b64: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "audio/mp4", "data": audio_b64}},
        {"text": PROMPT}
    ]}]}
    resp = requests.post(url, json=payload, timeout=120)
    resp.raise_for_status()
    candidates = resp.json().get('candidates', [])
    if not candidates:
        raise ValueError("Gemini returned empty candidates")
    text = candidates[0]['content']['parts'][0]['text'].strip()
    if not text:
        raise ValueError("Gemini returned empty text")
    return text

def call_gemini_text(text: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": TEXT_PROMPT + text}]}]}
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    candidates = resp.json().get('candidates', [])
    if not candidates:
        raise ValueError("Gemini returned empty candidates")
    result = candidates[0]['content']['parts'][0]['text'].strip()
    if not result:
        raise ValueError("Gemini returned empty text")
    return result

def call_gemini_correction(original: str, correction: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = CORRECTION_PROMPT.format(original=original, correction=correction)
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    candidates = resp.json().get('candidates', [])
    if not candidates:
        raise ValueError("Gemini returned empty candidates")
    result = candidates[0]['content']['parts'][0]['text'].strip()
    if not result:
        raise ValueError("Gemini returned empty text")
    return result

# ========== Async processing ==========

def _send_confirm_push(user_id: str, structured: str) -> None:
    """確認画面をpush_messageで送る（非同期処理後の共通処理）。"""
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            push_message_request=PushMessageRequest(
                to=user_id,
                messages=[TextMessage(
                    text=f"📋 以下の内容で記録しますね。確認してください。\n\n{structured}",
                    quick_reply=QuickReply(items=[
                        QuickReplyItem(action=PostbackAction(label='✅ はい', data='confirm_yes')),
                        QuickReplyItem(action=PostbackAction(label='✏️ 修正する', data='confirm_no')),
                        QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='confirm_cancel')),
                    ])
                )]
            )
        )

def _process_audio_async(user_id: str, audio_b64: str) -> None:
    """音声処理をバックグラウンドで実行する（スレッド用）。"""
    try:
        structured = call_gemini_audio(audio_b64)
    except requests.exceptions.Timeout:
        push_text(user_id, "⏱️ AIの処理に時間がかかっています。\nしばらくしてからもう一度送ってください。")
        return
    except Exception:
        push_text(user_id, "⚠️ 音声の解析に失敗しました。\n少し長めに話して、もう一度送ってください。\n（目安：30秒以上）")
        return

    if user_id in pending_cancel:
        pending_cancel.discard(user_id)
        push_text(user_id, "⛔ キャンセル済みのため、記録しませんでした。")
        return

    try:
        token = get_sheets_token()
        pending_set(user_id, structured, token)
    except Exception:
        pending[user_id] = structured

    _send_confirm_push(user_id, structured)

def _process_correction_async(user_id: str, original: str, correction_text: str) -> None:
    """修正処理をバックグラウンドで実行する（スレッド用）。"""
    try:
        structured = call_gemini_correction(original, correction_text)
    except requests.exceptions.Timeout:
        push_text(user_id, "⏱️ AIの処理に時間がかかっています。\nもう一度送ってください。")
        return
    except Exception:
        push_text(user_id, "⚠️ 処理中にエラーが発生しました。\nもう一度送ってください。")
        return

    try:
        token = get_sheets_token()
        pending_set(user_id, structured, token)
    except Exception:
        pending[user_id] = structured

    _send_confirm_push(user_id, structured)

# ========== LINE helpers ==========

def reply_text(reply_token: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

def push_text(user_id: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            push_message_request=PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)]
            )
        )

# ========== Messages ==========

WELCOME_MESSAGE = """\
🎙️ 音声日報サービスへようこそ！

このアカウントでできること：

🎤 音声を送るだけで日報が完成
📋 AIが話した内容を自動で整理
📊 スプレッドシートに自動書き込み
💬 チームへの共有も同時に実施

手入力は一切不要です。
帰り道や移動中に今日の業務を
ひとこと話すだけでOK👍

ーーーーーーーーーー
ご利用には利用登録が必要です。

下のメニューから
「初めての方はこちら」をタップして
登録をお願いします🙏"""

NOT_REGISTERED_MESSAGE = (
    "ご利用には利用登録が必要です。\n\n"
    "下のメニューから「初めての方はこちら」を\n"
    "タップして登録してください🙏"
)

WAITING_SHEET_MESSAGE = (
    "担当者がスプレッドシートを準備中です。\n"
    "準備が完了したらご連絡します。\n"
    "もうしばらくお待ちください🙏"
)

AUDIO_GUIDE_MESSAGE = (
    "🎙️ 音声メッセージを送ると日報を記録できます。\n"
    "マイクボタンをタップして録音開始、\n"
    "もう一度タップで送信します🎤"
)

# ========== Validation ==========

def is_valid_email(email: str) -> bool:
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email))

# ========== Flask routes ==========

@app.before_request
def setup():
    global _setup_done
    if not _setup_done:
        _setup_done = True
        try:
            token = get_sheets_token()
            ensure_session_sheet(token)
        except Exception as e:
            print(f"[setup error] {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route('/health', methods=['GET'])
def health():
    return 'OK'

@app.route('/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST'
        return resp

    def cors_response(body, status=200):
        resp = app.make_response((body, status))
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp

    try:
        data = request.get_json()
        line_user_id = data.get('line_user_id', '')
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()

        if not name or not email or not is_valid_email(email):
            return cors_response({'error': 'invalid fields'}, 400)

        token = get_sheets_token()

        if is_duplicate(line_user_id, name, email, token):
            if line_user_id:
                link_rich_menu(line_user_id, RICHMENU_REGISTERED)
            return cors_response({'status': 'already_registered'})

        spreadsheet_url = ''
        try:
            _, spreadsheet_url = create_user_spreadsheet(name, email, token)
        except Exception:
            pass

        append_member(line_user_id, name, email, token, spreadsheet_url)

        if SLACK_WEBHOOK_URL:
            try:
                requests.post(SLACK_WEBHOOK_URL, json={
                    "text": (
                        f"📝 *新規メンバーが登録しました*\n\n"
                        f"お名前：{name}\n"
                        f"メール：{email}\n\n"
                        "スプレッドシートを準備してマスタースプシのC列にURLを貼り付けてください。"
                    )
                }, timeout=10)
            except Exception:
                pass

        if line_user_id:
            try:
                msg = "✅ 登録が完了しました！\n\n"
                if spreadsheet_url:
                    msg += f"📊 あなた専用のスプレッドシートを作成しました：\n{spreadsheet_url}\n\n"
                else:
                    msg += "スプレッドシートは管理者が準備次第ご連絡します。\n\n"
                msg += (
                    "音声を送ってみてください🎤\n\n"
                    "📖 使い方ガイドはこちら：\n"
                    "https://yuki-lm92.github.io/nichishi-register/guide.html"
                )
                push_text(line_user_id, msg)
            except Exception:
                pass
            link_rich_menu(line_user_id, RICHMENU_REGISTERED)

        return cors_response({'status': 'ok'})

    except Exception:
        import traceback
        print(f"[register error] {traceback.format_exc()}")
        return cors_response({'error': 'internal server error'}, 500)

# ========== LINE event handlers ==========

@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    try:
        token = get_sheets_token()
        member = get_member(user_id, token)
    except Exception:
        member = None

    if member:
        link_rich_menu(user_id, RICHMENU_REGISTERED)
        reply_text(event.reply_token,
            f"おかえりなさい、{member['name']}さん！👋\n"
            "引き続きご利用ください🎤"
        )
    else:
        reply_text(event.reply_token, WELCOME_MESSAGE)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    # キャンセルは最優先（Sheets APIより先に処理）
    if text == 'キャンセル':
        pending_cancel.add(user_id)
        try:
            token = get_sheets_token()
            pending_del(user_id, token)
            session_del(user_id, token)
        except Exception:
            pending.pop(user_id, None)
            _session_cache.pop(user_id, None)
        reply_text(event.reply_token, "⛔ キャンセルしました。\n処理中の場合も完了後に破棄します。")
        return

    token = get_sheets_token()
    session_type, session_data = session_get(user_id, token)

    # 写真登録：日付入力待ち
    if session_type == 'photo_date':
        today = datetime.now()
        if text in ['今日', 'きょう']:
            month, day = today.month, today.day
        elif text in ['昨日', 'きのう']:
            yday = today - timedelta(days=1)
            month, day = yday.month, yday.day
        else:
            m = re.search(r'(\d{1,2})[/月](\d{1,2})', text)
            if not m:
                reply_text(event.reply_token,
                    "日付を入力してください。\n例：4/10、4月10日、今日、昨日")
                return
            month, day = int(m.group(1)), int(m.group(2))
        session_set(user_id, 'photo_ready', {'month': month, 'day': day}, token)
        reply_text(event.reply_token,
            f"✅ {month}月{day}日ですね！\nでは登録する写真を送ってください📸")
        return

    # フィードバック収集モード
    if session_type == 'feedback':
        category = session_data.get('category', '')
        session_del(user_id, token)
        reply_text(event.reply_token, "⏳ 送信中です...")
        try:
            save_feedback(user_id, category, text, token)
            push_text(user_id, "✅ ありがとうございます！内容を受け付けました🙏\n確認次第ご連絡します。")
        except Exception:
            push_text(user_id, "⚠️ 送信中にエラーが発生しました。\nしばらくしてからお試しください。")
        return

    # 修正モード
    if session_type == 'correction':
        session_del(user_id, token)
        original = pending_get(user_id, token)
        reply_text(event.reply_token, "✏️ 修正内容を受け取りました！\nAIが整理しています...")
        threading.Thread(
            target=_process_correction_async,
            args=(user_id, original, text),
            daemon=True
        ).start()
        return

    # 登録状況を確認
    member = get_member(user_id, token)

    if member is None:
        reply_text(event.reply_token, NOT_REGISTERED_MESSAGE)
        return

    if not member.get('spreadsheet_id'):
        reply_text(event.reply_token, WAITING_SHEET_MESSAGE)
        return

    link_rich_menu(user_id, RICHMENU_REGISTERED)
    reply_text(event.reply_token, AUDIO_GUIDE_MESSAGE)

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    user_id = event.source.user_id

    token = get_sheets_token()
    member = get_member(user_id, token)
    if not member or not member.get('spreadsheet_id'):
        reply_text(event.reply_token, NOT_REGISTERED_MESSAGE)
        return

    session_type, session_data = session_get(user_id, token)
    if session_type != 'photo_ready':
        reply_text(event.reply_token,
            "📸 写真を登録するには、メニューの「②写真を登録」をタップして日付を入力してから写真を送ってください。")
        return

    month, day = session_data['month'], session_data['day']
    session_del(user_id, token)

    reply_text(event.reply_token, "📸 写真を受け取りました！アップロード中です...")

    try:
        with ApiClient(configuration) as api_client:
            image_bytes = MessagingApiBlob(api_client).get_message_content(event.message.id)

        now = datetime.now()
        filename = f"activity_{user_id}_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
        file_id = upload_photo_to_drive(image_bytes, filename, token)

        session_set(user_id, 'photo_pending', {'file_id': file_id, 'month': month, 'day': day}, token)

        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                push_message_request=PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(
                        text=f"📸 アップロード完了！\n{month}月{day}日の活動写真として追加しますか？",
                        quick_reply=QuickReply(items=[
                            QuickReplyItem(action=PostbackAction(label='✅ 追加する', data='add_photo')),
                            QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='cancel_photo')),
                        ])
                    )]
                )
            )
    except Exception:
        push_text(user_id, "⚠️ 写真のアップロードに失敗しました。\nしばらくしてからお試しください。")

@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio(event):
    user_id = event.source.user_id

    token = get_sheets_token()
    member = get_member(user_id, token)

    if member is None:
        reply_text(event.reply_token, NOT_REGISTERED_MESSAGE)
        return

    if not member.get('spreadsheet_id'):
        reply_text(event.reply_token, WAITING_SHEET_MESSAGE)
        return

    # リッチメニュー復元・進行中の修正モードを解除
    link_rich_menu(user_id, RICHMENU_REGISTERED)
    try:
        session_del(user_id, token)
    except Exception:
        _session_cache.pop(user_id, None)

    # 音声データ取得・base64変換（reply_tokenを使う前に完了させる）
    with ApiClient(configuration) as api_client:
        audio_bytes = MessagingApiBlob(api_client).get_message_content(event.message.id)
    audio_b64 = base64.b64encode(audio_bytes).decode()

    # すぐに受付メッセージを返信（LINEのWebhookタイムアウト対策）
    reply_text(
        event.reply_token,
        "🎙️ 音声を受け取りました！\nAIが内容を整理しています...\n（10〜30秒ほどかかります）"
    )

    # Gemini処理はバックグラウンドスレッドで実行
    threading.Thread(
        target=_process_audio_async,
        args=(user_id, audio_b64),
        daemon=True
    ).start()

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data

    if data == 'confirm_yes':
        token = get_sheets_token()
        structured = pending_get(user_id, token)
        if not structured:
            reply_text(event.reply_token, "⚠️ 記録する内容が見つかりませんでした。\nもう一度音声を送ってください。")
            return
        try:
            sheet_name, member_name = record_to_sheet(user_id, structured)
            if sheet_name:
                reply_text(event.reply_token, f"✅ {sheet_name}の日報をスプレッドシートに記録しました！")
                send_to_slack(member_name, sheet_name, structured)
                pending_del(user_id, token)
            else:
                reply_text(event.reply_token, "⚠️ スプレッドシートへの記録に失敗しました。\n管理者にお問い合わせください。")
                pending_del(user_id, token)
        except Exception as e:
            print(f"[confirm_yes error] {e}")
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(
                            text="⚠️ 記録中にエラーが発生しました。\nもう一度試しますか？",
                            quick_reply=QuickReply(items=[
                                QuickReplyItem(action=PostbackAction(label='✅ リトライ', data='confirm_yes')),
                                QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='confirm_cancel')),
                            ])
                        )]
                    )
                )

    elif data == 'confirm_no':
        try:
            token = get_sheets_token()
            session_set(user_id, 'correction', {}, token)
        except Exception:
            _session_cache[user_id] = {'type': 'correction', 'data': {}, 'ts': time.time()}
        reply_text(
            event.reply_token,
            "修正内容をテキストで送ってください。\n"
            "例）「午後の作業時間を3時間に変えて」\n"
            "　　「日付を4月10日に変えて」"
        )

    elif data == 'confirm_cancel':
        try:
            _token = get_sheets_token()
            pending_del(user_id, _token)
            session_del(user_id, _token)
        except Exception:
            pending.pop(user_id, None)
            _session_cache.pop(user_id, None)
        reply_text(event.reply_token, "キャンセルしました。\n記録は行われていません。")

    elif data == 'guide_voice':
        reply_text(event.reply_token,
            "🎙️ 音声で日誌を入力する手順\n"
            "━━━━━━━━━━━\n"
            "① 画面左下の\n"
            "　 キーボードマークをタップ\n\n"
            "② スタンプボタンの右にある\n"
            "　 マイクボタンをタップ\n"
            "　 →録音開始\n\n"
            "③ 今日の業務内容を話す\n"
            "　 （目安：15秒〜2分）\n\n"
            "④ もう一度マイクボタンをタップ\n"
            "　 →録音停止・送信\n\n"
            "⑤ 内容を確認して「✅ はい」\n"
            "━━━━━━━━━━━\n"
            "では話してみてください！"
        )

    elif data == 'guide_photo':
        try:
            token = get_sheets_token()
            session_set(user_id, 'photo_date', {}, token)
        except Exception:
            _session_cache[user_id] = {'type': 'photo_date', 'data': {}, 'ts': time.time()}
        reply_text(event.reply_token,
            "📸 写真を登録する手順\n"
            "━━━━━━━━━━━\n"
            "① 日付を入力（今から）\n"
            "② 写真を送信\n"
            "③「✅ 追加する」をタップ\n"
            "━━━━━━━━━━━\n"
            "何日の日報に追加しますか？\n"
            "例：4/10　4月10日　今日　昨日"
        )

    elif data == 'start_feedback':
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text="どちらについてお送りですか？",
                        quick_reply=QuickReply(items=[
                            QuickReplyItem(action=PostbackAction(label='💬 フィードバック・改善要望', data='feedback_type_feedback')),
                            QuickReplyItem(action=PostbackAction(label='📞 管理者に連絡する', data='feedback_type_contact')),
                            QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='feedback_cancel')),
                        ])
                    )]
                )
            )

    elif data == 'feedback_type_feedback':
        try:
            token = get_sheets_token()
            session_set(user_id, 'feedback', {'category': 'フィードバック・改善要望'}, token)
        except Exception:
            _session_cache[user_id] = {'type': 'feedback', 'data': {'category': 'フィードバック・改善要望'}, 'ts': time.time()}
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text="💬 ご意見・改善要望をテキストで送ってください。\nどんな小さなことでも歓迎です！",
                        quick_reply=QuickReply(items=[
                            QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='feedback_cancel')),
                        ])
                    )]
                )
            )

    elif data == 'feedback_type_contact':
        try:
            token = get_sheets_token()
            session_set(user_id, 'feedback', {'category': '管理者への連絡'}, token)
        except Exception:
            _session_cache[user_id] = {'type': 'feedback', 'data': {'category': '管理者への連絡'}, 'ts': time.time()}
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text="📞 管理者への連絡内容をテキストで送ってください。",
                        quick_reply=QuickReply(items=[
                            QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='feedback_cancel')),
                        ])
                    )]
                )
            )

    elif data == 'feedback_cancel':
        try:
            token = get_sheets_token()
            session_del(user_id, token)
        except Exception:
            _session_cache.pop(user_id, None)
        reply_text(event.reply_token, "キャンセルしました。")

    elif data == 'add_photo':
        try:
            token = get_sheets_token()
            state_type, info = session_get(user_id, token)
        except Exception:
            state_type, info = None, {}

        if state_type != 'photo_pending' or not info:
            reply_text(event.reply_token, "⚠️ 写真データが見つかりません。もう一度送ってください。")
            return

        try:
            member = get_member(user_id, token)
            if not member or not member.get('spreadsheet_id'):
                reply_text(event.reply_token, "⚠️ メンバー情報が見つかりません。")
                return
            session_del(user_id, token)
            spreadsheet_id = member['spreadsheet_id']
            month, day = info['month'], info['day']
            sheet_title = f"{month}月{day}日"
            image_url = f"https://drive.google.com/uc?export=view&id={info['file_id']}"
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate"
            resp = requests.post(url, json={
                "valueInputOption": "USER_ENTERED",
                "data": [{"range": f"'{sheet_title}'!{PHOTO_CELL}", "values": [[f'=IMAGE("{image_url}")']]}]
            }, headers=_json_headers(token), timeout=15)
            if resp.ok:
                reply_text(event.reply_token, f"✅ {sheet_title}の活動写真を追加しました！")
            else:
                reply_text(event.reply_token, "⚠️ 写真の追加に失敗しました。\nしばらくしてからお試しください。")
        except Exception as e:
            print(f"[add_photo error] {e}")
            reply_text(event.reply_token, "⚠️ 写真の追加中にエラーが発生しました。")

    elif data == 'cancel_photo':
        try:
            token = get_sheets_token()
            session_del(user_id, token)
        except Exception:
            _session_cache.pop(user_id, None)
        reply_text(event.reply_token, "キャンセルしました。")

    elif data == 'open_spreadsheet':
        try:
            token = get_sheets_token()
            member = get_member(user_id, token)
            if member and member.get('spreadsheet_id'):
                url = f"https://docs.google.com/spreadsheets/d/{member['spreadsheet_id']}/edit"
                reply_text(event.reply_token, f"📊 スプレッドシートはこちらです：\n{url}")
            else:
                reply_text(event.reply_token,
                    "⏳ スプレッドシートはまだ準備中です。\n\n"
                    "管理者が承認・設定するまで1〜3日かかる場合があります。\n"
                    "準備ができたらご連絡しますので、もう少しお待ちください🙏"
                )
        except Exception:
            reply_text(event.reply_token, "⚠️ 確認中にエラーが発生しました。\nしばらくしてからお試しください。")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
