# Firmware Detect — hardware fixture campaign

Raw data captured while designing the Firmware **Detect** feature (see
[`board-inventory.md`](board-inventory.md) for the summarized inventory).

- **Collected:** 2026-07-18 on this WSL + Windows workstation.
- **Method:** `esptool flash_id` / `get_security_info` (authoritative for
  Espressif, works without MicroPython), `mpremote` raw-REPL / CircuitPython
  REPL for runtime fields, and UF2 `INFO_UF2.TXT` / `boot_out.txt` for
  bootloader-only boards.
- **Purpose:** ground-truth for the Detect parsers/matcher and the autoset
  rules. Prefer capturing esptool dumps on a bare or briefly-disconnected board
  so Detect stays esptool-primary.

These are verbatim field notes; some boards were re-probed multiple times (same
model, distinct `unique_id`/MAC) to harden family/variant matching.

---

## Fixture 1 — ESP32-P4 (COM4)

**esptool (authoritative — works without MicroPython):**
- Chip: `ESP32-P4 (revision v1.3)`
- Features: `Dual Core + LP Core, 400MHz`
- Crystal: `40MHz`
- MAC: `e8:f6:0a:e0:f0:70`
- Flash: **32 MB**
- Secure Boot / Flash Encryption: Disabled

**MicroPython (optional enrichment — this board happened to have it):**
- `sys.implementation._build`: `ESP32_GENERIC_P4-C6_WIFI`
- machine string mentions external ESP32-C6 Wi-Fi
- `machine.freq()`: 360 MHz (running; chip max from esptool is 400)
- `esp.flash_size()`: 33554432 (32 MB) — matched physical
- `gc.mem_free()`: ~33 MB (strong external-RAM signal)
- Partitions: only `factory` 4 MB @ 0x10000 (no `vfs` row); LittleFS `df` ~28 MB

**Autoset:** board `ESP32_GENERIC_P4`; variant from MP `_build` = `C6_WIFI`
(without MP, leave variant unset and offer C5/C6); flash 32 MB.

## Fixture 2 — ESP32-S3 (COM5 → COM6 in bootloader)

**esptool (after `machine.bootloader()`; native USB remapped COM5→COM6):**
- Chip: `ESP32-S3 (QFN56) (revision v0.1)`
- Features: `Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded Flash 8MB (GD)`
- Crystal: `40MHz`; USB mode: `USB-Serial/JTAG`
- MAC: `f4:12:fa:8d:95:cc`
- Flash: **8 MB** (quad, 3.3V eFuse)

**MicroPython:** `Generic ESP32S3 module with ESP32S3`, MP 1.24.0, freq 160 MHz,
`esp.flash_size()` 8 MB, `gc.mem_free()` ~214 KB → **no SPIRAM**. Partitions:
`factory` ~2 MB, `df` ~6 MB.

**Autoset:** `ESP32_GENERIC_S3`, default variant, 8 MB.
**UX note:** native USB-Serial/JTAG chips can change COM port on bootloader
entry — re-enumerate by MAC/serial before reconnecting.

## Fixture 3 — ESP32-S3 Octal-SPIRAM (COM8)

**MicroPython (primary; esptool failed while MP held the port):**
- machine: `Generic ESP32S3 module with Octal-SPIRAM with ESP32S3`
- MP 1.25.0-preview; unique_id/MAC `84fce66c8d0c`
- freq 240 MHz; `esp.flash_size()` 8 MB; `gc.mem_free()` ~**8.3 MB** → strong
  SPIRAM signal (contrast fixture 2 ~214 KB)

**Autoset:** `ESP32_GENERIC_S3`, variant **`SPIRAM_OCT`** (machine string says
Octal-SPIRAM), 8 MB.

## Fixture 4 — ESP32-S3 with SPIRAM, machine string silent (COM10)

- machine: `Generic ESP32S3 module with ESP32S3` (**no SPIRAM in string**)
- MP 1.21.0 (older; no `vfs` module); USB serial bogus `123456`
- unique_id `70041dad86c4`; freq 160 MHz; flash 8 MB
- `gc.mem_free()` ~**8.3 MB** → **SPIRAM present** despite generic string

