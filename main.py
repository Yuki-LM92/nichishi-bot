import os
import re
import json
import base64
import tempfile
import requests
from datetime import datetime
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
    TextMessageContent, FollowEvent
)
import google.auth
import google.auth.transport.requests

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
GEMINI_API_KEY = os.environ['GEMINI_API_KEY']
MASTER_SPREADSHEET_ID = os.environ['MASTER_SPREADSHEET_ID']
SLACK_WEBHOOK_URL           = os.environ.get('SLACK_WEBHOOK_URL', '')
RICHMENU_REGISTERED         = os.environ.get('RICHMENU_REGISTERED', '')
RICHMENU_UNREGISTERED       = os.environ.get('RICHMENU_UNREGISTERED', '')
TEMPLATE_SPREADSHEET_ID     = os.environ.get('TEMPLATE_SPREADSHEET_ID', '')
ADMIN_EMAIL                 = os.environ.get('ADMIN_EMAIL', '')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 確認待ちの内容を一時保存 (user_id → structured text)
pending = {}
# 修正モード：「修正する」を押した後にテキスト/音声を待っているユーザー
pending_correction = set()
# フィードバック収集中 (user_id → {'category': str})
pending_feedback = {}
# 処理中にキャンセルを要求したユーザー
pending_cancel = set()

TEMPLATE_SHEET_NAME = '●月●日（テンプレート）'
LIFF_URL = 'https://liff.line.me/2009693703-ONMSHAXr'

PROMPT = """
あなたは地域おこし協力隊の業務日報の記録係です。
送られてきた音声は、協力隊員が今日の業務を振り返って話したものです。

以下のフォーマットだけで出力してください（余計な説明は不要）：

📅 日付：（言及があれば。なければ空欄）
⏰ 活動内容：
・[時間帯があれば] 活動内容
・[時間帯があれば] 活動内容
（時間の言及がない場合はそのまま箇条書き）
📣 共有事項：（上司や仲間にSlackで伝えたいことがあれば記載。なければ「なし」）

音声に含まれる情報だけを使い、推測で補わないでください。
"""

TEXT_PROMPT = """
あなたは地域おこし協力隊の業務日報の記録係です。
以下のテキストは、協力隊員が今日の業務内容を伝えたものです。

以下のフォーマットだけで出力してください（余計な説明は不要）：

📅 日付：（言及があれば。なければ空欄）
⏰ 活動内容：
・[時間帯があれば] 活動内容
・[時間帯があれば] 活動内容
（時間の言及がない場合はそのまま箇条書き）
📣 共有事項：（上司や仲間にSlackで伝えたいことがあれば記載。なければ「なし」）

テキストに含まれる情報だけを使い、推測で補わないでください。

テキスト：
"""

CORRECTION_PROMPT = """
あなたは地域おこし協力隊の業務日報の記録係です。
以下の「現在の日報」に対して「修正指示」を適用し、修正後の日報を出力してください。

ルール：
- 修正指示に含まれる変更のみを反映し、それ以外の内容はそのまま維持する
- 以下のフォーマットだけで出力すること（余計な説明は不要）：

📅 日付：（言及があれば。なければ空欄）
⏰ 活動内容：
・[時間帯があれば] 活動内容
・[時間帯があれば] 活動内容
（時間の言及がない場合はそのまま箇条書き）
📣 共有事項：（上司や仲間にSlackで伝えたいことがあれば記載。なければ「なし」）

現在の日報：
{original}

修正指示：
{correction}
"""

# ========== Sheets API ==========

def get_sheets_token():
    creds, _ = google.auth.default(
        scopes=[
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive',
        ]
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token

def get_all_members(token):
    """メンバーシートの全行を取得する"""
    # 列順: A=名前, B=email, C=LINE_ID, D=spreadsheet_id, E=更新日付
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}/values/メンバー!A2:E"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json().get('values', [])

def get_member(user_id, token):
    """LINE_IDでメンバー情報を取得。未登録ならNoneを返す。"""
    for row in get_all_members(token):
        if len(row) > 2 and row[2] == user_id:
            return {
                'name': row[0] if len(row) > 0 else '',
                'spreadsheet_id': row[3] if len(row) > 3 else ''
            }
    return None

def is_duplicate(user_id, name, email, token):
    """LINE_ID・名前・メールアドレスのいずれかが一致すれば重複とみなす"""
    for row in get_all_members(token):
        row_line_id = row[2] if len(row) > 2 else ''
        row_name    = row[0] if len(row) > 0 else ''
        row_email   = row[1] if len(row) > 1 else ''
        if (user_id and row_line_id == user_id) or \
           (row_name == name and row_email == email):
            return True
    return False

