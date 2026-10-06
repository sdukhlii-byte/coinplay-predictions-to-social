import os
import sys
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
for k, v in {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h", "TELEGRAM_STRING_SESSION": "s",
             "SOURCE_CHAT_ID": "-100", "PUBLIC_BASE_URL": "https://x.example",
             "THREADS_ENABLED": "false"}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


def test_recent_bursts_has_chat_format_and_status_filter():
    db.init()
    a = db.start_zip_burst(-111, "zipkit · A vs B", {"match_id": "m", "platforms": {"instagram": {"text": "t", "media": []}}})
    rows = db.recent_bursts(10)
    row = next(r for r in rows if r["id"] == a)
    assert row["chat_id"] == -111 and row["format"] == "kit" and "updated_at" in row
    assert db.recent_bursts(10, "failed") == []
    assert any(r["id"] == a for r in db.recent_bursts(10, row["status"]))
