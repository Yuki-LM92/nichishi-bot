"""Flask application — event handlers and routes.
Storage / AI / LINE API concerns are delegated to their respective modules:
  config.py    — environment variables and constants
  storage.py   — Google Sheets / Drive persistence
  ai.py        — Gemini API calls
  messaging.py — LINE Messaging API (all functions are error-safe)
  utils.py     — pure utility functions
"""
import hmac
import os
import re
import base64
import calendar
import threading
import logging
import concurrent.futures
import requests
from datetime import datetime, timedelta

from flask import Flask, request, abort
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    MessageEvent, AudioMessageContent, PostbackEvent,
    TextMessageContent, FollowEvent, ImageMessageContent,
    StickerMessageContent, VideoMessageContent,
    FileMessageContent, LocationMessageContent,
)
from linebot.v3.messaging import TextMessage, QuickReply, QuickReplyItem, PostbackAction

import config
from storage import (
    get_sheets_token, ensure_session_sheet,
    session_get, session_set, session_del,
    pending_get, pending_set, pending_del,
    get_member, is_duplicate, append_member,
    create_user_spreadsheet, record_to_sheet,
    upload_photo_to_drive, write_photo_url_to_sheet,
    send_to_slack, save_feedback,
    get_spreadsheet_sheet_titles, read_day_activities,
    get_all_members,
    pending, _session_cache, pending_cancel,
    _cancel_lock, _cache_lock, _SAFE_FILE_ID_RE,
)
from ai import (
    call_gemini_audio, call_gemini_text,
    call_gemini_correction, call_gemini_summary,
)
from messaging import (
    handler, reply_text, push_text, push_confirm,
    push_message_object, link_rich_menu, get_message_content,
)
from utils import is_valid_email, _is_emoji_only, _get_week_dates, extract_spreadsheet_id

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
logger = logging.getLogger(__name__)

_setup_done: bool = False
_setup_lock = threading.Lock()
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix='nichi')

_DAY_JP = {0: '月', 1: '火', 2: '水', 3: '木', 4: '金'}


# ══════════════════════════════════════════════════════════════════════════
# Async processing (background threads)
# ══════════════════════════════════════════════════════════════════════════

def _process_input_async(user_id: str, content, is_audio: bool) -> None:
    try:
        structured = call_gemini_audio(content) if is_audio else call_gemini_text(content)
    except Exception as e:
        logger.error("[REC-01] gemini error (audio=%s): %s", is_audio, e)
        msg = ("⚠️ 音声の解析に失敗しました（REC-01）。\n少し長めに話して、もう一度送ってください。\n（目安：30秒以上）"
               if is_audio else
               "⚠️ テキストの解析に失敗しました（REC-01）。\nもう一度送ってください。")
        push_text(user_id, msg)
        return

    with _cancel_lock:
        if user_id in pending_cancel:
            pending_cancel.discard(user_id)
            push_text(user_id, "⛔ キャンセル済みのため、記録しませんでした。")
            return

    try:
        token = get_sheets_token()
        pending_set(user_id, structured, token)
    except Exception as e:
        logger.warning("[REC-05] pending_set failed, using in-memory fallback: %s", e)
        with _cache_lock:
            pending[user_id] = structured

    push_confirm(user_id, structured)


def _process_feedback_async(user_id: str, category: str, message: str) -> None:
    try:
        token = get_sheets_token()
        save_feedback(user_id, category, message, token)
        push_text(user_id, "✅ ありがとうございます！内容を受け付けました🙏\n確認次第ご連絡します。")
    except Exception as e:
        logger.error("[FB-01] save_feedback error: %s", e)
        push_text(user_id, "⚠️ 送信中にエラーが発生しました（FB-01）。\nしばらくしてからお試しください。")


def _process_correction_async(user_id: str, original: str, correction_text: str) -> None:
    try:
        structured = call_gemini_correction(original, correction_text)
    except Exception as e:
        logger.error("[REC-04] correction gemini error: %s", e)
        push_text(user_id, "⚠️ 処理中にエラーが発生しました。\nもう一度送ってください。")
        return

    try:
        token = get_sheets_token()
        pending_set(user_id, structured, token)
    except Exception as e:
        logger.warning("[REC-05] pending_set failed, using in-memory fallback: %s", e)
        with _cache_lock:
            pending[user_id] = structured

    push_confirm(user_id, structured)


def _process_image_async(user_id: str, image_bytes: bytes, filename: str,
                         month: int, day: int, token: str) -> None:
    try:
        file_id = upload_photo_to_drive(image_bytes, filename, token)
        session_set(user_id, 'photo_pending',
                    {'file_id': file_id, 'month': month, 'day': day}, token)
        push_message_object(user_id, TextMessage(
            text=f"📸 アップロード完了！\n{month}月{day}日の活動写真として追加しますか？",
            quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='✅ 追加する', data='add_photo')),
                QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='cancel_photo')),
            ])
        ))
    except Exception as e:
        logger.error("[PHO-02] upload error: %s", e)
        push_text(user_id, "⚠️ 写真のアップロードに失敗しました（PHO-02）。\nしばらくしてからお試しください。")


