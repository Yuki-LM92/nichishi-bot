import os

# main.py のモジュールレベル初期化（WebhookHandler, Configuration等）を通過させるためのダミー値
os.environ.setdefault('LINE_CHANNEL_SECRET',       'test_secret_dummy_32chars_padding')
os.environ.setdefault('LINE_CHANNEL_ACCESS_TOKEN', 'test_access_token_dummy')
os.environ.setdefault('GEMINI_API_KEY',            'test_gemini_key_dummy')
os.environ.setdefault('MASTER_SPREADSHEET_ID',     'test_spreadsheet_id_dummy')
