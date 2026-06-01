#!/usr/bin/env python3

import random
import re
import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List

import requests

# ================== PATHS ==================
SCRIPT_DIR = Path(__file__).resolve().parent

LOG_DIR = SCRIPT_DIR / "log"
SQLITE_DIR = SCRIPT_DIR / "sqlite"

LOG_FILE = LOG_DIR / "gosbase_win.log"
STATE_DB = SQLITE_DIR / "gosbase_win.sqlite"
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
from debug_utils import make_debug_log
from bitrix_fields import field_code
from bitrix_stages import lead_stage
from db import pymysql_connect
from env_loader import env_required, load_env as shared_load_env
from runtime import is_dry_run

LOG_DIR.mkdir(parents=True, exist_ok=True)
SQLITE_DIR.mkdir(parents=True, exist_ok=True)

# ================== CONSTANTS ==================
GOSBASE_URL = "https://gosbase.ru/v1/api/online/"
GOSBASE_PAGES = 3

GOSBASE_PAGE_SLEEP_SEC = 0.7
GOSBASE_MAX_RETRIES = 5
GOSBASE_BACKOFF_BASE_SEC = 2

B24_RPS_SLEEP_SEC = 0.8
BITRIX_MAX_RETRIES = 5
BITRIX_BACKOFF_BASE_SEC = 2
BITRIX_TIMEOUT_SEC = 20

RESET_EVERY_DAYS = 7

WORK_START_HOUR = 8
WORK_END_HOUR = 19
MOSCOW_TZ = timezone(timedelta(hours=3))

# ================== BITRIX FIELDS / SETTINGS ==================
COMPANY_INN_FIELD = field_code("company", "inn")

LEAD_INN_FIELD = field_code("lead", "inn")
LEAD_PURCHASE_FIELD = field_code("lead", "purchase_number")
LEAD_PROTO_DATE_FIELD = field_code("lead", "protocol_date")
LEAD_COMPANY_MANAGER_FIELD = field_code("lead", "company_manager")
LEAD_PURCHASE_LINK_FIELD = field_code("lead", "purchase_link")
LEAD_CUSTOMER_FIELD = field_code("lead", "customer")
LEAD_PURCHASE_OBJECT_FIELD = field_code("lead", "purchase_object")

LEAD_STATUS_ID_WINNERS = lead_stage("winner")
LEAD_STATUS_ID_PROCESSED = lead_stage("processed")
LEAD_STATUS_ID_WAIT_DEAL = lead_stage("wait_deal")

log = AppLogger(LOG_FILE)
debug_log = make_debug_log(log)


# ================== EXCEPTIONS ==================
class PipelineFatalError(Exception):
    pass


class BitrixUnavailableError(PipelineFatalError):
    pass


# ================== WORK WINDOW ==================
def in_work_window() -> bool:
    now = datetime.now(MOSCOW_TZ)
    if now.weekday() >= 5:
        return False
    return WORK_START_HOUR <= now.hour < WORK_END_HOUR