# ══════════════════════════════════════════════════════════════════════════
# Weekly summary
# ══════════════════════════════════════════════════════════════════════════

def _process_weekly_summary_for_member(row: list, week_dates: list, token: str) -> None:
    name           = row[0] if len(row) > 0 else ''
    line_id        = row[2] if len(row) > 2 else ''
    spreadsheet_id = extract_spreadsheet_id(row[3] if len(row) > 3 else '')

    if not line_id or not spreadsheet_id:
        return

    try:
        titles    = set(get_spreadsheet_sheet_titles(spreadsheet_id, token))
        recorded  = []
        missing   = []
        day_texts = []

        for d in week_dates:
            sheet_title = f"{d.month}月{d.day}日"
            if sheet_title in titles:
                recorded.append(d)
                activities = read_day_activities(spreadsheet_id, sheet_title, token)
                if activities:
                    day_texts.append(
                        f"【{d.month}/{d.day}（{_DAY_JP[d.weekday()]}）】\n{activities}"
                    )
            else:
                missing.append(d)

        first, last = week_dates[0], week_dates[-1]
        period = f"（{first.month}月{first.day}日〜{last.month}月{last.day}日）"

        if not recorded:
            msg = (
                f"📅 今週の日誌サマリー\n{period}\n\n"
                "今週はまだ日誌の記録がありません📭\n\n"
                "音声入力なら1〜2分で完了できます🎤\n"
                "来週もよろしくお願いします！"
            )
        else:
            recorded_str  = '・'.join(_DAY_JP[d.weekday()] for d in recorded)
            recorded_line = f"✅ 記録済み：{recorded_str}（{len(recorded)}日）"
            missing_line  = ''
            if missing:
                missing_parts = '・'.join(
                    f"{_DAY_JP[d.weekday()]}（{d.month}/{d.day}）" for d in missing
                )
                missing_line = f"\n📭 未記録：{missing_parts}"

            summary_line = ''
            if day_texts:
                gemini_summary = call_gemini_summary('\n\n'.join(day_texts))
                if gemini_summary:
                    summary_line = f"\n\n今週の活動まとめ：\n{gemini_summary}"

            msg = (
                f"📅 今週の日誌サマリー\n{period}\n\n"
                f"{recorded_line}{missing_line}"
                f"{summary_line}\n\n"
                "来週もよろしくお願いします！🌱"
            )

        push_text(line_id, msg)
        logger.info("[SUMMARY] sent user_id=%s name=%s recorded=%d",
                    line_id[:8], name, len(recorded))
    except Exception as e:
        logger.error("[SUMMARY-02] failed name=%s: %s", name, e)


# ══════════════════════════════════════════════════════════════════════════
# Chitchat — non-diary message replies
# ══════════════════════════════════════════════════════════════════════════

