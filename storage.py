"""Data persistence layer using Google Sheets / Drive.
To migrate to Firestore or another backend, replace only this module.

Public interface (grouped by concern):
  Auth    : get_sheets_token
  Session : session_get / session_set / session_del / ensure_session_sheet
  Pending : pending_get / pending_set / pending_del
  Members : get_all_members / get_member / is_duplicate / append_member
  Records : create_user_spreadsheet / get_template_sheet_id / copy_template
            write_to_sheet / record_to_sheet
  Photo   : upload_photo_to_drive / write_photo_url_to_sheet
  Slack   : send_to_slack
  Feedback: save_feedback
  Summary : get_spreadsheet_sheet_titles / read_day_activities
"""
import json
import os
import re
import time
import threading
import logging
import requests
import google.auth
import google.auth.transport.requests
from datetime import datetime

import config
from utils import (
    _sanitize_cell, _sheet_range,
    extract_spreadsheet_id, extract_date, extract_notes,
)

logger = logging.getLogger(__name__)

# ── In-memory caches (reset on Cloud Run restart) ─────────────────────────
pending: dict        = {}
_session_cache: dict = {}
pending_cancel: set  = set()

_token_cache: dict   = {'token': None, 'expires_at': 0.0}  # nosec B105
_members_cache: dict = {'data': None, 'ts': 0.0}

# ── Locks ─────────────────────────────────────────────────────────────────
_token_lock  = threading.Lock()
_cancel_lock = threading.Lock()
_cache_lock  = threading.Lock()

# ── Regex ─────────────────────────────────────────────────────────────────
_SAFE_FILE_ID_RE = re.compile(r'^[A-Za-z0-9_-]+$')


# ══════════════════════════════════════════════════════════════════════════
# HTTP helpers
# ══════════════════════════════════════════════════════════════════════════

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _json_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _http_retry(method: str, url: str, *, headers: dict,
                max_retries: int = 3, backoff: float = 1.0, **kwargs) -> requests.Response:
    """Sheets/Drive API の 429/5xx に対して exponential backoff でリトライする。"""
    resp = None
    for attempt in range(max_retries):
        resp = requests.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < max_retries - 1:
                wait = backoff * (2 ** attempt)
                logger.warning("[HTTP] status=%s retry=%d wait=%.1fs", resp.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
        return resp
    return resp  # type: ignore[return-value]


# ══════════════════════════════════════════════════════════════════════════
# Auth token
# ══════════════════════════════════════════════════════════════════════════

def get_sheets_token() -> str:
    with _token_lock:
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
        _token_cache['expires_at'] = (
            creds.expiry.timestamp() if creds.expiry else time.time() + 3600
        )
        return creds.token


# ══════════════════════════════════════════════════════════════════════════
# Session state (memory + Sheets persistence)
# ══════════════════════════════════════════════════════════════════════════

def _session_rows(token: str) -> list[tuple[int, list]]:
    """スプレッドシートの (1始まり行番号, 行データ) のリストを返す。空行は除外。"""
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
           f"/values/{config.SESSION_SHEET}!A:D")
    resp = _http_retry('get', url, headers=_auth_headers(token), timeout=15)
    if not resp.ok:
        return []
    return [(i + 1, r) for i, r in enumerate(resp.json().get('values', [])) if r and r[0]]


def session_get(user_id: str, token: str) -> tuple:
    """セッション状態を返す。キャッシュ優先、TTL切れなら None。"""
    with _cache_lock:
        cached = _session_cache.get(user_id)
        if cached and time.time() - cached['ts'] <= config.SESSION_TTL:
            return cached['type'], cached['data']
        _session_cache.pop(user_id, None)

    for _, row in _session_rows(token):
        if len(row) >= 2 and row[0] == user_id:
            state_type = row[1]
            if not state_type:
                return None, {}
            data = json.loads(row[2]) if len(row) > 2 and row[2] else {}
            ts = float(row[3]) if len(row) > 3 and row[3] else 0.0
            if time.time() - ts > config.SESSION_TTL:
                return None, {}
            with _cache_lock:
                _session_cache[user_id] = {'type': state_type, 'data': data, 'ts': ts}
            return state_type, data
    return None, {}


