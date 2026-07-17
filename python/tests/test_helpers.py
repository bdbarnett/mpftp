"""Unit tests that do not require a board."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load_sidecar():
    path = Path(__file__).resolve().parents[1] / "sidecar.py"
    spec = importlib.util.spec_from_file_location("mpftp_sidecar", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_sidecar()

    def test_split_fs_path(self):
        self.assertEqual(self.mod.split_fs_path(":/main.py"), (True, "/main.py"))
        self.assertEqual(self.mod.split_fs_path("./main.py"), (False, "./main.py"))
        self.assertEqual(self.mod.split_fs_path("/tmp/a"), (False, "/tmp/a"))

    def test_host_sha256(self):
        self.assertEqual(
            self.mod.host_sha256(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_host_rtc_tuple_shape(self):
        tup = self.mod.Session()._host_rtc_tuple()
        self.assertEqual(len(tup), 8)
        year, month, day, wday, hour, minute, sec, sub = tup
        self.assertGreaterEqual(year, 2024)
        self.assertTrue(1 <= month <= 12)
        self.assertTrue(1 <= day <= 31)
        self.assertTrue(0 <= wday <= 6)
        self.assertTrue(0 <= hour <= 23)
        self.assertEqual(sub, 0)


if __name__ == "__main__":
    unittest.main()
