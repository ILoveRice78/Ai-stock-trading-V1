"""On-disk state: journal, order tickets, paper portfolio, equity history, kill switch.

Standard library only (imported by the trade guard hook).
Everything lives in ./state/ (git-ignored) unless TRADER_STATE_DIR is set.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


def state_dir() -> Path:
    path = Path(os.environ.get("TRADER_STATE_DIR", PROJECT_ROOT / "state"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def journal_path() -> Path:
    return state_dir() / "journal.jsonl"


def tickets_path() -> Path:
    return state_dir() / "tickets.json"


def paper_portfolio_path() -> Path:
    return state_dir() / "paper_portfolio.json"


def live_account_path() -> Path:
    return state_dir() / "live_account.json"


def equity_history_path() -> Path:
    return state_dir() / "equity_history.jsonl"


def kill_switch_path() -> Path:
    return state_dir() / "KILL_SWITCH"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    """Atomic write so a crash never leaves a half-written state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def log_event(event: str, **data: Any) -> dict[str, Any]:
    record = {"ts": utcnow().isoformat(), "event": event, **data}
    append_jsonl(journal_path(), record)
    return record


def read_journal() -> list[dict[str, Any]]:
    return read_jsonl(journal_path())


def kill_switch_active() -> tuple[bool, str]:
    path = kill_switch_path()
    if path.exists():
        reason = path.read_text(encoding="utf-8").strip() or "no reason given"
        return True, reason
    return False, ""


def set_kill_switch(reason: str) -> None:
    kill_switch_path().write_text(f"{utcnow().isoformat()} {reason}\n", encoding="utf-8")
    log_event("kill_switch_on", reason=reason)


def clear_kill_switch() -> bool:
    path = kill_switch_path()
    if path.exists():
        path.unlink()
        log_event("kill_switch_off")
        return True
    return False