**Autoset:** `ESP32_GENERIC_S3`, variant **`SPIRAM`** (not OCT — string omits
"Octal"; heap proves external RAM), 8 MB. esptool failed after disconnect and
bootloader (native-USB hazard).

## Fixture 5 — Adafruit Metro M7 (MIMXRT1011) — non-ESP (COM12)

**Not Espressif.** esptool correctly fails ("No serial data").
- platform: `mimxrt`; machine `Adafruit Metro M7 with MIMXRT1011DAE5A`
- MP 1.22.2; unique_id `7be8715dd7794453`; freq **500 MHz**; mem_free ~60 KB
- USB VID/PID `61525` / `38914`

**Autoset:** port `mimxrt`; no ESP partition/flash/security UI.

## Fixture 6 — ESP32-S3 Arduino firmware, no MicroPython (COM13)

**Bare / non-MP path — Detect succeeds with esptool only.**
- Chip: `ESP32-S3 (QFN56) (revision v0.2)`
- Features: `Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz,` **`Embedded PSRAM 8MB (AP_3v3)`**
- Crystal 40MHz; USB-Serial/JTAG; MAC `30:30:f9:0f:6a:a4`; Flash **8 MB** (quad)
- USB VID/PID `303A` / `1001` (Espressif)

**Autoset (no MP):** `ESP32_GENERIC_S3`, variant **`SPIRAM`** (esptool reports
Embedded PSRAM — no heap needed), 8 MB. Status: No MicroPython (Arduino/other).

## Fixture 7 — Adafruit Feather RP2040 — non-ESP (COM14)

- platform `rp2`; machine `Adafruit Feather RP2040 with RP2040`; MP 1.20.0
- unique_id `47543634370b5929`; freq 125 MHz; mem_free ~217 KB
- USB VID `9114` (`0x239A` Adafruit — note: Adafruit ships both RP2040 and
  ESP32-S3 under this VID, so do not skip esptool on VID alone)

**Autoset:** port `rp2`; UF2 flash; no ESP UI.

## Fixture 8 — ESP32-S3 CircuitPython (Qualia) — not MicroPython (COM16)

**CircuitPython 9.0.5.**
- `sys.platform`: **`Espressif`** (CP style); `implementation.name`: `circuitpython`
- machine `Adafruit-Qualia-S3-RGB666 with ESP32S3`; board_id `adafruit_qualia_s3_rgb666`
- freq 240 MHz; mem_free ~**8.3 MB** → SPIRAM; USB VID/PID `9114` / `33096`
- esptool failed after disconnect (CP CDC may not expose ROM bootloader)

**Autoset:** `ESP32_GENERIC_S3` (or Qualia), variant `SPIRAM`, flash unknown
until esptool. Status: CircuitPython running (not MicroPython).

## Fixture 9 — ESP32-S3 + Embedded PSRAM via CH343, no MicroPython (COM17)

**esptool succeeded first try** (external USB-UART, not native USB).
- Chip `ESP32-S3 (QFN56) (revision v0.2)`; Embedded PSRAM 8MB (AP_3v3)
- Crystal 40MHz; MAC `dc:da:0c:56:97:a8`; Flash **8 MB**; security Disabled
- USB adapter: WCH CH343 VID `6790` / PID `21971`

**Autoset:** `ESP32_GENERIC_S3` / `SPIRAM` / 8 MB.

## Fixture 10 — ESP32-S3 + Embedded PSRAM via CH343, no MicroPython (COM18)

Same profile as fixture 9. Distinct MAC `dc:da:0c:48:ac:0c`. Confirms Detect
should key identity on **MAC**, not adapter serial (`5837055051`).
**Autoset:** `ESP32_GENERIC_S3` / `SPIRAM` / 8 MB.

## Fixture 11 — Waveshare RP2040-Plus 4MB (CircuitPython) — non-ESP (COM19)

- USB VID `11914` (`0x2E8A` Raspberry Pi — true RPi VID)
- CircuitPython 9.0.5; platform `RP2040`; machine `Waveshare RP2040-Plus (4MB) with rp2040`
- board_id `waveshare_rp2040_plus_4mb`; freq 125 MHz; mem_free ~178 KB
- uid `de60c882cf8f7424`

