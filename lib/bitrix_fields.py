#!/usr/bin/env python3

import sys
from pathlib import Path
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIB_PYTHON_DIR = PROJECT_ROOT / "lib" / "python"

lib_dir_str = str(LIB_PYTHON_DIR)
if lib_dir_str not in sys.path:
    sys.path.insert(0, lib_dir_str)

from config_loader import load_json_config


def load_fields() -> Dict[str, Any]:
    return load_json_config("bitrix_fields.json")


FIELDS = load_fields()


def entity_config(entity: str) -> Dict[str, Any]:
    return FIELDS[entity]


def entity_type_id(entity: str) -> Optional[int]:
    value = entity_config(entity).get("entity_type_id")
    return int(value) if value is not None else None


def field(entity: str, name: str) -> Dict[str, Any]:
    return entity_config(entity)["fields"][name]


def field_code(entity: str, name: str) -> str:
    return str(field(entity, name)["code"])


def field_item_code(entity: str, name: str) -> str:
    item_code = field(entity, name).get("item_code")
    return str(item_code) if item_code else field_code(entity, name)