def session_set(user_id: str, state_type: str, data: dict, token: str) -> None:
    ts = time.time()
    with _cache_lock:
        _session_cache[user_id] = {'type': state_type, 'data': data, 'ts': ts}
    values = [[user_id, state_type, json.dumps(data), str(ts)]]
    for row_num, row in _session_rows(token):
        if row[0] == user_id:
            url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
                   f"/values/{config.SESSION_SHEET}!A{row_num}:D{row_num}?valueInputOption=RAW")
            resp = _http_retry('put', url, json={"values": values},
                               headers=_json_headers(token), timeout=15)
            if not resp.ok:
                logger.warning("[SES-01] session_set put failed status=%s", resp.status_code)
            return
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
           f"/values/{config.SESSION_SHEET}!A:D:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
    resp = _http_retry('post', url, json={"values": values},
                       headers=_json_headers(token), timeout=15)
    if not resp.ok:
        logger.warning("[SES-01] session_set append failed status=%s", resp.status_code)


def session_del(user_id: str, token: str) -> None:
    with _cache_lock:
        _session_cache.pop(user_id, None)
    for row_num, row in _session_rows(token):
        if row[0] == user_id:
            url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
                   f"/values/{config.SESSION_SHEET}!A{row_num}:D{row_num}?valueInputOption=RAW")
            resp = _http_retry('put', url, json={"values": [['', '', '', '']]},
                               headers=_json_headers(token), timeout=15)
            if not resp.ok:
                logger.warning("[SES-02] session_del failed status=%s", resp.status_code)
            return


def ensure_session_sheet(token: str) -> None:
    """session_states シートが存在しない場合は作成する。"""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
    resp = _http_retry('get', url, headers=_auth_headers(token), timeout=15)
    if not resp.ok:
        return
    existing = [s['properties']['title'] for s in resp.json().get('sheets', [])]
    if config.SESSION_SHEET not in existing:
        url2 = (f"https://sheets.googleapis.com/v4/spreadsheets"
                f"/{config.MASTER_SPREADSHEET_ID}:batchUpdate")
        _http_retry('post', url2,
                    json={"requests": [{"addSheet": {"properties": {"title": config.SESSION_SHEET}}}]},
                    headers=_json_headers(token), timeout=15)


# ══════════════════════════════════════════════════════════════════════════
# Pending state (memory + Sheets persistence)
# ══════════════════════════════════════════════════════════════════════════

def _pending_rows(token: str) -> list[tuple[int, list]]:
    """スプレッドシートの (1始まり行番号, 行データ) のリストを返す。空行は除外。"""
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
           f"/values/{config.PENDING_SHEET}!A:B")
    resp = _http_retry('get', url, headers=_auth_headers(token), timeout=15)
    if not resp.ok:
        return []
    return [(i + 1, r) for i, r in enumerate(resp.json().get('values', [])) if r and r[0]]


def pending_get(user_id: str, token: str) -> str:
    with _cache_lock:
        if user_id in pending:
            return pending[user_id]
    for _, row in _pending_rows(token):
        if len(row) >= 2 and row[0] == user_id and row[1]:
            with _cache_lock:
                pending[user_id] = row[1]
            return row[1]
    return ''


