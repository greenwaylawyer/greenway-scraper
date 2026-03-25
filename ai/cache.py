"""SQLite-backed response cache for AI API calls.

Key format: SHA256(prompt_version + input_json)
TTL is configurable per config/ai.yaml -> cache.ttl_hours
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from ai.config import cache_config
from utils.logger import get_logger

logger = get_logger(__name__)


class AICache:
    """Persistent SQLite cache for AI responses."""

    def __init__(self):
        cfg = cache_config()
        self._enabled: bool = bool(cfg.get("enabled", True))
        self._ttl: timedelta = timedelta(hours=int(cfg.get("ttl_hours", 168)))
        self._prompt_version: str = str(cfg.get("prompt_version", "v1"))

        db_path_str = cfg.get("db_path", ".ai_cache.db")
        # Resolve relative to project root (parent of ai/)
        project_root = Path(__file__).parent.parent
        self._db_path = project_root / db_path_str

        if self._enabled:
            self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_cache (
                    cache_key TEXT PRIMARY KEY,
                    response   TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON ai_cache(created_at)")
            conn.commit()

    def _make_key(self, prompt_id: str, input_data: Any) -> str:
        """Stable cache key: SHA256(prompt_version + prompt_id + serialised input)."""
        raw = json.dumps(
            {"pv": self._prompt_version, "pid": prompt_id, "data": input_data},
            sort_keys=True,
            ensure_ascii=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, prompt_id: str, input_data: Any) -> Optional[Any]:
        if not self._enabled:
            return None
        key = self._make_key(prompt_id, input_data)
        cutoff = (datetime.utcnow() - self._ttl).isoformat()
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT response FROM ai_cache WHERE cache_key = ? AND created_at > ?",
                    (key, cutoff),
                ).fetchone()
                if row:
                    logger.debug("AI cache hit", prompt_id=prompt_id)
                    return json.loads(row[0])
        except Exception as e:
            logger.warning("AI cache read error", error=str(e))
        return None

    def set(self, prompt_id: str, input_data: Any, response: Any) -> None:
        if not self._enabled:
            return
        key = self._make_key(prompt_id, input_data)
        now = datetime.utcnow().isoformat()
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO ai_cache (cache_key, response, created_at) VALUES (?, ?, ?)",
                    (key, json.dumps(response), now),
                )
                conn.commit()
        except Exception as e:
            logger.warning("AI cache write error", error=str(e))

    def purge_expired(self) -> int:
        """Delete entries older than TTL. Returns count deleted."""
        if not self._enabled:
            return 0
        cutoff = (datetime.utcnow() - self._ttl).isoformat()
        try:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute("DELETE FROM ai_cache WHERE created_at <= ?", (cutoff,))
                conn.commit()
                return cur.rowcount
        except Exception as e:
            logger.warning("AI cache purge error", error=str(e))
            return 0


# Module-level singleton
_instance: Optional[AICache] = None


def get_cache() -> AICache:
    global _instance
    if _instance is None:
        _instance = AICache()
    return _instance