def try_chitchat_reply(user_id: str, text: str, reply_token: str, token: str) -> bool:
    """
    テキストがチャット（日誌以外）と判定できる場合に応答し True を返す。
    日誌として処理すべき場合は False を返す。
    """
    t  = text.strip()
    tl = t.lower()

    # ── カテゴリH: 絵文字・記号・超短文 ──────────────────────────
    if _is_emoji_only(t):
        reply_text(reply_token, "😊 日誌はいつでも音声かテキストで送ってください！")
        return True

    if len(t) <= 2:
        reply_text(reply_token, "日誌を送るときは、今日の業務内容を音声かテキストで送ってください。")
        return True

    if re.fullmatch(r'[\d\s\W]+', t):
        reply_text(reply_token, "日誌を送るときは、今日の業務内容を音声かテキストで送ってください。")
        return True

    # ── カテゴリA: 挨拶 ──────────────────────────────────────────
    if re.fullmatch(r'おはよ[うー]?|おはようございます?[。！]*', t):
        reply_text(reply_token,
                   "おはようございます！☀️ 今日もよろしくお願いします。\n"
                   "日誌はいつでも送ってください🎤")
        return True

    if re.fullmatch(r'こんにち[はわ][。！]*', t):
        reply_text(reply_token, "こんにちは！😊 何かあればいつでもどうぞ。")
        return True

    if re.fullmatch(r'こんばんは[。！]*', t):
        reply_text(reply_token,
                   "こんばんは！🌙 今日の日誌はもう送りましたか？\n"
                   "まだなら音声やテキストで送ってみてください。")
        return True

    if re.fullmatch(r'お疲れ[様さ]?[です。！]*|おつかれ[様さ]?[です。！]*', t):
        reply_text(reply_token, "お疲れ様でした！🎉 今日の日誌を忘れずに送ってくださいね。")
        return True

    if re.fullmatch(r'ありがとう[。！]*|ありがとうございます?[。！]*|ありがとうございました[。！]*', t):
        reply_text(reply_token, "どういたしまして😊 またいつでも声をかけてください！")
        return True

    if re.fullmatch(r'よろしく[お願いしますございます。！]*', t):
        reply_text(reply_token, "こちらこそよろしくお願いします！🙏 困ったことがあればいつでもどうぞ。")
        return True

    if re.fullmatch(r'は[い]?じめまして[。！]*', t):
        reply_text(reply_token,
                   "はじめまして！😊\n"
                   "このサービスは業務日誌を音声やテキストで簡単に記録できるサービスです。\n"
                   f"使い方はこちら：\n{config.GUIDE_URL}")
        return True

    # ── カテゴリC: テスト・様子見 ────────────────────────────────
    if re.fullmatch(r'テスト[送信]*[。！]*|test|てすと', tl):
        reply_text(reply_token,
                   "✅ ちゃんと届いています！\n"
                   "日誌を送るときはそのまま今日の業務内容を話すか、\n"
                   "テキストで入力してください。")
        return True

    if re.fullmatch(r'hello|hi|hey|ハロー|ヘイ', tl):
        reply_text(reply_token, "こんにちは！😊 日誌は音声かテキストで送ってください🎤")
        return True

    # ── カテゴリB: 使い方・ヘルプ ────────────────────────────────
    if re.search(r'ヘルプ|使い方|操作方法|どうやって使|使い方を教', t) or re.search(r'help', tl):
        reply_text(reply_token, f"📖 使い方ガイドはこちらです：\n{config.GUIDE_URL}")
        return True

    if re.search(r'音声.{0,10}(送り方|方法|やり方|操作|使い方)|マイク.{0,10}(使い方|操作|どう)', t):
        reply_text(reply_token,
                   "🎙️ 音声で日誌を入力する手順\n"
                   "━━━━━━━━━━━\n"
                   "① 画面左下のキーボードマークをタップ\n\n"
                   "② スタンプボタンの右にある\n"
                   "　 マイクボタンをタップ → 録音開始\n\n"
                   "③ 今日の業務内容を話す\n"
                   "　 （目安：15秒〜2分）\n\n"
                   "④ もう一度マイクボタンをタップ\n"
                   "　 → 録音停止・送信\n\n"
                   "⑤ 内容を確認して「✅ はい」\n"
                   "━━━━━━━━━━━\n"
                   "では話してみてください！")
        return True

    if re.search(r'テキスト.{0,10}(送れ|使え|できる|ok|OK)|文字.{0,10}(送れ|使え|できる)|文章で送', t):
        reply_text(reply_token,
                   "はい、テキストもOKです📝\n"
                   "今日の業務内容をそのまま入力して送ってください。\n"
                   "AIが日誌の形に整理します！")
        return True

    if re.search(r'写真.{0,10}(登録|送り方|方法|やり方|どう)|写真を(登録|追加|送)', t):
        reply_text(reply_token,
                   "📸 写真の登録はメニューの「②写真を登録」からどうぞ。\n"
                   "日付を入力してから写真を送ってください。")
        return True

    if re.search(r'スプレッドシート|記録を見|日誌を見|過去の日誌|昨日の日誌|先週の日誌|記録の確認', t):
        reply_text(reply_token,
                   "📊 過去の記録はメニューの「スプレッドシートを開く」からご確認いただけます。")
        return True

    # ── カテゴリD: AI・サービスへの質問 ─────────────────────────
    if re.search(r'AIですか|ロボットですか|ボットですか|人間ですか', t) or re.search(r'bot\s*ですか', tl):
        reply_text(reply_token,
                   "はい、AIを使った自動日誌記録サービスです🤖\n"
                   "音声やテキストを送ると、AIが日誌の形に整理して\n"
                   "スプレッドシートに記録します。")
        return True

    if re.search(r'誰が作った|誰が管理|運営は誰|作った人', t):
        reply_text(reply_token,
                   "このサービスは管理者が運営しています。\n"
                   "ご不明な点はメニューの「フィードバック・お問い合わせ」から\n"
                   "ご連絡ください。")
        return True

    if re.search(r'雑談|なんでも(しゃべ|話せ|聞け)|何でも聞け', t):
        reply_text(reply_token,
                   "ごめんなさい、日誌の記録専用サービスなので\n"
                   "雑談への対応は難しいです😅\n"
                   "業務内容を送ってもらえると助かります！")
        return True

    # ── カテゴリE: 誤送信・やり直し ──────────────────────────────
    if re.search(r'間違え|誤送信|取り消し|消して|送り間違', t):
        reply_text(reply_token,
                   "確認画面が表示されている場合は「⛔ キャンセル」をタップしてください。\n"
                   "AI処理中の場合は「キャンセル」とテキストで送ると中断できます。")
        return True

    # ── カテゴリF: 登録関連 ──────────────────────────────────────
    if re.search(r'登録したい|登録方法|どうやって登録|登録はどうすれば|登録の仕方', t):
        reply_text(reply_token, f"登録はこちらのフォームからお願いします📝：\n{config.LIFF_URL}")
        return True

    if re.search(r'登録(できてる|済み|確認|されてる|してる)|自分は登録', t):
        member = get_member(user_id, token)
        if member:
            reply_text(reply_token, "✅ 登録済みです。日誌はいつでも送れますよ！")
        else:
            reply_text(reply_token, config.NOT_REGISTERED_MESSAGE)
        return True

    # ── カテゴリG: 不具合・エラー報告 ───────────────────────────
    if re.search(r'動かない|壊れ(てる|た)|おかしい|バグ|エラーが出|不具合', t):
        reply_text(reply_token,
                   "ご不便をおかけしてすみません🙏\n"
                   "メニューの「フィードバック・お問い合わせ」から\n"
                   "詳しい状況を教えていただけますか？")
        return True

    if re.search(r'(返事|返信|反応).{0,5}(来ない|遅い|ない)|待ってる(んだけど|のに|けど)', t):
        reply_text(reply_token,
                   "AIの処理には10〜30秒かかることがあります。\n"
                   "もうしばらくお待ちください⏳\n"
                   "しばらく経っても届かない場合は、もう一度送ってみてください。")
        return True

    # ── カテゴリI: 過去記録・修正 ────────────────────────────────
    if re.search(r'(日誌|記録).{0,10}(修正|直したい|書き直し|変えたい)', t):
        reply_text(reply_token,
                   "確認画面が出ている場合は「✏️ 修正する」をタップしてください。\n"
                   "記録後に気づいた場合は、成功メッセージの「✏️ 内容を修正する」をタップしてください。")
        return True

    if re.search(r'今日の(日誌|記録)|今日.*記録.*見|記録.*確認|日誌.*確認|今日.*送った', t):
        member = get_member(user_id, token)
        if not member or not member.get('spreadsheet_id'):
            reply_text(reply_token, config.NOT_REGISTERED_MESSAGE)
            return True
        today = datetime.now(config.JST)
        sheet_title = f"{today.month}月{today.day}日"
        try:
            activities = read_day_activities(member['spreadsheet_id'], sheet_title, token)
            if activities:
                reply_text(reply_token, f"📋 {sheet_title}の記録\n\n{activities}")
            else:
                reply_text(reply_token,
                           f"📋 {sheet_title}の記録はまだありません。\n"
                           "音声かテキストで日誌を送ってください🎤")
        except Exception:
            reply_text(reply_token,
                       "⚠️ 記録の取得中にエラーが発生しました。\nしばらくしてからお試しください。")
        return True

    # ── カテゴリJ: 感情・反応 ────────────────────────────────────
    if re.fullmatch(r'面倒[くさい。！]*|めんどう[くさい。！]*|めんどい[。！]*', t):
        reply_text(reply_token,
                   "音声なら話すだけなので、ぜひ試してみてください🎤\n"
                   "慣れると1〜2分で終わりますよ！")
        return True

    if re.fullmatch(r'(すごい|すごー+|便利|いいね|最高|完璧|ナイス)[！!。]*', t):
        reply_text(reply_token, "ありがとうございます😊 これからもよろしくお願いします！")
        return True

    if re.fullmatch(r'わからない[。！]*|むずかしい[。！]*|難しい[。！]*|どうすれば[いい]*[？?。！]*', t):
        reply_text(reply_token,
                   f"📖 使い方ガイドをご覧ください：\n{config.GUIDE_URL}\n\n"
                   "困ったことはメニューのフィードバックからも聞けます！")
        return True

    return False


