from __future__ import annotations

from typing import Any, Dict, List

from backend.app.database import Database


class WatchlistService:
    def __init__(self, database: Database):
        self.database = database

    def list(self) -> List[Dict[str, Any]]:
        return self.database.list_watchlist()

    def add(self, code: str, name: str = "", note: str = "") -> Dict[str, Any]:
        normalized = code.strip().upper()
        if not normalized:
            raise ValueError("code is required")
        return self.database.add_watchlist(normalized, name.strip(), note.strip())

    def remove(self, code: str) -> None:
        self.database.remove_watchlist(code.strip().upper())