def pending_set(user_id: str, text: str, token: str) -> None:
    with _cache_lock:
        pending[user_id] = text
    for row_num, row in _pending_rows(token):
        if row[0] == user_id:
            url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
                   f"/values/{config.PENDING_SHEET}!A{row_num}:B{row_num}?valueInputOption=RAW")
            resp = _http_retry('put', url, json={"values": [[user_id, text]]},
                               headers=_json_headers(token), timeout=15)
            if not resp.ok:
                logger.warning("[PND-01] pending_set put failed status=%s", resp.status_code)
            return
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
           f"/values/{config.PENDING_SHEET}!A:B:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
    resp = _http_retry('post', url, json={"values": [[user_id, text]]},
                       headers=_json_headers(token), timeout=15)
    if not resp.ok:
        logger.warning("[PND-01] pending_set append failed status=%s", resp.status_code)


def pending_del(user_id: str, token: str) -> None:
    with _cache_lock:
        pending.pop(user_id, None)
    for row_num, row in _pending_rows(token):
        if row[0] == user_id:
            url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
                   f"/values/{config.PENDING_SHEET}!A{row_num}:B{row_num}?valueInputOption=RAW")
            resp = _http_retry('put', url, json={"values": [['', '']]},
                               headers=_json_headers(token), timeout=15)
            if not resp.ok:
                logger.warning("[PND-02] pending_del failed status=%s", resp.status_code)
            return


# ══════════════════════════════════════════════════════════════════════════
# Member data
# ══════════════════════════════════════════════════════════════════════════

def get_all_members(token: str) -> list:
    now = time.time()
    with _cache_lock:
        if (_members_cache['data'] is not None
                and now - _members_cache['ts'] < config.MEMBERS_CACHE_TTL):
            return _members_cache['data']
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
           f"/values/メンバー!A2:E")
    resp = _http_retry('get', url, headers=_auth_headers(token), timeout=15)
    if not resp.ok:
        logger.error("[REG-01] get_all_members status=%s body=%s",
                     resp.status_code, resp.text[:200])
        with _cache_lock:
            return _members_cache['data'] if _members_cache['data'] is not None else []
    rows = resp.json().get('values', [])
    with _cache_lock:
        _members_cache['data'] = rows
        _members_cache['ts'] = time.time()
    return rows


def get_member(user_id: str, token: str) -> dict | None:
    for row in get_all_members(token):
        if len(row) > 2 and row[2] == user_id:
            raw = row[3] if len(row) > 3 else ''
            return {
                'name': row[0] if len(row) > 0 else '',
                'spreadsheet_id': extract_spreadsheet_id(raw),
            }
    return None


def is_duplicate(user_id: str, name: str, email: str, token: str) -> bool:
    for row in get_all_members(token):
        row_line_id = row[2] if len(row) > 2 else ''
        row_name    = row[0] if len(row) > 0 else ''
        row_email   = row[1] if len(row) > 1 else ''
        if (user_id and row_line_id == user_id) or (row_name == name and row_email == email):
            return True
    return False


def append_member(user_id: str, name: str, email: str, token: str,
                  spreadsheet_url: str = '') -> None:
    now = datetime.now().strftime('%Y/%m/%d %H:%M')
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
           f"/values/メンバー!A:E:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
    payload = {"values": [[_sanitize_cell(name), _sanitize_cell(email),
                           user_id, spreadsheet_url, now]]}
    resp = _http_retry('post', url, json=payload, headers=_json_headers(token), timeout=15)
    if not resp.ok:
        logger.error("[REG-02] append_member status=%s body=%s",
                     resp.status_code, resp.text[:500])
    resp.raise_for_status()
    with _cache_lock:
        _members_cache['data'] = None
        _members_cache['ts'] = 0.0


# ══════════════════════════════════════════════════════════════════════════
# User spreadsheet management
# ══════════════════════════════════════════════════════════════════════════

