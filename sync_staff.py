#!/usr/bin/env python3
import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# ================== PATHS / LOG ==================
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "sync_staff.log"
ENV_PATH = Path("/var/www/your_user/data/data_project/core/secure/.env")
SERVER_CONFIG_PYTHON_DIR = Path("/var/www/your_user/data/data_project/configs/python")
SERVER_LIB_PYTHON_DIR = Path("/var/www/your_user/data/data_project/lib/python")

config_dir_str = str(SERVER_CONFIG_PYTHON_DIR)
if config_dir_str not in sys.path:
    sys.path.insert(0, config_dir_str)

lib_dir_str = str(SERVER_LIB_PYTHON_DIR)
if lib_dir_str not in sys.path:
    sys.path.insert(0, lib_dir_str)

from app_logger import AppLogger
from bitrix_client import BitrixClient
from db import mysql_connect
from debug_utils import make_debug_log
from env_loader import env_get, env_required, load_env as shared_load_env
from runtime import is_dry_run
from staff_roles import role_ids, role_map

log = AppLogger(LOG_FILE)
debug_log = make_debug_log(log)
MOSCOW_TZ = timezone(timedelta(hours=3))
USER_MAX_PAGES = 200
DEPARTMENT_MAX_PAGES = 200
UPSERT_USER_SQL = """
    INSERT INTO staff_users
      (
        user_id,
        full_name,
        department_id,
        department_name,
        is_admin,
        is_head,
        head_department_ids,
        head_department_names,
        is_active,
        date_register,
        last_sync_at
      )
    VALUES
      (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON DUPLICATE KEY UPDATE
      full_name=VALUES(full_name),
      department_id=VALUES(department_id),
      department_name=VALUES(department_name),
      is_admin=VALUES(is_admin),
      is_head=VALUES(is_head),
      head_department_ids=VALUES(head_department_ids),
      head_department_names=VALUES(head_department_names),
      is_active=VALUES(is_active),
      date_register=VALUES(date_register),
      last_sync_at=NOW()
"""


# ================== HELPERS ==================
def parse_admins(raw: Optional[str]) -> List[int]:
    if not raw:
        return []
    ids = []
    for part in raw.split(","):
        try:
            uid = int(part.strip())
        except ValueError:
            continue
        if uid > 0:
            ids.append(uid)
    return sorted(set(ids))


