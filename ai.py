"""Gemini API calls.
To switch to a different LLM, replace only this module.
All call_gemini_* functions raise on failure; callers must handle.
"""
import logging
import requests
from datetime import datetime
import config

logger = logging.getLogger(__name__)

_GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models"
    f"/{config.GEMINI_MODEL}:generateContent"
)

_REPORT_FORMAT = """
📅 日付：（言及があれば。なければ空欄）
⏰ 活動内容：
・HH:MM ～ HH:MM 活動内容
・HH:MM ～ HH:MM 活動内容
（複数ある場合はすべて列挙する）
📣 共有事項：（上司や仲間にSlackで伝えたいことがあれば記載。なければ「なし」）

時間のルール：
- 時間は必ず HH:MM ～ HH:MM 形式で記載する（例：08:00 ～ 09:30）
- 終了時刻の言及がない場合は --:-- とする（例：10:00 ～ --:--）
- 開始・終了ともに不明な場合は --:-- ～ --:-- とする
- 「8時ごろ」→ 08:00 ～ --:--、「10時から11時」→ 10:00 ～ 11:00
- 途中で終わった記録も省略せずすべて記載する
"""

_CORRECTION_FORMAT = """
📅 日付：
⏰ 活動内容：
・HH:MM ～ HH:MM 活動内容
（複数ある場合はすべて列挙する）
📣 共有事項：
"""

_AUDIO_PROMPT = f"""
あなたは地域おこし協力隊の業務日報の記録係です。
送られてきた音声は、協力隊員が今日の業務を振り返って話したものです。

以下のフォーマットだけで出力してください（余計な説明は不要）：
{_REPORT_FORMAT}
音声に含まれる情報だけを使い、推測で補わないでください。
"""

_TEXT_PROMPT = f"""
あなたは地域おこし協力隊の業務日報の記録係です。
以下のテキストは、協力隊員が今日の業務内容を伝えたものです。

以下のフォーマットだけで出力してください（余計な説明は不要）：
{_REPORT_FORMAT}
テキストに含まれる情報だけを使い、推測で補わないでください。

テキスト：
"""

_CORRECTION_PROMPT = f"""
あなたは地域おこし協力隊の業務日報の記録係です。
以下の「現在の日報」に「修正指示」を適用した結果を出力してください。

【修正指示の解釈ルール】
修正指示は口語・略記・記号など様々な形式で書かれる。以下のように解釈すること：
- 「A → B」「A ⇒ B」「A × → B」「A を B に」「A は B」はすべて「A を B に置き換える」
- 「A × 」「A を削除」「A はなし」は「A を削除する」
- 文中に出てくる語句が修正指示のキーワードと一致する場合、それを対象と判断する
- 指示が短くても文脈から意図を読み取り、最も自然な修正を行うこと

【絶対ルール】
1. 修正指示で言及されていない項目は、現在の日報の文字列を一字一句そのままコピーすること。推測・省略・補完は一切しないこと。
2. 修正指示で言及された項目だけを、修正指示の内容で書き換える。
3. 以下のフォーマット構造で出力すること（余計な説明・前置き・後書きは不要）：
{_CORRECTION_FORMAT}
"""

_SUMMARY_PROMPT = (
    "あなたは地域おこし協力隊の週次日誌サマリーを作成するアシスタントです。\n"
    "以下の活動記録を読んで、200字以内で今週の活動を簡潔にまとめてください。\n"
    "箇条書きではなく、1〜2文の自然な文章で書いてください。\n\n"
)


def _call_gemini(payload: dict, timeout: int = 60) -> str:
    headers = {"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"}
    resp = requests.post(_GEMINI_URL, json=payload, headers=headers, timeout=timeout)
    if not resp.ok:
        logger.error("[GEMINI] status=%s body=%s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    candidates = resp.json().get('candidates', [])
    if not candidates:
        raise ValueError("Gemini returned empty candidates")
    candidate = candidates[0]
    finish_reason = candidate.get('finishReason', '')
    if finish_reason in ('SAFETY', 'RECITATION', 'OTHER'):
        raise ValueError(f"Gemini blocked response: finishReason={finish_reason}")
    parts = candidate.get('content', {}).get('parts', [])
    if not parts:
        raise ValueError("Gemini returned no parts")
    text = parts[0].get('text', '').strip()
    if not text:
        raise ValueError("Gemini returned empty text")
    return text


def _validate_structured(text: str, context: str) -> str:
    """日報フォーマットの最低限のマーカーが含まれているか検証する。"""
    if '⏰' not in text:
        logger.warning("[GEMINI] %s: structured output missing ⏰ marker. raw=%s", context, text[:200])
        raise ValueError(f"Gemini {context}: output missing activity marker")
    return text


def call_gemini_audio(audio_b64: str) -> str:
    today = datetime.now().strftime('%Y年%m月%d日')
    prompt = _AUDIO_PROMPT + f"\n※ 日付の言及がない場合は今日の日付（{today}）を使用してください。"
    payload = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "audio/mp4", "data": audio_b64}},
        {"text": prompt},
    ]}]}
    return _validate_structured(_call_gemini(payload, timeout=120), 'audio')


def call_gemini_text(text: str) -> str:
    today = datetime.now().strftime('%Y年%m月%d日')
    date_note = f"※ 日付の言及がない場合は今日の日付（{today}）を使用してください。\n\n"
    payload = {"contents": [{"parts": [{"text": _TEXT_PROMPT + date_note + text}]}]}
    return _validate_structured(_call_gemini(payload, timeout=60), 'text')


def call_gemini_correction(original: str, correction: str) -> str:
    # .format() ではなく文字列結合でプロンプト構築（ユーザー入力に {} が含まれる場合の KeyError 回避）
    prompt = (
        _CORRECTION_PROMPT
        + "---\n現在の日報（修正前）：\n" + original
        + "\n\n---\n修正指示：\n" + correction + "\n"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    return _validate_structured(_call_gemini(payload, timeout=60), 'correction')


def call_gemini_summary(activities_text: str) -> str:
    payload = {"contents": [{"parts": [{"text": _SUMMARY_PROMPT + activities_text}]}]}
    try:
        return _call_gemini(payload, timeout=30)
    except Exception as e:
        logger.warning("[SUMMARY-01] gemini summary error: %s", e)
        return ''