def create_user_spreadsheet(name: str, email: str, token: str) -> tuple:
    if not config.TEMPLATE_SPREADSHEET_ID:
        return None, None
    resp = _http_retry('post',
        f'https://www.googleapis.com/drive/v3/files/{config.TEMPLATE_SPREADSHEET_ID}/copy',
        json={'name': f'{name}さんの業務日誌',
              'mimeType': 'application/vnd.google-apps.spreadsheet'},
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        timeout=30,
    )
    resp.raise_for_status()
    file_id = resp.json().get('id')
    if not file_id:
        logger.error("[REG-05] Drive copy returned no file id body=%s", resp.text[:200])
        raise ValueError("Drive copy returned no file id")
    perm_url = f'https://www.googleapis.com/drive/v3/files/{file_id}/permissions'
    for share_email in filter(None, [config.ADMIN_EMAIL, email]):
        try:
            requests.post(perm_url,
                json={'type': 'user', 'role': 'writer', 'emailAddress': share_email},
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                timeout=15,
            )
        except Exception as e:
            logger.warning("[REG-04] share permission failed domain=%s: %s",
                           share_email.split('@')[-1], e)
    spreadsheet_url = f'https://docs.google.com/spreadsheets/d/{file_id}/edit'
    return file_id, spreadsheet_url


def get_template_sheet_id(spreadsheet_id: str, token: str) -> int | None:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    resp = _http_retry('get', url, headers=_auth_headers(token), timeout=15)
    if not resp.ok:
        logger.error("[REC-06] get_template_sheet_id failed id=%s... status=%s body=%s",
                     spreadsheet_id[:20], resp.status_code, resp.text[:500])
        return None
    for sheet in resp.json().get('sheets', []):
        if sheet['properties']['title'] == config.TEMPLATE_SHEET_NAME:
            return sheet['properties']['sheetId']
    logger.warning("[REC-07] template sheet not found in spreadsheet id=%s...",
                   spreadsheet_id[:20])
    return None


def copy_template(spreadsheet_id: str, template_id: int, new_title: str, token: str) -> bool:
    """シートをテンプレートからコピーする。True=新規作成、False=既存シートを上書き。"""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
    payload = {"requests": [{"duplicateSheet": {
        "sourceSheetId": template_id,
        "newSheetName": new_title,
        "insertSheetIndex": 1,
    }}]}
    resp = _http_retry('post', url, json=payload, headers=_json_headers(token), timeout=15)
    if resp.status_code == 400 and 'already exists' in resp.text:
        return False
    resp.raise_for_status()
    return True


def write_to_sheet(spreadsheet_id: str, sheet_title: str, name: str,
                   structured_text: str, month: int, day: int, token: str) -> int:
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
            m = re.match(r'((?:\d{2}:\d{2}|--:--)\s*[～~]\s*(?:\d{2}:\d{2}|--:--))\s+(.*)', item)
            if m:
                activities.append((m.group(1).strip(), m.group(2).strip()))
            else:
                activities.append(('', item))
        elif mode == 'notes' and line:
            notes += '\n' + line

    data = [
        {"range": _sheet_range(sheet_title, config.DATE_CELL),
         "values": [[f"令和{reiwa_year}年{month}月{day}日"]]},
        {"range": _sheet_range(sheet_title, config.NAME_CELL),
         "values": [[_sanitize_cell(name)]]},
    ]
    for i, (time_str, content) in enumerate(activities[:config.ACT_MAX_ROWS]):
        row = config.ACT_START_ROW + i
        data.append({"range": _sheet_range(sheet_title, f"A{row}"),
                     "values": [[_sanitize_cell(time_str)]]})
        data.append({"range": _sheet_range(sheet_title, f"B{row}"),
                     "values": [[_sanitize_cell(content)]]})
    overflow = activities[config.ACT_MAX_ROWS:]
    cell_parts = []
    if overflow:
        logger.info("[WRITE-01] activity overflow rows=%d, folding %d to notes",
                    len(activities), len(overflow))
        overflow_lines = '\n'.join(f"{t} {c}".strip() for t, c in overflow)
        cell_parts.append(f"【活動の続き】\n{overflow_lines}")
    if notes and notes != 'なし':
        cell_parts.append(f"【共有事項】\n{notes}")
    data.append({"range": _sheet_range(sheet_title, f"B{config.NOTES_ROW}"),
                 "values": [[_sanitize_cell('\n'.join(cell_parts)) if cell_parts else '']]})

    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
           f"/values:batchUpdate")
    resp = _http_retry('post', url, headers=_json_headers(token),
                       json={"valueInputOption": "RAW", "data": data}, timeout=15)
    resp.raise_for_status()
    return len(overflow)


