"""Paths and small shared helpers."""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

APP_NAME = "web-modder"
TOOLKIT_REPO = os.environ.get("WM_TOOLKIT_REPO", "https://github.com/rehan-remade/universal-modder")


def data_dir() -> Path:
    """Everything the app stores: keys, sessions, the universal-modder checkout. Override with WM_HOME."""
    d = Path(os.environ.get("WM_HOME", Path.home() / ".web-modder")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def sessions_dir() -> Path:
    d = data_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def toolkit_dir() -> Path:
    """Where universal-modder lives. WM_TOOLKIT points at an existing clone instead."""
    if os.environ.get("WM_TOOLKIT"):
        return Path(os.environ["WM_TOOLKIT"]).expanduser().resolve()
    return data_dir() / "universal-modder"


_write_lock = threading.Lock()


def write_json(path: Path, obj, private: bool = False) -> None:
    """Atomic write: a crash mid-save never leaves a half-written keys or session file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _write_lock:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        if private:
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def access_token() -> str:
    """The token the browser must present. Stable across restarts so bookmarks keep working."""
    if os.environ.get("WM_TOKEN"):
        return os.environ["WM_TOKEN"]
    p = data_dir() / "token.txt"
    if p.exists():
        t = p.read_text().strip()
        if t:
            return t
    t = secrets.token_urlsafe(24)
    p.write_text(t)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return t
