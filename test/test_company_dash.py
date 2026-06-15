#!/usr/bin/env python3
"""Unit tests for company_dash.py"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

# Добавляем родительскую папку в путь, чтобы импортировать company_dash.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Mock all server-side modules BEFORE importing company_dash
# ---------------------------------------------------------------------------
_mock_logger = MagicMock()
_mock_app_logger_mod = MagicMock()
_mock_app_logger_mod.AppLogger.return_value = _mock_logger

_mock_bitrix_fields_mod = MagicMock()
_mock_bitrix_fields_mod.field_code = MagicMock(side_effect=lambda e, n: f"UF_CRM_{n.upper()}")

for _name, _mod in [
    ("app_logger", _mock_app_logger_mod),
    ("bitrix_client", MagicMock()),
    ("debug_utils", MagicMock()),
    ("bitrix_fields", _mock_bitrix_fields_mod),
    ("db", MagicMock()),
    ("env_loader", MagicMock()),
]:
    sys.modules.setdefault(_name, _mod)

import company_dash  # noqa: E402


# ===========================================================================
# join_multi
# ===========================================================================
class TestJoinMulti(unittest.TestCase):

    def test_plain_string(self):
        self.assertEqual(company_dash.join_multi("hello"), "hello")

    def test_string_stripped(self):
        self.assertEqual(company_dash.join_multi("  hi  "), "hi")

    def test_empty_string_returns_none(self):
        self.assertIsNone(company_dash.join_multi(""))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(company_dash.join_multi("   "))

    def test_none_returns_none(self):
        self.assertIsNone(company_dash.join_multi(None))

    def test_int_returns_none(self):
        self.assertIsNone(company_dash.join_multi(42))

    def test_list_of_dicts_with_value(self):
        data = [{"VALUE": "+79001234567"}, {"VALUE": "+79009876543"}]
        self.assertEqual(company_dash.join_multi(data), "+79001234567, +79009876543")

    def test_list_single_item(self):
        data = [{"VALUE": "test@mail.ru"}]
        self.assertEqual(company_dash.join_multi(data), "test@mail.ru")

    def test_list_with_missing_value_key(self):
        self.assertIsNone(company_dash.join_multi([{"OTHER": "x"}]))

    def test_list_with_empty_value(self):
        self.assertIsNone(company_dash.join_multi([{"VALUE": ""}]))

    def test_list_mixed_valid_and_empty(self):
        data = [{"VALUE": ""}, {"VALUE": "89991234567"}]
        self.assertEqual(company_dash.join_multi(data), "89991234567")

    def test_empty_list_returns_none(self):
        self.assertIsNone(company_dash.join_multi([]))

    def test_list_with_non_dict_items_ignored(self):
        data = ["string", None, {"VALUE": "valid"}]
        self.assertEqual(company_dash.join_multi(data), "valid")


# ===========================================================================
# normalize_inn
# ===========================================================================
class TestNormalizeInn(unittest.TestCase):

    def test_valid_10_digit_inn(self):
        self.assertEqual(company_dash.normalize_inn("7712345678"), "7712345678")

    def test_valid_12_digit_inn(self):
        self.assertEqual(company_dash.normalize_inn("771234567890"), "771234567890")

    def test_9_digits_returns_empty(self):
        self.assertEqual(company_dash.normalize_inn("123456789"), "")

    def test_11_digits_returns_empty(self):
        self.assertEqual(company_dash.normalize_inn("12345678901"), "")

    def test_13_digits_returns_empty(self):
        self.assertEqual(company_dash.normalize_inn("1234567890123"), "")

    def test_none_returns_empty(self):
        self.assertEqual(company_dash.normalize_inn(None), "")

    def test_empty_string_returns_empty(self):
        self.assertEqual(company_dash.normalize_inn(""), "")

    def test_strips_spaces_and_checks_digits(self):
        self.assertEqual(company_dash.normalize_inn("  7712345678  "), "7712345678")

    def test_extracts_digits_from_mixed_string(self):
        # "771-234-5678" → "7712345678" (10 цифр) → валидный
        self.assertEqual(company_dash.normalize_inn("771-234-5678"), "7712345678")

    def test_letters_only_returns_empty(self):
        self.assertEqual(company_dash.normalize_inn("abcdef"), "")

    def test_integer_input_10_digits(self):
        self.assertEqual(company_dash.normalize_inn(7712345678), "7712345678")


# ===========================================================================
# mpb_signature
# ===========================================================================
class TestMpbSignature(unittest.TestCase):

    def test_formats_signature_correctly(self):
        cur = MagicMock()
        cur.fetchone.return_value = (999, 42, "2024-01-15", 123456)
        result = company_dash.mpb_signature(cur)
        self.assertEqual(result, "id=999;cnt=42;dt=2024-01-15;crc=123456")

    def test_zero_values(self):
        cur = MagicMock()
        cur.fetchone.return_value = (0, 0, "", 0)
        result = company_dash.mpb_signature(cur)
        self.assertEqual(result, "id=0;cnt=0;dt=;crc=0")


# ===========================================================================
# should_run
# ===========================================================================
class TestShouldRun(unittest.TestCase):

    def _make_cursor(self, stored_sig, current_sig_row):
        """
        stored_sig       — подпись уже сохранённая в БД (или None если строки нет)
        current_sig_row  — кортеж (mx, cnt, dt, crc) который вернёт mpb_signature
        """
        cur = MagicMock()
        # fetchone вызывается 2 раза:
        # 1й — mpb_signature (mx, cnt, dt, checksum)
        # 2й — SELECT signature FROM dashboard_sync_state
        state_row = (stored_sig,) if stored_sig is not None else None
        cur.fetchone.side_effect = [current_sig_row, state_row]
        return cur

    def test_returns_false_when_signature_unchanged(self):
        sig_row = (1, 10, "2024-01-01", 999)
        expected_sig = "id=1;cnt=10;dt=2024-01-01;crc=999"
        cur = self._make_cursor(expected_sig, sig_row)

        run, sig = company_dash.should_run(cur)
        self.assertFalse(run)
        self.assertEqual(sig, expected_sig)

    def test_returns_true_when_signature_changed(self):
        sig_row = (2, 10, "2024-01-02", 888)
        cur = self._make_cursor("id=1;cnt=10;dt=2024-01-01;crc=999", sig_row)

        run, sig = company_dash.should_run(cur)
        self.assertTrue(run)
        self.assertEqual(sig, "id=2;cnt=10;dt=2024-01-02;crc=888")

    def test_returns_true_when_no_stored_signature(self):
        sig_row = (5, 3, "2024-03-01", 777)
        cur = self._make_cursor(None, sig_row)
        # fetchone для state вернёт None (нет строки)
        cur.fetchone.side_effect = [sig_row, None]

        run, sig = company_dash.should_run(cur)
        self.assertTrue(run)


# ===========================================================================
# finalize_signature
# ===========================================================================
class TestFinalizeSignature(unittest.TestCase):

    def test_executes_upsert_with_correct_signature(self):
        cur = MagicMock()
        company_dash.finalize_signature(cur, "id=1;cnt=5;dt=2024-01-01;crc=123")
        cur.execute.assert_called_once()
        args = cur.execute.call_args
        # второй аргумент — кортеж с подписью
        self.assertIn("id=1;cnt=5;dt=2024-01-01;crc=123", args[0][1])


# ===========================================================================
# load_aggregates
# ===========================================================================
class TestLoadAggregates(unittest.TestCase):

    def test_returns_empty_when_no_rows(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        inns, agg = company_dash.load_aggregates(cur)
        self.assertEqual(inns, [])
        self.assertEqual(agg, {})

    def test_skips_invalid_inn(self):
        cur = MagicMock()
        # ИНН "123" невалидный (не 10 и не 12 цифр)
        cur.fetchall.return_value = [("123", 1, 1, 100.0, 10.0, "2024-01-01")]
        inns, agg = company_dash.load_aggregates(cur)
        self.assertEqual(inns, [])
        self.assertEqual(agg, {})

    def test_parses_aggregate_row_correctly(self):
        cur = MagicMock()
        cur.fetchall.side_effect = [
            # первый fetchall — агрегаты
            [("7712345678", 3, 2, 500000.0, 15000.0, "2024-06-01")],
            # второй fetchall — mop/статус последней сделки
            [("7712345678", 42, "Иванов", "S")],
        ]
        inns, agg = company_dash.load_aggregates(cur)

        self.assertEqual(inns, ["7712345678"])
        self.assertIn("7712345678", agg)
        data = agg["7712345678"]
        self.assertEqual(data["deals_count"], 3)
        self.assertEqual(data["success_deals_count"], 2)
        self.assertAlmostEqual(data["total_bg_sum"], 500000.0)
        self.assertAlmostEqual(data["total_kv_sum"], 15000.0)
        self.assertEqual(data["last_issue_date"], "2024-06-01")
        self.assertEqual(data["mop_id"], 42)
        self.assertEqual(data["mop"], "Иванов")
        self.assertEqual(data["last_deal_status"], "S")

    def test_mop_defaults_to_none_if_not_in_second_query(self):
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [("7712345678", 1, 1, 100.0, 5.0, "2024-01-01")],
            [],  # второй запрос ничего не вернул
        ]
        inns, agg = company_dash.load_aggregates(cur)
        self.assertIsNone(agg["7712345678"]["mop_id"])
        self.assertIsNone(agg["7712345678"]["mop"])

    def test_multiple_inns(self):
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [
                ("7712345678", 2, 1, 200.0, 10.0, "2024-01-01"),
                ("771234567890", 1, 1, 100.0, 5.0, "2024-02-01"),
            ],
            [],
        ]
        inns, agg = company_dash.load_aggregates(cur)
        self.assertEqual(len(inns), 2)
        self.assertIn("7712345678", agg)
        self.assertIn("771234567890", agg)


# ===========================================================================
# load_companies
# ===========================================================================
class TestLoadCompanies(unittest.TestCase):

    UF_INN = company_dash.UF_COMPANY_INN  # "UF_CRM_INN"

    def _make_bitrix(self, companies):
        bx = MagicMock()
        bx.list_all.return_value = companies
        return bx

    def test_returns_company_by_inn(self):
        bx = self._make_bitrix([{
            "ID": "101",
            self.UF_INN: "7712345678",
            "TITLE": "ООО Ромашка",
            "PHONE": [{"VALUE": "+79001234567"}],
            "EMAIL": [{"VALUE": "info@romashka.ru"}],
        }])
        result = company_dash.load_companies(bx)
        self.assertIn("7712345678", result)
        c = result["7712345678"]
        self.assertEqual(c["company_id"], 101)
        self.assertEqual(c["name"], "ООО Ромашка")
        self.assertEqual(c["phone"], "+79001234567")
        self.assertEqual(c["email"], "info@romashka.ru")

    def test_skips_company_without_inn(self):
        bx = self._make_bitrix([{
            "ID": "1",
            self.UF_INN: "",
            "TITLE": "Без ИНН",
            "PHONE": None,
            "EMAIL": None,
        }])
        result = company_dash.load_companies(bx)
        self.assertEqual(result, {})

    def test_skips_invalid_company_id(self):
        bx = self._make_bitrix([{
            "ID": "not_a_number",
            self.UF_INN: "7712345678",
            "TITLE": "Плохой ID",
            "PHONE": None,
            "EMAIL": None,
        }])
        result = company_dash.load_companies(bx)
        self.assertEqual(result, {})

    def test_keeps_first_on_duplicate_inn(self):
        bx = self._make_bitrix([
            {"ID": "1", self.UF_INN: "7712345678", "TITLE": "Первая", "PHONE": None, "EMAIL": None},
            {"ID": "2", self.UF_INN: "7712345678", "TITLE": "Вторая", "PHONE": None, "EMAIL": None},
        ])
        result = company_dash.load_companies(bx)
        self.assertEqual(len(result), 1)
        self.assertEqual(result["7712345678"]["company_id"], 1)
        self.assertEqual(result["7712345678"]["name"], "Первая")

    def test_phone_and_email_can_be_none(self):
        bx = self._make_bitrix([{
            "ID": "5",
            self.UF_INN: "7712345678",
            "TITLE": "Без контактов",
            "PHONE": None,
            "EMAIL": None,
        }])
        result = company_dash.load_companies(bx)
        self.assertIsNone(result["7712345678"]["phone"])
        self.assertIsNone(result["7712345678"]["email"])

    def test_empty_title_stored_as_empty_string(self):
        bx = self._make_bitrix([{
            "ID": "7",
            self.UF_INN: "7712345678",
            "TITLE": None,
            "PHONE": None,
            "EMAIL": None,
        }])
        result = company_dash.load_companies(bx)
        self.assertEqual(result["7712345678"]["name"], "")

    def test_empty_bitrix_response_returns_empty(self):
        bx = self._make_bitrix([])
        result = company_dash.load_companies(bx)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
