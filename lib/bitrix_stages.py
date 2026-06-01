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


def load_stages() -> Dict[str, Any]:
    return load_json_config("bitrix_stages.json")


STAGES = load_stages()


def stage(entity: str, name: str, category: Optional[str] = None) -> Dict[str, Any]:
    entity_data = STAGES[entity]
    if category is not None:
        entity_data = entity_data[category]
    return entity_data[name]


def stage_code(entity: str, name: str, category: Optional[str] = None) -> str:
    return str(stage(entity, name, category)["code"])


def stage_title(entity: str, name: str, category: Optional[str] = None) -> str:
    return str(stage(entity, name, category)["title"])


def lead_stage(name: str) -> str:
    return stage_code("lead", name)


def lead_stage_title(name: str) -> str:
    return stage_title("lead", name)


def deal_stage(name: str, category: str = "main") -> str:
    return stage_code("deal", name, category)


def deal_stage_title(name: str, category: str = "main") -> str:
    return stage_title("deal", name, category)