# ══════════════════════════════════════════════════════════════════════════
# Flask routes
# ══════════════════════════════════════════════════════════════════════════

@app.before_request
def setup():
    global _setup_done
    if _setup_done:
        return
    with _setup_lock:
        if not _setup_done:
            if not config.SCHEDULER_SECRET:
                logger.warning("[STARTUP] SCHEDULER_SECRET is not set. "
                               "/weekly_summary endpoint is disabled.")
            try:
                token = get_sheets_token()
                ensure_session_sheet(token)
                _setup_done = True
            except Exception as e:
                logger.error("[setup error] %s", e)


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
    return {'status': 'ok', 'ts': datetime.utcnow().isoformat() + 'Z'}, 200


@app.route('/weekly_summary', methods=['POST'])
def weekly_summary():
    auth = request.headers.get('Authorization', '')
    if not config.SCHEDULER_SECRET or not hmac.compare_digest(auth, f'Bearer {config.SCHEDULER_SECRET}'):
        abort(401)
    try:
        token   = get_sheets_token()
        members = get_all_members(token)
    except Exception as e:
        logger.error("[SUMMARY] get_all_members failed: %s", e)
        return {'error': 'members unavailable'}, 500

    now_jst    = datetime.now(config.JST)
    week_dates = _get_week_dates(now_jst)
    for row in members:
        _executor.submit(_process_weekly_summary_for_member, row, week_dates, token)
    return {'status': 'ok', 'members': len(members)}, 200


