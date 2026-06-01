#!/usr/bin/env python3

import os
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_ENV_PATH = Path("/var/www/your_user/data/data_project/core/secure/.env")
AI_CALL_ENV_PATH = Path("/var/www/your_user/data/data_project/core/secure/ai_call.env")


def load_env(path: Path = DEFAULT_ENV_PATH) -> Dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(".env not found: %s" % path)

    env: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("'\"")
    return env


def load_env_with_ai_call(
    base_path: Path = DEFAULT_ENV_PATH,
    ai_call_path: Path = AI_CALL_ENV_PATH,
) -> Dict[str, str]:
    env = load_env(base_path)
    env.update(load_env(ai_call_path))
    return env


def env_get(env: Dict[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    value = env.get(key)
    if value is None:
        value = os.getenv(key)
    if value is None:
        return default

    text = str(value).strip()
    if text == "":
        return default

    return text


def env_required(env: Dict[str, str], key: str) -> str:
    value = env_get(env, key)
    if not value:
        raise RuntimeError("ENV missing %s" % key)
    return value


def env_required_or_warn(
    env: Dict[str, str],
    key: str,
    dry_run: bool,
    logger: Any,
) -> str:
    if dry_run:
        value = env_get(env, key, "") or ""
        if not value:
            logger.warning("%s not set (dry-run, skipped)", key)
        return value

    return env_required(env, key)
