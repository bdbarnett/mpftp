# Board inventory & Detect fixture report

An inventory of the boards on hand, compiled from the Firmware **Detect**
fixture campaign (2026-07-18). Raw per-board field notes are in
[`firmware-fixtures.md`](firmware-fixtures.md).

For **pydisplay panel / `board_config` bring-ups** (resolution, touch, DotClock
vs MIPI quirks) see the sister doc
[`pydisplay-display-boards.md`](pydisplay-display-boards.md).

Data was gathered with `esptool` (authoritative for Espressif — chip, flash,
security), `mpremote` / CircuitPython REPL (runtime freq, heap, build), and UF2
`INFO_UF2.TXT` / `boot_out.txt` for bootloader-only boards.

> **Note on flash/RAM columns:** for **ESP32** boards flash size and PSRAM are
> *measured* by esptool. For non-ESP boards, flash and SRAM are authoritative
> **chip / board specs** (RP2040 / SAMD / nRF52 / STM32 / i.MX RT datasheets and
> known board hardware); the *free heap* (`~… free`) is what the runtime
> reported at probe time. External QSPI flash is called out as `+N MB QSPI`
> (MCUs like RP2040 and i.MX RT have no internal program flash).

---

## Summary

- **30 data points** captured across **~24 distinct physical boards**
  (several generic ESP32-S3 modules and ESP32-PICO-V3-02 units were re-probed;
  see [Duplicates](#duplicates--repeat-probes)).
- **6 firmware ports** represented: `esp32`, `rp2`, `samd`, `nrf`, `mimxrt`, `stm32`.
- **3 runtimes seen in the wild:** MicroPython, CircuitPython, Arduino/bare.

### By firmware port

| Port | Count | Fixtures |
|------|-------|----------|
| `esp32` | 16 | 1–4, 6, 8–10, 12–15, 17, 20, 22, 28 |
| `rp2`   | 4  | 7, 11, 16, 21 |
| `samd`  | 5  | 18, 24, 26, 27, 30 |
| `nrf`   | 2  | 19, 29 |
| `mimxrt`| 2  | 5, 23 |
| `stm32` | 1  | 25 |

### By chip family

| Family | Count | Notes |
|--------|-------|-------|
| ESP32-S3 | 11 | mix of no-PSRAM, embedded PSRAM, and Octal-SPIRAM |
| classic ESP32 (PICO-V3-02) | 3 | embedded flash + PSRAM |
| ESP32-P4 | 1 | 32 MB, 400 MHz, external C6 Wi-Fi |
| ESP32-S2 | 1 | Adafruit FunHouse |
| RP2040 | 4 | Pico, 2× Feather (incl. DVI), Waveshare Plus |
| SAMD21 | 3 | QT Py M0, PyRuler, Trinket M0 |
| SAMD51 | 2 | Thing Plus (J20), PyGamer (J19) |
| nRF52840 | 2 | XIAO Sense, Feather Express |
| NXP i.MX RT | 2 | Metro M7 (RT1011), Teensy 4.1 (RT1062) |
| STM32H7 | 1 | NUCLEO-H743ZI2 |

### Superlatives

- **Fastest:** Teensy 4.1 (i.MX RT1062) **600 MHz**; Metro M7 500 MHz; ESP32-P4 400 MHz.
- **Most flash:** ESP32-P4 **32 MB** (all other ESP boards are 8 MB).
- **Most RAM/heap:** ESP32-P4 ~33 MB and the SPIRAM S3s ~8.3 MB free.
- **Only Arduino/bare board:** fixture 6 (ESP32-S3, no MicroPython — proves
  esptool-first Detect works with no REPL).
- **Trickiest to flash:** Feather nRF52840 Express (SoftDevice S140 6.1.1
  pinning) and the PyRuler clone (UF2 not sticking — left for later).

---

## Inventory

| # | Board / model | Chip / MCU | Port | Flash | SRAM / free heap | Runtime seen | Interface |
|---|---------------|-----------|------|-------|------------------|--------------|-----------|
| 1 | ESP32-P4 dev board | ESP32-P4 (v1.3) | esp32 | 32 MB | ~33 MB free (SPIRAM) | MicroPython (`C6_WIFI`) | UART bridge |
| 2 | Generic ESP32-S3 module | ESP32-S3 (v0.1) | esp32 | 8 MB | ~214 KB (no PSRAM) | MicroPython 1.24 | native USB-JTAG |
| 3 | Generic ESP32-S3 (Octal) | ESP32-S3 | esp32 | 8 MB | ~8.3 MB (Octal PSRAM) | MicroPython 1.25-pre | native USB-JTAG |
| 4 | Generic ESP32-S3 (SPIRAM, silent) | ESP32-S3 | esp32 | 8 MB | ~8.3 MB (SPIRAM) | MicroPython 1.21 | native USB-JTAG |
| 5 | Adafruit Metro M7 | i.MX RT1011DAE5A | mimxrt | 8 MB QSPI | 128 KB / ~60 KB free | MicroPython 1.22 | Adafruit VID |
| 6 | ESP32-S3 module (Arduino) | ESP32-S3 (v0.2) | esp32 | 8 MB | Embedded PSRAM 8 MB | Arduino / bare | native USB-JTAG |
| 7 | Adafruit Feather RP2040 | RP2040 | rp2 | 8 MB QSPI | 264 KB / ~217 KB free | MicroPython 1.20 | Adafruit VID |
| 8 | Adafruit Qualia S3 RGB666 | ESP32-S3 | esp32 | (unread) | ~8.3 MB (SPIRAM) | CircuitPython 9.0.5 | Adafruit VID |
| 9 | ESP32-S3 module (CH343) | ESP32-S3 (v0.2) | esp32 | 8 MB | Embedded PSRAM 8 MB | bare | WCH CH343 |
| 10 | ESP32-S3 module (CH343) | ESP32-S3 (v0.2) | esp32 | 8 MB | Embedded PSRAM 8 MB | bare | WCH CH343 |
| 11 | Waveshare RP2040-Plus 4MB | RP2040 | rp2 | 4 MB QSPI | 264 KB / ~178 KB free | CircuitPython 9.0.5 | RPi VID |
| 12 | Generic ESP32-S3 module | ESP32-S3 | esp32 | 8 MB | ~170 KB (no PSRAM) | MicroPython 1.21 | native USB-JTAG |
| 13 | Adafruit FunHouse | ESP32-S2 | esp32 | (unread) | ~2.0 MB (PSRAM) | CircuitPython 8.2 | Adafruit VID |
| 14 | Generic ESP32-S3 module | ESP32-S3 | esp32 | 8 MB | ~250 KB (no PSRAM) | MicroPython 1.21 | native USB-JTAG |
| 15 | Generic ESP32-S3 module | ESP32-S3 | esp32 | 8 MB | (no PSRAM) | MicroPython 1.21 | native USB-JTAG |
| 16 | Raspberry Pi Pico | RP2040 | rp2 | 2 MB QSPI | 264 KB / ~226 KB free | MicroPython 1.23-pre | RPi VID |
| 17 | ESP32-PICO-V3-02 board | ESP32-PICO-V3-02 (v3.0) | esp32 | 8 MB | Embedded PSRAM | bare | WCH CH9102 |
| 18 | Adafruit QT Py M0 | SAMD21E18A | samd | 256 KB | 32 KB | UF2 bootloader | Adafruit VID |
| 19 | Seeed XIAO nRF52840 Sense | nRF52840 | nrf | 1 MB + 2 MB QSPI | 256 KB / ~140 KB free | CircuitPython 8.2 | Seeed VID |
| 20 | ESP32-PICO-V3-02 board | ESP32-PICO-V3-02 (v3.0) | esp32 | 8 MB | Embedded PSRAM | bare | WCH CH9102 |
| 21 | Adafruit Feather RP2040 DVI | RP2040 | rp2 | 8 MB QSPI | 264 KB / ~106 KB free | CircuitPython 8.2 (252 MHz OC) | Adafruit VID |
| 22 | Generic ESP32-S3 module | ESP32-S3 | esp32 | 8 MB | ~248 KB (no PSRAM) | MicroPython 1.21 | native USB-JTAG |
| 23 | Teensy 4.1 | i.MX RT1062DVJ6A | mimxrt | 8 MB QSPI | 1 MB / ~927 KB free | CircuitPython 8.2 → MP 1.29 | Adafruit CDC / HalfKay |
| 24 | SparkFun Thing Plus SAMD51 | SAMD51J20A | samd | 1 MB | 256 KB / ~216 KB free | CircuitPython 8.2 | SparkFun VID |
| 25 | ST NUCLEO-H743ZI2 | STM32H743ZI | stm32 | 2 MB | 1 MB / ~455 KB free | MicroPython 1.23-pre → 1.29 | ST-Link MSD |
| 26 | Adafruit PyRuler | SAMD21E18 | samd | 256 KB | 32 KB | CircuitPython 8.2 | Adafruit VID |
| 27 | Adafruit PyGamer | SAMD51J19A | samd | 512 KB + 8 MB QSPI | 192 KB / ~155 KB free | CircuitPython 9.0.3 | Adafruit VID |
| 28 | ESP32-PICO-V3-02 board | ESP32-PICO-V3-02 (v3.0) | esp32 | 8 MB | Embedded PSRAM (~2 MB free) | → MP 1.29 SPIRAM | WCH CH9102 |
| 29 | Adafruit Feather nRF52840 Express | nRF52840 | nrf | 1 MB + 2 MB QSPI | 256 KB / ~141 KB free | CircuitPython 8.2 → MP 1.29 | Adafruit VID |
| 30 | Adafruit Trinket M0 | SAMD21E18 | samd | 256 KB | 32 KB | CircuitPython 8.2 | Adafruit VID |

---

## Most interesting datum per board

**ESP32**
- **#1 ESP32-P4** — the outlier: 32 MB flash, 400 MHz dual-core + LP, ~33 MB
  heap, and an **external C6 Wi-Fi co-processor** that esptool can't see
  (variant only knowable from MicroPython `_build`).
- **#2 ESP32-S3** — clean "no PSRAM" baseline: ~214 KB heap despite the generic
  S3 module string.
- **#3 ESP32-S3 Octal** — machine string literally says `Octal-SPIRAM` →
  unambiguous `SPIRAM_OCT`; ~8.3 MB heap.
- **#4 ESP32-S3** — the cautionary case: generic machine string with **no**
  "SPIRAM", yet ~8.3 MB heap proves external RAM → variant must come from heap,
  not the string.
- **#6 ESP32-S3 (Arduino)** — no MicroPython at all; esptool alone reports
  `Embedded PSRAM 8MB`. Proof that Detect works on a bare board.
- **#8 Qualia S3 RGB666** — CircuitPython reports `sys.platform == 'Espressif'`
  (not `esp32`); family must be inferred from `implementation.name`.
- **#9 / #10 ESP32-S3 (CH343)** — external USB-UART bridges let esptool succeed
  first-try; identity keyed on **MAC**, not the (shared) adapter serial.
- **#12 / #14 / #15 / #22 ESP32-S3 batch** — four physically distinct modules
  (distinct `unique_id`s) that all report the same generic string + bogus USB
  serial `123456` → never key identity on USB serial.
- **#13 FunHouse (ESP32-S2)** — the only S2; uname `ESP32S2` + ~2 MB heap →
  S2 + SPIRAM.
- **#17 / #20 / #28 ESP32-PICO-V3-02** — the classic-ESP32 case: embedded flash
  **and** PSRAM in one package; `get_security_info` is **not implemented**
  (must be tolerated, not treated as a failure).

**RP2040 (`rp2`)**
- **#7 Feather RP2040** — Adafruit VID `0x239A` also ships ESP32-S3, so VID
  alone can't gate "skip esptool".
- **#11 Waveshare RP2040-Plus** — true Raspberry Pi VID `0x2E8A`; only 4 MB flash.
- **#16 Raspberry Pi Pico** — reference RP2040, 2 MB flash.
- **#21 Feather RP2040 DVI** — running **overclocked at 252 MHz**; no dedicated
  DVI board in the MP tree (closest `ADAFRUIT_FEATHER_RP2040`).

**SAMD (`samd`)**
- **#18 QT Py M0** — caught in UF2 bootloader (`QTPY_BOOT`); Board-ID from
  `INFO_UF2.TXT` is the detect key.
- **#24 Thing Plus SAMD51** — exact MP board match `SPARKFUN_SAMD51_THING_PLUS`
  (samd51**j20**).
- **#26 PyRuler** — SAMD21 clone with no MP board; UF2 wouldn't stick (returns to
  `TRINKETBOOT`) — a known recovery gap left for later.
- **#27 PyGamer** — samd51**j19** (contrast the j20 Thing Plus); closest
  `SAMD_GENERIC_D51X19`.
- **#30 Trinket M0** — the genuine article; exact MP match `ADAFRUIT_TRINKET_M0`.

**nRF52840 (`nrf`)**
- **#19 XIAO nRF52840 Sense** — Seeed VID `0x2886`; CIRCUITPY-only, UF2 flash path.
- **#29 Feather nRF52840 Express** — bootloader pins **SoftDevice S140 6.1.1**;
  a default 7.3.0 UF2 fails — must match the SoftDevice version when building.

**NXP i.MX RT (`mimxrt`)**
- **#5 Metro M7** — entry i.MX RT1011 at 500 MHz; 128 KB SRAM (~60 KB free at
  probe), program runs from 8 MB external QSPI.
- **#23 Teensy 4.1** — the speed king at **600 MHz**, ~927 KB heap; flashed via
  HalfKay HID (`teensy_loader_cli`), not esptool.

**STM32 (`stm32`)**
- **#25 NUCLEO-H743ZI2** — reports a **frequency tuple** `(400, 200, 100, 100)`;
  flashed by ST-Link mass-storage drag-and-drop.

---

## Duplicates / repeat probes

Some models were probed more than once (distinct silicon, verified by
`unique_id`/MAC), useful for hardening the matcher rather than counting as
separate models:

- **Generic ESP32-S3 (no PSRAM) ×4** — fixtures 12, 14, 15, 22 (COM10 cohort).
- **ESP32-PICO-V3-02 ×3** — fixtures 17, 20, 28.
- **ESP32-S3 + Embedded PSRAM via CH343 ×2** — fixtures 9, 10.

Counting each model once gives roughly **24 distinct boards** on hand.
