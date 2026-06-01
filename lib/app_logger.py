#!/usr/bin/env python3

import json
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEBUG_ENV_PATH = Path("/var/www/your_user/data/data_project/debug/debug.env")
MAX_LOG_SIZE_BYTES = 5242880
LOG_TAIL_SIZE_BYTES = 2621440
DEBUG_CACHE_TTL_SEC = 30.0
_debug_cache: Tuple[Optional[bool], float] = (None, 0.0)


def is_debug_enabled(path: Path = DEBUG_ENV_PATH) -> bool:
    global _debug_cache

    cached_value, cached_at = _debug_cache
    now = time.monotonic()
    if cached_value is not None and now - cached_at < DEBUG_CACHE_TTL_SEC:
        return cached_value

    if not path.is_file():
        _debug_cache = (False, now)
        return False

    result = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "app_debug":
            result = value.strip().strip("'\"") == "1"
            break

    _debug_cache = (result, now)
    return result


def ensure_log_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o775)
    except OSError:
        pass


def rotate_if_needed(
    path: Path,
    max_size: int = MAX_LOG_SIZE_BYTES,
    tail_size: int = LOG_TAIL_SIZE_BYTES,
) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return

    if size < max_size:
        return

    with path.open("rb") as fh:
        fh.seek(max(0, size - tail_size))
        tail = fh.read()
    with path.open("wb") as fh:
        fh.write(tail)


def _json_context(ctx: Optional[Dict[str, Any]]) -> str:
    if not ctx:
        return ""
    return " | " + json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))


class AppLogger:
    def __init__(self, log_file: Path, debug_log_file: Optional[Path] = None):
        self.log_file = log_file
        self.debug_log_file = debug_log_file or log_file.with_name(
            "%s_debug%s" % (log_file.stem, log_file.suffix)
        )
        ensure_log_dir(self.log_file.parent)

    def _write(self, message: str, ctx: Optional[Dict[str, Any]] = None) -> None:
        # Open/close per write is acceptable for short-lived cron scripts.
        # Long-running daemons should use a persistent file handler.
        rotate_if_needed(self.log_file)
        line = "[%s] %s%s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            message,
            _json_context(ctx),
        )
        with self.log_file.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def write(self, message: str, ctx: Optional[Dict[str, Any]] = None) -> None:
        self._write(message, ctx)

    def _format(self, message: str, args: Tuple[Any, ...]) -> str:
        if not args:
            return message
        try:
            return message % args
        except Exception:
            return "%s %s" % (message, " ".join(str(arg) for arg in args))

    def info(self, message: str, *args: Any, ctx: Optional[Dict[str, Any]] = None) -> None:
        self._write("[INFO] " + self._format(message, args), ctx)

    def warning(self, message: str, *args: Any, ctx: Optional[Dict[str, Any]] = None) -> None:
        self._write("[WARN] " + self._format(message, args), ctx)

    def error(self, message: str, *args: Any, ctx: Optional[Dict[str, Any]] = None) -> None:
        self._write("[ERROR] " + self._format(message, args), ctx)

    def exception(self, message: str, *args: Any, ctx: Optional[Dict[str, Any]] = None) -> None:
        text = self._format(message, args)
        trace = traceback.format_exc()
        self._write("[ERROR] %s\n%s" % (text, trace), ctx)

    def debug(self, scope: str, message: str, ctx: Optional[Dict[str, Any]] = None) -> None:
        if not is_debug_enabled():
            return

        rotate_if_needed(self.debug_log_file)
        line = "[%s] [DEBUG] [%s] %s%s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            scope,
            message,
            _json_context(ctx),
        )
        with self.debug_log_file.open("a", encoding="utf-8") as fh:
            fh.write(line)
