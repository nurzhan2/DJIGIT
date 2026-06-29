"""
Хранение истории диалогов в SQLite.

Контент каждого сообщения хранится как JSON-список "частей" (parts),
чтобы единообразно держать и текст, и изображения:
  текст:    [{"type": "text", "text": "..."}]
  картинка: [{"type": "text", "text": "подпись"},
             {"type": "image", "data": "<base64>", "mime": "image/jpeg"}]

Роли соответствуют формату Gemini: "user" и "model".
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"


def init_db() -> None:
    """Создаёт таблицу, если её ещё нет."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user ON messages(user_id, id)"
        )


def add_message(user_id: int, role: str, parts: list) -> None:
    """Добавляет сообщение (список частей) в историю."""
    serialized = json.dumps(parts, ensure_ascii=False)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, serialized),
        )


def get_history(user_id: int, limit: int = 20) -> list[dict]:
    """Возвращает последние `limit` сообщений пользователя (от старых к новым)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

    history = []
    for role, content in reversed(rows):
        history.append({"role": role, "parts": json.loads(content)})
    return history


def clear_history(user_id: int) -> None:
    """Удаляет всю историю пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
