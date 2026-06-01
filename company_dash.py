#!/usr/bin/env python3

import sys
import time
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ================== PATHS ==================
SCRIPT_DIR = Path(__file__).resolve().parent
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
from db import mysql_connect
from env_loader import env_required, load_env as shared_load_env
from runtime import is_dry_run

LOG_DIR = SCRIPT_DIR / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "company_dash.log"

log = AppLogger(LOG_FILE)
debug_log = make_debug_log(log)

UF_COMPANY_INN = field_code("company", "inn")
MAX_DELETE_RATIO = 0.2


# ================== HELPERS ==================
def join_multi(v: Any) -> Optional[str]:
    if isinstance(v, str):
        return v.strip() or None
    if not isinstance(v, list):
        return None
    vals = [x.get("VALUE") for x in v if isinstance(x, dict) and x.get("VALUE")]
    return ", ".join(vals) if vals else None


def mpb_signature(cur: Any) -> str:
    cur.execute(
        """
        SELECT
            COALESCE(MAX(id),0),
            COUNT(*),
            COALESCE(MAX(close_date),''),
            COALESCE(SUM(CRC32(CONCAT_WS('#',
                id, inn, stage_semantic_id, amount, kv, mop_id, mop, close_date
            ))),0)
        FROM mpb_deals
        """
    )
    mx, cnt, dt, checksum = cur.fetchone()
    return "id=%s;cnt=%s;dt=%s;crc=%s" % (mx, cnt, dt, checksum)


def should_run(cur: Any) -> Tuple[bool, str]:
    sig = mpb_signature(cur)

    cur.execute("SELECT signature FROM dashboard_sync_state WHERE name='company_dash'")
    row = cur.fetchone()
    if row and row[0] == sig:
        log.info("mpb_deals unchanged (sig=%s), exit", sig)
        return False, sig

    return True, sig


def finalize_signature(cur: Any, sig: str) -> None:
    cur.execute(
        """
        INSERT INTO dashboard_sync_state (name, signature)
        VALUES ('company_dash', %s)
        ON DUPLICATE KEY UPDATE signature=VALUES(signature)
        """,
        (sig,),
    )


def normalize_inn(raw: Any) -> str:
    if raw is None:
        return ""
    inn = "".join(ch for ch in str(raw).strip() if ch.isdigit())
    return inn if len(inn) in (10, 12) else ""


def load_aggregates(cur: Any) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    cur.execute(
        """
        SELECT
            TRIM(inn) AS inn,
            COUNT(*) AS deals_count,
            SUM(stage_semantic_id = 'S') AS success_deals_count,
            SUM(CASE WHEN stage_semantic_id IN ('S','F') THEN COALESCE(amount,0) ELSE 0 END) AS total_bg_sum,
            SUM(CASE WHEN stage_semantic_id IN ('S','F') THEN COALESCE(kv,0) ELSE 0 END) AS total_kv_sum,
            MAX(CASE WHEN stage_semantic_id IN ('S','F') THEN close_date ELSE NULL END) AS last_issue_date
        FROM mpb_deals
        WHERE stage_semantic_id IN ('S','F')
          AND inn IS NOT NULL AND TRIM(inn) <> ''
        GROUP BY TRIM(inn)
        """
    )

    agg: Dict[str, Dict[str, Any]] = {}
    inns: List[str] = []

    for inn, deals_cnt, succ_cnt, bg, kv, dt in cur.fetchall():
        k = normalize_inn(inn)
        if not k:
            continue
        inns.append(k)
        agg[k] = {
            "deals_count": int(deals_cnt or 0),
            "success_deals_count": int(succ_cnt or 0),
            "total_bg_sum": float(bg or 0),
            "total_kv_sum": float(kv or 0),
            "last_issue_date": dt,
            "mop_id": None,
            "mop": None,
            "last_deal_status": None,
        }

    if not inns:
        return [], {}

    cur.execute(
        """
        SELECT TRIM(d.inn) AS inn, d.mop_id, d.mop, d.stage_semantic_id
        FROM mpb_deals d
        JOIN (
            SELECT TRIM(inn) AS inn, MAX(id) AS id
            FROM mpb_deals
            WHERE stage_semantic_id IN ('S','F')
              AND inn IS NOT NULL AND TRIM(inn) <> ''
            GROUP BY TRIM(inn)
        ) x ON x.id = d.id
        """
    )
    for inn, mop_id, mop, st in cur.fetchall():
        k = normalize_inn(inn)
        if k in agg:
            agg[k]["mop_id"] = mop_id
            agg[k]["mop"] = mop
            agg[k]["last_deal_status"] = str(st).strip() if st is not None else None

    return inns, agg


def load_companies(bitrix: BitrixClient) -> Dict[str, Dict[str, Any]]:
    companies = bitrix.list_all(
        "crm.company.list",
        {
            "select": ["ID", "TITLE", "PHONE", "EMAIL", UF_COMPANY_INN],
            "order": {"ID": "ASC"},
        },
        max_pages=1000,
    )

    log.info("Bitrix companies loaded: %s", len(companies))
    debug_log("bitrix", "companies_loaded", {"count": len(companies)})

    result: Dict[str, Dict[str, Any]] = {}
    for company in companies:
        inn = normalize_inn(company.get(UF_COMPANY_INN) or "")
        if not inn:
            continue
        if inn in result:
            log.warning("Duplicate company INN %s, keep first", inn)
            continue
        try:
            company_id = int(company.get("ID"))
        except (TypeError, ValueError):
            log.warning("Invalid company ID=%r, skip", company.get("ID"))
            continue
        result[inn] = {
            "company_id": company_id,
            "name": (company.get("TITLE") or "").strip(),
            "phone": join_multi(company.get("PHONE")),
            "email": join_multi(company.get("EMAIL")),
        }
    return result