**Autoset:** port `rp2`; UF2 flash. Status: CircuitPython.

## Fixture 12 — ESP32-S3 no SPIRAM (MicroPython) (COM10)

MP 1.21.0 on native USB. machine `Generic ESP32S3 module with ESP32S3`;
unique_id `3485186bfcac` (USB serial bogus `123456`); freq 160 MHz; flash 8 MB;
mem_free ~**170 KB** → **no SPIRAM**.
**Autoset:** `ESP32_GENERIC_S3` / default / 8 MB.
**Contrast:** fixture 4 had same string + MP + bogus serial but ~8 MB heap →
SPIRAM. Heap (or esptool PSRAM feature) drives the variant, not the string.

## Fixture 13 — Adafruit FunHouse (ESP32-S2, CircuitPython) (COM20)

**First S2 fixture.** CircuitPython 8.2.0.
- platform `Espressif`; uname sysname **`ESP32S2`**; machine `Adafruit FunHouse with ESP32S2`
- board_id `adafruit_funhouse`; freq 240 MHz; mem_free ~**2.0 MB** → likely PSRAM
- USB VID/PID Adafruit `9114` / `33018`

**Autoset:** `ESP32_GENERIC_S2` (or FunHouse), variant `SPIRAM` if present.
Family mapper must accept uname `ESP32S2` / CP `Espressif` → S2.

## Fixture 14 — ESP32-S3 no SPIRAM (MicroPython) (COM10)

Same profile as fixture 12. Distinct unique_id `3485188d5428`.
**Autoset:** `ESP32_GENERIC_S3` / default / 8 MB.

## Fixture 15 — ESP32-S3 no SPIRAM (MicroPython) (COM10)

Same cohort as 12/14. unique_id `c04e3003f630`. Autoset: default / 8 MB.

## Fixture 16 — Raspberry Pi Pico (MicroPython) — non-ESP (COM21)

- platform `rp2`; machine `Raspberry Pi Pico with RP2040`; MP 1.23.0-preview
- unique_id `e462a052c73e4a29`; freq 125 MHz; mem_free ~226 KB
- USB VID/PID `11914` / `5` (RPi)

**Autoset:** port `rp2`; UF2 flash.

## Fixture 17 — classic ESP32-PICO-V3-02 + Embedded PSRAM, no MicroPython (COM22)

**First classic ESP32 (not S2/S3/P4).** CH9102 UART (VID `6790` / PID `21972`).
- Chip **`ESP32-PICO-V3-02` (revision v3.0)**
- Features: Wi-Fi, BT, Dual Core + LP Core, 240MHz, Embedded Flash, Embedded
  PSRAM, Vref cal in eFuse
- Crystal 40MHz; MAC `e8:9f:6d:2e:ec:94`; Flash **8 MB**
- `get-security-info`: **not implemented** (FF00) — treat as unknown, don't fail

**Autoset:** `ESP32_GENERIC`, variant `SPIRAM`, 8 MB.

## Fixture 18 — Adafruit QT Py M0 (SAMD21) — UF2 bootloader (COM24 / was COM23)

**Not ESP.** In UF2 bootloader after replug.
- Volume `QTPY_BOOT` (`D:`); `INFO_UF2.TXT`: UF2 Bootloader v1.23.1-adafruit,
  Model QT Py M0, Board-ID `SAMD21E18A-QTPy-v0`
- USB Adafruit VID `0x239A`, bootloader PID `203` (`0x00CB`)

**Autoset:** port `samd`; UF2 flash. Detect via UF2 volume label + `INFO_UF2.TXT`
Board-ID.

## Fixture 19 — Seeed XIAO nRF52840 Sense (CircuitPython) (COM26)

**First nRF52 fixture.** VID `10374` (`0x2886` Seeed).
- CircuitPython 8.2.0; platform `nRF52840`; uname `nrf52`
- machine `Seeed XIAO nRF52840 Sense with nRF52840`; board_id `Seeed_XIAO_nRF52840_Sense`
- uid `644cd1accbe535bb`; freq 64 MHz; mem_free ~140 KB; CIRCUITPY on `D:`