@app.route('/register', methods=['POST', 'OPTIONS'])
def register():
    origin = request.headers.get('Origin', '')

    if request.method == 'OPTIONS':
        if origin not in config.ALLOWED_ORIGINS:
            abort(403)
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin'] = origin
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST'
        return resp

    def cors_response(body, status=200):
        resp = app.make_response((body, status))
        if origin in config.ALLOWED_ORIGINS:
            resp.headers['Access-Control-Allow-Origin'] = origin
        return resp

    try:
        data         = request.get_json() or {}
        line_user_id = (data.get('line_user_id', '') or '')[:100]
        name         = (data.get('name', '') or '').strip()[:100]
        email        = (data.get('email', '') or '').strip()[:256]

        if not name or not email or not is_valid_email(email):
            return cors_response({'error': 'invalid fields'}, 400)

        token = get_sheets_token()

        if is_duplicate(line_user_id, name, email, token):
            if line_user_id:
                link_rich_menu(line_user_id, config.RICHMENU_REGISTERED)
            return cors_response({'status': 'already_registered'})

        spreadsheet_url = ''
        try:
            _, spreadsheet_url = create_user_spreadsheet(name, email, token)
        except Exception as e:
            logger.error("[REG-05] create_user_spreadsheet failed name=%s: %s", name, e)

        append_member(line_user_id, name, email, token, spreadsheet_url or '')

        if config.SLACK_WEBHOOK_URL:
            try:
                requests.post(config.SLACK_WEBHOOK_URL, json={
                    "text": (
                        f"📝 *新規メンバーが登録しました*\n\n"
                        f"お名前：{name}\n\n"
                        "スプレッドシートを準備してマスタースプシのC列にURLを貼り付けてください。"
                    )
                }, timeout=10)
            except Exception as e:
                logger.warning("[REG-05] slack notify failed: %s", e)

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
                    f"{config.GUIDE_URL}"
                )
                push_text(line_user_id, msg)
            except Exception as e:
                logger.warning("[REG-06] push message failed user=%s: %s", line_user_id, e)
            link_rich_menu(line_user_id, config.RICHMENU_REGISTERED)

        return cors_response({'status': 'ok'})

    except Exception:
        logger.exception("[REG-03] register error")
        return cors_response({'error': 'internal server error (REG-03)'}, 500)


# ══════════════════════════════════════════════════════════════════════════
# LINE event handlers
# ══════════════════════════════════════════════════════════════════════════

@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    try:
        token  = get_sheets_token()
        member = get_member(user_id, token)
    except Exception:
        member = None

    if member:
        link_rich_menu(user_id, config.RICHMENU_REGISTERED)
        reply_text(event.reply_token,
                   f"おかえりなさい、{member['name']}さん！👋\n"
                   "引き続きご利用ください🎤")
    else:
        reply_text(event.reply_token, config.WELCOME_MESSAGE)


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    user_id = event.source.user_id
    text    = event.message.text.strip()

    # キャンセルは最優先（Sheets APIより先に処理）
    if text == 'キャンセル':
        with _cancel_lock:
            pending_cancel.add(user_id)
        try:
            token = get_sheets_token()
            pending_del(user_id, token)
            session_del(user_id, token)
        except Exception:
            pending.pop(user_id, None)
            _session_cache.pop(user_id, None)
        reply_text(event.reply_token,
                   "⛔ キャンセルしました。\n処理中の場合も完了後に破棄します。")
        return

    try:
        token = get_sheets_token()
    except Exception as e:
        logger.error("[SYS-01] get_sheets_token failed in handle_text: %s", e)
        reply_text(event.reply_token,
                   "⚠️ 一時的なエラーが発生しました。\nしばらくしてからもう一度お試しください。")
        return
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
                push_message_object(user_id, TextMessage(
                    text="📸 写真登録中です。\n日付を入力してください。\n例：4/10、4月10日、今日、昨日\n\n"
                         "日誌を送りたい場合は先にキャンセルしてください。",
                    quick_reply=QuickReply(items=[
                        QuickReplyItem(action=PostbackAction(
                            label='⛔ キャンセル', data='cancel_photo')),
                    ])
                ))
                return
            month, day = int(m.group(1)), int(m.group(2))
            max_day = calendar.monthrange(today.year, month)[1] if 1 <= month <= 12 else 0
            if not (1 <= month <= 12 and 1 <= day <= max_day):
                reply_text(event.reply_token,
                           "日付が正しくありません。\n例：4/10、4月10日、今日、昨日")
                return
        session_set(user_id, 'photo_ready', {'month': month, 'day': day}, token)
        reply_text(event.reply_token,
                   f"✅ {month}月{day}日ですね！\nでは登録する写真を送ってください📸")
        return

    # 写真登録：写真送信待ち（テキストが来た場合はエスケープ提示）
    if session_type == 'photo_ready':
        month = session_data.get('month', '?')
        day   = session_data.get('day', '?')
        push_message_object(user_id, TextMessage(
            text=f"📸 {month}月{day}日の写真を送るのをお待ちしています。\n\n"
                 "日誌を送りたい場合は先にキャンセルしてください。",
            quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(
                    label='⛔ キャンセル', data='cancel_photo')),
            ])
        ))
        return

    # フィードバック収集モード
    if session_type == 'feedback':
        category = session_data.get('category', '')
        session_del(user_id, token)
        reply_text(event.reply_token, "⏳ 送信中です...")
        _executor.submit(_process_feedback_async, user_id, category, text)
        return

    # 修正モード
    if session_type == 'correction':
        original = pending_get(user_id, token)
        if not original:
            reply_text(event.reply_token,
                       "⚠️ 修正する日誌が見つかりませんでした。\nもう一度音声を送ってください。")
            session_del(user_id, token)
            return
        session_del(user_id, token)
        reply_text(event.reply_token, "✏️ 修正内容を受け取りました！\nAIが整理しています...")
        _executor.submit(_process_correction_async, user_id, original, text)
        return

    # チャット判定
    if try_chitchat_reply(user_id, text, event.reply_token, token):
        return

    # 登録状況を確認
    member = get_member(user_id, token)
    if member is None:
        reply_text(event.reply_token, config.NOT_REGISTERED_MESSAGE)
        return
    if not member.get('spreadsheet_id'):
        reply_text(event.reply_token, config.WAITING_SHEET_MESSAGE)
        return

    link_rich_menu(user_id, config.RICHMENU_REGISTERED)
    try:
        session_del(user_id, token)
    except Exception:
        _session_cache.pop(user_id, None)
    reply_text(event.reply_token,
               "📝 テキストを受け取りました！\nAIが内容を整理しています...\n（10〜30秒ほどかかります）")
    _executor.submit(_process_input_async, user_id, text, False)


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    user_id = event.source.user_id

    try:
        token  = get_sheets_token()
        member = get_member(user_id, token)
    except Exception as e:
        logger.error("[PHO-01] token/member fetch failed: %s", e)
        reply_text(event.reply_token,
                   "⚠️ 一時的なエラーが発生しました。\nしばらくしてからもう一度お試しください。")
        return

    if not member or not member.get('spreadsheet_id'):
        reply_text(event.reply_token, config.NOT_REGISTERED_MESSAGE)
        return

    session_type, session_data = session_get(user_id, token)
    if session_type != 'photo_ready':
        reply_text(event.reply_token,
                   "📸 写真を登録するには、メニューの「②写真を登録」をタップして"
                   "日付を入力してから写真を送ってください。")
        return

    month, day = session_data['month'], session_data['day']
    session_del(user_id, token)

    try:
        image_bytes = get_message_content(event.message.id)
    except Exception as e:
        logger.error("[PHO-01] get_message_content error: %s", e)
        reply_text(event.reply_token,
                   "⚠️ 写真の取得に失敗しました（PHO-01）。\nもう一度送ってください。")
        return

    if len(image_bytes) > config.MAX_IMAGE_BYTES:
        logger.warning("[PHO-02] image too large size=%d user=%s", len(image_bytes), user_id)
        reply_text(event.reply_token,
                   "⚠️ 写真のサイズが大きすぎます（PHO-02）。\n10MB以下の写真を送ってください。")
        return

    reply_text(event.reply_token, "📸 写真を受け取りました！アップロード中です...")
    now = datetime.now()
    filename = f"activity_{user_id}_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
    _executor.submit(_process_image_async, user_id, image_bytes, filename, month, day, token)


