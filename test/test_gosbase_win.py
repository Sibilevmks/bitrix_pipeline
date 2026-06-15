#!/usr/bin/env python3
"""Unit tests for gosbase_win.py"""
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Mock server-side modules BEFORE importing gosbase_win
# ---------------------------------------------------------------------------
_mock_logger = MagicMock()
_mock_app_logger_mod = MagicMock()
_mock_app_logger_mod.AppLogger.return_value = _mock_logger

_mock_bitrix_fields_mod = MagicMock()
_mock_bitrix_fields_mod.field_code = MagicMock(side_effect=lambda e, n: f"UF_CRM_{n.upper()}")

_mock_bitrix_stages_mod = MagicMock()
_mock_bitrix_stages_mod.lead_stage = MagicMock(side_effect=lambda n: f"STAGE_{n.upper()}")

for _name, _mod in [
    ("app_logger", _mock_app_logger_mod),
    ("bitrix_client", MagicMock()),
    ("debug_utils", MagicMock()),
    ("bitrix_fields", _mock_bitrix_fields_mod),
    ("bitrix_stages", _mock_bitrix_stages_mod),
    ("db", MagicMock()),
    ("env_loader", MagicMock()),
]:
    sys.modules.setdefault(_name, _mod)

# requests может не быть установлен — мокируем заранее на всякий случай
try:
    import requests as _real_requests
    _has_real_requests = True
except ImportError:
    _has_real_requests = False
    sys.modules.setdefault("requests", MagicMock())
    import requests as _real_requests

