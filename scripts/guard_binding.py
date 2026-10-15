"""Worker launch environment binding. The complete contract is in guard/README.md."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Mapping

DEFAULT_PROTECTED_BRANCHES = ("main", "master", "develop")


@dataclass(frozen=True)
class Binding:
    issue_key: str
    stage: str
    attempt: int
    session_id: str
    root: Path
    launched_at: datetime
    protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v.strip() for v in (self.issue_key, self.stage, self.session_id)):
            raise ValueError("worker issue, stage and session binding must be non-empty")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("worker attempt must be a positive integer")
        if not self.root.is_absolute():
            raise ValueError("CHIEF_ROOT must be absolute")
        if not isinstance(self.launched_at, datetime) or self.launched_at.utcoffset() is None:
            raise ValueError("CHIEF_LAUNCHED_AT must be a timezone-aware ISO 8601 timestamp")
        if not self.protected_branches or not all(isinstance(b, str) and b.strip() for b in self.protected_branches):
            raise ValueError("CHIEF_PROTECTED_BRANCHES must be a non-empty JSON array of branch names")

    @property
    def store_path(self) -> Path:
        return self.root / "store.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Binding:
        env = os.environ if env is None else env
        try:
            branches = json.loads(env.get("CHIEF_PROTECTED_BRANCHES", json.dumps(DEFAULT_PROTECTED_BRANCHES)))
            if not isinstance(branches, list):
                raise ValueError("CHIEF_PROTECTED_BRANCHES must be a JSON array")
            root = env["CHIEF_ROOT"]
            if not root.strip():
                raise ValueError("CHIEF_ROOT must be non-empty")
            return cls(env["CHIEF_ISSUE_KEY"], env["CHIEF_STAGE"], int(env["CHIEF_ATTEMPT"]),
                       env["CHIEF_SESSION_ID"], Path(root), datetime.fromisoformat(env["CHIEF_LAUNCHED_AT"]),
                       tuple(branches))
        except KeyError as exc:
            raise ValueError(f"missing worker binding: {exc.args[0]}") from None
