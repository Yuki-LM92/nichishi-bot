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
    ReplyMessageRequest, TextMessage,
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

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 確認待ちの内容を一時保存
pending = {}
# 名前入力待ちのユーザー
waiting_for_name = set()

TEMPLATE_SHEET_NAME = '●月●日（テンプレート）'

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

# ========== Sheets API ==========

def append_member(user_id, name, token):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}/values/メンバー!A:C:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    payload = {"values": [[user_id, name, '']]}
    resp = requests.post(url, json=payload, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    })
    resp.raise_for_status()

def get_member(user_id, token):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SPREADSHEET_ID}/values/メンバー!A2:C"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    for row in resp.json().get('values', []):
        if len(row) >= 3 and row[0] == user_id:
            return {'name': row[1], 'spreadsheet_id': row[2]}
    return None

def get_sheets_token():
    creds, _ = google.auth.default(
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token

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

    # 構造化テキストをパース
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
            # [時間帯] 内容 を分割
            m = re.match(r'\[(.+?)\]\s*(.*)', item)
            if m:
                activities.append((m.group(1), m.group(2)))
            else:
                activities.append(('', item))
        elif mode == 'notes' and line:
            notes += '\n' + line

    # セルデータを構築
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
    token = get_sheets_token()
    member = get_member(user_id, token)
    if not member:
        return False
    spreadsheet_id = member['spreadsheet_id']
    name = member['name']
    today = datetime.now()
    sheet_title = f"{today.month}月{today.day}日"

    template_id = get_template_sheet_id(spreadsheet_id, token)
    if template_id is None:
        return False
    copy_template(spreadsheet_id, template_id, sheet_title, token)
    write_to_sheet(spreadsheet_id, sheet_title, name, structured_text, token)
    return True

# ========== LINE handlers ==========

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
                        ])
                    )
                ]
            )
        )

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

WELCOME_MESSAGE = """\
━━━━━━━━━━━━━━━━━━━
🎙️ 音声日報サービスへようこそ！
━━━━━━━━━━━━━━━━━━━

このアカウントでできること：

🎤 毎日の業務を「音声を送るだけ」で日報が完成
📋 話した内容をAIが自動で整理・記録
📊 スプレッドシートに自動で書き込み
💬 チームへの共有も同時に行えます

手入力は一切不要。帰り道や移動中に
今日の業務をひとこと話すだけでOKです。

━━━━━━━━━━━━━━━━━━━
ご利用にはひとつだけ登録が必要です。

👇 フルネームをこのトーク画面に
　　入力して送信してください。

例）桂太郎
━━━━━━━━━━━━━━━━━━━\
"""

def looks_like_name(text):
    if len(text) > 12 or len(text) < 2:
        return False
    question_words = ['？', '?', '何', 'どう', 'いつ', 'どこ', 'なぜ', 'どれ', 'どの', 'http', 'できる', 'ですか', 'ますか']
    return not any(w in text for w in question_words)

@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    waiting_for_name.add(user_id)
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=WELCOME_MESSAGE)]
            )
        )

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    # 登録状況をスプシで確認（サーバー再起動後のリカバリー）
    if user_id not in waiting_for_name:
        token = get_sheets_token()
        member = get_member(user_id, token)
        if member is None:
            # 未登録 → 名前入力待ちに追加してウェルカムを再送
            waiting_for_name.add(user_id)
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=WELCOME_MESSAGE)]
                    )
                )
            return
        elif not member.get('spreadsheet_id'):
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="担当者がスプレッドシートを準備中です。もうしばらくお待ちください🙏")]
                    )
                )
            return
        else:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="🎙️ 音声メッセージを送ると日報を記録できます。\nマイクボタンを長押しして話してみてください！")]
                    )
                )
            return

    if not looks_like_name(text):
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text=(
                            "フルネームの入力をお願いします🙏\n\n"
                            "例）桂太郎\n\n"
                            "ご不明な点は担当者までお問い合わせください。"
                        )
                    )]
                )
            )
        return

    waiting_for_name.discard(user_id)
    token = get_sheets_token()
    append_member(user_id, text, token)

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(
                    text=(
                        f"✅ 登録完了しました！\n\n"
                        f"お名前：{text}\n\n"
                        "担当者がスプレッドシートを準備してご連絡します。\n"
                        "ご連絡後すぐに音声日報をご利用いただけます。\n\n"
                        "もうしばらくお待ちください。"
                    )
                )]
            )
        )

@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio(event):
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        audio_bytes = MessagingApiBlob(api_client).get_message_content(event.message.id)

    with tempfile.NamedTemporaryFile(suffix='.m4a', delete=False) as f:
        f.write(audio_bytes)
        audio_path = f.name

    try:
        with open(audio_path, 'rb') as f:
            audio_b64 = base64.b64encode(f.read()).decode()

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [
            {"inline_data": {"mime_type": "audio/mp4", "data": audio_b64}},
            {"text": PROMPT}
        ]}]}
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        structured = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()

        pending[user_id] = structured
        send_confirm(event.reply_token, structured)

    finally:
        os.unlink(audio_path)

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data

    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)

        if data == 'confirm_yes':
            structured = pending.get(user_id, '')
            success = record_to_sheet(user_id, structured)
            msg = '✅ スプレッドシートに記録しました！' if success else '✅ 確認しました。\n（スプレッドシート未登録のためスキップ）'
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            ))
            pending.pop(user_id, None)

        elif data == 'confirm_no':
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text='修正内容を音声またはテキストで送ってください。')]
            ))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