import gosbase_win  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_test_db() -> sqlite3.Connection:
    """In-memory SQLite с той же схемой, что создаёт init_db()."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE processed (
            contract_idx INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def make_b24() -> gosbase_win.Bitrix24:
    """Bitrix24 с замоканным self.call."""
    b24 = object.__new__(gosbase_win.Bitrix24)
    b24.client = MagicMock()
    return b24


# ===========================================================================
# normalize_inn
# ===========================================================================
class TestNormalizeInn(unittest.TestCase):

    def test_valid_10_digits(self):
        self.assertEqual(gosbase_win.normalize_inn("7712345678"), "7712345678")

    def test_valid_12_digits(self):
        self.assertEqual(gosbase_win.normalize_inn("771234567890"), "771234567890")

    def test_strips_dashes(self):
        self.assertEqual(gosbase_win.normalize_inn("771-234-5678"), "7712345678")

    def test_9_digits_returns_empty(self):
        self.assertEqual(gosbase_win.normalize_inn("123456789"), "")

    def test_11_digits_returns_empty(self):
        self.assertEqual(gosbase_win.normalize_inn("12345678901"), "")

    def test_none_returns_empty(self):
        self.assertEqual(gosbase_win.normalize_inn(None), "")

    def test_empty_string_returns_empty(self):
        self.assertEqual(gosbase_win.normalize_inn(""), "")

    def test_letters_only_returns_empty(self):
        self.assertEqual(gosbase_win.normalize_inn("abcdef"), "")

    def test_integer_input(self):
        self.assertEqual(gosbase_win.normalize_inn(7712345678), "7712345678")


# ===========================================================================
# normalize_date
# ===========================================================================
class TestNormalizeDate(unittest.TestCase):

    def test_date_string(self):
        self.assertEqual(gosbase_win.normalize_date("2024-01-15"), "2024-01-15")

    def test_datetime_string_truncated(self):
        self.assertEqual(gosbase_win.normalize_date("2024-01-15 10:30:00"), "2024-01-15")

    def test_iso_t_replaced(self):
        self.assertEqual(gosbase_win.normalize_date("2024-01-15T10:30:00"), "2024-01-15")

    def test_none_returns_empty(self):
        self.assertEqual(gosbase_win.normalize_date(None), "")

    def test_empty_string_returns_empty(self):
        self.assertEqual(gosbase_win.normalize_date(""), "")

    def test_zero_returns_empty(self):
        self.assertEqual(gosbase_win.normalize_date(0), "")


# ===========================================================================
# normalize_text
# ===========================================================================
class TestNormalizeText(unittest.TestCase):

    def test_plain_string(self):
        self.assertEqual(gosbase_win.normalize_text("hello"), "hello")

    def test_collapses_spaces(self):
        self.assertEqual(gosbase_win.normalize_text("foo   bar"), "foo bar")

    def test_strips_edges(self):
        self.assertEqual(gosbase_win.normalize_text("  hi  "), "hi")

    def test_collapses_newlines(self):
        self.assertEqual(gosbase_win.normalize_text("foo\n\nbar"), "foo bar")

    def test_none_returns_empty(self):
        self.assertEqual(gosbase_win.normalize_text(None), "")

    def test_empty_string(self):
        self.assertEqual(gosbase_win.normalize_text(""), "")


# ===========================================================================
# safe_int
# ===========================================================================
class TestSafeInt(unittest.TestCase):

    def test_plain_int(self):
        self.assertEqual(gosbase_win.safe_int(42), 42)

    def test_string_digit(self):
        self.assertEqual(gosbase_win.safe_int("42"), 42)

    def test_float_truncated(self):
        self.assertEqual(gosbase_win.safe_int(3.9), 3)

    def test_none_returns_none(self):
        self.assertIsNone(gosbase_win.safe_int(None))

    def test_non_numeric_string_returns_none(self):
        self.assertIsNone(gosbase_win.safe_int("abc"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(gosbase_win.safe_int(""))

    def test_negative(self):
        self.assertEqual(gosbase_win.safe_int(-5), -5)


# ===========================================================================
# build_observer_ids
# ===========================================================================
class TestBuildObserverIds(unittest.TestCase):

    def test_both_valid(self):
        self.assertEqual(gosbase_win.build_observer_ids(1, 2), [1, 2])

    def test_no_extra(self):
        self.assertEqual(gosbase_win.build_observer_ids(5, None), [5])

    def test_deduplicates(self):
        self.assertEqual(gosbase_win.build_observer_ids(3, 3), [3])

    def test_invalid_responsible(self):
        self.assertEqual(gosbase_win.build_observer_ids(None, 7), [7])

    def test_both_none(self):
        self.assertEqual(gosbase_win.build_observer_ids(None, None), [])

    def test_zero_excluded(self):
        self.assertEqual(gosbase_win.build_observer_ids(0, 5), [5])


# ===========================================================================
# append_sample
# ===========================================================================
class TestAppendSample(unittest.TestCase):

    def test_appends_value(self):
        bucket = []
        gosbase_win.append_sample(bucket, "abc")
        self.assertEqual(bucket, ["abc"])

    def test_does_not_append_duplicate(self):
        bucket = ["abc"]
        gosbase_win.append_sample(bucket, "abc")
        self.assertEqual(bucket, ["abc"])

    def test_respects_limit(self):
        bucket = ["x"] * 10
        gosbase_win.append_sample(bucket, "new")
        self.assertEqual(len(bucket), 10)

    def test_does_not_append_empty(self):
        bucket = []
        gosbase_win.append_sample(bucket, "")
        self.assertEqual(bucket, [])

    def test_custom_limit(self):
        bucket = ["a", "b"]
        gosbase_win.append_sample(bucket, "c", limit=2)
        self.assertEqual(bucket, ["a", "b"])

    def test_appends_up_to_limit(self):
        bucket = []
        for i in range(12):
            gosbase_win.append_sample(bucket, str(i))
        self.assertEqual(len(bucket), 10)


# ===========================================================================
# first_non_empty
# ===========================================================================
class TestFirstNonEmpty(unittest.TestCase):

    def test_returns_first_non_empty(self):
        self.assertEqual(gosbase_win.first_non_empty("", None, "hello", "world"), "hello")

    def test_all_empty_returns_empty(self):
        self.assertEqual(gosbase_win.first_non_empty("", None, "  "), "")

    def test_single_value(self):
        self.assertEqual(gosbase_win.first_non_empty("test"), "test")

    def test_strips_and_normalizes(self):
        self.assertEqual(gosbase_win.first_non_empty("  hi  "), "hi")

    def test_no_args_returns_empty(self):
        self.assertEqual(gosbase_win.first_non_empty(), "")


# ===========================================================================
# build_purchase_link
# ===========================================================================
class TestBuildPurchaseLink(unittest.TestCase):

    def test_builds_correct_url(self):
        result = gosbase_win.build_purchase_link("0123456789012345")
        self.assertIn("zakupki.gov.ru", result)
        self.assertIn("0123456789012345", result)

    def test_empty_returns_empty(self):
        self.assertEqual(gosbase_win.build_purchase_link(""), "")

    def test_none_returns_empty(self):
        self.assertEqual(gosbase_win.build_purchase_link(None), "")


# ===========================================================================
# extract_items
# ===========================================================================
class TestExtractItems(unittest.TestCase):

    def test_list_returned_as_is(self):
        data = [{"a": 1}]
        self.assertEqual(gosbase_win.extract_items(data), data)

    def test_dict_with_data_key(self):
        self.assertEqual(gosbase_win.extract_items({"data": [1, 2]}), [1, 2])

    def test_dict_with_result_key(self):
        self.assertEqual(gosbase_win.extract_items({"result": [3, 4]}), [3, 4])

    def test_dict_with_items_key(self):
        self.assertEqual(gosbase_win.extract_items({"items": [5, 6]}), [5, 6])

    def test_unknown_structure_returns_empty(self):
        self.assertEqual(gosbase_win.extract_items({"other": [1]}), [])

    def test_none_returns_empty(self):
        self.assertEqual(gosbase_win.extract_items(None), [])

    def test_string_returns_empty(self):
        self.assertEqual(gosbase_win.extract_items("string"), [])

    def test_empty_dict_returns_empty(self):
        self.assertEqual(gosbase_win.extract_items({}), [])


# ===========================================================================
# extract_purchase_info
# ===========================================================================
class TestExtractPurchaseInfo(unittest.TestCase):

    def test_full_item(self):
        item = {
            "purchase": {
                "notificationNumber": "0123456789012345",
                "purchaseObjectInfo": "Поставка товара",
                "customer": {"fullName": "ООО Заказчик"},
            }
        }
        result = gosbase_win.extract_purchase_info(item)
        self.assertEqual(result["purchase_number"], "0123456789012345")
        self.assertIn("0123456789012345", result["purchase_link"])
        self.assertEqual(result["purchase_object"], "Поставка товара")
        self.assertEqual(result["customer_name"], "ООО Заказчик")

    def test_falls_back_to_item_fields(self):
        item = {
            "notificationNumber": "9999",
            "purchaseObjectInfo": "Предмет",
            "customerFullName": "Клиент",
        }
        result = gosbase_win.extract_purchase_info(item)
        self.assertEqual(result["purchase_number"], "9999")
        self.assertEqual(result["purchase_object"], "Предмет")
        self.assertEqual(result["customer_name"], "Клиент")

    def test_empty_item_returns_empty_strings(self):
        result = gosbase_win.extract_purchase_info({})
        self.assertEqual(result["purchase_number"], "")
        self.assertEqual(result["purchase_link"], "")
        self.assertEqual(result["purchase_object"], "")
        self.assertEqual(result["customer_name"], "")

    def test_purchase_number_from_item_when_nested_absent(self):
        item = {"notificationNumber": "ABC123", "purchase": {}}
        result = gosbase_win.extract_purchase_info(item)
        self.assertEqual(result["purchase_number"], "ABC123")

    def test_customer_short_name_fallback(self):
        item = {
            "purchase": {
                "customer": {"name": "Краткое имя"},
            }
        }
        result = gosbase_win.extract_purchase_info(item)
        self.assertEqual(result["customer_name"], "Краткое имя")


# ===========================================================================
# SQLite state: already_processed / mark_processed / get_meta / set_meta
# ===========================================================================
class TestSqliteState(unittest.TestCase):

    def setUp(self):
        self.conn = make_test_db()

    def tearDown(self):
        self.conn.close()

    def test_not_processed_initially(self):
        self.assertFalse(gosbase_win.already_processed(self.conn, 42))

    def test_mark_then_check(self):
        gosbase_win.mark_processed(self.conn, 42)
        self.assertTrue(gosbase_win.already_processed(self.conn, 42))

    def test_mark_idempotent(self):
        gosbase_win.mark_processed(self.conn, 7)
        gosbase_win.mark_processed(self.conn, 7)  # не падает
        self.assertTrue(gosbase_win.already_processed(self.conn, 7))

    def test_get_meta_missing_returns_none(self):
        self.assertIsNone(gosbase_win.get_meta(self.conn, "no_such_key"))

    def test_set_and_get_meta(self):
        gosbase_win.set_meta(self.conn, "foo", "bar")
        self.assertEqual(gosbase_win.get_meta(self.conn, "foo"), "bar")

    def test_set_meta_overwrites(self):
        gosbase_win.set_meta(self.conn, "k", "v1")
        gosbase_win.set_meta(self.conn, "k", "v2")
        self.assertEqual(gosbase_win.get_meta(self.conn, "k"), "v2")

    def test_different_contract_ids_independent(self):
        gosbase_win.mark_processed(self.conn, 1)
        self.assertFalse(gosbase_win.already_processed(self.conn, 2))


# ===========================================================================
# maybe_weekly_reset
# ===========================================================================
class TestMaybeWeeklyReset(unittest.TestCase):

    def setUp(self):
        self.conn = make_test_db()
        # добавим несколько записей
        self.conn.execute("INSERT INTO processed VALUES (1, '2024-01-01')")
        self.conn.execute("INSERT INTO processed VALUES (2, '2024-01-01')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    @patch("gosbase_win.STATE_DB")
    @patch("gosbase_win.shutil.copy2")
    def test_resets_when_no_last_reset_meta(self, mock_copy, mock_state_db):
        mock_state_db.exists.return_value = False
        gosbase_win.maybe_weekly_reset(self.conn)
        cur = self.conn.execute("SELECT COUNT(*) FROM processed")
        self.assertEqual(cur.fetchone()[0], 0)

    @patch("gosbase_win.STATE_DB")
    @patch("gosbase_win.shutil.copy2")
    def test_resets_when_week_has_passed(self, mock_copy, mock_state_db):
        mock_state_db.exists.return_value = False
        old_date = (datetime.utcnow() - timedelta(days=8)).isoformat(timespec="seconds")
        gosbase_win.set_meta(self.conn, "last_reset_utc", old_date)
        gosbase_win.maybe_weekly_reset(self.conn)
        cur = self.conn.execute("SELECT COUNT(*) FROM processed")
        self.assertEqual(cur.fetchone()[0], 0)

    @patch("gosbase_win.STATE_DB")
    @patch("gosbase_win.shutil.copy2")
    def test_skips_reset_when_recent(self, mock_copy, mock_state_db):
        mock_state_db.exists.return_value = False
        recent = (datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds")
        gosbase_win.set_meta(self.conn, "last_reset_utc", recent)
        gosbase_win.maybe_weekly_reset(self.conn)
        cur = self.conn.execute("SELECT COUNT(*) FROM processed")
        self.assertEqual(cur.fetchone()[0], 2)  # не тронуто

    @patch("gosbase_win.STATE_DB")
    @patch("gosbase_win.shutil.copy2")
    def test_makes_backup_when_file_exists(self, mock_copy, mock_state_db):
        mock_state_db.exists.return_value = True
        mock_state_db.suffix = ".sqlite"
        mock_state_db.with_suffix.return_value = Path("/tmp/backup.sqlite.bak")
        gosbase_win.maybe_weekly_reset(self.conn)
        mock_copy.assert_called_once()

    @patch("gosbase_win.STATE_DB")
    @patch("gosbase_win.shutil.copy2")
    def test_updates_last_reset_meta_after_reset(self, mock_copy, mock_state_db):
        mock_state_db.exists.return_value = False
        gosbase_win.maybe_weekly_reset(self.conn)
        last = gosbase_win.get_meta(self.conn, "last_reset_utc")
        self.assertIsNotNone(last)


# ===========================================================================
# has_success_deal_db
# ===========================================================================
class TestHasSuccessDealDb(unittest.TestCase):

    def _make_db_conn(self, found: bool):
        db_conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,) if found else None
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        db_conn.cursor.return_value = cursor
        return db_conn

    def test_returns_true_when_found(self):
        result = gosbase_win.has_success_deal_db(self._make_db_conn(True), "7712345678", 42)
        self.assertTrue(result)

    def test_returns_false_when_not_found(self):
        result = gosbase_win.has_success_deal_db(self._make_db_conn(False), "7712345678", 42)
        self.assertFalse(result)

    def test_returns_false_for_empty_inn(self):
        result = gosbase_win.has_success_deal_db(self._make_db_conn(True), "", 42)
        self.assertFalse(result)

    def test_returns_false_for_none_inn(self):
        result = gosbase_win.has_success_deal_db(self._make_db_conn(True), None, 42)
        self.assertFalse(result)

    def test_returns_false_for_zero_mop_id(self):
        result = gosbase_win.has_success_deal_db(self._make_db_conn(True), "7712345678", 0)
        self.assertFalse(result)

    def test_returns_false_for_none_mop_id(self):
        result = gosbase_win.has_success_deal_db(self._make_db_conn(True), "7712345678", None)
        self.assertFalse(result)


# ===========================================================================
# Bitrix24.call
# ===========================================================================
class TestBitrix24Call(unittest.TestCase):

    def setUp(self):
        self.b24 = make_b24()

    def test_passes_response_through(self):
        self.b24.client.call.return_value = {"result": [1, 2]}
        result = self.b24.call("crm.company.list", {})
        self.assertEqual(result, {"result": [1, 2]})

    def test_raises_bitrix_unavailable_on_runtime_error_call_failed(self):
        self.b24.client.call.side_effect = RuntimeError("Bitrix call failed after 5 retries")
        with self.assertRaises(gosbase_win.BitrixUnavailableError):
            self.b24.call("crm.company.list", {})

    def test_raises_bitrix_unavailable_on_non_json_response(self):
        self.b24.client.call.side_effect = RuntimeError("Bitrix non-json response abc")
        with self.assertRaises(gosbase_win.BitrixUnavailableError):
            self.b24.call("crm.company.list", {})

    def test_reraises_other_runtime_errors(self):
        self.b24.client.call.side_effect = RuntimeError("Something else went wrong")
        with self.assertRaises(RuntimeError) as ctx:
            self.b24.call("crm.company.list", {})
        self.assertNotIsInstance(ctx.exception, gosbase_win.BitrixUnavailableError)


# ===========================================================================
# Bitrix24.find_company
# ===========================================================================
class TestBitrix24FindCompany(unittest.TestCase):

    def setUp(self):
        self.b24 = make_b24()

    def test_returns_first_company(self):
        company = {"ID": "10", "TITLE": "ООО Ромашка", "ASSIGNED_BY_ID": "5"}
        self.b24.client.call.return_value = {"result": [company]}
        result = self.b24.find_company("7712345678")
        self.assertEqual(result["ID"], "10")

    def test_returns_none_when_not_found(self):
        self.b24.client.call.return_value = {"result": []}
        self.assertIsNone(self.b24.find_company("7712345678"))

    def test_returns_none_when_result_missing(self):
        self.b24.client.call.return_value = {}
        self.assertIsNone(self.b24.find_company("7712345678"))


# ===========================================================================
# Bitrix24.find_allowed_lead_by_inn
# ===========================================================================
class TestBitrix24FindAllowedLeadByInn(unittest.TestCase):

    def setUp(self):
        self.b24 = make_b24()

    def test_returns_none_for_invalid_inn(self):
        # normalize_inn("bad") вернёт "" → сразу None без вызова API
        result = self.b24.find_allowed_lead_by_inn("bad")
        self.assertIsNone(result)
        self.b24.client.call.assert_not_called()

    def test_returns_lead_when_found(self):
        lead = {"ID": "1", "TITLE": "Лид", "STATUS_ID": "STAGE_PROCESSED", "ASSIGNED_BY_ID": "7"}
        self.b24.client.call.return_value = {"result": [lead]}
        result = self.b24.find_allowed_lead_by_inn("7712345678")
        self.assertEqual(result["ID"], "1")

    def test_returns_none_when_no_leads(self):
        self.b24.client.call.return_value = {"result": []}
        self.assertIsNone(self.b24.find_allowed_lead_by_inn("7712345678"))


# ===========================================================================
# Bitrix24.find_existing_winner_lead
# ===========================================================================
class TestBitrix24FindExistingWinnerLead(unittest.TestCase):

    def setUp(self):
        self.b24 = make_b24()
        self.inn_field = gosbase_win.LEAD_INN_FIELD
        self.purchase_field = gosbase_win.LEAD_PURCHASE_FIELD

    def test_returns_none_for_invalid_inn(self):
        result = self.b24.find_existing_winner_lead("bad", "1234")
        self.assertIsNone(result)
        self.b24.client.call.assert_not_called()

    def test_returns_matching_lead(self):
        lead = {
            "ID": "55",
            self.inn_field: "7712345678",
            self.purchase_field: "0123456789012345",
        }
        self.b24.client.call.return_value = {"result": [lead]}
        result = self.b24.find_existing_winner_lead("7712345678", "0123456789012345")
        self.assertEqual(result["ID"], "55")

    def test_skips_lead_with_different_inn(self):
        lead = {
            "ID": "55",
            self.inn_field: "9999999999",
            self.purchase_field: "0123456789012345",
        }
        self.b24.client.call.return_value = {"result": [lead]}
        result = self.b24.find_existing_winner_lead("7712345678", "0123456789012345")
        self.assertIsNone(result)

    def test_skips_lead_with_different_purchase(self):
        lead = {
            "ID": "55",
            self.inn_field: "7712345678",
            self.purchase_field: "OTHER_NUMBER",
        }
        self.b24.client.call.return_value = {"result": [lead]}
        result = self.b24.find_existing_winner_lead("7712345678", "0123456789012345")
        self.assertIsNone(result)

    def test_returns_lead_when_no_purchase_number(self):
        # если purchase_number пустой — фильтрация по нему не применяется
        lead = {
            "ID": "77",
            self.inn_field: "7712345678",
            self.purchase_field: "",
        }
        self.b24.client.call.return_value = {"result": [lead]}
        result = self.b24.find_existing_winner_lead("7712345678", "")
        self.assertEqual(result["ID"], "77")


# ===========================================================================
# Bitrix24.create_lead
# ===========================================================================
class TestBitrix24CreateLead(unittest.TestCase):

    def setUp(self):
        self.b24 = make_b24()

    def test_returns_lead_id(self):
        self.b24.client.call.return_value = {"result": 123}
        lead_id = self.b24.create_lead({"TITLE": "Тест"})
        self.assertEqual(lead_id, 123)

    def test_raises_when_id_missing(self):
        self.b24.client.call.return_value = {"result": None}
        with self.assertRaises(RuntimeError):
            self.b24.create_lead({"TITLE": "Тест"})

    def test_raises_when_result_key_absent(self):
        self.b24.client.call.return_value = {}
        with self.assertRaises(RuntimeError):
            self.b24.create_lead({"TITLE": "Тест"})


# ===========================================================================
# Bitrix24.get_lead_observers
# ===========================================================================
class TestBitrix24GetLeadObservers(unittest.TestCase):

    def setUp(self):
        self.b24 = make_b24()

    def test_extracts_observer_ids(self):
        self.b24.client.call.return_value = {
            "result": {"item": {"observers": [1, 2, 3]}}
        }
        self.assertEqual(self.b24.get_lead_observers(10), [1, 2, 3])

    def test_deduplicates(self):
        self.b24.client.call.return_value = {
            "result": {"item": {"observers": [5, 5, 5]}}
        }
        self.assertEqual(self.b24.get_lead_observers(10), [5])

    def test_returns_empty_when_no_observers(self):
        self.b24.client.call.return_value = {
            "result": {"item": {"observers": []}}
        }
        self.assertEqual(self.b24.get_lead_observers(10), [])

    def test_returns_empty_when_item_missing(self):
        self.b24.client.call.return_value = {"result": {}}
        self.assertEqual(self.b24.get_lead_observers(10), [])

    def test_skips_non_int_observers(self):
        self.b24.client.call.return_value = {
            "result": {"item": {"observers": [1, "abc", None, 3]}}
        }
        self.assertEqual(self.b24.get_lead_observers(10), [1, 3])


# ===========================================================================
# Bitrix24.set_lead_observers
# ===========================================================================
class TestBitrix24SetLeadObservers(unittest.TestCase):

    def setUp(self):
        self.b24 = make_b24()

    def test_skips_when_no_valid_ids(self):
        self.b24.set_lead_observers(10, [])
        self.b24.client.call.assert_not_called()

    def test_sets_and_verifies(self):
        # crm.item.update → ничего, crm.item.get → возвращает тех же observers
        self.b24.client.call.side_effect = [
            {},                                                    # crm.item.update
            {"result": {"item": {"observers": [1, 2]}}},          # crm.item.get
        ]
        self.b24.set_lead_observers(10, [1, 2])  # не должен падать

    def test_raises_on_observer_mismatch(self):
        self.b24.client.call.side_effect = [
            {},                                                    # crm.item.update
            {"result": {"item": {"observers": [1]}}},             # crm.item.get — observer 2 пропал
        ]
        with self.assertRaises(RuntimeError):
            self.b24.set_lead_observers(10, [1, 2])

    def test_deduplicates_input(self):
        self.b24.client.call.side_effect = [
            {},
            {"result": {"item": {"observers": [5]}}},
        ]
        self.b24.set_lead_observers(10, [5, 5, 5])  # не должен падать


if __name__ == "__main__":
    unittest.main(verbosity=2)
