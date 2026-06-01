#!/usr/bin/env python3
import argparse
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter
from typing import Any, Dict, List, Optional, Set

# ================== PATHS / LOG ==================
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "mpb_deals.log"
ENV_PATH = Path("/var/www/your_user/data/data_project/core/secure/.env")
SERVER_CONFIG_PYTHON_DIR = Path("/var/www/your_user/data/data_project/configs")
SERVER_LIB_PYTHON_DIR = Path("/var/www/your_user/data/data_project/lib")

config_dir_str = str(SERVER_CONFIG_PYTHON_DIR)
if config_dir_str not in sys.path:
    sys.path.insert(0, config_dir_str)

lib_dir_str = str(SERVER_LIB_PYTHON_DIR)
if lib_dir_str not in sys.path:
    sys.path.insert(0, lib_dir_str)

from app_logger import AppLogger
from bitrix_client import BitrixClient
from debug_utils import make_debug_log
from bitrix_fields import field_code
from bitrix_stages import lead_stage
from db import mysql_connect
from env_loader import env_required, load_env as shared_load_env
from runtime import is_dry_run
from staff_roles import role_flag, role_ids

log = AppLogger(LOG_FILE)
debug_log = make_debug_log(log)

# ================== CONST / FIELDS ==================
UF_KV = field_code("deal", "kv")
UF_MOP = field_code("deal", "mop")
UF_REASON = field_code("deal", "reason_close")
UF_BANK = field_code("deal", "bank")
UF_PRODUCT = field_code("deal", "product_type")
UF_INN = field_code("deal", "inn")
UF_CLOSE_DATE = field_code("deal", "close_date")
UF_TRANSFER_DATE = field_code("deal", "mpb_transfer_date")
UF_APPROVED_DATE = field_code("deal", "approved_date")
UF_MPB_WORK_DATE = field_code("deal", "mpb_work_date")
UF_DO_TRANSFER = field_code("deal", "do_transfer_date")
UF_DOCS_REWORK = field_code("deal", "docs_rework_date")
UF_CLIENT_SIGN = field_code("deal", "client_sign_date")
UF_BANKS_REVIEW = field_code("deal", "banks_review_date")
UF_FLAG_SPECIAL = field_code("deal", "flag_special")

COLD_STAGE = lead_stage("cold")
WIN_STAGE = lead_stage("winner")
CALL_SYNC_LOOKBACK_HOURS = 2
USER_RESOLVE_CHUNK_SIZE = 50

def load_managers(cursor: Any) -> Dict[int, str]:
    sales_department_ids = set(role_ids("sales_department_ids"))
    if not sales_department_ids:
        raise RuntimeError("staff_roles.json: sales_department_ids is empty")

    cursor.execute(
        """
        SELECT user_id, full_name, department_id
        FROM staff_users
        ORDER BY user_id ASC
        """
    )
    rows = cursor.fetchall() or []
    if not rows:
        raise RuntimeError("staff_users is empty or unavailable")

    staff_by_id: Dict[int, Dict[str, Any]] = {}
    for user_id, full_name, department_id in rows:
        try:
            uid = int(user_id)
        except Exception:
            continue
        if uid <= 0:
            continue

        try:
            dept_id = int(department_id or 0)
        except Exception:
            dept_id = 0

        staff_by_id[uid] = {
            "full_name": str(full_name or "").strip() or f"ID {uid}",
            "department_id": dept_id,
        }

    historical_mop_ids: Set[int] = set()
    if role_flag("include_sales_users_with_mpb_deals", True):
        cursor.execute(
            """
            SELECT DISTINCT mop_id
            FROM mpb_deals
            WHERE mop_id IS NOT NULL
              AND mop_id <> 0
            """
        )
        for (mop_id,) in cursor.fetchall() or []:
            try:
                historical_mop_ids.add(int(mop_id))
            except Exception:
                continue

    managers: Dict[int, str] = {}
    for uid, staff in staff_by_id.items():
        dept_id = int(staff["department_id"])
        if dept_id in sales_department_ids or uid in historical_mop_ids:
            managers[uid] = str(staff["full_name"])

    if not managers:
        raise RuntimeError("No managers loaded from staff_users")

    log.info(
        "Managers loaded from staff_users: %s (sales_departments=%s, historical=%s)",
        len(managers),
        sorted(sales_department_ids),
        len(historical_mop_ids),
    )
    return managers

