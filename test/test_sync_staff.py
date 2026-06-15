#!/usr/bin/env python3
"""Unit tests for sync_staff.py"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Добавляем родительскую папку в путь, чтобы импортировать sync_staff.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Mock all server-side modules BEFORE importing sync_staff
# ---------------------------------------------------------------------------
_mock_logger = MagicMock()
_mock_app_logger_mod = MagicMock()
_mock_app_logger_mod.AppLogger.return_value = _mock_logger

for _name, _mod in [
    ("app_logger", _mock_app_logger_mod),
    ("bitrix_client", MagicMock()),
    ("db", MagicMock()),
    ("debug_utils", MagicMock()),
    ("env_loader", MagicMock()),
    ("staff_roles", MagicMock()),
]:
    sys.modules.setdefault(_name, _mod)

import sync_staff  # noqa: E402


# ===========================================================================
# parse_admins
# ===========================================================================
class TestParseAdmins(unittest.TestCase):

    def test_single_id(self):
        self.assertEqual(sync_staff.parse_admins("42"), [42])

    def test_multiple_ids(self):
        self.assertEqual(sync_staff.parse_admins("1,2,3"), [1, 2, 3])

    def test_deduplicates(self):
        self.assertEqual(sync_staff.parse_admins("5,5,5"), [5])

    def test_returns_sorted(self):
        self.assertEqual(sync_staff.parse_admins("3,1,2"), [1, 2, 3])

    def test_ignores_non_numeric(self):
        self.assertEqual(sync_staff.parse_admins("1,abc,3"), [1, 3])

    def test_ignores_zero_and_negative(self):
        self.assertEqual(sync_staff.parse_admins("0,-1,5"), [5])

    def test_whitespace_around_ids(self):
        self.assertEqual(sync_staff.parse_admins(" 1 , 2 "), [1, 2])

    def test_none_returns_empty(self):
        self.assertEqual(sync_staff.parse_admins(None), [])

    def test_empty_string_returns_empty(self):
        self.assertEqual(sync_staff.parse_admins(""), [])


# ===========================================================================
# normalize_active
# ===========================================================================
class TestNormalizeActive(unittest.TestCase):

    def test_y(self):
        self.assertEqual(sync_staff.normalize_active("Y"), 1)

    def test_lowercase_y(self):
        self.assertEqual(sync_staff.normalize_active("y"), 1)

    def test_one_string(self):
        self.assertEqual(sync_staff.normalize_active("1"), 1)

    def test_true_bool(self):
        self.assertEqual(sync_staff.normalize_active(True), 1)

    def test_one_int(self):
        self.assertEqual(sync_staff.normalize_active(1), 1)

    def test_n(self):
        self.assertEqual(sync_staff.normalize_active("N"), 0)

    def test_lowercase_n(self):
        self.assertEqual(sync_staff.normalize_active("n"), 0)

    def test_zero_string(self):
        self.assertEqual(sync_staff.normalize_active("0"), 0)

    def test_false_bool(self):
        self.assertEqual(sync_staff.normalize_active(False), 0)

    def test_zero_int(self):
        self.assertEqual(sync_staff.normalize_active(0), 0)

    def test_none(self):
        self.assertEqual(sync_staff.normalize_active(None), 0)

    def test_empty_string(self):
        self.assertEqual(sync_staff.normalize_active(""), 0)

    def test_unknown_value(self):
        self.assertEqual(sync_staff.normalize_active("MAYBE"), 0)


# ===========================================================================
# iso_to_mysql
# ===========================================================================
class TestIsoToMysql(unittest.TestCase):

    def test_utc_z_converted_to_moscow(self):
        # 10:00 UTC → 13:00 Москва (UTC+3)
        self.assertEqual(sync_staff.iso_to_mysql("2024-01-15T10:00:00Z"), "2024-01-15 13:00:00")

    def test_explicit_utc_offset_converted_to_moscow(self):
        self.assertEqual(sync_staff.iso_to_mysql("2024-01-15T10:00:00+00:00"), "2024-01-15 13:00:00")

    def test_moscow_offset_unchanged(self):
        # 10:00+03:00 — уже Москва
        self.assertEqual(sync_staff.iso_to_mysql("2024-01-15T10:00:00+03:00"), "2024-01-15 10:00:00")

    def test_naive_datetime_kept_as_is(self):
        self.assertEqual(sync_staff.iso_to_mysql("2024-01-15T10:00:00"), "2024-01-15 10:00:00")

    def test_none_returns_none(self):
        self.assertIsNone(sync_staff.iso_to_mysql(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(sync_staff.iso_to_mysql(""))

    def test_invalid_string_returns_none(self):
        self.assertIsNone(sync_staff.iso_to_mysql("not-a-date"))

    def test_zero_returns_none(self):
        self.assertIsNone(sync_staff.iso_to_mysql(0))


# ===========================================================================
# build_full_name
# ===========================================================================
class TestBuildFullName(unittest.TestCase):

    def test_all_three_parts(self):
        user = {"LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович"}
        self.assertEqual(sync_staff.build_full_name(user), "Иванов Иван Иванович")

    def test_no_second_name(self):
        user = {"LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": ""}
        self.assertEqual(sync_staff.build_full_name(user), "Иванов Иван")

    def test_only_last_name(self):
        user = {"LAST_NAME": "Иванов", "NAME": "", "SECOND_NAME": None}
        self.assertEqual(sync_staff.build_full_name(user), "Иванов")

    def test_empty_user_returns_empty(self):
        self.assertEqual(sync_staff.build_full_name({}), "")

    def test_none_values_skipped(self):
        user = {"LAST_NAME": None, "NAME": None, "SECOND_NAME": None}
        self.assertEqual(sync_staff.build_full_name(user), "")

    def test_strips_whitespace_from_parts(self):
        user = {"LAST_NAME": "  Иванов  ", "NAME": "  Иван  ", "SECOND_NAME": ""}
        self.assertEqual(sync_staff.build_full_name(user), "Иванов Иван")


# ===========================================================================
# int_list
# ===========================================================================
class TestIntList(unittest.TestCase):

    def test_plain_ints(self):
        self.assertEqual(sync_staff.int_list([1, 2, 3]), [1, 2, 3])

    def test_string_numbers(self):
        self.assertEqual(sync_staff.int_list(["1", "2"]), [1, 2])

    def test_filters_zero_and_negative(self):
        self.assertEqual(sync_staff.int_list([0, -1, 5]), [5])

    def test_deduplicates_and_sorts(self):
        self.assertEqual(sync_staff.int_list([3, 3, 1, 2]), [1, 2, 3])

    def test_skips_non_convertible(self):
        self.assertEqual(sync_staff.int_list([1, "abc", None, 3]), [1, 3])

    def test_non_list_returns_empty(self):
        self.assertEqual(sync_staff.int_list(None), [])
        self.assertEqual(sync_staff.int_list("abc"), [])
        self.assertEqual(sync_staff.int_list(42), [])

    def test_empty_list(self):
        self.assertEqual(sync_staff.int_list([]), [])


# ===========================================================================
# build_department_maps
# ===========================================================================
class TestBuildDepartmentMaps(unittest.TestCase):

    def test_builds_name_map(self):
        depts = [
            {"ID": 1, "NAME": "Продажи", "UF_HEAD": 0, "PARENT": 0},
            {"ID": 2, "NAME": "Маркетинг", "UF_HEAD": 0, "PARENT": 0},
        ]
        names, _ = sync_staff.build_department_maps(depts)
        self.assertEqual(names[1], "Продажи")
        self.assertEqual(names[2], "Маркетинг")

    def test_builds_heads_map(self):
        depts = [{"ID": 10, "NAME": "Отдел", "UF_HEAD": 42, "PARENT": 0}]
        _, heads = sync_staff.build_department_maps(depts)
        self.assertEqual(heads[42], [10])

    def test_user_heads_multiple_departments_sorted(self):
        depts = [
            {"ID": 20, "NAME": "B", "UF_HEAD": 42, "PARENT": 0},
            {"ID": 10, "NAME": "A", "UF_HEAD": 42, "PARENT": 0},
        ]
        _, heads = sync_staff.build_department_maps(depts)
        self.assertEqual(heads[42], [10, 20])

    def test_skips_zero_dept_id(self):
        depts = [{"ID": 0, "NAME": "Плохой", "UF_HEAD": 0, "PARENT": 0}]
        names, heads = sync_staff.build_department_maps(depts)
        self.assertEqual(names, {})

    def test_skips_empty_department_name(self):
        depts = [{"ID": 5, "NAME": "", "UF_HEAD": 0, "PARENT": 0}]
        names, _ = sync_staff.build_department_maps(depts)
        self.assertNotIn(5, names)

    def test_empty_input(self):
        names, heads = sync_staff.build_department_maps([])
        self.assertEqual(names, {})
        self.assertEqual(heads, {})

    def test_no_head_for_dept(self):
        depts = [{"ID": 1, "NAME": "Отдел", "UF_HEAD": 0, "PARENT": 0}]
        _, heads = sync_staff.build_department_maps(depts)
        self.assertEqual(heads, {})


# ===========================================================================
# pick_primary_department
# ===========================================================================
class TestPickPrimaryDepartment(unittest.TestCase):

    @patch("sync_staff.role_ids", return_value=[])
    def test_returns_first_sorted_when_no_priority(self, _):
        self.assertEqual(sync_staff.pick_primary_department([5, 3, 8]), 3)

    @patch("sync_staff.role_ids", return_value=[8])
    def test_returns_preferred_department(self, _):
        self.assertEqual(sync_staff.pick_primary_department([5, 3, 8]), 8)

    @patch("sync_staff.role_ids", return_value=[99])
    def test_falls_back_to_first_when_preferred_absent(self, _):
        self.assertEqual(sync_staff.pick_primary_department([5, 3]), 3)

    @patch("sync_staff.role_ids", return_value=[])
    def test_empty_list_returns_none(self, _):
        self.assertIsNone(sync_staff.pick_primary_department([]))

    @patch("sync_staff.role_ids", return_value=[])
    def test_single_department(self, _):
        self.assertEqual(sync_staff.pick_primary_department([7]), 7)

    @patch("sync_staff.role_ids", return_value=[])
    def test_filters_zero_and_negative(self, _):
        self.assertIsNone(sync_staff.pick_primary_department([0, -1]))


# ===========================================================================
# department_name
# ===========================================================================
class TestDepartmentName(unittest.TestCase):

    def test_returns_name_from_bitrix_map(self):
        self.assertEqual(sync_staff.department_name(10, {10: "Продажи"}), "Продажи")

    @patch("sync_staff.role_map", return_value={"10": "Из конфига"})
    def test_falls_back_to_role_map_str_key(self, _):
        self.assertEqual(sync_staff.department_name(10, {}), "Из конфига")

    @patch("sync_staff.role_map", return_value={10: "Из конфига int"})
    def test_falls_back_to_role_map_int_key(self, _):
        self.assertEqual(sync_staff.department_name(10, {}), "Из конфига int")

    @patch("sync_staff.role_map", return_value={})
    def test_returns_placeholder_when_not_found(self, _):
        self.assertEqual(sync_staff.department_name(99, {}), "Department #99")

    def test_none_id_returns_none(self):
        self.assertIsNone(sync_staff.department_name(None, {}))

    def test_zero_id_returns_none(self):
        self.assertIsNone(sync_staff.department_name(0, {}))


# ===========================================================================
# bitrix_call
# ===========================================================================
class TestBitrixCall(unittest.TestCase):

    def test_returns_response_on_success(self):
        bx = MagicMock()
        bx.call.return_value = {"result": [1, 2]}
        result = sync_staff.bitrix_call(bx, "user.get")
        self.assertEqual(result, {"result": [1, 2]})

    def test_raises_runtime_error_on_exception(self):
        bx = MagicMock()
        bx.call.side_effect = Exception("timeout")
        with self.assertRaises(RuntimeError):
            sync_staff.bitrix_call(bx, "user.get")

    def test_passes_params_to_call(self):
        bx = MagicMock()
        bx.call.return_value = {}
        sync_staff.bitrix_call(bx, "user.get", {"filter": {"ACTIVE": "Y"}})
        bx.call.assert_called_once_with(
            "user.get",
            {"filter": {"ACTIVE": "Y"}},
            timeout=60,
            max_retries=5,
            backoff_base_sec=2.0,
        )

    def test_passes_empty_dict_when_no_params(self):
        bx = MagicMock()
        bx.call.return_value = {}
        sync_staff.bitrix_call(bx, "department.get")
        bx.call.assert_called_once_with(
            "department.get", {}, timeout=60, max_retries=5, backoff_base_sec=2.0
        )


# ===========================================================================
# build_user_row
# ===========================================================================
class TestBuildUserRow(unittest.TestCase):

    def _user(self, uid="5", last="Иванов", name="Иван", active="Y", depts=None):
        return {
            "ID": uid,
            "LAST_NAME": last,
            "NAME": name,
            "SECOND_NAME": "",
            "ACTIVE": active,
            "DATE_REGISTER": None,
            "UF_DEPARTMENT": depts if depts is not None else [10],
        }

    @patch("sync_staff.role_ids", return_value=[])
    @patch("sync_staff.role_map", return_value={})
    def test_basic_row_fields(self, _rm, _ri):
        row = sync_staff.build_user_row(self._user(), set(), {10: "Продажи"}, {})
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 5)              # user_id
        self.assertEqual(row[1], "Иванов Иван")  # full_name
        self.assertEqual(row[2], 10)             # primary_dept
        self.assertEqual(row[3], "Продажи")      # dept_name
        self.assertEqual(row[4], 0)              # is_admin
        self.assertEqual(row[5], 0)              # is_head
        self.assertEqual(row[6], "")             # head_department_ids
        self.assertIsNone(row[7])                # head_department_names
        self.assertEqual(row[8], 1)              # is_active

    @patch("sync_staff.role_ids", return_value=[])
    @patch("sync_staff.role_map", return_value={})
    def test_admin_flag_set(self, _rm, _ri):
        row = sync_staff.build_user_row(self._user(uid="7"), {7}, {}, {})
        self.assertEqual(row[4], 1)

    @patch("sync_staff.role_ids", return_value=[])
    @patch("sync_staff.role_map", return_value={})
    def test_head_flag_and_fields(self, _rm, _ri):
        row = sync_staff.build_user_row(
            self._user(uid="3", depts=[]),
            set(),
            {10: "Отдел"},
            {3: [10]},
        )
        self.assertEqual(row[5], 1)         # is_head
        self.assertEqual(row[6], "10")      # head_department_ids
        self.assertEqual(row[7], "Отдел")   # head_department_names

    @patch("sync_staff.role_ids", return_value=[])
    @patch("sync_staff.role_map", return_value={})
    def test_multiple_headed_departments(self, _rm, _ri):
        row = sync_staff.build_user_row(
            self._user(uid="4", depts=[]),
            set(),
            {10: "Отдел А", 20: "Отдел Б"},
            {4: [10, 20]},
        )
        self.assertEqual(row[6], "10,20")
        self.assertEqual(row[7], "Отдел А | Отдел Б")

    @patch("sync_staff.role_ids", return_value=[])
    @patch("sync_staff.role_map", return_value={})
    def test_inactive_user(self, _rm, _ri):
        row = sync_staff.build_user_row(self._user(active="N"), set(), {10: "X"}, {})
        self.assertEqual(row[8], 0)

    @patch("sync_staff.role_ids", return_value=[])
    @patch("sync_staff.role_map", return_value={})
    def test_empty_name_gets_placeholder(self, _rm, _ri):
        row = sync_staff.build_user_row(
            self._user(uid="9", last="", name=""),
            set(), {}, {},
        )
        self.assertEqual(row[1], "ID 9")

    @patch("sync_staff.role_ids", return_value=[])
    @patch("sync_staff.role_map", return_value={})
    def test_invalid_user_id_returns_none(self, _rm, _ri):
        user = self._user(uid="not_a_number")
        self.assertIsNone(sync_staff.build_user_row(user, set(), {}, {}))

    @patch("sync_staff.role_ids", return_value=[])
    @patch("sync_staff.role_map", return_value={})
    def test_zero_user_id_returns_none(self, _rm, _ri):
        self.assertIsNone(sync_staff.build_user_row(self._user(uid="0"), set(), {}, {}))

    @patch("sync_staff.role_ids", return_value=[])
    @patch("sync_staff.role_map", return_value={})
    def test_date_register_converted(self, _rm, _ri):
        user = self._user()
        user["DATE_REGISTER"] = "2023-05-10T08:00:00Z"
        row = sync_staff.build_user_row(user, set(), {10: "X"}, {})
        self.assertEqual(row[9], "2023-05-10 11:00:00")  # UTC+3


if __name__ == "__main__":
    unittest.main(verbosity=2)