@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio(event):
    user_id = event.source.user_id

    try:
        token  = get_sheets_token()
        member = get_member(user_id, token)
    except Exception as e:
        logger.error("[AUD-01] token/member fetch failed: %s", e)
        reply_text(event.reply_token,
                   "⚠️ 一時的なエラーが発生しました。\nしばらくしてからもう一度お試しください。")
        return

    if member is None:
        reply_text(event.reply_token, config.NOT_REGISTERED_MESSAGE)
        return
    if not member.get('spreadsheet_id'):
        reply_text(event.reply_token, config.WAITING_SHEET_MESSAGE)
        return

    link_rich_menu(user_id, config.RICHMENU_REGISTERED)
    try:
        session_del(user_id, token)
    except Exception:
        _session_cache.pop(user_id, None)

    try:
        audio_bytes = get_message_content(event.message.id)
    except Exception as e:
        logger.error("[AUD-01] get_message_content error: %s", e)
        reply_text(event.reply_token,
                   "⚠️ 音声の取得に失敗しました（AUD-01）。\nもう一度送ってください。")
        return

    if len(audio_bytes) > config.MAX_AUDIO_BYTES:
        logger.warning("[AUD-02] audio too large size=%d user=%s", len(audio_bytes), user_id)
        reply_text(event.reply_token,
                   "⚠️ 音声ファイルが大きすぎます（AUD-02）。\n短めの録音を送ってください。")
        return

    audio_b64 = base64.b64encode(audio_bytes).decode()
    reply_text(event.reply_token,
               "🎙️ 音声を受け取りました！\nAIが内容を整理しています...\n（10〜30秒ほどかかります）")
    _executor.submit(_process_input_async, user_id, audio_b64, True)


@handler.add(MessageEvent, message=StickerMessageContent)
def handle_sticker(event):
    reply_text(event.reply_token,
               "😊 日誌はいつでも音声かテキストで送ってください！")


@handler.add(MessageEvent, message=VideoMessageContent)
def handle_video(event):
    reply_text(event.reply_token,
               "動画はこちらでは処理できません📵\n日誌は音声かテキストで送ってください🎤")


