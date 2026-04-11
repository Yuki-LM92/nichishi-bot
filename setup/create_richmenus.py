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
W, H        = 2500, 843  # compact size (1 row)

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
    draw.text((cx, cy - 90), "📝  はじめての方はこちら", font=f_main, fill='white', anchor='mm')
    draw.text((cx, cy + 70), "タップして利用登録へ", font=f_sub, fill=(200, 255, 215), anchor='mm')
    return img_to_buf(img)

def create_registered_image():
    """登録済み: 3分割（スプシ・ガイド・フィードバック）"""
    img = Image.new('RGB', (W, H), (248, 253, 248))
    draw = ImageDraw.Draw(img)
    cell_w = W // 3

    cells = [
        ('📊', 'スプレッドシート', 'を開く',         (230, 248, 236)),
        ('📖', '使い方',          'ガイド',           (230, 243, 255)),
        ('💬', 'フィードバック・', 'お問い合わせ',     (255, 248, 225)),
    ]

    f_icon = load_font(145)
    f_main = load_font(67)
    f_sub  = load_font(57)

    for i, (icon, line1, line2, bg) in enumerate(cells):
        x0, x1 = i * cell_w, (i + 1) * cell_w
        draw.rectangle([x0, 0, x1 - 1, H], fill=bg)
        if i > 0:
            draw.line([(x0, 25), (x0, H - 25)], fill=(200, 220, 205), width=5)
        cx = x0 + cell_w // 2
        cy = H // 2
        draw.text((cx, cy - 145), icon,  font=f_icon, fill='#222', anchor='mm')
        draw.text((cx, cy + 35),  line1, font=f_main, fill='#1a1a1a', anchor='mm')
        draw.text((cx, cy + 115), line2, font=f_sub,  fill='#555', anchor='mm')

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
        f'{LINE_API}/richmenu/{menu_id}/content',
        headers={**HEADERS, 'Content-Type': 'image/png'},
        data=buf.read()
    )
    resp.raise_for_status()
    print(f"  ✅ 画像アップロード完了")

def set_default(menu_id):
    resp = requests.post(f'{LINE_API}/user/all/richmenu/{menu_id}', headers=HEADERS)
    resp.raise_for_status()
    print(f"  ✅ デフォルトメニューに設定")

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
    "size": {"width": 2500, "height": 843},
    "selected": True,
    "name": "登録済みユーザーメニュー",
    "chatBarText": "メニュー",
    "areas": [
        {
            "bounds": {"x": 0, "y": 0, "width": 833, "height": 843},
            "action": {"type": "postback", "label": "スプレッドシートを開く", "data": "open_spreadsheet"}
        },
        {
            "bounds": {"x": 833, "y": 0, "width": 834, "height": 843},
            "action": {"type": "uri", "label": "使い方ガイド", "uri": GUIDE_URL}
        },
        {
            "bounds": {"x": 1667, "y": 0, "width": 833, "height": 843},
            "action": {"type": "postback", "label": "フィードバック", "data": "start_feedback"}
        }
    ]
}

# ========== 実行 ==========

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
