#!/usr/bin/env python3

import random
import time
from typing import Any, Callable, Dict, List, Optional

import requests

try:
    from debug_utils import make_debug_log as _make_debug_log
except ImportError:
    _make_debug_log = None


class BitrixClient:
    def __init__(
        self,
        webhook: str,
        timeout: int = 60,
        max_retries: int = 5,
        backoff_base_sec: float = 2.0,
        rps_sleep_sec: float = 0.5,
        logger: Optional[Any] = None,
        debug_log: Optional[Callable[[str, str, Optional[Dict[str, Any]]], None]] = None,
    ):
        # Keep both names for compatibility with existing scripts.
        self.webhook_url = webhook
        self.base = webhook.rstrip("/") + "/"
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base_sec = backoff_base_sec
        self.rps_sleep_sec = rps_sleep_sec
        self.logger = logger
        self._debug_log = debug_log or (
            _make_debug_log(logger) if (_make_debug_log and logger) else None
        )
        self.session = requests.Session()

    def _debug(self, scope: str, message: str, ctx: Optional[Dict[str, Any]] = None) -> None:
        if self._debug_log:
            self._debug_log(scope, message, ctx or {})

    def _warn(self, message: str, *args: Any) -> None:
        if self.logger and hasattr(self.logger, "warning"):
            self.logger.warning(message, *args)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "BitrixClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _wait(
        self,
        attempt: int,
        retry_after: Optional[str] = None,
        backoff_base_sec: Optional[float] = None,
    ) -> None:
        if retry_after and retry_after.isdigit():
            wait_sec = int(retry_after)
        else:
            base = self.backoff_base_sec if backoff_base_sec is None else backoff_base_sec
            wait_sec = base * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
        time.sleep(wait_sec)

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        backoff_base_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = self.base + method.strip().lstrip("/") + ".json"
        payload = params or {}
        last_error = ""
        timeout_value = self.timeout if timeout is None else timeout
        retries_value = self.max_retries if max_retries is None else max_retries
        backoff_value = self.backoff_base_sec if backoff_base_sec is None else backoff_base_sec

        for attempt in range(1, retries_value + 1):
            try:
                response = self.session.post(url, json=payload, timeout=timeout_value)
                status_code = response.status_code

                try:
                    data = response.json()
                except ValueError:
                    last_error = (response.text or "")[:300]
                    self._debug(
                        "bitrix",
                        "non_json_response",
                        {"method": method, "attempt": attempt, "status": status_code},
                    )
                    if status_code == 429 or 500 <= status_code < 600:
                        self._warn(
                            "Bitrix retry non-json method=%s status=%s attempt=%s/%s",
                            method,
                            status_code,
                            attempt,
                            retries_value,
                        )
                        self._wait(attempt, response.headers.get("Retry-After"), backoff_value)
                        continue
                    raise RuntimeError("Bitrix non-json response: %s" % last_error)

                if status_code == 429 or 500 <= status_code < 600:
                    self._warn(
                        "Bitrix retry status method=%s status=%s attempt=%s/%s",
                        method,
                        status_code,
                        attempt,
                        retries_value,
                    )
                    self._debug(
                        "bitrix",
                        "retry_status",
                        {"method": method, "attempt": attempt, "status": status_code},
                    )
                    self._wait(attempt, response.headers.get("Retry-After"), backoff_value)
                    continue

                if isinstance(data, dict) and "error" in data:
                    error_code = str(data.get("error", ""))
                    error_desc = str(data.get("error_description", ""))
                    if error_code in ("QUERY_LIMIT_EXCEEDED",):
                        self._warn(
                            "Bitrix retry app_error method=%s error=%s attempt=%s/%s",
                            method,
                            error_code,
                            attempt,
                            retries_value,
                        )
                        self._debug(
                            "bitrix",
                            "retry_app_error",
                            {"method": method, "attempt": attempt, "error": error_code},
                        )
                        self._wait(attempt, backoff_base_sec=backoff_value)
                        continue
                    raise RuntimeError("Bitrix error in %s: %s %s" % (method, error_code, error_desc))

                self._debug(
                    "bitrix",
                    "call_ok",
                    {
                        "method": method,
                        "attempt": attempt,
                        "status": status_code,
                        "has_next": isinstance(data, dict) and "next" in data,
                    },
                )
                time.sleep(self.rps_sleep_sec)
                return data if isinstance(data, dict) else {"result": data}

            except requests.RequestException as exc:
                last_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
                self._warn(
                    "Bitrix request error method=%s error=%s attempt=%s/%s, retry",
                    method,
                    type(exc).__name__,
                    attempt,
                    retries_value,
                )
                self._debug(
                    "bitrix",
                    "request_error",
                    {"method": method, "attempt": attempt, "error": type(exc).__name__},
                )
                self._wait(attempt, backoff_base_sec=backoff_value)

        raise RuntimeError(
            "Bitrix call failed after %s attempts: %s %s"
            % (retries_value, method, last_error)
        )

    def list_all(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        result_key: Optional[str] = None,
        max_pages: int = 1000,
    ) -> List[Any]:
        payload = dict(params or {})
        payload.setdefault("start", 0)
        rows: List[Any] = []

        for page_num in range(1, max_pages + 1):
            data = self.call(method, payload)
            result = data.get("result", [])
            if result_key and isinstance(result, dict):
                items = result.get(result_key, [])
            else:
                items = result

            if isinstance(items, list):
                rows.extend(items)
            elif items:
                rows.append(items)

            next_value = data.get("next")
            self._debug(
                "bitrix",
                "page_loaded",
                {
                    "method": method,
                    "next": next_value,
                    "count": len(items) if isinstance(items, list) else 1,
                },
            )
            if page_num % 10 == 0:
                self._warn(
                    "Bitrix list_all progress method=%s pages=%s rows=%s",
                    method,
                    page_num,
                    len(rows),
                )
            if next_value is None:
                return rows

            try:
                payload["start"] = int(next_value)
            except (TypeError, ValueError):
                raise RuntimeError(
                    "Bitrix pagination invalid next for %s: %r" % (method, next_value)
                )

        raise RuntimeError("Bitrix pagination max_pages reached: %s" % method)