def record_to_sheet(user_id: str, structured_text: str) -> tuple:
    """Returns (sheet_title, member_name, overflow_count, is_new_sheet). sheet_title is None on failure."""
    token = get_sheets_token()
    member = get_member(user_id, token)
    if not member or not member.get('spreadsheet_id'):
        return None, None, 0, False
    spreadsheet_id = member['spreadsheet_id']
    name = member['name']
    month, day = extract_date(structured_text)
    sheet_title = f"{month}月{day}日"
    template_id = get_template_sheet_id(spreadsheet_id, token)
    if template_id is None:
        return None, None, 0, False
    is_new = copy_template(spreadsheet_id, template_id, sheet_title, token)
    overflow = write_to_sheet(spreadsheet_id, sheet_title, name, structured_text, month, day, token)
    return sheet_title, name, overflow, is_new


# ══════════════════════════════════════════════════════════════════════════
# Photo upload
# ══════════════════════════════════════════════════════════════════════════

def upload_photo_to_drive(image_bytes: bytes, filename: str, token: str) -> str:
    boundary = os.urandom(16).hex().encode()  # ランダムboundaryで安全性向上
    metadata = json.dumps({'name': filename, 'mimeType': 'image/jpeg'}).encode()
    body = (
        b'--' + boundary + b'\r\n'
        b'Content-Type: application/json; charset=UTF-8\r\n\r\n' +
        metadata + b'\r\n'
        b'--' + boundary + b'\r\n'
        b'Content-Type: image/jpeg\r\n\r\n' +
        image_bytes + b'\r\n'
        b'--' + boundary + b'--'
    )
    resp = _http_retry('post',
        'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart',
        data=body,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': f'multipart/related; boundary={boundary.decode()}',
        },
        timeout=30,
    )
    if not resp.ok:
        logger.error("[PHO-02] upload_photo_to_drive status=%s body=%s",
                     resp.status_code, resp.text[:500])
    resp.raise_for_status()
    file_id = resp.json()['id']
    perm_resp = _http_retry('post',
        f'https://www.googleapis.com/drive/v3/files/{file_id}/permissions',
        json={'type': 'anyone', 'role': 'reader'},
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        timeout=15,
    )
    if not perm_resp.ok:
        logger.error("[PHO-05] set_public_permission failed file_id=%s status=%s",
                     file_id, perm_resp.status_code)
    return file_id


def write_photo_url_to_sheet(spreadsheet_id: str, sheet_title: str,
                             file_id: str, token: str) -> bool:
    image_url = f"https://drive.google.com/uc?export=view&id={file_id}"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate"
    resp = _http_retry('post', url, json={
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": _sheet_range(sheet_title, config.PHOTO_CELL),
                  "values": [[f'=IMAGE("{image_url}")']]}],
    }, headers=_json_headers(token), timeout=15)
    return resp.ok


# ══════════════════════════════════════════════════════════════════════════
# Slack notification
# ══════════════════════════════════════════════════════════════════════════

def send_to_slack(member_name: str, sheet_title: str, structured_text: str) -> None:
    if not config.SLACK_WEBHOOK_URL:
        return
    notes = extract_notes(structured_text)
    if not notes or notes == 'なし':
        return
    text = (
        f"📋 *{member_name}さんの日報（{sheet_title}）*\n\n"
        f"📣 共有事項：\n{notes}"
    )
    try:
        requests.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    except Exception as e:
        logger.warning("[SLK-01] send_to_slack: %s", e)