def normalize_active(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 1 if value else 0

    text = str(value).strip()
    if text in {"Y", "y", "1"}:
        return 1
    if text in {"N", "n", "0", ""}:
        return 0

    log.warning("Unexpected ACTIVE value: %r", text)
    return 0


def iso_to_mysql(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(MOSCOW_TZ).replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def build_full_name(user: Dict[str, Any]) -> str:
    parts = [
        str(user.get("LAST_NAME") or "").strip(),
        str(user.get("NAME") or "").strip(),
        str(user.get("SECOND_NAME") or "").strip(),
    ]
    return " ".join(part for part in parts if part)


def int_list(values: Any) -> List[int]:
    if not isinstance(values, list):
        return []
    out = []
    for value in values:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item > 0:
            out.append(item)
    return sorted(set(out))


def pick_primary_department(department_ids: Sequence[int]) -> Optional[int]:
    if not department_ids:
        return None
    dept_ids = sorted(x for x in set(int(value) for value in department_ids) if x > 0)
    for preferred in role_ids("department_priority_ids"):
        if preferred in dept_ids:
            return preferred
    return dept_ids[0] if dept_ids else None


def department_name(department_id: Optional[int], bitrix_names: Dict[int, str]) -> Optional[str]:
    if not department_id:
        return None

    name = str(bitrix_names.get(department_id) or "").strip()
    if name:
        return name

    fallbacks = role_map("department_name_fallbacks")
    fallback = fallbacks.get(str(department_id))
    if fallback is None:
        fallback = fallbacks.get(department_id)
    if fallback is None:
        log.warning("Department name missing in Bitrix and fallback map: department_id=%s", department_id)
        return "Department #%s" % department_id
    return str(fallback)


def bitrix_call(bitrix: BitrixClient, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        return bitrix.call(method, params or {}, timeout=60, max_retries=5, backoff_base_sec=2.0)
    except Exception as exc:
        raise RuntimeError("Bitrix API call failed: %s %s" % (method, str(exc)[:500])) from exc


def paginate_users(
    bitrix: BitrixClient,
    extra_params: Optional[Dict[str, Any]] = None,
    max_pages: int = USER_MAX_PAGES,
) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    start = 0

    for _ in range(max_pages):
        params = {
            "order": {"ID": "ASC"},
            "select": [
                "ID", "ACTIVE", "NAME", "LAST_NAME", "SECOND_NAME",
                "DATE_REGISTER", "UF_DEPARTMENT",
            ],
            "start": start,
        }
        params.update(extra_params or {})
        data = bitrix_call(bitrix, "user.get", params)
        items = data.get("result", [])
        if not isinstance(items, list) or not items:
            return result

        for user in items:
            if not isinstance(user, dict):
                continue
            try:
                uid = int(user.get("ID"))
            except (TypeError, ValueError):
                continue
            if uid > 0:
                result[uid] = user

        next_value = data.get("next")
        if next_value is None:
            return result
        start = int(next_value)

    raise RuntimeError("MAX_PAGES reached in paginate_users")


def fetch_users_all(bitrix: BitrixClient) -> List[Dict[str, Any]]:
    users: Dict[int, Dict[str, Any]] = {}
    for active_value in ("Y", "N"):
        users.update(paginate_users(bitrix, {"filter": {"ACTIVE": active_value}}))

    if not users:
        log.warning("No users from ACTIVE filters, trying without filter")
        users = paginate_users(bitrix)

    return [users[uid] for uid in sorted(users)]


def fetch_departments_all(bitrix: BitrixClient) -> List[Dict[str, Any]]:
    departments: Dict[int, Dict[str, Any]] = {}
    start = 0

    for _ in range(DEPARTMENT_MAX_PAGES):
        data = bitrix_call(bitrix, "department.get", {"start": start})
        items = data.get("result", [])
        if not isinstance(items, list) or not items:
            return [departments[dept_id] for dept_id in sorted(departments)]

        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                dept_id = int(item.get("ID") or 0)
            except (TypeError, ValueError):
                continue
            if dept_id <= 0:
                continue
            departments[dept_id] = {
                "ID": dept_id,
                "NAME": str(item.get("NAME") or "").strip(),
                "UF_HEAD": int(item.get("UF_HEAD") or 0),
                "PARENT": int(item.get("PARENT") or 0),
            }

        next_value = data.get("next")
        if next_value is None:
            return [departments[dept_id] for dept_id in sorted(departments)]
        start = int(next_value)

    raise RuntimeError("MAX_PAGES reached in fetch_departments_all")


def build_department_maps(departments: Sequence[Dict[str, Any]]) -> Tuple[Dict[int, str], Dict[int, List[int]]]:
    names: Dict[int, str] = {}
    heads: Dict[int, List[int]] = {}

    for department in departments:
        dept_id = int(department.get("ID") or 0)
        if dept_id <= 0:
            continue
        dept_name = str(department.get("NAME") or "").strip()
        if dept_name:
            names[dept_id] = dept_name
        head_id = int(department.get("UF_HEAD") or 0)
        if head_id > 0:
            heads.setdefault(head_id, []).append(dept_id)

    for uid, dept_ids in list(heads.items()):
        heads[uid] = sorted(set(dept_ids))

    return names, heads


def build_user_row(
    user: Dict[str, Any],
    admins: Set[int],
    bitrix_department_names: Dict[int, str],
    user_heads: Dict[int, List[int]],
) -> Optional[Tuple[Any, ...]]:
    try:
        uid = int(user.get("ID") or 0)
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None

    full_name = build_full_name(user) or "ID %s" % uid
    dept_ids = int_list(user.get("UF_DEPARTMENT"))
    primary_dept = pick_primary_department(dept_ids)
    primary_dept_name = department_name(primary_dept, bitrix_department_names) or ""

    if len(dept_ids) > 1:
        debug_log(
            "sync_staff",
            "multi_department_user",
            {
                "user_id": uid,
                "dept_ids": dept_ids,
                "picked": primary_dept,
                "picked_name": primary_dept_name,
            },
        )

    headed_dept_ids = user_heads.get(uid, [])
    headed_dept_names = []
    for dept_id in headed_dept_ids:
        name = department_name(dept_id, bitrix_department_names)
        if name is not None:
            headed_dept_names.append(name)

    if headed_dept_ids:
        debug_log(
            "sync_staff",
            "department_head_detected",
            {
                "user_id": uid,
                "head_department_ids": headed_dept_ids,
                "head_department_names": headed_dept_names,
            },
        )

    return (
        uid,
        full_name,
        primary_dept,
        primary_dept_name,
        1 if uid in admins else 0,
        1 if headed_dept_ids else 0,
        ",".join(str(x) for x in headed_dept_ids),
        " | ".join(headed_dept_names) if headed_dept_names else None,
        normalize_active(user.get("ACTIVE")),
        iso_to_mysql(user.get("DATE_REGISTER")),
    )


# ================== MAIN ==================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Bitrix staff users into staff_users")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=is_dry_run(),
        help="Run sync and rollback DB changes",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    t0 = time.time()
    log.info("=== sync_staff START ===")
    if args.dry_run:
        log.info("DRY RUN enabled: DB changes will be rolled back")

    conn = None
    cursor = None
    try:
        env = shared_load_env(ENV_PATH)
        webhook = env_required(env, "STAFF_WEBHOOK")
        admins = set(parse_admins(env_get(env, "STAFF_ADMINS", "")))
        log.info("admins loaded: %s", sorted(admins))

        conn = mysql_connect(env)
        cursor = conn.cursor()

        bitrix = BitrixClient(
            webhook,
            timeout=90,
            max_retries=5,
            backoff_base_sec=2.0,
            rps_sleep_sec=0.5,
            logger=log,
        )

        departments = fetch_departments_all(bitrix)
        department_names, user_heads = build_department_maps(departments)
        log.info("departments fetched: count=%s heads=%s", len(departments), len(user_heads))

        users = fetch_users_all(bitrix)
        log.info("users fetched: count=%s", len(users))

        rows = []
        multi_dept_count = 0
        heads_count = 0

        for user in users:
            dept_count = len(int_list(user.get("UF_DEPARTMENT")))
            row = build_user_row(user, admins, department_names, user_heads)
            if row is None:
                continue
            if user_heads.get(int(row[0]), []):
                heads_count += 1
            if dept_count > 1:
                multi_dept_count += 1
            rows.append(row)

        if rows:
            cursor.executemany(UPSERT_USER_SQL, rows)

        if args.dry_run:
            conn.rollback()
            log.info("DRY RUN rollback complete: processed=%s", len(rows))
        else:
            conn.commit()

        dt = int(time.time() - t0)
        log.info(
            "=== sync_staff END OK (%ss, processed=%s, multi_dept_logged=%s, heads_logged=%s) ===",
            dt,
            len(rows),
            multi_dept_count,
            heads_count,
        )
        return 0

    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception as rollback_error:
                log.error("rollback failed: %s", rollback_error)
        log.exception("FATAL: %s", exc)
        return 1
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
