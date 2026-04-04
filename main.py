import os
import tempfile
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage,
    QuickReply, QuickReplyItem, PostbackAction
)
from linebot.v3.webhooks import (
    MessageEvent, AudioMessageContent, PostbackEvent, TextMessageContent
)
from google import genai
from google.genai import types

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
GEMINI_API_KEY = os.environ['GEMINI_API_KEY']

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 確認待ちの内容を一時保存
pending = {}

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

@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio(event):
    user_id = event.source.user_id

    # 音声をダウンロード
    with ApiClient(configuration) as api_client:
        audio_bytes = MessagingApiBlob(api_client).get_message_content(event.message.id)

    with tempfile.NamedTemporaryFile(suffix='.m4a', delete=False) as f:
        f.write(audio_bytes)
        audio_path = f.name

    try:
        # Gemini Files APIにアップロードして文字起こし＋構造化
        with open(audio_path, 'rb') as f:
            audio_file = gemini_client.files.upload(
                file=f,
                config=types.UploadFileConfig(mime_type='audio/mp4')
            )
        response = gemini_client.models.generate_content(
            model='gemini-1.5-pro',
            contents=[audio_file, PROMPT]
        )
        gemini_client.files.delete(name=audio_file.name)

        structured = response.text.strip()
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
            # TODO: Googleスプレッドシート記録 + Slack投稿（次のステップで実装）
            api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text='✅ 記録しました！\n（スプレッドシート・Slack連携は準備中です）')]
                )
            )
            pending.pop(user_id, None)

        elif data == 'confirm_no':
            api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text='修正内容を音声またはテキストで送ってください。')]
                )
            )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