@handler.add(MessageEvent, message=FileMessageContent)
def handle_file(event):
    reply_text(event.reply_token,
               "ファイルはこちらでは処理できません📵\n日誌は音声かテキストで送ってください🎤")


@handler.add(MessageEvent, message=LocationMessageContent)
def handle_location(event):
    reply_text(event.reply_token,
               "位置情報はこちらでは処理できません📵\n日誌は音声かテキストで送ってください🎤")


# ══════════════════════════════════════════════════════════════════════════
# Postback handlers
# ══════════════════════════════════════════════════════════════════════════

def _pb_confirm_yes(event):
    user_id = event.source.user_id
    try:
        token = get_sheets_token()
    except Exception as e:
        logger.error("[REC-03] get_sheets_token failed: %s", e)
        reply_text(event.reply_token,
                   "⚠️ 認証エラーが発生しました。\nしばらくしてからもう一度お試しください。")
        return
    structured = pending_get(user_id, token)
    if not structured:
        reply_text(event.reply_token,
                   "⚠️ 記録する内容が見つかりませんでした。\nもう一度音声を送ってください。")
        return
    try:
        sheet_name, member_name, overflow = record_to_sheet(user_id, structured)
        if sheet_name:
            pending_del(user_id, token)
            try:
                session_set(user_id, 'last_record',
                            {'text': structured, 'sheet_name': sheet_name}, token)
            except Exception:
                pass
            msg = f"✅ {sheet_name}の日報をスプレッドシートに記録しました！"
            if overflow:
                msg += (f"\n\n⚠️ 活動が{config.ACT_MAX_ROWS}件を超えたため、"
                        f"{overflow}件分は備考欄に追記しました。")
            push_message_object(user_id, TextMessage(
                text=msg,
                quick_reply=QuickReply(items=[
                    QuickReplyItem(action=PostbackAction(
                        label='✏️ 内容を修正する', data='revise_last_record')),
                ])
            ))
            send_to_slack(member_name, sheet_name, structured)
        else:
            reply_text(event.reply_token,
                       "⚠️ スプレッドシートへの記録に失敗しました（REC-02）。\n管理者にお問い合わせください。")
    except Exception as e:
        logger.error("[REC-03] confirm_yes error: %s", e)
        push_message_object(event.source.user_id, TextMessage(
            text="⚠️ 記録中にエラーが発生しました（REC-03）。\nもう一度試しますか？",
            quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='✅ リトライ', data='confirm_yes')),
                QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='confirm_cancel')),
            ])
        ))


def _pb_confirm_no(event):
    user_id = event.source.user_id
    try:
        token = get_sheets_token()
        session_set(user_id, 'correction', {}, token)
    except Exception:
        _session_cache[user_id] = {'type': 'correction', 'data': {}, 'ts': 0.0}
    reply_text(event.reply_token,
               "修正内容をテキストで送ってください。\n"
               "例）「午後の作業時間を3時間に変えて」\n"
               "　　「日付を4月10日に変えて」")


def _pb_confirm_cancel(event):
    user_id = event.source.user_id
    try:
        t = get_sheets_token()
        pending_del(user_id, t)
        session_del(user_id, t)
    except Exception:
        pending.pop(user_id, None)
        _session_cache.pop(user_id, None)
    reply_text(event.reply_token, "キャンセルしました。\n記録は行われていません。")


def _pb_guide_voice(event):
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
               "では話してみてください！")


def _pb_guide_photo(event):
    user_id = event.source.user_id
    try:
        token = get_sheets_token()
        session_set(user_id, 'photo_date', {}, token)
    except Exception:
        _session_cache[user_id] = {'type': 'photo_date', 'data': {}, 'ts': 0.0}
    reply_text(event.reply_token,
               "📸 写真を登録する手順\n"
               "━━━━━━━━━━━\n"
               "① 日付を入力（今から）\n"
               "② 写真を送信\n"
               "③「✅ 追加する」をタップ\n"
               "━━━━━━━━━━━\n"
               "何日の日報に追加しますか？\n"
               "例：4/10　4月10日　今日　昨日")


def _pb_start_feedback(event):
    push_message_object(event.source.user_id, TextMessage(
        text="どちらについてお送りですか？",
        quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(
                label='💬 フィードバック・改善要望', data='feedback_type_feedback')),
            QuickReplyItem(action=PostbackAction(
                label='📞 管理者に連絡する', data='feedback_type_contact')),
            QuickReplyItem(action=PostbackAction(
                label='⛔ キャンセル', data='feedback_cancel')),
        ])
    ))


def _pb_feedback_type(event, category: str, prompt_text: str):
    user_id = event.source.user_id
    try:
        token = get_sheets_token()
        session_set(user_id, 'feedback', {'category': category}, token)
    except Exception:
        _session_cache[user_id] = {'type': 'feedback',
                                   'data': {'category': category}, 'ts': 0.0}
    push_message_object(user_id, TextMessage(
        text=prompt_text,
        quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='feedback_cancel')),
        ])
    ))