# ================== MAIN ==================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync company_dashboard from mpb_deals and Bitrix companies")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=is_dry_run(),
        help="Run checks and build rows without DB commit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    t0 = time.time()
    log.info("=== company_dash START ===")
    if args.dry_run:
        log.info("DRY RUN enabled: DB changes will be rolled back")
    conn = None
    cur = None

    try:
        env = shared_load_env(ENV_PATH)
        webhook = env_required(env, "MBD_DEALS_WEBHOOK")
        conn = mysql_connect(env)
        cur = conn.cursor()

        run_needed, sig = should_run(cur)
        if not run_needed:
            conn.commit()
            return 0

        inns, agg = load_aggregates(cur)
        log.info("INNs with S/F deals: %s", len(inns))

        if not inns:
            log.info("No INNs to update")
            conn.commit()
            return 0

        bitrix = BitrixClient(
            webhook,
            timeout=90,
            max_retries=5,
            backoff_base_sec=2.0,
            rps_sleep_sec=0.5,
            logger=log,
        )

        companies = load_companies(bitrix)
        if not companies:
            log.error("Bitrix companies were not loaded, skip company_dashboard update")
            conn.rollback()
            return 1

        log.info("Bitrix companies with INN: %s", len(companies))

        missing = [inn for inn in inns if inn not in companies]
        if missing:
            log.warning(
                "INNs from mpb_deals not found in Bitrix: %s total, first 200: %s",
                len(missing),
                ", ".join(missing[:200]),
            )

        rows = []
        dashboard_inns = []
        for inn in inns:
            company = companies.get(inn)
            if company is None:
                continue

            data = agg[inn]

            company_id = int(company["company_id"])
            name = company["name"] if company.get("name") else "INN %s" % inn
            phone = company.get("phone")
            email = company.get("email")
            success_count = int(data.get("success_deals_count") or 0)
            dashboard_inns.append(inn)
            rows.append(
                (
                    company_id, inn, name, phone, email,
                    int(data.get("deals_count") or 0), success_count,
                    data.get("last_issue_date"), data.get("last_deal_status"),
                    float(data.get("total_bg_sum") or 0), float(data.get("total_kv_sum") or 0),
                    data.get("mop_id"), data.get("mop"),
                )
            )

        if not rows:
            matched = len(inns) - len(missing)
            raise RuntimeError(
                "No company_dashboard rows prepared: %s INNs total, %s matched in Bitrix, %s missing"
                % (len(inns), matched, len(missing))
            )

        upsert_sql = """
            INSERT INTO company_dashboard
            (company_id, inn, name, phone, email,
             deals_count, success_deals_count, last_issue_date, last_deal_status,
             total_bg_sum, total_kv_sum, mop_id, mop)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                company_id=VALUES(company_id),
                name=VALUES(name),
                phone=VALUES(phone),
                email=VALUES(email),
                deals_count=VALUES(deals_count),
                success_deals_count=VALUES(success_deals_count),
                last_issue_date=VALUES(last_issue_date),
                last_deal_status=VALUES(last_deal_status),
                total_bg_sum=VALUES(total_bg_sum),
                total_kv_sum=VALUES(total_kv_sum),
                mop_id=VALUES(mop_id),
                mop=VALUES(mop)
        """
        cur.executemany(upsert_sql, rows)
        upserted = len(rows)

        deleted = 0
        if missing:
            log.warning("Skip company_dashboard cleanup because %s INNs are missing in Bitrix", len(missing))
        else:
            cur.execute("DROP TEMPORARY TABLE IF EXISTS tmp_inn")
            cur.execute(
                """
                CREATE TEMPORARY TABLE tmp_inn (
                    inn VARCHAR(12)
                    CHARACTER SET utf8mb4
                    COLLATE utf8mb4_unicode_ci
                    PRIMARY KEY
                ) ENGINE=MEMORY
                """
            )
            cur.executemany("INSERT INTO tmp_inn (inn) VALUES (%s)", [(inn,) for inn in dashboard_inns])

            cur.execute(
                """
                SELECT COUNT(*)
                FROM company_dashboard cd
                LEFT JOIN tmp_inn t
                  ON t.inn = cd.inn COLLATE utf8mb4_unicode_ci
                WHERE t.inn IS NULL
                """
            )
            row = cur.fetchone()
            to_delete = int((row[0] if row else 0) or 0)
            log.info("Rows to delete from company_dashboard: %s", to_delete)
            max_delete = len(dashboard_inns) * MAX_DELETE_RATIO
            if to_delete > max_delete:
                ratio = 100 * to_delete / max(len(dashboard_inns), 1)
                raise RuntimeError(
                    "Suspicious DELETE count: %s rows (%.0f%% of upserted %s)"
                    % (to_delete, ratio, len(dashboard_inns))
                )

            cur.execute(
                """
                DELETE cd
                FROM company_dashboard cd
                LEFT JOIN tmp_inn t
                  ON t.inn = cd.inn COLLATE utf8mb4_unicode_ci
                WHERE t.inn IS NULL
                """
            )
            deleted = cur.rowcount

        finalize_signature(cur, sig)

        if args.dry_run:
            conn.rollback()
            log.info("DRY RUN rollback: upsert=%s delete=%s sig=%s", upserted, deleted, sig)
        else:
            conn.commit()

        dt = int(time.time() - t0)
        log.info("=== company_dash END OK (%ss, upsert=%s, delete=%s) ===", dt, upserted, deleted)
        return 0

    except Exception:
        if conn is not None:
            conn.rollback()
        log.exception("FATAL")
        return 1
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