# ================== SQLITE STATE DB ==================
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(STATE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed (
            contract_idx INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def already_processed(conn: sqlite3.Connection, contract_idx: int) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM processed WHERE contract_idx = ?",
        (contract_idx,),
    )
    return cur.fetchone() is not None


def mark_processed(conn: sqlite3.Connection, contract_idx: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO processed (contract_idx, created_at) VALUES (?, ?)",
        (contract_idx, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    cur = conn.execute("SELECT v FROM meta WHERE k = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )
    conn.commit()


def maybe_weekly_reset(conn: sqlite3.Connection) -> None:
    now = datetime.utcnow()
    last = get_meta(conn, "last_reset_utc")

    do_reset = False
    if not last:
        do_reset = True
    else:
        try:
            last_dt = datetime.fromisoformat(last)
            if now - last_dt >= timedelta(days=RESET_EVERY_DAYS):
                do_reset = True
        except Exception:
            do_reset = True

    if not do_reset:
        return

    cur = conn.execute("SELECT COUNT(*) FROM processed")
    before = int(cur.fetchone()[0])

    log.warning("RESET_START weekly_full_reset=1 before=%s", before)
    if STATE_DB.exists():
        backup_path = STATE_DB.with_suffix(STATE_DB.suffix + ".bak")
        shutil.copy2(str(STATE_DB), str(backup_path))
        log.warning("RESET_BACKUP saved=%s", backup_path)

    conn.execute("DELETE FROM processed")
    conn.commit()

    try:
        conn.execute("VACUUM")
    except Exception:
        log.warning("RESET_VACUUM_FAIL")

    set_meta(conn, "last_reset_utc", now.isoformat(timespec="seconds"))
    log.warning("RESET_FINISH ok=1")


def has_success_deal_db(db_conn, normalized_inn: str, mop_id: int) -> bool:
    normalized_mop_id = safe_int(mop_id)

    if not normalized_inn or not normalized_mop_id:
        return False

    sql = """
        SELECT 1
        FROM mpb_deals
        WHERE inn = %s
          AND mop_id = %s
          AND stage_semantic_id = 'S'
        LIMIT 1
    """

    with db_conn.cursor() as cur:
        cur.execute(sql, (normalized_inn, normalized_mop_id))
        return cur.fetchone() is not None


# ================== HELPERS ==================
def normalize_inn(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits if len(digits) in (10, 12) else ""


def normalize_date(value: Any) -> str:
    if not value:
        return ""
    value = str(value).replace("T", " ")
    return value[:10]


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def build_observer_ids(responsible_id: int, extra_observer_id: Optional[int]) -> List[int]:
    result: List[int] = []
    for user_id in (responsible_id, extra_observer_id):
        uid = safe_int(user_id)
        if uid and uid not in result:
            result.append(uid)
    return result


def append_sample(bucket: list, value: str, limit: int = 10) -> None:
    if value and value not in bucket and len(bucket) < limit:
        bucket.append(value)


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = normalize_text(value)
        if text:
            return text
    return ""


def build_purchase_link(notification_number: str) -> str:
    if not notification_number:
        return ""
    return (
        "https://zakupki.gov.ru/epz/order/notice/ea44/view/common-info.html"
        f"?regNumber={notification_number}"
    )


def extract_purchase_info(item: Dict[str, Any]) -> Dict[str, str]:
    purchase = item.get("purchase") if isinstance(item.get("purchase"), dict) else {}
    customer = purchase.get("customer") if isinstance(purchase.get("customer"), dict) else {}

    purchase_number = first_non_empty(
        purchase.get("notificationNumber"),
        item.get("notificationNumber"),
    )
    purchase_object = first_non_empty(
        item.get("purchaseObjectInfo"),
        purchase.get("purchaseObjectInfo"),
        item.get("purchase_object_info"),
        purchase.get("purchase_object_info"),
        item.get("purchaseObject"),
        purchase.get("purchaseObject"),
        item.get("name"),
        purchase.get("name"),
    )
    customer_name = first_non_empty(
        customer.get("fullName"),
        customer.get("name"),
        item.get("customerFullName"),
        item.get("customerName"),
        purchase.get("customerFullName"),
        purchase.get("customerName"),
    )

    return {
        "purchase_number": purchase_number,
        "purchase_link": build_purchase_link(purchase_number),
        "purchase_object": purchase_object,
        "customer_name": customer_name,
    }


# ================== GOSBASE ==================
def gosbase_fetch_page(session: requests.Session, key: str, page: int) -> Optional[Any]:
    for attempt in range(1, GOSBASE_MAX_RETRIES + 1):
        try:
            debug_log("gosbase", "request", {"page": page, "attempt": attempt})
            response = session.get(
                GOSBASE_URL,
                params={"key": key, "page": page},
                timeout=30,
            )
            debug_log(
                "gosbase",
                "response",
                {"page": page, "attempt": attempt, "status": response.status_code},
            )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_s = int(retry_after)
                else:
                    wait_s = int(
                        GOSBASE_BACKOFF_BASE_SEC * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
                    )
                log.warning("GOSBASE_429 page=%s attempt=%s wait=%ss", page, attempt, wait_s)
                time.sleep(wait_s)
                continue

            if 400 <= response.status_code < 500:
                log.error("GOSBASE_CLIENT_ERROR status=%s page=%s", response.status_code, page)
                return None

            if 500 <= response.status_code < 600:
                wait_s = int(
                    GOSBASE_BACKOFF_BASE_SEC * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
                )
                log.warning(
                    "GOSBASE_%s page=%s attempt=%s wait=%ss",
                    response.status_code,
                    page,
                    attempt,
                    wait_s,
                )
                time.sleep(wait_s)
                continue

            response.raise_for_status()
            data = response.json()
            debug_log(
                "gosbase",
                "json_ok",
                {"page": page, "data_type": type(data).__name__},
            )
            return data

        except requests.RequestException as exc:
            wait_s = int(
                GOSBASE_BACKOFF_BASE_SEC * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
            )
            log.warning(
                "GOSBASE_ERROR page=%s attempt=%s err=%s wait=%ss",
                page,
                attempt,
                type(exc).__name__,
                wait_s,
            )
            time.sleep(wait_s)

    log.error("GOSBASE_PAGE_FAILED page=%s after=%s retries", page, GOSBASE_MAX_RETRIES)
    return None


def extract_items(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "result", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


# ================== BITRIX ==================
class Bitrix24:
    def __init__(self, webhook: str):
        self.client = BitrixClient(
            webhook,
            timeout=BITRIX_TIMEOUT_SEC,
            max_retries=BITRIX_MAX_RETRIES,
            backoff_base_sec=BITRIX_BACKOFF_BASE_SEC,
            rps_sleep_sec=B24_RPS_SLEEP_SEC,
            logger=log,
        )

    def call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self.client.call(method, payload)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise BitrixUnavailableError(str(exc)) from exc
        except RuntimeError as exc:
            message = str(exc)
            if message.startswith("Bitrix call failed after") or message.startswith("Bitrix non-json response"):
                raise BitrixUnavailableError(message) from exc
            raise

    def find_company(self, inn: str) -> Optional[Dict[str, Any]]:
        result = self.call(
            "crm.company.list",
            {
                "filter": {COMPANY_INN_FIELD: inn},
                "select": ["ID", "TITLE", "ASSIGNED_BY_ID"],
            },
        ).get("result", [])
        return result[0] if result else None

    def find_allowed_lead_by_inn(self, inn: str) -> Optional[Dict[str, Any]]:
        normalized_inn = normalize_inn(inn)
        if not normalized_inn:
            return None

        result = self.call(
            "crm.lead.list",
            {
                "filter": {
                    "STATUS_ID": [LEAD_STATUS_ID_PROCESSED, LEAD_STATUS_ID_WAIT_DEAL],
                    LEAD_INN_FIELD: normalized_inn,
                },
                "select": [
                    "ID",
                    "TITLE",
                    "STATUS_ID",
                    "ASSIGNED_BY_ID",
                    LEAD_INN_FIELD,
                ],
                "order": {"ID": "DESC"},
                "start": 0,
            },
        ).get("result", [])

        return result[0] if result else None

    def find_existing_winner_lead(self, inn: str, purchase_number: str) -> Optional[Dict[str, Any]]:
        normalized_inn = normalize_inn(inn)
        normalized_purchase = normalize_text(purchase_number)

        if not normalized_inn:
            return None

        lead_filter: Dict[str, Any] = {
            "STATUS_ID": LEAD_STATUS_ID_WINNERS,
            LEAD_INN_FIELD: normalized_inn,
        }
        if normalized_purchase:
            lead_filter[LEAD_PURCHASE_FIELD] = normalized_purchase

        result = self.call(
            "crm.lead.list",
            {
                "filter": lead_filter,
                "select": [
                    "ID",
                    "TITLE",
                    "STATUS_ID",
                    LEAD_INN_FIELD,
                    LEAD_PURCHASE_FIELD,
                ],
                "order": {"ID": "DESC"},
                "start": 0,
            },
        ).get("result", [])

        for item in result:
            item_inn = normalize_inn(item.get(LEAD_INN_FIELD))
            item_purchase = normalize_text(item.get(LEAD_PURCHASE_FIELD))

            if item_inn != normalized_inn:
                continue
            if normalized_purchase and item_purchase != normalized_purchase:
                continue
            return item

        return None

    def create_lead(self, fields: Dict[str, Any]) -> int:
        resp = self.call("crm.lead.add", {"fields": fields})
        lead_id = safe_int(resp.get("result"))
        if not lead_id:
            raise RuntimeError("crm.lead.add: ID missing in response: %s" % resp)
        return lead_id

    def get_lead_observers(self, lead_id: int) -> List[int]:
        resp = self.call(
            "crm.item.get",
            {
                "entityTypeId": 1,  # 1 = Lead
                "id": int(lead_id),
            },
        )

        item = (resp.get("result") or {}).get("item") or {}
        raw_observers = item.get("observers") or []

        result: List[int] = []
        for value in raw_observers:
            uid = safe_int(value)
            if uid and uid not in result:
                result.append(uid)

        return result

    def set_lead_observers(self, lead_id: int, observer_ids: List[int]) -> None:
        # Cloud Bitrix24 has no separate REST method for adding lead observers.
        # Standard lead observers are managed through the universal CRM API:
        # crm.item.update, entityTypeId=1, fields.observers=[...].
        normalized_ids: List[int] = []

        for user_id in observer_ids:
            uid = safe_int(user_id)
            if uid and uid not in normalized_ids:
                normalized_ids.append(uid)

        if not normalized_ids:
            log.info("LEAD_OBSERVERS_SKIP_EMPTY lead_id=%s", lead_id)
            return

        self.call(
            "crm.item.update",
            {
                "entityTypeId": 1,  # 1 = Lead
                "id": int(lead_id),
                "fields": {
                    "observers": normalized_ids,
                },
            },
        )

        # Read back the lead because crm.item.update responses can omit fields.
        actual_ids = self.get_lead_observers(lead_id)
        missing = [uid for uid in normalized_ids if uid not in actual_ids]

        log.info(
            "LEAD_OBSERVERS_SET lead_id=%s requested=%s actual=%s",
            lead_id,
            normalized_ids,
            actual_ids,
        )

        if missing:
            raise RuntimeError(
                "LEAD_OBSERVERS_MISMATCH lead_id=%s requested=%s actual=%s missing=%s"
                % (lead_id, normalized_ids, actual_ids, missing)
            )


# ================== MAIN ==================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create winner leads from Gosbase")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=is_dry_run(),
        help="Do not create Bitrix leads or mark contracts",
    )
    parser.add_argument("--pages", type=int, default=GOSBASE_PAGES, help="Number of Gosbase pages to scan")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log.info("START")
    if args.dry_run:
        log.info("DRY_RUN enabled")

    if not in_work_window():
        log.info("EXIT OUTSIDE_WORK_WINDOW schedule=Mon-Fri_08-19_MSK")
        return 0

    try:
        env = shared_load_env(ENV_PATH)
    except Exception as exc:
        log.exception("ENV_LOAD_FAIL err=%s", exc)
        return 1

    try:
        gosbase_key = env_required(env, "GOSBASE")
        webhook = env_required(env, "WINNERS")
        winners_observer_id = safe_int(env.get("WINNERS_OBSERVER_ID"))
    except Exception as exc:
        log.exception("ENV_MISSING GOSBASE or WINNERS err=%s", exc)
        return 1

    log.info("CONFIG winners_observer_id=%s", winners_observer_id)

    try:
        db_conn = pymysql_connect(env, dict_cursor=True, autocommit=True)
    except Exception as exc:
        log.exception("MYSQL_CONNECT_FAIL err=%s", exc)
        return 1

    conn = None
    gosbase_session = None

    created = 0
    scanned = 0
    skipped_already_processed = 0
    skipped_no_inn = 0
    skipped_no_company_and_no_lead = 0
    skipped_no_success_deal = 0
    skipped_no_responsible = 0
    skipped_duplicate_winner = 0
    found_company = 0
    matched = 0
    matched_by_db = 0
    matched_by_lead = 0

    sample_no_company_and_no_lead = []
    sample_no_success_deal = []
    sample_no_responsible = []
    sample_duplicate_winner = []
    sample_matched_by_lead = []
    sample_created = []

    try:
        conn = init_db()
        b24 = Bitrix24(webhook)
        gosbase_session = requests.Session()
        maybe_weekly_reset(conn)
        total_pages = max(1, args.pages)
        for page in range(1, total_pages + 1):
            log.info("FETCH_PAGE page=%s", page)

            data = gosbase_fetch_page(gosbase_session, gosbase_key, page)
            if data is None:
                log.error("STOP_ON_PAGE_FAIL page=%s", page)
                raise PipelineFatalError(f"Gosbase page fetch failed: page={page}")

            items = extract_items(data)
            if not items:
                log.info("EMPTY_PAGE page=%s", page)
                break

            for item in items:
                scanned += 1

                contract_idx = safe_int(item.get("contract_idx"))
                if contract_idx is None:
                    log.warning("SKIP NO_CONTRACT_IDX")
                    continue

                if already_processed(conn, contract_idx):
                    skipped_already_processed += 1
                    log.info("SKIP ALREADY_PROCESSED contract_idx=%s", contract_idx)
                    continue

                inn = normalize_inn(item.get("winner_inn"))
                if not inn:
                    skipped_no_inn += 1
                    log.warning("SKIP NO_INN contract_idx=%s", contract_idx)
                    mark_processed(conn, contract_idx)
                    continue

                company = None
                lead = None

                source_type = ""
                source_id = None
                source_title = ""
                responsible_id = None

                try:
                    company = b24.find_company(inn)
                except BitrixUnavailableError as exc:
                    log.exception(
                        "BITRIX_FIND_COMPANY_FAIL inn=%s contract_idx=%s err=%s",
                        inn,
                        contract_idx,
                        exc,
                    )
                    log.error("STOP_RUN BITRIX_UNAVAILABLE during crm.company.list")
                    raise
                except Exception as exc:
                    log.exception(
                        "BITRIX_FIND_COMPANY_FAIL_NONFATAL inn=%s contract_idx=%s err=%s",
                        inn,
                        contract_idx,
                        exc,
                    )
                    continue

                if company:
                    found_company += 1

                    company_id = safe_int(company.get("ID"))
                    company_title = normalize_text(company.get("TITLE"))
                    company_manager_id = safe_int(company.get("ASSIGNED_BY_ID"))

                    if not company_manager_id:
                        skipped_no_responsible += 1
                        append_sample(sample_no_responsible, f"company:{company_id}:{inn}:{contract_idx}")
                        log.error(
                            "SKIP COMPANY_HAS_NO_ASSIGNED company_id=%s inn=%s contract_idx=%s",
                            company_id,
                            inn,
                            contract_idx,
                        )
                        mark_processed(conn, contract_idx)
                        continue

                    allow_create = False

                    try:
                        success_deal_exists = has_success_deal_db(db_conn, inn, company_manager_id)
                    except Exception as exc:
                        log.exception(
                            "MYSQL_SUCCESS_DEAL_CHECK_FAIL company_id=%s mop_id=%s inn=%s contract_idx=%s err=%s",
                            company_id,
                            company_manager_id,
                            inn,
                            contract_idx,
                            exc,
                        )
                        continue

                    if success_deal_exists:
                        allow_create = True
                        matched_by_db += 1
                        log.info(
                            "MATCH_BY_DB company_id=%s mop_id=%s inn=%s contract_idx=%s",
                            company_id,
                            company_manager_id,
                            inn,
                            contract_idx,
                        )
                    else:
                        try:
                            lead = b24.find_allowed_lead_by_inn(inn)
                        except BitrixUnavailableError as exc:
                            log.exception(
                                "BITRIX_LEAD_CHECK_FAIL company_id=%s mop_id=%s inn=%s contract_idx=%s err=%s",
                                company_id,
                                company_manager_id,
                                inn,
                                contract_idx,
                                exc,
                            )
                            log.error("STOP_RUN BITRIX_UNAVAILABLE during crm.lead.list")
                            raise
                        except Exception as exc:
                            log.exception(
                                "BITRIX_LEAD_CHECK_FAIL_NONFATAL company_id=%s mop_id=%s inn=%s contract_idx=%s err=%s",
                                company_id,
                                company_manager_id,
                                inn,
                                contract_idx,
                                exc,
                            )
                            continue

                        if lead:
                            allow_create = True
                            matched_by_lead += 1
                            append_sample(sample_matched_by_lead, f"company:{company_id}:{inn}:{contract_idx}")
                            log.info(
                                "MATCH_BY_LEAD company_id=%s mop_id=%s inn=%s contract_idx=%s",
                                company_id,
                                company_manager_id,
                                inn,
                                contract_idx,
                            )

                    if not allow_create:
                        skipped_no_success_deal += 1
                        append_sample(
                            sample_no_success_deal,
                            f"{company_id}:{company_manager_id}:{inn}:{contract_idx}",
                        )
                        log.info(
                            "SKIP NO_SUCCESS_DEAL_AND_NO_ALLOWED_LEAD company_id=%s mop_id=%s inn=%s contract_idx=%s",
                            company_id,
                            company_manager_id,
                            inn,
                            contract_idx,
                        )
                        mark_processed(conn, contract_idx)
                        continue

                    source_type = "company"
                    source_id = company_id
                    source_title = company_title
                    responsible_id = company_manager_id

                else:
                    try:
                        lead = b24.find_allowed_lead_by_inn(inn)
                    except BitrixUnavailableError as exc:
                        log.exception(
                            "BITRIX_LEAD_FIND_FAIL inn=%s contract_idx=%s err=%s",
                            inn,
                            contract_idx,
                            exc,
                        )
                        log.error("STOP_RUN BITRIX_UNAVAILABLE during crm.lead.list")
                        raise
                    except Exception as exc:
                        log.exception(
                            "BITRIX_LEAD_FIND_FAIL_NONFATAL inn=%s contract_idx=%s err=%s",
                            inn,
                            contract_idx,
                            exc,
                        )
                        continue

                    if not lead:
                        skipped_no_company_and_no_lead += 1
                        append_sample(sample_no_company_and_no_lead, f"{inn}:{contract_idx}")
                        log.info(
                            "SKIP COMPANY_AND_ALLOWED_LEAD_NOT_FOUND inn=%s contract_idx=%s",
                            inn,
                            contract_idx,
                        )
                        mark_processed(conn, contract_idx)
                        continue

                    lead_id = safe_int(lead.get("ID"))
                    lead_title = normalize_text(lead.get("TITLE"))
                    lead_status = normalize_text(lead.get("STATUS_ID"))
                    lead_manager_id = safe_int(lead.get("ASSIGNED_BY_ID"))

                    if not lead_manager_id:
                        skipped_no_responsible += 1
                        append_sample(sample_no_responsible, f"lead:{lead_id}:{inn}:{contract_idx}")
                        log.error(
                            "SKIP LEAD_HAS_NO_ASSIGNED lead_id=%s status=%s inn=%s contract_idx=%s",
                            lead_id,
                            lead_status,
                            inn,
                            contract_idx,
                        )
                        mark_processed(conn, contract_idx)
                        continue

                    matched_by_lead += 1
                    append_sample(sample_matched_by_lead, f"lead:{lead_id}:{inn}:{contract_idx}")
                    log.info(
                        "MATCH_BY_LEAD_ONLY lead_id=%s status=%s mop_id=%s inn=%s contract_idx=%s",
                        lead_id,
                        lead_status,
                        lead_manager_id,
                        inn,
                        contract_idx,
                    )

                    source_type = "lead"
                    source_id = lead_id
                    source_title = lead_title
                    responsible_id = lead_manager_id

                matched += 1

                purchase_info = extract_purchase_info(item)
                purchase_number = purchase_info["purchase_number"]
                proto_date = normalize_date(item.get("protokol_date"))
                observer_ids = build_observer_ids(responsible_id, winners_observer_id)

                try:
                    existing_winner = b24.find_existing_winner_lead(inn, purchase_number)
                except BitrixUnavailableError as exc:
                    log.exception(
                        "BITRIX_FIND_EXISTING_WINNER_FAIL source_type=%s source_id=%s inn=%s purchase=%s contract_idx=%s err=%s",
                        source_type,
                        source_id,
                        inn,
                        purchase_number,
                        contract_idx,
                        exc,
                    )
                    log.error("STOP_RUN BITRIX_UNAVAILABLE during crm.lead.list")
                    raise
                except Exception as exc:
                    log.exception(
                        "BITRIX_FIND_EXISTING_WINNER_FAIL_NONFATAL source_type=%s source_id=%s inn=%s purchase=%s contract_idx=%s err=%s",
                        source_type,
                        source_id,
                        inn,
                        purchase_number,
                        contract_idx,
                        exc,
                    )
                    continue

                if existing_winner:
                    existing_winner_id = safe_int(existing_winner.get("ID"))
                    skipped_duplicate_winner += 1

                    if existing_winner_id:
                        try:
                            b24.set_lead_observers(existing_winner_id, observer_ids)
                            log.info(
                                "DUPLICATE_WINNER_OBSERVERS_FIXED lead_id=%s observers=%s inn=%s purchase=%s contract_idx=%s",
                                existing_winner_id,
                                observer_ids,
                                inn,
                                purchase_number,
                                contract_idx,
                            )
                        except BitrixUnavailableError:
                            raise
                        except Exception as exc:
                            log.exception(
                                "DUPLICATE_WINNER_OBSERVERS_FIX_FAIL lead_id=%s observers=%s inn=%s purchase=%s contract_idx=%s err=%s",
                                existing_winner_id,
                                observer_ids,
                                inn,
                                purchase_number,
                                contract_idx,
                                exc,
                            )
                            continue

                    append_sample(
                        sample_duplicate_winner,
                        f"{existing_winner_id}:{inn}:{purchase_number}:{contract_idx}",
                    )
                    log.info(
                        "SKIP DUPLICATE_WINNER lead_id=%s inn=%s purchase=%s contract_idx=%s",
                        existing_winner_id,
                        inn,
                        purchase_number,
                        contract_idx,
                    )
                    mark_processed(conn, contract_idx)
                    continue

                lead_title_source = source_title or inn

                create_fields: Dict[str, Any] = {
                    "TITLE": "Победитель//%s" % lead_title_source,
                    "ASSIGNED_BY_ID": int(responsible_id),
                    "STATUS_ID": LEAD_STATUS_ID_WINNERS,
                    LEAD_INN_FIELD: inn,
                    LEAD_PURCHASE_FIELD: str(purchase_number),
                    LEAD_PROTO_DATE_FIELD: proto_date,
                    LEAD_COMPANY_MANAGER_FIELD: int(responsible_id),
                    LEAD_PURCHASE_LINK_FIELD: purchase_info["purchase_link"],
                    LEAD_CUSTOMER_FIELD: purchase_info["customer_name"],
                    LEAD_PURCHASE_OBJECT_FIELD: purchase_info["purchase_object"],
                }

                create_fields = {
                    key: value
                    for key, value in create_fields.items()
                    if value not in ("", None, [])
                }

                if args.dry_run:
                    created += 1
                    append_sample(
                        sample_created,
                        f"dry_run:{source_type}:{source_id}:{inn}:{contract_idx}",
                    )
                    log.info(
                        "DRY_RUN WOULD_CREATE source_type=%s source_id=%s responsible_id=%s observers=%s inn=%s purchase=%s contract_idx=%s",
                        source_type,
                        source_id,
                        responsible_id,
                        observer_ids,
                        inn,
                        purchase_number,
                        contract_idx,
                    )
                    continue

                try:
                    new_lead_id = b24.create_lead(create_fields)

                    # Mark the contract processed only after lead creation and observer update.
                    b24.set_lead_observers(new_lead_id, observer_ids)
                    mark_processed(conn, contract_idx)

                    created += 1
                    append_sample(
                        sample_created,
                        f"{new_lead_id}:{source_type}:{source_id}:{inn}:{contract_idx}",
                    )
                    log.info(
                        "LEAD_CREATED lead_id=%s source_type=%s source_id=%s responsible_id=%s observers=%s inn=%s purchase=%s contract_idx=%s customer=%s purchase_object=%s purchase_link=%s",
                        new_lead_id,
                        source_type,
                        source_id,
                        responsible_id,
                        observer_ids,
                        inn,
                        purchase_number,
                        contract_idx,
                        purchase_info["customer_name"],
                        purchase_info["purchase_object"],
                        purchase_info["purchase_link"],
                    )

                except BitrixUnavailableError as exc:
                    log.exception(
                        "LEAD_CREATE_FAIL source_type=%s source_id=%s inn=%s contract_idx=%s observers=%s err=%s",
                        source_type,
                        source_id,
                        inn,
                        contract_idx,
                        observer_ids,
                        exc,
                    )
                    log.error("STOP_RUN BITRIX_UNAVAILABLE during lead create/observers")
                    raise
                except Exception as exc:
                    log.exception(
                        "LEAD_CREATE_FAIL_NONFATAL source_type=%s source_id=%s inn=%s contract_idx=%s observers=%s err=%s",
                        source_type,
                        source_id,
                        inn,
                        contract_idx,
                        observer_ids,
                        exc,
                    )
                    continue

            if page < total_pages:
                time.sleep(GOSBASE_PAGE_SLEEP_SEC)

    except BitrixUnavailableError as exc:
        log.error("PIPELINE_FATAL BITRIX_UNAVAILABLE err=%s", exc)
        return 1

    except PipelineFatalError as exc:
        log.error("PIPELINE_FATAL err=%s", exc)
        return 1

    except Exception as exc:
        log.exception("PIPELINE_FATAL_UNEXPECTED err=%s", exc)
        return 1

    finally:
        if gosbase_session is not None:
            try:
                gosbase_session.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        try:
            db_conn.close()
        except Exception:
            pass

    log.info(
        "FINISH scanned=%s skipped_already_processed=%s new_candidates=%s skipped_no_inn=%s "
        "found_company=%s matched=%s created=%s matched_by_db=%s matched_by_lead=%s "
        "skipped_no_company_and_no_lead=%s skipped_no_success_deal=%s "
        "skipped_no_responsible=%s skipped_duplicate_winner=%s",
        scanned,
        skipped_already_processed,
        scanned - skipped_already_processed,
        skipped_no_inn,
        found_company,
        matched,
        created,
        matched_by_db,
        matched_by_lead,
        skipped_no_company_and_no_lead,
        skipped_no_success_deal,
        skipped_no_responsible,
        skipped_duplicate_winner,
    )
    log.info(
        "DIAG_SAMPLES no_company_and_no_lead=%s no_success_deal=%s no_responsible=%s "
        "duplicate_winner=%s matched_by_lead=%s created=%s",
        sample_no_company_and_no_lead,
        sample_no_success_deal,
        sample_no_responsible,
        sample_duplicate_winner,
        sample_matched_by_lead,
        sample_created,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

