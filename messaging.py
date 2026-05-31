"""LINE Messaging API helpers.
All public functions are error-safe — they log failures but never raise.
To switch from LINE to another channel, replace only this module.
"""
import logging
import requests
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, PushMessageRequest, TextMessage,
    QuickReply, QuickReplyItem, PostbackAction,
)
import config

logger = logging.getLogger(__name__)

_configuration = Configuration(access_token=config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(config.LINE_CHANNEL_SECRET)


def reply_text(reply_token: str, text: str) -> None:
    try:
        with ApiClient(_configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
            )
    except Exception as e:
        logger.error("[LINE-01] reply_text failed: %s", e)


def push_text(user_id: str, text: str) -> None:
    try:
        with ApiClient(_configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
            )
    except Exception as e:
        logger.error("[LINE-02] push_text failed user=%s: %s", user_id, e)


def push_confirm(user_id: str, structured: str) -> None:
    """確認画面をpush_messageで送る。失敗時はシンプルな push_text にフォールバック。"""
    try:
        with ApiClient(_configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(
                        text=f"📋 以下の内容で記録しますね。確認してください。\n\n{structured}",
                        quick_reply=QuickReply(items=[
                            QuickReplyItem(action=PostbackAction(label='✅ はい',     data='confirm_yes')),
                            QuickReplyItem(action=PostbackAction(label='✏️ 修正する', data='confirm_no')),
                            QuickReplyItem(action=PostbackAction(label='⛔ キャンセル', data='confirm_cancel')),
                        ])
                    )]
                )
            )
    except Exception as e:
        logger.error("[LINE-03] push_confirm failed user=%s: %s", user_id, e)
        push_text(user_id, "⚠️ メッセージ送信に失敗しました（REC-06）。\nもう一度音声を送ってください。")


def push_message_object(user_id: str, message) -> None:
    """任意の LINE SDK メッセージオブジェクトをエラーセーフに push する。"""
    try:
        with ApiClient(_configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=[message])
            )
    except Exception as e:
        logger.error("[LINE-05] push_message_object failed user=%s: %s", user_id, e)


def link_rich_menu(user_id: str, menu_id: str) -> None:
    if not menu_id:
        return
    try:
        requests.post(
            f'https://api.line.me/v2/bot/user/{user_id}/richmenu/{menu_id}',
            headers={'Authorization': f'Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}'},
            timeout=10,
        )
    except Exception as e:
        logger.warning("[RMN-01] link_rich_menu user=%s: %s", user_id, e)


def get_message_content(message_id: str) -> bytes:
    """LINEサーバーからメッセージコンテンツ（音声/画像）を取得する。失敗時は例外を投げる。"""
    with ApiClient(_configuration) as api_client:
        return MessagingApiBlob(api_client).get_message_content(message_id)