def _pb_feedback_type_feedback(event):
    _pb_feedback_type(event, 'フィードバック・改善要望',
                      "💬 ご意見・改善要望をテキストで送ってください。\nどんな小さなことでも歓迎です！")


def _pb_feedback_type_contact(event):
    _pb_feedback_type(event, '管理者への連絡',
                      "📞 管理者への連絡内容をテキストで送ってください。")


def _pb_feedback_cancel(event):
    user_id = event.source.user_id
    try:
        token = get_sheets_token()
        session_del(user_id, token)
    except Exception:
        _session_cache.pop(user_id, None)
    reply_text(event.reply_token, "キャンセルしました。")


def _pb_add_photo(event):
    user_id = event.source.user_id
    token = None
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
        file_id = info.get('file_id', '')
        if not _SAFE_FILE_ID_RE.match(file_id):
            logger.error("[PHO-04] invalid file_id: %s", file_id)
            reply_text(event.reply_token, "⚠️ 写真データが無効です（PHO-04）。もう一度送ってください。")
            return
        session_del(user_id, token)
        spreadsheet_id = member['spreadsheet_id']
        month, day = info['month'], info['day']
        sheet_title = f"{month}月{day}日"
        if write_photo_url_to_sheet(spreadsheet_id, sheet_title, file_id, token):
            reply_text(event.reply_token, f"✅ {sheet_title}の活動写真を追加しました！")
        else:
            logger.error("[PHO-03] write_photo_url_to_sheet failed")
            reply_text(event.reply_token,
                       "⚠️ 写真の追加に失敗しました（PHO-03）。\nしばらくしてからお試しください。")
    except Exception as e:
        logger.error("[PHO-03] add_photo error: %s", e)
        reply_text(event.reply_token, "⚠️ 写真の追加中にエラーが発生しました（PHO-03）。")


def _pb_cancel_photo(event):
    user_id = event.source.user_id
    try:
        token = get_sheets_token()
        session_del(user_id, token)
    except Exception:
        _session_cache.pop(user_id, None)
    reply_text(event.reply_token, "キャンセルしました。")


def _pb_revise_last_record(event):
    user_id = event.source.user_id
    try:
        token = get_sheets_token()
        session_type, session_data = session_get(user_id, token)
    except Exception as e:
        logger.error("[REC-08] revise_last_record token error: %s", e)
        reply_text(event.reply_token,
                   "⚠️ エラーが発生しました。もう一度お試しください。")
        return

    if session_type != 'last_record' or not session_data:
        reply_text(event.reply_token,
                   "⚠️ 修正できる記録が見つかりません。\n新しく日誌を送ってください。")
        return

    original = session_data.get('text', '')
    try:
        pending_set(user_id, original, token)
        session_set(user_id, 'correction', {}, token)
    except Exception:
        with _cache_lock:
            pending[user_id] = original
        _session_cache[user_id] = {'type': 'correction', 'data': {}, 'ts': 0.0}

    reply_text(event.reply_token,
               "✏️ 修正内容をテキストで送ってください。\n"
               "例）「午後の作業時間を3時間に変えて」\n"
               "　　「日付を4月10日に変えて」")


def _pb_open_spreadsheet(event):
    user_id = event.source.user_id
    try:
        token  = get_sheets_token()
        member = get_member(user_id, token)
        if member and member.get('spreadsheet_id'):
            url = f"https://docs.google.com/spreadsheets/d/{member['spreadsheet_id']}/edit"
            reply_text(event.reply_token, f"📊 スプレッドシートはこちらです：\n{url}")
        else:
            reply_text(event.reply_token,
                       "⏳ スプレッドシートはまだ準備中です。\n\n"
                       "管理者が承認・設定するまで1〜3日かかる場合があります。\n"
                       "準備ができたらご連絡しますので、もう少しお待ちください🙏")
    except Exception:
        reply_text(event.reply_token,
                   "⚠️ 確認中にエラーが発生しました。\nしばらくしてからお試しください。")


_POSTBACK_DISPATCH = {
    'confirm_yes':            _pb_confirm_yes,
    'confirm_no':             _pb_confirm_no,
    'confirm_cancel':         _pb_confirm_cancel,
    'guide_voice':            _pb_guide_voice,
    'guide_photo':            _pb_guide_photo,
    'start_feedback':         _pb_start_feedback,
    'feedback_type_feedback': _pb_feedback_type_feedback,
    'feedback_type_contact':  _pb_feedback_type_contact,
    'feedback_cancel':        _pb_feedback_cancel,
    'add_photo':              _pb_add_photo,
    'cancel_photo':           _pb_cancel_photo,
    'open_spreadsheet':       _pb_open_spreadsheet,
    'revise_last_record':     _pb_revise_last_record,
}


@handler.add(PostbackEvent)
def handle_postback(event):
    fn = _POSTBACK_DISPATCH.get(event.postback.data)
    if fn:
        fn(event)
    else:
        logger.warning("[PB-00] unknown postback: %s", event.postback.data)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)  # nosec B104 – Cloud Run requires binding all interfaces
