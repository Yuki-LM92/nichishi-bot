#!/usr/bin/env python3
"""
リッチメニュー自動セットアップスクリプト

事前準備:
  pip install Pillow requests

実行方法:
  LINE_CHANNEL_ACCESS_TOKEN=xxxx python setup/create_richmenus.py
"""
import os
import sys
import json
import requests
from io import BytesIO

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("❌ Pillow が必要です: pip install Pillow")
    sys.exit(1)

TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
if not TOKEN:
    print("❌ LINE_CHANNEL_ACCESS_TOKEN が未設定です")
    print("例) LINE_CHANNEL_ACCESS_TOKEN=xxxx python setup/create_richmenus.py")
    sys.exit(1)

HEADERS     = {'Authorization': f'Bearer {TOKEN}'}
LINE_API    = 'https://api.line.me/v2/bot'
LIFF_URL    = 'https://liff.line.me/2009693703-ONMSHAXr'
GUIDE_URL   = 'https://yuki-lm92.github.io/nichishi-register/guide.html'
W, H        = 2500, 843   # compact size (1 row)
W2, H2      = 2500, 1686  # large size (2 rows)

# ========== 画像生成 ==========

def load_font(size):
    candidates = [
        '/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc',
        '/System/Library/Fonts/ヒラギノ角ゴ Pro W6.otf',
        '/Library/Fonts/ヒラギノ角ゴ Pro W6.otf',
        '/usr/share/fonts/truetype/noto/NotoSansCJKjp-Bold.otf',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()

def img_to_buf(img):
    buf = BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return buf

def create_unregistered_image():
    """未登録: 全面グリーン・登録ボタン1つ"""
    img = Image.new('RGB', (W, H), (6, 199, 85))
    draw = ImageDraw.Draw(img)
    f_main = load_font(105)
    f_sub  = load_font(68)
    cx, cy = W // 2, H // 2
    draw.text((cx, cy - 80), "はじめての方はこちら", font=f_main, fill='white', anchor='mm')
    draw.text((cx, cy + 70), "タップして利用登録へ", font=f_sub, fill=(200, 255, 215), anchor='mm')
    return img_to_buf(img)

def create_registered_image():
    """登録済み: 2段構成（上段3ボタン・下段3ボタン）"""
    img = Image.new('RGB', (W2, H2), (248, 253, 248))
    draw = ImageDraw.Draw(img)

    f_num  = load_font(120)
    f_main = load_font(65)
    f_sub  = load_font(58)
    f_step = load_font(48)

    # ---- 上段：3分割 ----
    top_cells = [
        {
            'num': '1', 'title': '音声で日誌入力',
            'items': ['マイクを長押し', '話す（15秒〜2分）', '指を離して送信', '「はい」で記録完了'],
            'item_label': 'Step',
            'accent': (6, 199, 85),   'bg': (230, 248, 236),
        },
        {
            'num': '2', 'title': '写真を登録',
            'items': ['このボタンをタップ', '日付を入力', '写真を送信', '「追加する」で完了'],
            'item_label': 'Step',
            'accent': (30, 130, 220), 'bg': (230, 243, 255),
        },
        {
            'num': '3', 'title': 'その他の機能',
            'items': ['今日の記録を確認', '今週の記録状況', 'スプレッドシート', '使い方ガイド'],
            'item_label': '・',
            'accent': (140, 90, 210), 'bg': (243, 238, 255),
        },
    ]
    cell_w_top = W2 // 3
    for i, cell in enumerate(top_cells):
        x0 = i * cell_w_top
        x1 = W2 if i == len(top_cells) - 1 else (i + 1) * cell_w_top
        draw.rectangle([x0, 0, x1 - 1, H], fill=cell['bg'])
        draw.rectangle([x0, 0, x1 - 1, 22], fill=cell['accent'])
        if i > 0:
            draw.line([(x0, 22), (x0, H)], fill=(200, 220, 210), width=4)
        # 数字バッジ＋タイトル（横並び）
        draw.text((x0 + 75, 130), cell['num'],   font=f_num,  fill=cell['accent'], anchor='mm')
        draw.text((x0 + 175, 130), cell['title'], font=f_main, fill='#1a1a1a',      anchor='lm')
        # 操作案内 or 機能一覧
        step_y = 270
        for j, item in enumerate(cell['items']):
            label = f"{cell['item_label']}{j+1}  {item}" if cell['item_label'] == 'Step' else f"{cell['item_label']} {item}"
            draw.text((x0 + 55, step_y), label, font=f_step, fill='#555')
            step_y += 108

    # ---- 区切り線 ----
    draw.line([(0, H), (W2, H)], fill=(200, 215, 205), width=6)

    # ---- 下段：3分割 ----
    bottom_cells = [
        ('スプレッドシート', 'を開く',      (6, 199, 85),   (230, 248, 236)),
        ('使い方',          'ガイド',        (30, 130, 220), (230, 243, 255)),
        ('フィードバック・', 'お問い合わせ', (220, 150, 0),  (255, 248, 225)),
    ]
    cell_w_bot = W2 // 3
    for i, (line1, line2, accent, bg) in enumerate(bottom_cells):
        x0, x1 = i * cell_w_bot, (i + 1) * cell_w_bot
        y0 = H
        draw.rectangle([x0, y0, x1 - 1, H2], fill=bg)
        draw.rectangle([x0, y0, x1 - 1, y0 + 18], fill=accent)
        if i > 0:
            draw.line([(x0, y0 + 18), (x0, H2)], fill=(210, 225, 215), width=4)
        cx = x0 + cell_w_bot // 2
        cy = y0 + H // 2
        draw.text((cx, cy - 45), line1, font=f_main, fill='#1a1a1a', anchor='mm')
        draw.text((cx, cy + 65), line2, font=f_sub,  fill='#444',    anchor='mm')

    return img_to_buf(img)

# ========== LINE API ==========

def create_menu(definition):
    resp = requests.post(
        f'{LINE_API}/richmenu',
        headers={**HEADERS, 'Content-Type': 'application/json'},
        json=definition
    )
    resp.raise_for_status()
    menu_id = resp.json()['richMenuId']
    print(f"  ✅ 作成: {menu_id}")
    return menu_id

def upload_image(menu_id, buf):
    resp = requests.post(
        f'https://api-data.line.me/v2/bot/richmenu/{menu_id}/content',
        headers={**HEADERS, 'Content-Type': 'image/png'},
        data=buf.read()
    )
    resp.raise_for_status()
    print(f"  ✅ 画像アップロード完了")

def set_default(menu_id):
    resp = requests.post(f'{LINE_API}/user/all/richmenu/{menu_id}', headers=HEADERS)
    resp.raise_for_status()
    print(f"  ✅ デフォルトメニューに設定")

def delete_all_menus():
    """既存のリッチメニューを全削除"""
    resp = requests.get(f'{LINE_API}/richmenu/list', headers=HEADERS)
    if resp.status_code != 200:
        return
    for menu in resp.json().get('richmenus', []):
        mid = menu['richMenuId']
        requests.delete(f'{LINE_API}/richmenu/{mid}', headers=HEADERS)
        print(f"  🗑️ 削除: {mid}")

# ========== メニュー定義 ==========

UNREGISTERED_DEF = {
    "size": {"width": 2500, "height": 843},
    "selected": True,
    "name": "未登録ユーザーメニュー",
    "chatBarText": "メニュー",
    "areas": [
        {
            "bounds": {"x": 0, "y": 0, "width": 2500, "height": 843},
            "action": {"type": "uri", "label": "利用登録", "uri": LIFF_URL}
        }
    ]
}

REGISTERED_DEF = {
    "size": {"width": 2500, "height": 1686},
    "selected": True,
    "name": "登録済みユーザーメニュー",
    "chatBarText": "メニュー",
    "areas": [
        {
            "bounds": {"x": 0, "y": 0, "width": 833, "height": 843},
            "action": {"type": "postback", "label": "音声で日誌入力", "data": "guide_voice"}
        },
        {
            "bounds": {"x": 833, "y": 0, "width": 833, "height": 843},
            "action": {"type": "postback", "label": "写真を登録", "data": "guide_photo"}
        },
        {
            "bounds": {"x": 1666, "y": 0, "width": 834, "height": 843},
            "action": {"type": "postback", "label": "その他", "data": "other_menu"}
        },
        {
            "bounds": {"x": 0, "y": 843, "width": 833, "height": 843},
            "action": {"type": "postback", "label": "スプレッドシートを開く", "data": "open_spreadsheet"}
        },
        {
            "bounds": {"x": 833, "y": 843, "width": 834, "height": 843},
            "action": {"type": "uri", "label": "使い方ガイド", "uri": GUIDE_URL}
        },
        {
            "bounds": {"x": 1667, "y": 843, "width": 833, "height": 843},
            "action": {"type": "postback", "label": "フィードバック", "data": "start_feedback"}
        }
    ]
}

# ========== 実行 ==========

print("\n🗑️ 既存メニューをクリア中...")
delete_all_menus()

print("\n🔧 未登録ユーザー用メニューを作成中...")
unregistered_id = create_menu(UNREGISTERED_DEF)
upload_image(unregistered_id, create_unregistered_image())
set_default(unregistered_id)

print("\n🔧 登録済みユーザー用メニューを作成中...")
registered_id = create_menu(REGISTERED_DEF)
upload_image(registered_id, create_registered_image())

print(f"""
{'=' * 48}
✅ セットアップ完了！

Cloud Run の環境変数に以下を追加してください:

  RICHMENU_UNREGISTERED = {unregistered_id}
  RICHMENU_REGISTERED   = {registered_id}

{'=' * 48}
""")
