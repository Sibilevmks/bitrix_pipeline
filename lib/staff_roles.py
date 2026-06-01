#!/usr/bin/env python3

import sys
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIB_PYTHON_DIR = PROJECT_ROOT / "lib" / "python"

lib_dir_str = str(LIB_PYTHON_DIR)
if lib_dir_str not in sys.path:
    sys.path.insert(0, lib_dir_str)

from config_loader import load_json_config
from runtime import is_truthy_value


def load_staff_roles() -> Dict[str, Any]:
    return load_json_config("staff_roles.json")


STAFF_ROLES = load_staff_roles()


def role_ids(name: str) -> List[int]:
    values = STAFF_ROLES.get(name, [])
    ids = []
    for value in values:
        try:
            role_id = int(value)
        except (TypeError, ValueError):
            continue
        if role_id > 0:
            ids.append(role_id)
    return sorted(set(ids))


def role_flag(name: str, default: bool = False) -> bool:
    value = STAFF_ROLES.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return is_truthy_value(str(value))


def role_map(name: str) -> Dict[str, Any]:
    values = STAFF_ROLES.get(name, {})
    return values if isinstance(values, dict) else {}
