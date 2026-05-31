"""Centralised configuration — all env-var reads happen here.
To change a setting, only this file needs to be updated.
"""
import os
from datetime import timezone, timedelta

# ── Required ──────────────────────────────────────────────────────────────
LINE_CHANNEL_SECRET       = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
GEMINI_API_KEY            = os.environ['GEMINI_API_KEY']
MASTER_SPREADSHEET_ID     = os.environ['MASTER_SPREADSHEET_ID']

# ── Optional ──────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL       = os.environ.get('SLACK_WEBHOOK_URL', '')
RICHMENU_REGISTERED     = os.environ.get('RICHMENU_REGISTERED', '')
RICHMENU_UNREGISTERED   = os.environ.get('RICHMENU_UNREGISTERED', '')
TEMPLATE_SPREADSHEET_ID = os.environ.get('TEMPLATE_SPREADSHEET_ID', '')
ADMIN_EMAIL             = os.environ.get('ADMIN_EMAIL', '')
GEMINI_MODEL            = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
SCHEDULER_SECRET        = os.environ.get('SCHEDULER_SECRET', '').strip()

# ── File size limits ───────────────────────────────────────────────────────
MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

# ── Timezone ───────────────────────────────────────────────────────────────
JST = timezone(timedelta(hours=9))

# ── URL constants ──────────────────────────────────────────────────────────
LIFF_URL  = 'https://liff.line.me/2009693703-ONMSHAXr'
GUIDE_URL = 'https://yuki-lm92.github.io/nichishi-register/guide.html'

# ── Sheet names ────────────────────────────────────────────────────────────
PENDING_SHEET       = 'pending_states'
SESSION_SHEET       = 'session_states'
TEMPLATE_SHEET_NAME = '●月●日（テンプレート）'

# ── Sheet layout ───────────────────────────────────────────────────────────
DATE_CELL     = 'A3'
NAME_CELL     = 'B6'
ACT_START_ROW = 10
ACT_MAX_ROWS  = 7
NOTES_ROW     = 17
PHOTO_CELL    = 'F2'

# ── Timing ─────────────────────────────────────────────────────────────────
SESSION_TTL       = 30 * 60  # seconds
MEMBERS_CACHE_TTL = 60        # seconds

# ── CORS ───────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS = frozenset({'https://liff.line.me'})

# ── User-facing messages ───────────────────────────────────────────────────
WELCOME_MESSAGE = """\
📓 業務日誌サービスへようこそ！

このアカウントでできること：

🎤 音声を送るだけで日誌が完成
📝 テキストでの入力にも対応
📋 AIが話した内容を自動で整理
📊 スプレッドシートに自動書き込み

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