# ══════════════════════════════════════════════════════════════════════════
# Feedback
# ══════════════════════════════════════════════════════════════════════════

def _ensure_feedback_sheet(token: str) -> None:
    meta_url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
                f"?fields=sheets.properties.title")
    resp = _http_retry('get', meta_url, headers=_auth_headers(token), timeout=15)
    resp.raise_for_status()
    titles = [s['properties']['title'] for s in resp.json().get('sheets', [])]
    if 'フィードバック' in titles:
        return

    batch_url = (f"https://sheets.googleapis.com/v4/spreadsheets"
                 f"/{config.MASTER_SPREADSHEET_ID}:batchUpdate")
    _http_retry('post', batch_url,
        json={"requests": [{"addSheet": {"properties": {"title": "フィードバック"}}}]},
        headers=_json_headers(token), timeout=15,
    ).raise_for_status()

    header_url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
                  f"/values/フィードバック!A1:append?valueInputOption=RAW")
    _http_retry('post', header_url,
        json={"values": [["日時", "名前", "カテゴリ", "内容"]]},
        headers=_json_headers(token), timeout=15,
    ).raise_for_status()


def save_feedback(user_id: str, category: str, message: str, token: str) -> None:
    _ensure_feedback_sheet(token)
    member = get_member(user_id, token)
    name = member['name'] if member else '不明'
    now = datetime.now().strftime('%Y/%m/%d %H:%M')
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{config.MASTER_SPREADSHEET_ID}"
           f"/values/フィードバック!A:D:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
    payload = {"values": [[now, _sanitize_cell(name),
                           _sanitize_cell(category), _sanitize_cell(message)]]}
    resp = _http_retry('post', url, json=payload, headers=_json_headers(token), timeout=15)
    resp.raise_for_status()

    if config.SLACK_WEBHOOK_URL and category == '管理者への連絡':
        try:
            requests.post(config.SLACK_WEBHOOK_URL, json={
                "text": (
                    f"📞 *管理者への連絡が届きました*\n\n"
                    f"送信者：{name}\n"
                    f"内容：{message}\n\n"
                    f"日時：{now}"
                )
            }, timeout=10)
        except Exception as e:
            logger.warning("[FB-02] slack admin notify failed: %s", e)


# ══════════════════════════════════════════════════════════════════════════
# Weekly summary helpers
# ══════════════════════════════════════════════════════════════════════════

def get_spreadsheet_sheet_titles(spreadsheet_id: str, token: str) -> list[str]:
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
           f"?fields=sheets.properties.title")
    resp = _http_retry('get', url, headers=_auth_headers(token), timeout=15)
    if not resp.ok:
        logger.warning("[SUMMARY] get sheet titles failed id=%s status=%s",
                       spreadsheet_id[:8], resp.status_code)
        return []
    return [s['properties']['title'] for s in resp.json().get('sheets', [])]


def read_day_activities(spreadsheet_id: str, sheet_title: str, token: str) -> str:
    end_row = config.NOTES_ROW
    range_str = _sheet_range(sheet_title, f"A{config.ACT_START_ROW}:B{end_row}")
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
           f"/values/{range_str}")
    resp = _http_retry('get', url, headers=_auth_headers(token), timeout=15)
    if not resp.ok:
        return ''
    values = resp.json().get('values', [])
    act_rows = values[:config.ACT_MAX_ROWS]
    lines = []
    for row in act_rows:
        time_str = row[0].strip() if len(row) > 0 else ''
        content  = row[1].strip() if len(row) > 1 else ''
        if content:
            prefix = f"・{time_str} " if time_str else "・"
            lines.append(prefix + content)
    if len(values) > config.ACT_MAX_ROWS:
        notes_row = values[config.ACT_MAX_ROWS]
        notes_cell = notes_row[1].strip() if len(notes_row) > 1 else ''
        if notes_cell:
            lines.append(f"\n📣 {notes_cell}")
    return '\n'.join(lines)