def append_member(user_id, name, email, token, spreadsheet_url=''):
    # 列順: A=名前, B=email, C=LINE_ID, D=スプレッドシート編集用URL, E=更新日付
    now = datetime.now().strftime('%Y/%m/%d %H:%M')
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
        f"/values/メンバー!A:E:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    payload = {"values": [[name, email, user_id, spreadsheet_url, now]]}
    resp = requests.post(url, json=payload, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    })
    resp.raise_for_status()

def create_user_spreadsheet(name, email, token):
    """テンプレートをコピーしてユーザー専用スプレッドシートを自動生成する"""
    if not TEMPLATE_SPREADSHEET_ID:
        return None, None

    # テンプレートをコピー
    resp = requests.post(
        f'https://www.googleapis.com/drive/v3/files/{TEMPLATE_SPREADSHEET_ID}/copy',
        json={'name': f'{name}さんの業務日誌'},
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        timeout=30
    )
    resp.raise_for_status()
    file_id = resp.json()['id']

    # 管理者・ユーザーに共有
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

def get_template_sheet_id(spreadsheet_id, token):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?fields=sheets.properties"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    for sheet in resp.json().get('sheets', []):
        if sheet['properties']['title'] == TEMPLATE_SHEET_NAME:
            return sheet['properties']['sheetId']
    return None

def copy_template(spreadsheet_id, template_id, new_title, token):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
    payload = {"requests": [{"duplicateSheet": {
        "sourceSheetId": template_id,
        "newSheetName": new_title,
        "insertSheetIndex": 1
    }}]}
    resp = requests.post(url, json=payload, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    })
    if resp.status_code == 400 and 'already exists' in resp.text:
        return  # 今日のシートはすでにある
    resp.raise_for_status()

def write_to_sheet(spreadsheet_id, sheet_title, name, structured_text, token):
    today = datetime.now()
    reiwa_year = today.year - 2018

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
        {"range": f"'{sheet_title}'!C3",
         "values": [[f"令和{reiwa_year}年{today.month}月{today.day}日"]]},
        {"range": f"'{sheet_title}'!C6",
         "values": [[name]]},
    ]
    for i, (time_str, content) in enumerate(activities[:7]):
        row = 10 + i
        data.append({"range": f"'{sheet_title}'!A{row}", "values": [[time_str]]})
        data.append({"range": f"'{sheet_title}'!C{row}", "values": [[content]]})
    if notes and notes != 'なし':
        data.append({"range": f"'{sheet_title}'!B17", "values": [[notes]]})

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate"
    resp = requests.post(url, json={"valueInputOption": "USER_ENTERED", "data": data},
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    resp.raise_for_status()

def record_to_sheet(user_id, structured_text):
    """記録成功時は (sheet_title, member_name) を返す。失敗時は (None, None) を返す。"""
    token = get_sheets_token()
    member = get_member(user_id, token)
    if not member or not member.get('spreadsheet_id'):
        return None, None
    spreadsheet_id = member['spreadsheet_id']
    name = member['name']
    today = datetime.now()
    sheet_title = f"{today.month}月{today.day}日"

    template_id = get_template_sheet_id(spreadsheet_id, token)
    if template_id is None:
        return None, None
    copy_template(spreadsheet_id, template_id, sheet_title, token)
    write_to_sheet(spreadsheet_id, sheet_title, name, structured_text, token)
    return sheet_title, name

# ========== Slack ==========

def extract_notes(structured_text):
    """構造化テキストから共有事項を抽出する。"""
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

def send_to_slack(member_name, sheet_title, structured_text):
    """共有事項をSlackに送信する。SLACK_WEBHOOK_URLが未設定なら何もしない。"""
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
        pass  # Slack通知の失敗は日報記録に影響させない

# ========== Rich Menu ==========

def link_rich_menu(user_id, menu_id):
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

def save_feedback(user_id, category, message, token):
    """フィードバックをマスタースプシの「フィードバック」シートに記録"""
    member = get_member(user_id, token)
    name = member['name'] if member else '不明'
    now = datetime.now().strftime('%Y/%m/%d %H:%M')
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}"
        f"/values/フィードバック!A:D:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    payload = {"values": [[now, name, category, message]]}
    resp = requests.post(url, json=payload, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    })
    resp.raise_for_status()

# ========== Gemini ==========

def call_gemini_audio(audio_b64):
    """音声データをGeminiに送り、構造化テキストを返す。失敗時は例外を投げる。"""
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

