#!/usr/bin/env python3
"""Unit tests for mpb_deals.py"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Добавляем родительскую папку в путь, чтобы импортировать mpb_deals.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import datetime

# ---------------------------------------------------------------------------
# Mock all server-side modules BEFORE importing mpb_deals,
# because mpb_deals runs module-level code (field_code, lead_stage, AppLogger)
# ---------------------------------------------------------------------------
_mock_logger = MagicMock()
_mock_app_logger_mod = MagicMock()
_mock_app_logger_mod.AppLogger.return_value = _mock_logger

_mock_role_flag = MagicMock(return_value=False)
_mock_role_ids = MagicMock(return_value=[10, 20])
_mock_staff_roles_mod = MagicMock()
_mock_staff_roles_mod.role_flag = _mock_role_flag
_mock_staff_roles_mod.role_ids = _mock_role_ids

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
    ("staff_roles", _mock_staff_roles_mod),
]:
    sys.modules.setdefault(_name, _mod)

import mpb_deals  # noqa: E402  (must come after sys.modules patching)


# ===========================================================================
# to_date_str
# ===========================================================================
class TestToDateStr(unittest.TestCase):

    def test_valid_date_string(self):
        self.assertEqual(mpb_deals.to_date_str("2024-01-15"), "2024-01-15")

    def test_datetime_string_truncated_to_date(self):
        self.assertEqual(mpb_deals.to_date_str("2024-01-15 10:30:00"), "2024-01-15")

    def test_none_returns_none(self):
        self.assertIsNone(mpb_deals.to_date_str(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(mpb_deals.to_date_str(""))

    def test_zero_returns_none(self):
        self.assertIsNone(mpb_deals.to_date_str(0))

    def test_invalid_format_returns_none(self):
        self.assertIsNone(mpb_deals.to_date_str("15-01-2024"))

    def test_garbage_returns_none(self):
        self.assertIsNone(mpb_deals.to_date_str("not-a-date"))


# ===========================================================================
# norm_emp_id
# ===========================================================================
class TestNormEmpId(unittest.TestCase):

    def test_plain_int(self):
        self.assertEqual(mpb_deals.norm_emp_id(42), 42)

    def test_string_digit(self):
        self.assertEqual(mpb_deals.norm_emp_id("42"), 42)

    def test_user_prefix(self):
        self.assertEqual(mpb_deals.norm_emp_id("user_42"), 42)

    def test_none_returns_none(self):
        self.assertIsNone(mpb_deals.norm_emp_id(None))

    def test_empty_list_returns_none(self):
        self.assertIsNone(mpb_deals.norm_emp_id([]))

    def test_list_with_value(self):
        self.assertEqual(mpb_deals.norm_emp_id([99]), 99)

    def test_list_with_none_returns_none(self):
        self.assertIsNone(mpb_deals.norm_emp_id([None]))

    def test_non_digit_string_returns_none(self):
        self.assertIsNone(mpb_deals.norm_emp_id("abc"))

    def test_user_prefix_non_digit_returns_none(self):
        self.assertIsNone(mpb_deals.norm_emp_id("user_abc"))


# ===========================================================================
# to_flag
# ===========================================================================
class TestToFlag(unittest.TestCase):

    def test_y(self):
        self.assertEqual(mpb_deals.to_flag("Y"), 1)

    def test_yes(self):
        self.assertEqual(mpb_deals.to_flag("YES"), 1)

    def test_true(self):
        self.assertEqual(mpb_deals.to_flag("TRUE"), 1)

    def test_one_string(self):
        self.assertEqual(mpb_deals.to_flag("1"), 1)

    def test_on(self):
        self.assertEqual(mpb_deals.to_flag("ON"), 1)

    def test_lowercase_y(self):
        self.assertEqual(mpb_deals.to_flag("y"), 1)

    def test_n(self):
        self.assertEqual(mpb_deals.to_flag("N"), 0)

    def test_false(self):
        self.assertEqual(mpb_deals.to_flag("false"), 0)

    def test_empty_string(self):
        self.assertEqual(mpb_deals.to_flag(""), 0)

    def test_zero(self):
        self.assertEqual(mpb_deals.to_flag(0), 0)

    def test_none(self):
        self.assertEqual(mpb_deals.to_flag(None), 0)


# ===========================================================================
# normalized_inn
# ===========================================================================
class TestNormalizedInn(unittest.TestCase):

    def test_valid_inn(self):
        self.assertEqual(mpb_deals.normalized_inn("7712345678"), "7712345678")

    def test_none_returns_none(self):
        self.assertIsNone(mpb_deals.normalized_inn(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(mpb_deals.normalized_inn(""))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(mpb_deals.normalized_inn("   "))

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(mpb_deals.normalized_inn("  123  "), "123")


# ===========================================================================
# parse_bx_dt
# ===========================================================================
class TestParseBxDt(unittest.TestCase):

    def test_iso_format(self):
        self.assertEqual(
            mpb_deals.parse_bx_dt("2024-01-15T10:30:00"),
            datetime(2024, 1, 15, 10, 30, 0),
        )

    def test_datetime_with_space_separator(self):
        self.assertEqual(
            mpb_deals.parse_bx_dt("2024-01-15 10:30:00"),
            datetime(2024, 1, 15, 10, 30, 0),
        )

    def test_z_suffix_returns_naive_datetime(self):
        result = mpb_deals.parse_bx_dt("2024-01-15T10:30:00Z")
        self.assertIsNotNone(result)
        self.assertIsNone(result.tzinfo)
        self.assertEqual(result.date(), datetime(2024, 1, 15).date())

    def test_none_returns_none(self):
        self.assertIsNone(mpb_deals.parse_bx_dt(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(mpb_deals.parse_bx_dt(""))

    def test_invalid_string_returns_none(self):
        self.assertIsNone(mpb_deals.parse_bx_dt("not-a-date"))


# ===========================================================================
# normalize_vox_rows
# ===========================================================================
class TestNormalizeVoxRows(unittest.TestCase):

    def test_plain_list_of_dicts(self):
        data = [{"a": 1}, {"b": 2}]
        self.assertEqual(mpb_deals.normalize_vox_rows(data), data)

    def test_dict_with_items_key(self):
        self.assertEqual(mpb_deals.normalize_vox_rows({"items": [{"x": 1}]}), [{"x": 1}])

    def test_dict_with_result_key(self):
        self.assertEqual(mpb_deals.normalize_vox_rows({"result": [{"x": 1}]}), [{"x": 1}])

    def test_non_list_input_returns_empty(self):
        self.assertEqual(mpb_deals.normalize_vox_rows("string"), [])

    def test_none_returns_empty(self):
        self.assertEqual(mpb_deals.normalize_vox_rows(None), [])

    def test_empty_list_returns_empty(self):
        self.assertEqual(mpb_deals.normalize_vox_rows([]), [])

    def test_filters_out_non_dict_items(self):
        data = [{"a": 1}, "str", 42, None, {"b": 2}]
        self.assertEqual(mpb_deals.normalize_vox_rows(data), [{"a": 1}, {"b": 2}])


# ===========================================================================
# ensure_calls_call_id_unique
# ===========================================================================
class TestEnsureCallsCallIdUnique(unittest.TestCase):

    def _cursor(self, count):
        c = MagicMock()
        c.fetchone.return_value = (count,)
        return c

    def test_raises_when_unique_index_missing(self):
        with self.assertRaises(RuntimeError):
            mpb_deals.ensure_calls_call_id_unique(self._cursor(0))

    def test_passes_when_unique_index_exists(self):
        mpb_deals.ensure_calls_call_id_unique(self._cursor(1))  # no exception

    def test_raises_when_fetchone_returns_none(self):
        c = MagicMock()
        c.fetchone.return_value = None
        with self.assertRaises(RuntimeError):
            mpb_deals.ensure_calls_call_id_unique(c)


# ===========================================================================
# bx_call / bx_call_raw
# ===========================================================================
class TestBxCall(unittest.TestCase):

    def test_returns_result_field_from_dict(self):
        bx = MagicMock()
        bx.call.return_value = {"result": [1, 2, 3]}
        self.assertEqual(mpb_deals.bx_call(bx, "any.method"), [1, 2, 3])

    def test_returns_none_if_result_key_absent(self):
        bx = MagicMock()
        bx.call.return_value = {"other": "data"}
        self.assertIsNone(mpb_deals.bx_call(bx, "any.method"))

    def test_returns_value_as_is_when_not_dict(self):
        bx = MagicMock()
        bx.call.return_value = [7, 8, 9]
        self.assertEqual(mpb_deals.bx_call(bx, "any.method"), [7, 8, 9])

    def test_returns_empty_list_on_exception(self):
        bx = MagicMock()
        bx.call.side_effect = Exception("network error")
        self.assertEqual(mpb_deals.bx_call(bx, "any.method"), [])

    def test_raises_on_exception_when_raise_on_error(self):
        bx = MagicMock()
        bx.call.side_effect = Exception("network error")
        with self.assertRaises(Exception):
            mpb_deals.bx_call(bx, "any.method", raise_on_error=True)


# ===========================================================================
# load_managers
# ===========================================================================
class TestLoadManagers(unittest.TestCase):

    def setUp(self):
        _mock_role_ids.return_value = [10, 20]
        _mock_role_flag.return_value = False

    def _cursor(self, staff_rows, mop_rows=None):
        c = MagicMock()
        c.fetchall.side_effect = [
            staff_rows,
            mop_rows if mop_rows is not None else [],
        ]
        return c

    def test_loads_manager_in_sales_department(self):
        cursor = self._cursor([(1, "Ivan Ivanov", 10)])
        result = mpb_deals.load_managers(cursor)
        self.assertEqual(result, {1: "Ivan Ivanov"})

    def test_excludes_user_outside_sales_departments(self):
        cursor = self._cursor([(1, "Ivan", 10), (2, "Petr", 99)])
        result = mpb_deals.load_managers(cursor)
        self.assertIn(1, result)
        self.assertNotIn(2, result)

    def test_uses_id_placeholder_for_empty_name(self):
        cursor = self._cursor([(5, "", 10)])
        result = mpb_deals.load_managers(cursor)
        self.assertEqual(result[5], "ID 5")

    def test_raises_when_sales_department_ids_empty(self):
        _mock_role_ids.return_value = []
        with self.assertRaises(RuntimeError):
            mpb_deals.load_managers(MagicMock())

    def test_raises_when_staff_users_empty(self):
        c = MagicMock()
        c.fetchall.return_value = []
        with self.assertRaises(RuntimeError):
            mpb_deals.load_managers(c)

    def test_raises_when_no_managers_qualify(self):
        cursor = self._cursor([(1, "Ivan", 99)])  # dept 99 not in [10, 20]
        with self.assertRaises(RuntimeError):
            mpb_deals.load_managers(cursor)

    def test_includes_historical_mop_ids(self):
        _mock_role_flag.return_value = True
        c = MagicMock()
        # staff: uid=1 dept=99 (not sales), uid=2 dept=10 (sales)
        # historical mop_ids: uid=1
        c.fetchall.side_effect = [
            [(1, "Ivan", 99), (2, "Maria", 10)],
            [(1,)],
        ]
        result = mpb_deals.load_managers(c)
        self.assertIn(1, result)   # included via historical
        self.assertIn(2, result)   # included via sales dept

    def test_skips_invalid_user_id(self):
        cursor = self._cursor([("not_int", "Name", 10)])
        with self.assertRaises(RuntimeError):  # no managers loaded
            mpb_deals.load_managers(cursor)

    def test_skips_zero_user_id(self):
        cursor = self._cursor([(0, "Name", 10)])
        with self.assertRaises(RuntimeError):
            mpb_deals.load_managers(cursor)


if __name__ == "__main__":
    unittest.main(verbosity=2)