# ================== BITRIX ==================
def bx_call(
    bitrix: BitrixClient,
    method: str,
    data: Optional[dict] = None,
    timeout: int = 60,
    retries: int = 3,
    sleep_sec: int = 5,
    raise_on_error: bool = False,
) -> Any:
    raw = bx_call_raw(bitrix, method, data, timeout, retries, sleep_sec, raise_on_error)
    return raw.get("result") if isinstance(raw, dict) else raw

def bx_call_raw(
    bitrix: BitrixClient,
    method: str,
    data: Optional[dict] = None,
    timeout: int = 60,
    retries: int = 3,
    sleep_sec: int = 5,
    raise_on_error: bool = False,
) -> Dict[str, Any]:
    params = data or {}
    debug_log(
        "bitrix",
        "call_start",
        {"method": method, "timeout": timeout, "retries": retries},
    )
    try:
        return bitrix.call(
            method,
            params,
            timeout=timeout,
            max_retries=retries,
            backoff_base_sec=sleep_sec,
        )
    except Exception as exc:
        log.error("BX %s failed: %s", method, str(exc)[:500])
        if raise_on_error:
            raise
        return {"result": []}

# ================== HELPERS ==================
def to_date_str(v: Any) -> Optional[str]:
    if not v:
        return None
    s = str(v).strip()[:10]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s[:10]
    except ValueError:
        return None

