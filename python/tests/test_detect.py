"""Unit tests for the firmware Detect parsers / matcher (no hardware).

Sample esptool text is taken from the fixture campaign (ESP32-P4, ESP32-S3
with/without PSRAM, classic ESP32-PICO-V3-02) captured in the plan.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load_engine():
    path = Path(__file__).resolve().parents[1] / "firmware_engine.py"
    spec = importlib.util.spec_from_file_location("mpftp_firmware_engine", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# Fixture 1 — ESP32-P4 32 MB (COM4), esptool-only.
P4_FLASH_ID = """\
esptool.py v4.7.0
Serial port COM4
Connecting....
Chip is ESP32-P4 (revision v1.3)
Features: Dual Core + LP Core, 400MHz
Crystal is 40MHz
MAC: e8:f6:0a:e0:f0:70
Uploading stub...
Running stub...
Stub running...
Manufacturer: 20
Device: 4020
Detected flash size: 32MB
Hard resetting via RTS pin...
"""

SECURITY_OFF = """\
Security Information:
=====================
Flags: 0x00000000 (0b0)
Secure Boot: Disabled
Flash Encryption: Disabled
"""

# Fixture 2 — ESP32-S3, embedded flash, no PSRAM.
S3_NO_PSRAM = """\
Chip is ESP32-S3 (QFN56) (revision v0.1)
Features: Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded Flash 8MB (GD)
Crystal is 40MHz
MAC: f4:12:fa:8d:95:cc
Detected flash size: 8MB
"""

# Fixture 6 — ESP32-S3 with embedded PSRAM (no MicroPython).
S3_PSRAM = """\
Chip is ESP32-S3 (QFN56) (revision v0.2)
Features: Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_3v3)
Crystal is 40MHz
MAC: 30:30:f9:0f:6a:a4
Detected flash size: 8MB
"""

# Fixture 17 — classic ESP32-PICO-V3-02 with embedded PSRAM.
PICO_V3 = """\
Chip is ESP32-PICO-V3-02 (revision v3.0)
Features: Wi-Fi, BT, Dual Core + LP Core, 240MHz, Embedded Flash, Embedded PSRAM, Vref calibration in eFuse
Crystal is 40MHz
MAC: e8:9f:6d:2e:ec:94
Detected flash size: 8MB
"""

# Non-ESP: esptool cannot talk to the ROM bootloader.
NOT_ESP = """\
esptool.py v4.7.0
Serial port COM14
Connecting......
A fatal error occurred: Failed to connect to Espressif device: No serial data received.
"""


def _tree():
    return [
        {
            "port": "esp32",
            "kind": "boards",
            "boards": [
                {"board": "ESP32_GENERIC", "variants": ["SPIRAM", "D2WD", "UNICORE"]},
                {"board": "ESP32_GENERIC_S3", "variants": ["SPIRAM", "SPIRAM_OCT", "FLASH_4M"]},
                {"board": "ESP32_GENERIC_P4", "variants": ["C5_WIFI", "C6_WIFI"]},
            ],
        }
    ]


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _load_engine()

    def test_parse_p4(self):
        f = self.eng.parse_esptool_flash_id(P4_FLASH_ID)
        self.assertEqual(f["chip"], "ESP32-P4")
        self.assertEqual(f["revision"], "v1.3")
        self.assertEqual(f["cores"], 2)
        self.assertTrue(f["lpCore"])
        self.assertEqual(f["maxMhz"], 400)
        self.assertEqual(f["crystalMhz"], 40)
        self.assertEqual(f["mac"], "e8:f6:0a:e0:f0:70")
        self.assertEqual(f["flashMb"], 32)
        self.assertFalse(f["psram"]["present"])

    def test_parse_s3_qfn_and_flash(self):
        f = self.eng.parse_esptool_flash_id(S3_NO_PSRAM)
        self.assertEqual(f["chip"], "ESP32-S3")
        self.assertEqual(f["revision"], "v0.1")
        self.assertEqual(f["maxMhz"], 240)
        self.assertEqual(f["flashMb"], 8)
        self.assertFalse(f["psram"]["present"])

    def test_parse_s3_embedded_psram(self):
        f = self.eng.parse_esptool_flash_id(S3_PSRAM)
        self.assertTrue(f["psram"]["present"])
        self.assertFalse(f["psram"]["octal"])

    def test_parse_security(self):
        s = self.eng.parse_esptool_security(SECURITY_OFF)
        self.assertTrue(s["available"])
        self.assertEqual(s["secureBoot"], "Disabled")
        self.assertEqual(s["flashEncryption"], "Disabled")

    def test_security_absent(self):
        s = self.eng.parse_esptool_security("get_security_info is not implemented")
        self.assertFalse(s["available"])

    def test_family_from_chip(self):
        self.assertEqual(self.eng.family_from_chip("ESP32-P4"), "P4")
        self.assertEqual(self.eng.family_from_chip("ESP32-S3"), "S3")
        self.assertEqual(self.eng.family_from_chip("ESP32-PICO-V3-02"), "")
        self.assertEqual(self.eng.family_from_chip("ESP32"), "")


class MatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _load_engine()

    def _match(self, text, mp_hints=None):
        f = self.eng.parse_esptool_flash_id(text)
        family = self.eng.family_from_chip(f["chip"])
        return self.eng.match_esp_target(
            family, f["psram"], f["flashMb"], mp_hints or {}, _tree()
        )

    def test_p4_matched_default_variant(self):
        m = self._match(P4_FLASH_ID)
        self.assertEqual(m["board"], "ESP32_GENERIC_P4")
        self.assertEqual(m["variant"], "")
        self.assertEqual(m["flashSize"], "32MB")
        self.assertEqual(m["flashConfig"], "CONFIG_ESPTOOLPY_FLASHSIZE_32MB")
        self.assertEqual(m["confidence"], "matched")
        self.assertIn("C6_WIFI", m["variantOptions"])

    def test_p4_c6_wifi_from_mp_build(self):
        m = self._match(P4_FLASH_ID, {"build": "ESP32_GENERIC_P4-C6_WIFI"})
        self.assertEqual(m["variant"], "C6_WIFI")

    def test_s3_no_psram_default(self):
        m = self._match(S3_NO_PSRAM)
        self.assertEqual(m["board"], "ESP32_GENERIC_S3")
        self.assertEqual(m["variant"], "")
        self.assertEqual(m["flashSize"], "8MB")

    def test_s3_embedded_psram_spiram(self):
        m = self._match(S3_PSRAM)
        self.assertEqual(m["board"], "ESP32_GENERIC_S3")
        self.assertEqual(m["variant"], "SPIRAM")

    def test_s3_octal_from_machine_string(self):
        m = self._match(S3_NO_PSRAM, {"machine": "Generic ESP32S3 module with Octal-SPIRAM"})
        self.assertEqual(m["variant"], "SPIRAM_OCT")

    def test_s3_large_heap_spiram(self):
        m = self._match(S3_NO_PSRAM, {"memfree": 8_300_000})
        self.assertEqual(m["variant"], "SPIRAM")

    def test_pico_classic_spiram(self):
        m = self._match(PICO_V3)
        self.assertEqual(m["board"], "ESP32_GENERIC")
        self.assertEqual(m["variant"], "SPIRAM")

    def test_family_only_when_board_absent(self):
        f = self.eng.parse_esptool_flash_id(S3_NO_PSRAM)
        m = self.eng.match_esp_target("S3", f["psram"], 8, {}, [])
        self.assertEqual(m["confidence"], "family-only")
        self.assertEqual(m["board"], "ESP32_GENERIC_S3")


class ClassifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _load_engine()

    def test_not_espressif_reason(self):
        self.assertIn("No serial data", self.eng._esptool_fail_reason(NOT_ESP, 2))

    def test_suggested_port_from_mp(self):
        self.assertEqual(self.eng.suggested_port_from_mp({"platform": "rp2"}), "rp2")
        self.assertEqual(self.eng.suggested_port_from_mp({"platform": "pyboard"}), "stm32")
        self.assertEqual(
            self.eng.suggested_port_from_mp({"platform": "nRF52840", "machine": "XIAO"}), "nrf"
        )
        self.assertEqual(
            self.eng.suggested_port_from_mp({"platform": "MicroChip SAMD51"}), "samd"
        )
        self.assertEqual(
            self.eng.suggested_port_from_mp({"platform": "NXP IMXRT10XX"}), "mimxrt"
        )

    def test_mp_indicates_esp(self):
        self.assertTrue(self.eng._mp_indicates_esp({"platform": "esp32"}))
        self.assertTrue(self.eng._mp_indicates_esp({"platform": "Espressif"}))
        self.assertFalse(self.eng._mp_indicates_esp({"platform": "rp2"}))


class SplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = _load_engine()

    def _rows(self, text):
        return self.eng.parse_partitions_csv(text)

    def test_resize_existing_storage(self):
        base = self._rows(
            "# Name, Type, SubType, Offset, Size, Flags\n"
            "nvs, data, nvs, 0x9000, 0x6000,\n"
            "phy_init, data, phy, 0xf000, 0x1000,\n"
            "factory, app, factory, 0x10000, 0x200000,\n"
            "vfs, data, fat, 0x210000, 0x1F0000,\n"
        )
        rows, warnings = self.eng.compute_split(base, 0x400000, 8 * 1024 * 1024)
        storage = rows[-1]
        self.assertEqual(storage["name"], "vfs")
        self.assertEqual(self.eng._parse_size(storage["size"]), 0x400000)
        # factory ends at 0x210000, storage must start there.
        self.assertEqual(self.eng._parse_size(storage["offset"]), 0x210000)

    def test_append_when_only_factory(self):
        base = self._rows(
            "# Name, Type, SubType, Offset, Size, Flags\n"
            "nvs, data, nvs, 0x9000, 0x6000,\n"
            "phy_init, data, phy, 0xf000, 0x1000,\n"
            "factory, app, factory, 0x10000, 0x400000,\n"
        )
        rows, warnings = self.eng.compute_split(base, 0, 32 * 1024 * 1024)
        self.assertTrue(any("added a trailing vfs" in w for w in warnings))
        storage = rows[-1]
        self.assertTrue(self.eng._is_storage_row(storage))
        # storage fills to end of 32 MB flash.
        end = self.eng._table_target_size(rows)
        self.assertLessEqual(end, 32 * 1024 * 1024)
        self.assertGreater(self.eng._parse_size(storage["size"]), 0x1000000)

    def test_overflow_warning(self):
        base = self._rows(
            "# Name, Type, SubType, Offset, Size, Flags\n"
            "factory, app, factory, 0x10000, 0x200000,\n"
            "vfs, data, fat, 0x210000, 0x1F0000,\n"
        )
        rows, warnings = self.eng.compute_split(base, 32 * 1024 * 1024, 8 * 1024 * 1024)
        self.assertTrue(any("exceeds flash" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