def call_gemini_text(text):
    """テキストをGeminiで日報フォーマットに変換する。失敗時は例外を投げる。"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [
        {"text": TEXT_PROMPT + text}
    ]}]}
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    candidates = resp.json().get('candidates', [])
    if not candidates:
        raise ValueError("Gemini returned empty candidates")
    result = candidates[0]['content']['parts'][0]['text'].strip()
    if not result:
        raise ValueError("Gemini returned empty text")
    return result

def call_gemini_correction(original, correction):
    """元の日報と修正指示をGeminiに送り、修正済み日報を返す。失敗時は例外を投げる。"""
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

# ========== LINE helpers ==========

def send_confirm(reply_token, structured_text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[
                    TextMessage(
                        text=f"📋 以下の内容で記録しますね。確認してください。\n\n{structured_text}",
                        quick_reply=QuickReply(items=[
                            QuickReplyItem(action=PostbackAction(label='✅ はい', data='confirm_yes')),
                            QuickReplyItem(action=PostbackAction(label='✏️ 修正する', data='confirm_no')),
                            QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='confirm_cancel')),
                        ])
                    )
                ]
            )
        )

def reply_text(reply_token, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

def push_text(user_id, text):
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
    "マイクボタンを長押しして話してみてください！"
)

# ========== Flask routes ==========

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

        if not name or not email:
            return cors_response({'error': 'missing fields'}, 400)

        token = get_sheets_token()

        # 二重登録チェック（LINE_ID・名前・メールのいずれかが一致）
        if is_duplicate(line_user_id, name, email, token):
            return cors_response({'status': 'already_registered'})

        # スプレッドシートを自動生成
        spreadsheet_url = ''
        try:
            _, spreadsheet_url = create_user_spreadsheet(name, email, token)
        except Exception:
            pass  # 失敗しても登録は続行

        # マスタースプシに記録（URLも同時に保存）
        append_member(line_user_id, name, email, token, spreadsheet_url)

        # Slackに登録通知を送信
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

        # 登録完了メッセージ＋ガイドURL送信＋リッチメニュー切り替え
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

    except Exception as e:
        import traceback
        print(f"[register error] {traceback.format_exc()}")
        return cors_response({'error': str(e)}, 500)

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
        # 登録済みユーザーが再フォロー → 登録済みメニューを再リンク
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

    # 処理中キャンセル
    if text == 'キャンセル':
        pending_cancel.add(user_id)
        pending.pop(user_id, None)
        pending_correction.discard(user_id)
        pending_feedback.pop(user_id, None)
        reply_text(event.reply_token, "⛔ キャンセルしました。\n処理中の場合も完了後に破棄します。")
        return

    # フィードバック収集モード
    if user_id in pending_feedback:
        category = pending_feedback.pop(user_id)['category']
        reply_text(event.reply_token, "⏳ 送信中です...")
        try:
            token = get_sheets_token()
            save_feedback(user_id, category, text, token)
            push_text(user_id, "✅ ありがとうございます！内容を受け付けました🙏\n確認次第ご連絡します。")
        except Exception:
            push_text(user_id, "⚠️ 送信中にエラーが発生しました。\nしばらくしてからお試しください。")
        return

    # 修正モード：「修正する」を押してテキストを送ってきた場合
    if user_id in pending_correction:
        pending_correction.discard(user_id)
        reply_text(event.reply_token, "✏️ 修正内容を受け取りました！\nAIが整理しています...")
        try:
            original = pending.get(user_id, '')
            structured = call_gemini_correction(original, text)
            pending[user_id] = structured
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).push_message(
                    push_message_request=PushMessageRequest(
                        to=user_id,
                        messages=[
                            TextMessage(
                                text=f"📋 以下の内容で記録しますね。確認してください。\n\n{structured}",
                                quick_reply=QuickReply(items=[
                                    QuickReplyItem(action=PostbackAction(label='✅ はい', data='confirm_yes')),
                                    QuickReplyItem(action=PostbackAction(label='✏️ 修正する', data='confirm_no')),
                                    QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='confirm_cancel')),
                                ])
                            )
                        ]
                    )
                )
        except requests.exceptions.Timeout:
            push_text(user_id, "⏱️ AIの処理に時間がかかっています。\nもう一度送ってください。")
        except Exception:
            push_text(user_id, "⚠️ 処理中にエラーが発生しました。\nもう一度送ってください。")
        return

    # 登録状況を確認
    token = get_sheets_token()
    member = get_member(user_id, token)

    if member is None:
        reply_text(event.reply_token, NOT_REGISTERED_MESSAGE)
        return

    if not member.get('spreadsheet_id'):
        reply_text(event.reply_token, WAITING_SHEET_MESSAGE)
        return

    reply_text(event.reply_token, AUDIO_GUIDE_MESSAGE)

@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio(event):
    user_id = event.source.user_id

    # 登録状況を確認
    token = get_sheets_token()
    member = get_member(user_id, token)

    if member is None:
        reply_text(event.reply_token, NOT_REGISTERED_MESSAGE)
        return

    if not member.get('spreadsheet_id'):
        reply_text(event.reply_token, WAITING_SHEET_MESSAGE)
        return

    # 修正モードを解除（音声で修正する場合も通常処理へ）
    pending_correction.discard(user_id)

    # すぐに受付メッセージを返信（reply_tokenはここで使い切る）
    reply_text(
        event.reply_token,
        "🎙️ 音声を受け取りました！\nAIが内容を整理しています...\n（10〜30秒ほどかかります）"
    )

    # 音声データを取得
    with ApiClient(configuration) as api_client:
        audio_bytes = MessagingApiBlob(api_client).get_message_content(event.message.id)

    with tempfile.NamedTemporaryFile(suffix='.m4a', delete=False) as f:
        f.write(audio_bytes)
        audio_path = f.name

    try:
        with open(audio_path, 'rb') as f:
            audio_b64 = base64.b64encode(f.read()).decode()

        try:
            structured = call_gemini_audio(audio_b64)
        except requests.exceptions.Timeout:
            push_text(user_id, "⏱️ AIの処理に時間がかかっています。\nしばらくしてからもう一度送ってください。")
            return
        except Exception:
            push_text(user_id, "⚠️ 音声の解析に失敗しました。\n少し長めに話して、もう一度送ってください。\n（目安：30秒以上）")
            return

        # キャンセルされていたら破棄
        if user_id in pending_cancel:
            pending_cancel.discard(user_id)
            push_text(user_id, "⛔ キャンセル済みのため、記録しませんでした。")
            return

        pending[user_id] = structured

        # 結果はpush_messageで送る（reply_tokenは使用済みのため）
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                push_message_request=PushMessageRequest(
                    to=user_id,
                    messages=[
                        TextMessage(
                            text=f"📋 以下の内容で記録しますね。確認してください。\n\n{structured}",
                            quick_reply=QuickReply(items=[
                                QuickReplyItem(action=PostbackAction(label='✅ はい', data='confirm_yes')),
                                QuickReplyItem(action=PostbackAction(label='✏️ 修正する', data='confirm_no')),
                                QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='confirm_cancel')),
                            ])
                        )
                    ]
                )
            )

    finally:
        os.unlink(audio_path)

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data

    if data == 'confirm_yes':
        structured = pending.get(user_id, '')
        if not structured:
            reply_text(event.reply_token, "⚠️ 記録する内容が見つかりませんでした。\nもう一度音声を送ってください。")
            return
        try:
            sheet_name, member_name = record_to_sheet(user_id, structured)
            if sheet_name:
                msg = f"✅ {sheet_name}の日報をスプレッドシートに記録しました！"
                send_to_slack(member_name, sheet_name, structured)
            else:
                msg = "⚠️ スプレッドシートへの記録に失敗しました。\n管理者にお問い合わせください。"
        except Exception as e:
            msg = f"⚠️ 記録中にエラーが発生しました。\nしばらくしてから再試行するか、管理者にお問い合わせください。"
        reply_text(event.reply_token, msg)
        pending.pop(user_id, None)

    elif data == 'confirm_no':
        pending_correction.add(user_id)
        reply_text(
            event.reply_token,
            "修正内容をテキストで送ってください。\n"
            "例）「午後の作業時間を3時間に変えて」\n"
            "　　「日付を4月10日に変えて」"
        )

    elif data == 'confirm_cancel':
        pending.pop(user_id, None)
        pending_correction.discard(user_id)
        reply_text(event.reply_token, "キャンセルしました。\n記録は行われていません。")

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
        pending_feedback[user_id] = {'category': 'フィードバック・改善要望'}
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
        pending_feedback[user_id] = {'category': '管理者への連絡'}
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
        pending_feedback.pop(user_id, None)
        reply_text(event.reply_token, "キャンセルしました。")

    elif data == 'open_spreadsheet':
        try:
            token = get_sheets_token()
            member = get_member(user_id, token)
            if member and member.get('spreadsheet_id'):
                reply_text(
                    event.reply_token,
                    f"📊 スプレッドシートはこちらです：\n{member['spreadsheet_id']}"
                )
            else:
                reply_text(
                    event.reply_token,
                    "⏳ スプレッドシートはまだ準備中です。\n\n"
                    "管理者が承認・設定するまで1〜3日かかる場合があります。\n"
                    "準備ができたらご連絡しますので、もう少しお待ちください🙏"
                )
        except Exception:
            reply_text(event.reply_token, "⚠️ 確認中にエラーが発生しました。\nしばらくしてからお試しください。")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