def norm_emp_id(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        if not v:
            return None
        v = v[0]
        if v is None:
            return None
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    if s.startswith("user_"):
        try:
            return int(s.split("_", 1)[1])
        except Exception:
            return None
    return None

def to_flag(v: Any) -> int:
    return 1 if str(v).strip().upper() in {"Y", "1", "TRUE", "YES", "ON"} else 0

def normalized_inn(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None

def ensure_calls_call_id_unique(cursor: Any) -> None:
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 'calls'
          AND column_name = 'call_id'
          AND non_unique = 0
        """
    )
    row = cursor.fetchone()
    has_unique = int((row[0] if row else 0) or 0) > 0
    if not has_unique:
        raise RuntimeError("calls.call_id must have a UNIQUE index for idempotent upsert")

def parse_bx_dt(s: Any) -> Optional[datetime]:
    if not s:
        return None
    ss = str(s).strip()
    try:
        parsed = datetime.fromisoformat(ss.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except Exception:
        try:
            return datetime.strptime(ss[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

def normalize_vox_rows(res: Any) -> List[dict]:
    if isinstance(res, dict):
        res = res.get("items") or res.get("result") or []
    if not isinstance(res, list):
        return []
    return [x for x in res if isinstance(x, dict)]

def get_field_definitions(bitrix: BitrixClient) -> Dict[str, Dict[str, Any]]:
    needed = {UF_MOP, UF_REASON, UF_BANK, UF_PRODUCT}
    defs: Dict[str, Dict[str, Any]] = {}
    rows = bx_call(bitrix, "crm.deal.userfield.list", {}, timeout=90, raise_on_error=True) or []
    for f in rows:
        name = f.get("FIELD_NAME")
        if name in needed:
            defs[name] = {"id": f.get("ID"), "type": f.get("USER_TYPE_ID")}
    return defs

def load_reference_values(bitrix: BitrixClient) -> Dict[str, Any]:
    defs = get_field_definitions(bitrix)
    refs: Dict[str, Any] = {}
    for code, meta in defs.items():
        f_id = meta.get("id")
        f_type = meta.get("type")
        if not f_id:
            refs[code] = {}
            continue
        if f_type == "employee":
            refs[code] = "employee"
            continue
        if f_type == "enumeration":
            r = bx_call(bitrix, "crm.deal.userfield.get", {"id": f_id}, timeout=90, raise_on_error=True) or {}
            lst = r.get("LIST") if isinstance(r, dict) else None
            if isinstance(lst, list):
                refs[code] = {str(i.get("ID")): str(i.get("VALUE")) for i in lst}
            else:
                refs[code] = {}
            continue
        refs[code] = {}
    return refs

def resolve_users(bitrix: BitrixClient, user_ids: Set[str]) -> Dict[str, str]:
    if not user_ids:
        return {}
    ids = sorted({str(x).strip() for x in user_ids if str(x).strip()})
    out: Dict[str, str] = {}
    for idx in range(0, len(ids), USER_RESOLVE_CHUNK_SIZE):
        chunk = ids[idx:idx + USER_RESOLVE_CHUNK_SIZE]
        res = bx_call(bitrix, "user.get", {"ID": chunk}, timeout=60, raise_on_error=True) or []
        if not isinstance(res, list):
            continue
        for u in res:
            uid = u.get("ID")
            if uid is None:
                continue
            out[str(uid)] = f"{(u.get('NAME') or '').strip()} {(u.get('LAST_NAME') or '').strip()}".strip()
    return out

def load_stage_names(bitrix: BitrixClient) -> Dict[str, str]:
    rows = bx_call(bitrix, "crm.status.list", {}, timeout=90, raise_on_error=True) or []
    out: Dict[str, str] = {}
    for item in rows:
        ent = str(item.get("ENTITY_ID") or "")
        if ent.startswith("DEAL_STAGE"):
            out[str(item.get("STATUS_ID"))] = str(item.get("NAME") or "")
    return out

# ================== STEPS ==================
def step_deals(bitrix: BitrixClient, cursor: Any) -> None:
    log.info("=== STEP 1: reference data + deals ===")
    refs = load_reference_values(bitrix)
    stage_names = load_stage_names(bitrix)
    
    log.info("Loading deals...")
    start = 0
    deals_buffer: List[dict] = []
    user_ids_needed: Set[str] = set()
    loaded_ids: Set[str] = set()
    pages = 0
    max_pages = 1000
    
    while pages < max_pages:
        params = {
            "filter": {"!STAGE_ID": "NEW"},
            "select": [
                "ID", "TITLE", "DATE_CREATE", "STAGE_ID", "STAGE_SEMANTIC_ID",
                "OPPORTUNITY", "ASSIGNED_BY_ID",
                UF_KV, UF_MOP, UF_REASON, UF_BANK, UF_PRODUCT, UF_INN,
                UF_CLOSE_DATE, UF_TRANSFER_DATE, UF_APPROVED_DATE,
                UF_MPB_WORK_DATE, UF_DO_TRANSFER, UF_DOCS_REWORK, UF_CLIENT_SIGN, UF_BANKS_REVIEW,
                UF_FLAG_SPECIAL
            ],
            "order": {"ID": "ASC"},
            "start": start
        }
        debug_log("bitrix", "deal_page_request", {"start": start})
        raw = bx_call_raw(bitrix, "crm.deal.list", params, timeout=90, raise_on_error=True)
        deals = raw.get("result", [])
        if not isinstance(deals, list) or not deals:
            break
        debug_log("mpb_deals", "deal_page_loaded", {"start": start, "count": len(deals)})
        
        ids = {str(d.get("ID")) for d in deals if d.get("ID") is not None}
        if ids and ids.issubset(loaded_ids):
            raise RuntimeError("crm.deal.list returned already processed ids")
        loaded_ids.update(ids)
        
        for d in deals:
            if refs.get(UF_MOP) == "employee" and d.get(UF_MOP):
                user_ids_needed.add(str(d.get(UF_MOP)))
            if d.get("ASSIGNED_BY_ID"):
                user_ids_needed.add(str(d.get("ASSIGNED_BY_ID")))
            deals_buffer.append(d)
        
        pages += 1
        if pages % 10 == 0:
            log.info("Deals progress: %s deals, %s pages", len(deals_buffer), pages)

        next_value = raw.get("next")
        if next_value is None:
            break
        start = int(next_value)
    
    if pages >= max_pages:
        raise RuntimeError("MAX_PAGES reached for deals")
    
    log.info("Deals in buffer: %s", len(deals_buffer))
    user_map = resolve_users(bitrix, user_ids_needed)
    inn_values = [
        inn
        for inn in (normalized_inn(d.get(UF_INN)) for d in deals_buffer)
        if inn
    ]
    inn_counter = Counter(inn_values)
    
    q = """
        INSERT INTO mpb_deals (
            id, title, date_create, stage_name, stage_semantic_id, id_stage,
            amount, kv, mop, mop_id, reason_close, bank, product_type, inn, client_type,
            responsible_id, responsible_name, close_date, mpb_transfer_date, approved_date,
            mpb_work_date, do_transfer_date, docs_rework_date, client_sign_date, banks_review_date,
            flag_special
        ) VALUES (
            %s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,
            %s
        )
        ON DUPLICATE KEY UPDATE
            title=VALUES(title),
            date_create=VALUES(date_create),
            stage_name=VALUES(stage_name),
            stage_semantic_id=VALUES(stage_semantic_id),
            id_stage=VALUES(id_stage),
            amount=VALUES(amount),
            kv=VALUES(kv),
            mop=VALUES(mop),
            mop_id=VALUES(mop_id),
            reason_close=VALUES(reason_close),
            bank=VALUES(bank),
            product_type=VALUES(product_type),
            inn=VALUES(inn),
            client_type=VALUES(client_type),
            responsible_id=VALUES(responsible_id),
            responsible_name=VALUES(responsible_name),
            close_date=VALUES(close_date),
            mpb_transfer_date=VALUES(mpb_transfer_date),
            approved_date=VALUES(approved_date),
            mpb_work_date=VALUES(mpb_work_date),
            do_transfer_date=VALUES(do_transfer_date),
            docs_rework_date=VALUES(docs_rework_date),
            client_sign_date=VALUES(client_sign_date),
            banks_review_date=VALUES(banks_review_date),
            flag_special=VALUES(flag_special)
    """
    
    saved = 0
    batch: List[tuple] = []
    mop_is_employee = refs.get(UF_MOP) == "employee"
    for d in deals_buffer:
        mop_name = user_map.get(str(d.get(UF_MOP))) if mop_is_employee else d.get(UF_MOP)
        mop_id_val = norm_emp_id(d.get(UF_MOP)) if mop_is_employee else None
        responsible_id = d.get("ASSIGNED_BY_ID")
        responsible = user_map.get(str(responsible_id), f"ID {responsible_id}" if responsible_id else "")
        inn = normalized_inn(d.get(UF_INN))
        client_type = "Повторный" if inn and inn_counter.get(inn, 0) > 1 else "Новый"
        
        mpb_work_date = to_date_str(d.get(UF_MPB_WORK_DATE))
        do_transfer   = to_date_str(d.get(UF_DO_TRANSFER))
        docs_rework   = to_date_str(d.get(UF_DOCS_REWORK))
        client_sign   = to_date_str(d.get(UF_CLIENT_SIGN))
        banks_review  = to_date_str(d.get(UF_BANKS_REVIEW))
        flag_special  = to_flag(d.get(UF_FLAG_SPECIAL))
        
        row = (
            d.get("ID"),
            d.get("TITLE"),
            d.get("DATE_CREATE"),
            stage_names.get(d.get("STAGE_ID"), d.get("STAGE_ID")),
            d.get("STAGE_SEMANTIC_ID"),
            d.get("STAGE_ID"),
            d.get("OPPORTUNITY"),
            d.get(UF_KV),
            mop_name,
            mop_id_val,
            refs.get(UF_REASON, {}).get(str(d.get(UF_REASON)), d.get(UF_REASON)),
            refs.get(UF_BANK, {}).get(str(d.get(UF_BANK)), d.get(UF_BANK)),
            refs.get(UF_PRODUCT, {}).get(str(d.get(UF_PRODUCT)), d.get(UF_PRODUCT)),
            inn,
            client_type,
            d.get("ASSIGNED_BY_ID"),
            responsible,
            d.get(UF_CLOSE_DATE),
            d.get(UF_TRANSFER_DATE),
            d.get(UF_APPROVED_DATE),
            mpb_work_date,
            do_transfer,
            docs_rework,
            client_sign,
            banks_review,
            flag_special
        )
        batch.append(row)
        if len(batch) >= 500:
            cursor.executemany(q, batch)
            saved += len(batch)
            batch.clear()
            log.info("Deals upsert progress: %s", saved)

    if batch:
        cursor.executemany(q, batch)
        saved += len(batch)
    
    log.info("OK: deals upsert=%s", saved)

def _fetch_lead_counts(
    bitrix: BitrixClient,
    stage: str,
    managers: Dict[int, str],
    step_name: str,
) -> Dict[int, int]:
    counts = {uid: 0 for uid in managers}
    seen_ids: Set[int] = set()
    errors = 0
    start = 0
    pages = 0
    max_pages = 500

    while pages < max_pages:
        params = {
            "filter": {"STATUS_ID": stage},
            "select": ["ID", "ASSIGNED_BY_ID"],
            "order": {"ID": "ASC"},
            "start": start
        }
        debug_log(
            "bitrix",
            "lead_page_request",
            {"step": step_name, "stage": stage, "start": start},
        )
        raw = bx_call_raw(bitrix, "crm.lead.list", params, timeout=90, raise_on_error=True)
        leads = raw.get("result", [])
        if not isinstance(leads, list) or not leads:
            break

        previous_seen_ids = set(seen_ids)
        current_ids: Set[int] = set()
        for lead in leads:
            try:
                lead_id = int(lead.get("ID"))
            except Exception:
                errors += 1
                continue

            current_ids.add(lead_id)
            if lead_id in seen_ids:
                continue

            seen_ids.add(lead_id)
            uid = norm_emp_id(lead.get("ASSIGNED_BY_ID"))
            if uid in counts:
                counts[uid] += 1

        if current_ids and current_ids.issubset(previous_seen_ids):
            raise RuntimeError("%s pagination returned already processed ids" % step_name)

        pages += 1
        if pages % 10 == 0:
            log.info("%s progress: %s leads, %s pages", step_name, len(seen_ids), pages)

        next_value = raw.get("next")
        if next_value is None:
            break
        start = int(next_value)

    if pages >= max_pages:
        raise RuntimeError("MAX_PAGES reached for %s" % step_name)

    log.info("%s unique=%s errors=%s pages=%s", step_name, len(seen_ids), errors, pages)
    return counts

def step_cold_leads(bitrix: BitrixClient, cursor: Any, managers: Dict[int, str]) -> None:
    log.info("=== STEP 2: cold leads -> cold_leads_cache.count ===")
    cold_counts = _fetch_lead_counts(bitrix, COLD_STAGE, managers, "cold_leads")

    q = """
        INSERT INTO cold_leads_cache (manager_id, count, updated_at)
        VALUES (%s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            count = VALUES(count),
            updated_at = VALUES(updated_at)
    """
    cursor.executemany(q, list(cold_counts.items()))
    
    log.info("OK: cold_leads_cache.count updated")

def step_winners(bitrix: BitrixClient, cursor: Any, managers: Dict[int, str]) -> None:
    log.info("=== STEP 3: winners -> cold_leads_cache.winners_count ===")
    winners_counts = _fetch_lead_counts(bitrix, WIN_STAGE, managers, "winners")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    q = """
        INSERT INTO cold_leads_cache (manager_id, winners_count, winners_updated_at)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE
            winners_count = VALUES(winners_count),
            winners_updated_at = VALUES(winners_updated_at)
    """
    data_rows = [(mid, winners_counts.get(mid, 0), now) for mid in managers]
    cursor.executemany(q, data_rows)
    
    log.info("OK: winners updated")

def step_calls(bitrix: BitrixClient, cursor: Any, managers: Dict[int, str]) -> None:
    log.info("=== STEP 4: calls import -> calls (last 2 hours, hard stop) ===")
    ensure_calls_call_id_unique(cursor)
    now = datetime.now()
    date_from_dt = now - timedelta(hours=CALL_SYNC_LOOKBACK_HOURS)
    date_from = date_from_dt.strftime("%Y-%m-%d %H:%M:%S")
    date_to   = now.strftime("%Y-%m-%d %H:%M:%S")
    
    log.info("calls period: %s .. %s", date_from, date_to)
    
    start = 0
    total_ins = 0
    total_upd = 0
    total_skip = 0
    loops = 0
    max_loops = 40
    page_size = 50
    seen_call_ids: Set[str] = set()
    stop_all = False
    
    while loops < max_loops and not stop_all:
        log.info("voximplant.statistic.get start=%s", start)
        params = {
            "FILTER": {
                ">=CALL_START_DATE": date_from,
                "<=CALL_START_DATE": date_to
            },
            "SORT": "CALL_START_DATE",
            "ORDER": "DESC",
            "LIMIT": page_size,
            "start": start
        }
        debug_log("bitrix", "calls_page_request", {"start": start, "limit": page_size})
        raw = bx_call_raw(bitrix, "voximplant.statistic.get", params, timeout=60, retries=2, sleep_sec=5, raise_on_error=True)
        rows = normalize_vox_rows(raw.get("result", []))
        
        if not rows:
            log.info("No calls in voximplant.statistic.get for period (or Bitrix returned no data).")
            break
        
        page_call_ids = {str(v.get("CALL_ID")) for v in rows if v.get("CALL_ID")}
        if page_call_ids and page_call_ids.issubset(seen_call_ids):
            raise RuntimeError("voximplant.statistic.get returned already processed call ids")
        seen_call_ids.update(page_call_ids)
        
        for v in rows:
            st = parse_bx_dt(v.get("CALL_START_DATE"))
            if st and st < date_from_dt:
                stop_all = True
                break
            
            call_id   = v.get("CALL_ID")
            call_date = v.get("CALL_START_DATE")
            user_id   = v.get("PORTAL_USER_ID")
            call_type = v.get("CALL_TYPE")
            number    = v.get("CONTACT_PHONE_NUMBER") or v.get("PHONE_NUMBER")
            
            try:
                duration = int(v.get("CALL_DURATION") or 0)
            except Exception:
                duration = 0
            
            if v.get("INTERNAL", "N") == "Y" or duration <= 0:
                total_skip += 1
                continue
            
            try:
                uid_int = int(user_id) if user_id is not None else None
            except Exception:
                uid_int = None
            
            if uid_int is None or uid_int not in managers or not call_id:
                total_skip += 1
                continue
            
            result_code_raw = v.get("CALL_FAILED_CODE")
            try:
                result_code_update = int(result_code_raw) if str(result_code_raw or "").strip() else None
            except Exception:
                result_code_update = None
            result_code_insert = result_code_update if result_code_update is not None else 200
            reason = v.get("CALL_FAILED_REASON") or v.get("CALL_FAILED_REASON_TEXT")
            entity_type = v.get("CRM_ENTITY_TYPE")
            entity_id   = v.get("CRM_ENTITY_ID")
            file_id     = None
            
            sql_row = (
                entity_type, entity_id, number, result_code_insert, reason,
                uid_int, call_type, call_date, duration, file_id, call_id,
                result_code_update, reason, reason
            )
            
            cursor.execute("""
                INSERT INTO calls
                    (entity_type, entity_id, number, result_code, reason,
                     user_id, call_type, call_date, duration, file_id, call_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    entity_type=VALUES(entity_type),
                    entity_id=VALUES(entity_id),
                    number=VALUES(number),
                    result_code=IF(%s IS NULL, result_code, VALUES(result_code)),
                    reason=IF(%s IS NULL OR %s = '', reason, VALUES(reason)),
                    user_id=VALUES(user_id),
                    call_type=VALUES(call_type),
                    call_date=VALUES(call_date),
                    duration=GREATEST(COALESCE(duration,0), COALESCE(VALUES(duration),0)),
                    file_id=VALUES(file_id)
            """, sql_row)
            if cursor.rowcount == 1:
                total_ins += 1
            elif cursor.rowcount >= 2:
                total_upd += 1
        
        log.info("voximplant page: %s (ins=%s upd=%s skip=%s)", len(rows), total_ins, total_upd, total_skip)
        
        if stop_all:
            log.info("HARD-STOP: reached CALL_START_DATE < date_from, stop.")
            break
        
        loops += 1
        next_value = raw.get("next")
        if next_value is None:
            break
        start = int(next_value)
    
    if loops >= max_loops:
        log.warning("MAX_LOOPS reached for calls direct import, stop")
    
    log.info("OK: calls import done (ins=%s upd=%s skip=%s)", total_ins, total_upd, total_skip)
# ================== MAIN ==================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync MPB deals, lead counters, and calls")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=is_dry_run(),
        help="Run all steps and rollback DB changes",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    t0 = time.time()
    log.info("=== deals_sync START ===")
    log.info("ENV=%s", ENV_PATH)
    log.info("LOG=%s", LOG_FILE)
    if args.dry_run:
        log.info("DRY RUN enabled: DB changes will be rolled back")
    
    try:
        env = shared_load_env(ENV_PATH)
    except Exception as e:
        log.exception("ENV load failed: %s", e)
        return 1
    
    try:
        webhook = env_required(env, "CRM_DEALS_WEBHOOK")
    except Exception as e:
        log.exception("ENV load failed: %s", e)
        return 1
    
    try:
        conn = mysql_connect(env)
    except Exception as e:
        log.exception("DB connect failed: %s", e)
        return 1
    
    cursor = None
    try:
        bitrix = BitrixClient(
            webhook,
            timeout=90,
            max_retries=5,
            backoff_base_sec=2,
            rps_sleep_sec=0.5,
            logger=log,
        )
        cursor = conn.cursor()
        managers = load_managers(cursor)
        
        step_deals(bitrix, cursor)
        if not args.dry_run:
            conn.commit()
        
        step_cold_leads(bitrix, cursor, managers)
        if not args.dry_run:
            conn.commit()
        
        step_winners(bitrix, cursor, managers)
        if not args.dry_run:
            conn.commit()
        
        step_calls(bitrix, cursor, managers)
        if args.dry_run:
            conn.rollback()
            log.info("DRY RUN rollback complete")
        else:
            conn.commit()
        
    except Exception as e:
        log.exception("FATAL ERROR: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return 1
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
    
    dt = int(time.time() - t0)
    log.info("=== deals_sync END OK (%ss) ===", dt)
    return 0

if __name__ == "__main__":
    sys.exit(main())



