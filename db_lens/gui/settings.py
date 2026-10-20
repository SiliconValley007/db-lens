"""Small persisted GUI preferences (theme, window, query defaults, recent queries) next to the profile config."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .. import store

DEFAULTS: dict[str, Any] = {"theme": "light", "geometry": "1280x820", "show_log": True, "limit": 100, "timeout": 30, "strict": False, "history": []}
MAX_HISTORY = 40


class Settings:
    def __init__(self) -> None:
        self.path = Path(store.CONFIG).parent / "gui.json"
        self.data: dict[str, Any] = dict(DEFAULTS)
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, k: str) -> Any:
        return self.data[k]

    def __setitem__(self, k: str, v: Any) -> None:
        self.data[k] = v

    def remember_query(self, sql: str) -> None:
        h = [q for q in self.data["history"] if q != sql]
        self.data["history"] = ([sql] + h)[:MAX_HISTORY]

    def save(self) -> None:
        """Atomic, best effort: a failure to save preferences must never disturb the application."""
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".gui-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass
