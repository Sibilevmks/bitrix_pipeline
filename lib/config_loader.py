#!/usr/bin/env python3

import json
from pathlib import Path
from typing import Any, Dict


CONFIG_ROOT = Path("/var/www/your_user/data/data_project/configs")
_cache: Dict[str, Dict[str, Any]] = {}


def config_path(name: str) -> Path:
    clean_name = name.strip().lstrip("/\\")
    if not clean_name:
        raise ValueError("Config name is empty")
    if not clean_name.endswith(".json"):
        clean_name = clean_name + ".json"

    return CONFIG_ROOT / clean_name


def load_json_config(name: str) -> Dict[str, Any]:
    path = config_path(name)
    cache_key = str(path)
    if cache_key in _cache:
        return _cache[cache_key]

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise RuntimeError("Config must be a JSON object: %s" % path)

    _cache[cache_key] = data
    return data