**Autoset:** port `nrf`; UF2/nrfutil flash — not esptool. Status: CircuitPython.

## Fixture 20 — classic ESP32-PICO-V3-02 + Embedded PSRAM, no MicroPython (COM28)

Same chip profile as fixture 17. CH9102. Distinct MAC `4c:75:25:ee:d7:08`.
Crystal warning once (`41.01` vs 40 MHz normalized). Autoset: `ESP32_GENERIC` /
`SPIRAM` / 8 MB.

## Fixture 21 — Adafruit Feather RP2040 DVI (CircuitPython) (COM29)

- platform `RP2040`; CircuitPython 8.2.0
- machine/board_id `Adafruit Feather RP2040 DVI` / `adafruit_feather_rp2040_dvi`
- uid `df6254209f1b4f29`; freq **252 MHz** (overclocked vs stock 125); mem_free ~106 KB
- MP tree has `ADAFRUIT_FEATHER_RP2040` (no dedicated DVI board — closest match)

**Autoset:** port `rp2` / `ADAFRUIT_FEATHER_RP2040`; UF2 flash.
**RP2 detect tool:** picotool (BOOTSEL → `RPI-RP2` volume, `INFO_UF2.TXT`
Board-ID `RPI-RP2`); picotool `info` needs WinUSB via Zadig on Windows
(mass-storage works without it).

## Fixture 22 — ESP32-S3 no SPIRAM (MicroPython) (COM10)

Same profile as 12/14/15. Distinct unique_id `ecda3b9956dc`. Autoset: default / 8 MB.

## Fixture 23 — Teensy 4.1 (IMXRT1062) CircuitPython (COM30)

**First Teensy / high-end mimxrt.** VID `9114` (Adafruit CDC on PJRC board).
- CircuitPython 8.2.0; platform `NXP IMXRT10XX` / uname `mimxrt10xx`
- machine/board_id `Teensy 4.1 with IMXRT1062DVJ6A` / `teensy41`
- uid `2F057F67D2891A2AD600005002004200`; freq **600 MHz**; mem_free ~**927 KB**

**Autoset:** port `mimxrt`, board `TEENSY41`. Flashed → MP 1.29.0-preview via
`teensy_loader_cli` (HalfKay HID — not esptool; Win32 HID build preferred over
libusb on Windows). After flash: COM remaps (COM30→COM31), VID `0xF055`.

## Fixture 24 — SparkFun Thing Plus SAMD51 (CircuitPython) (COM32)

**First SAMD51 fixture.** VID `6991` (`0x1B4F` SparkFun).
- CircuitPython 8.2.0; platform `MicroChip SAMD51` / uname `samd51`
- machine/board_id `SparkFun Thing Plus - SAMD51 with samd51j20` / `sparkfun_samd51_thing_plus`
- uid `EA56D46D32573653202020321F4403FF`; freq 120 MHz; mem_free ~216 KB

**Autoset:** port `samd`, board `SPARKFUN_SAMD51_THING_PLUS` (exact match). UF2 flash.

## Fixture 25 — STM32 NUCLEO-H743ZI2 (MicroPython) (COM33)

**First stm32 fixture.** VID `1155` (`0x0483` STMicroelectronics).
- MP 1.23.0-preview; platform `pyboard`; machine `NUCLEO_H743ZI2 with STM32H743`
- unique_id `3d0022001151323435373638`; freq tuple **(400, 200, 100, 100) MHz**;
  mem_free ~**455 KB**

**Autoset:** port `stm32`, board `NUCLEO_H743ZI2`. Flash via ST-Link MSD drag-drop
of `firmware.bin` — not esptool. Flashed → MP 1.29.0-preview.

## Fixture 26 — Adafruit PyRuler (SAMD21) CircuitPython (COM34)

- CircuitPython 8.2.0; board_id `pyruler`; machine `Adafruit PyRuler with samd21e18`
- uid `DCF15C8B393952504A312E31203106FF`; CIRCUITPY on `D:`; VID `9114`

**Autoset:** port `samd`; no `PYRULER` in MP tree — closest
`ADAFRUIT_TRINKET_M0` / `ADAFRUIT_QTPY_SAMD21`. UF2 flash.
**Flash note:** bootloader updated Trinket M0 v2→v4.0.0; official CP UF2 returned
to `TRINKETBOOT` — app image not sticking. Left for later (out of scope).

## Fixture 27 — Adafruit PyGamer (SAMD51) CircuitPython (COM36)

- CircuitPython 9.0.3; platform `MicroChip SAMD51` / uname `samd51`
- machine/board_id `Adafruit PyGamer with samd51j19` / `pygamer`
- uid `C04CD4883546395320202033481705FF`; freq 120 MHz; mem_free ~155 KB

**Autoset:** port `samd`; no `PYGAMER` in MP tree — closest `SAMD_GENERIC_D51X19`
(samd51j19; contrast fixture 24 samd51j20). UF2 flash.

## Fixture 28 — classic ESP32-PICO-V3-02 + Embedded PSRAM (COM37)

Same chip profile as 17/20. CH9102 (`54B0007563`). Distinct MAC
`4c:75:25:ee:d8:b4`. Autoset: `ESP32_GENERIC` / `SPIRAM` / 8 MB. Flashed → MP
1.29.0-preview `ESP32_GENERIC-SPIRAM` (needed `erase-flash` first — partial write
left a corrupted VFS). mem_free ~2.0 MB.

## Fixture 29 — Adafruit Feather nRF52840 Express (CircuitPython) (COM38)

**Second nRF52 fixture.** VID `9114` Adafruit.
- CircuitPython 8.2.0; platform `nRF52840` / uname `nrf52`
- machine/board_id `Adafruit Feather nRF52840 Express with nRF52840` / `feather_nrf52840_express`
- uid `79205479286866EB`; freq 64 MHz; mem_free ~141 KB

**Autoset:** port `nrf`. MP tree has `FEATHER52` (nRF52832) — not a 52840 match;
closest Adafruit-bootloader build `SEEED_XIAO_NRF52` UF2 family `0xADA52840`.
**Flash note:** `FTHR840BOOT` SoftDevice **S140 6.1.1** — must build
`SEEED_XIAO_NRF52` with `SOFTDEV_VERSION=6.1.1` (UF2 start `0x26000`); default
7.3.0 UF2 fails. Flashed → MP 1.29.0-preview (USB VID becomes Seeed `0x2886`).

## Fixture 30 — Adafruit Trinket M0 (SAMD21) CircuitPython (COM41)

**True Trinket M0** (contrast fixture 26 PyRuler clone). VID `9114`.
- CircuitPython 8.2.0; board_id `trinket_m0`; machine `Adafruit Trinket M0 with samd21e18`
- uid `8F6CBA0D3546525020312E372C1418FF`; CIRCUITPY on `D:`

**Autoset:** port `samd`, board `ADAFRUIT_TRINKET_M0` (exact match). UF2 via
`TRINKETBOOT` (double-reset).

---

## Autoset rules distilled from these fixtures

| Signal | Autoset |
|--------|---------|
| Chip family from esptool | `ESP32_GENERIC`, `_S2`, `_S3`, `_C3`, `_C6`, `_H2`, `_P4`, … |
| Single-core feature | `UNICORE` if that variant exists |
| PSRAM | esptool `Embedded PSRAM` → `SPIRAM`; machine `Octal-SPIRAM` → `SPIRAM_OCT`; else MP large heap → `SPIRAM` |
| Physical flash | `CONFIG_ESPTOOLPY_FLASHSIZE_*` + layout base |
| ESP32-P4 Wi-Fi | `C5_WIFI` / `C6_WIFI` only from MP `_build`/machine or explicit user pick |
| Identity key | **MAC** (esptool) — never the USB adapter serial (often bogus, e.g. `123456`) |

Confidence: `matched` / `family-only` / `unknown`. Non-Espressif → suggested
firmware port only, never a forced `ESP32_GENERIC_*`.
