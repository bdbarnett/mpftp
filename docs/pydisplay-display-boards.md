# Pydisplay display boards (bring-up notes)

Sister doc to [`board-inventory.md`](board-inventory.md) (esptool / Detect
hardware inventory). This file tracks **pydisplay `board_config` bring-ups** —
panel resolution, touch, interface module, and quirks — for boards exercised in
the July 2026 DotClock / `mipidsi` / busdisplay (`spibus` / `i80bus`) + LVGL
(`lv_test_timer`) campaign.

Paths are under pydisplay `board_configs/fbdisplay/<name>/` unless a
`busdisplay/…` path is given.
Native drivers live in pydevices **displayif** (`displayif.DotClockFramebuffer`
or `mipidsi`). Soft-reset / scanout lessons:
[displayif `SOFT_RESET_AND_BRINGUP.md`](https://github.com/PyDevices/displayif/blob/main/SOFT_RESET_AND_BRINGUP.md).

Typical MicroPython flash for the ESP32-S3 RGB boards below:
`ESP32_GENERIC_S3` + **`SPIRAM_OCT`** (8 MB octal PSRAM). P4 uses its own board
variant (`C6_WIFI` in inventory fixture #1).

---

## Summary table

| Product / nickname | `board_config` dir | Resolution | Panel / bus | Touch | Expander / IO | Inventory # |
|--------------------|--------------------|------------|-------------|-------|---------------|-------------|
| Waveshare ESP32-P4-WIFI6-Touch-LCD-4B | `esp32-p4-wifi6-touch-lcd-4b` | 720×720 | ST7703 **MIPI DSI** (`mipidsi`) | GT911 | — | [#1](board-inventory.md) |
| Adafruit Qualia S3 + TL040HDS20 | `qualia_tl040hds20` (+ CP `cp_qualia_tl040hds20`) | 720×720 | RGB-666→565 **DotClock** | FT6x36 @ `0x48` | PCA9554 @ `0x3f` | [#8](board-inventory.md) |
| Waveshare ESP32-S3-Touch-LCD-4.3 | `esp32-s3-touch-lcd-4_3` | 800×480 | ST7262 RGB **DotClock** | GT911 @ `0x5D` | CH422G | *(not yet a Detect fixture)* |
| LILYGO T-RGB 2.1″ round | `t-rgb_480` | 480×480 | ST7701 RGB **DotClock** | CST820 (`cst8xx`) | XL9535 | *(not yet a Detect fixture)* |
| Waveshare ESP32-S3-Touch-LCD-7 (sku 27078) | `esp32-s3-touch-lcd-7` | 800×480 | ST7262 RGB **DotClock** | GT911 @ `0x5D` | CH422G | *(not yet a Detect fixture)* |
| LILYGO T-Embed | `busdisplay/spi/t-embed` | 170×320 | ST7789 **SPI** (`spibus`) | — (rotary) | GPIO46 power | *(not yet a Detect fixture)* |
| LILYGO T-HMI | `busdisplay/i80/t-hmi` | 240×320 | ST7789 **I80** (`i80bus`) | XPT2046 SPI | GPIO14/10 power | *(not yet a Detect fixture)* |
| Waveshare RP2040-Touch-LCD-1.28 | `busdisplay/spi/rp2040-touch-lcd-1.28` (+ CP `cp_rp2040-touch-lcd-1.28`) | 240×240 round | GC9A01A **SPI** (`spibus` / FourWire) | CST816 (`cst8xx` / `cst816`) | — | *(not yet a Detect fixture)* |
| Adafruit Metro M7 + 2.8″ TFT Touch Shield (1947) | `busdisplay/spi/metro_m7_tft_touch_shield_1947` | 240×320 | ILI9341 **SPI** (`spibus` SoftSPI or SPI0) | FT6206 @ `0x38` | Onboard AirLift (NINA) | *(not yet a Detect fixture)* |

---

## Per-board detail

### Waveshare ESP32-P4-WIFI6-Touch-LCD-4B

- **board_config title:** `Waveshare ESP32-P4-WIFI6-Touch-LCD-4B - MicroPython`
- **Dir:** `esp32-p4-wifi6-touch-lcd-4b` (CP sibling: `cp_esp32-p4-wifi6-touch-lcd-4b`)
- **Resolution:** 720×720
- **Display:** `mipidsi.Bus` + `mipidsi.Display`, ST7703 init sequence, 2-lane DSI,
  pixel clock 46 MHz
- **Touch:** GT911, 5 points
- **SoC / flash notes:** ESP32-P4, 32 MB flash, large SPIRAM heap; external C6
  Wi-Fi (see inventory #1)
- **Role in campaign:** Soft-reset / timer lifecycle reference for displayif

### Adafruit Qualia S3 RGB666 + TL040HDS20

- **board_config title:** `Qualia S3 RGB-666 with TL040HDS20 4.0" 720x720 Square Display`
- **Dirs:** `qualia_tl040hds20` (MicroPython / `displayif.DotClockFramebuffer`);
  `cp_qualia_tl040hds20` (CircuitPython `dotclockframebuffer` +
  `FramebufferDisplay(auto_refresh=True)` + `displayio.Bitmap`)
- **Resolution:** 720×720 @ 16 MHz PCLK
- **Display:** Parallel RGB DotClock; Qualia needs **BGR** 5/6/5 data-pin order
  and PCA9554 bring-up matching CP `adafruit_qualia_s3_rgb666`
- **Touch:** FT6x36 @ `0x48` (`get_positions` on MP)
- **Expander:** PCA9554 @ `0x3f` (not `0x38`)
- **SoC:** ESP32-S3 + octal PSRAM; Adafruit VID (inventory #8)
- **Interesting:** First MP DotClock bring-up. Bounce buffer required (horizontal
  slide without it). MP must use **double panel FBs** + `auto_refresh=False`
  because LVGL paints the panel FB; CP uses a separate Bitmap so
  `auto_refresh=True` is correct there. See displayif soft-reset notes.

### Waveshare ESP32-S3-Touch-LCD-4.3

- **board_config title:** `Waveshare ESP32-S3-Touch-LCD-4.3 — 800x480 RGB565 (ST7262) + GT911`
- **Dir:** `esp32-s3-touch-lcd-4_3`
- **Resolution:** 800×480 @ 16 MHz PCLK
- **Display:** ST7262 RGB DotClock; same GPIO map family as the 7″ board
- **Touch:** GT911 @ `0x5D` (RST/INT via CH422G); **diagonal axis remap** in
  `touch_read_func` (landscape values reflected over the diagonal — not plain
  `SWAP_XY`)
- **Expander:** CH422G (BL / LCD RST / TP RST on EXIO)
- **USB:** Often dual USB (native + UART); prefer labeled UART for stable REPL

### LILYGO T-RGB 480 (2.1″ round)

- **board_config title:** `480x480 ST7701 parallel RGB - MicroPython (ESP32-S3)` /
  LILYGO T-RGB 2.1″ full circle
- **Dir:** `t-rgb_480`
- **Resolution:** 480×480 @ 12 MHz PCLK (round panel)
- **Display:** ST7701; SPI init via `st7701.run_init`, then DotClock RGB scanout
- **Touch:** CST820 via `cst8xx` (RST on XL9535 IO1, IRQ=GPIO1); poll
  continuously (edge-only IRQ)
- **Expander:** XL9535 (power / LCD CS / SPI / RST)
- **Flash / USB:** Native USB-Serial/JTAG; ROM download often needs
  **BOOT+RESET**, then **RESET** after flash for a clean CDC port. Flash as
  `ESP32_GENERIC_S3` + `SPIRAM_OCT`.
- **Interesting:** Single-FB + `auto_refresh=True` made `fill_rect` look correct
  without `show()` but broke under LVGL animation; restored double-FB +
  `show()` (verified with `lv_test_timer`).

### Waveshare ESP32-S3-Touch-LCD-7 (sku 27078)

- **board_config title:** `Waveshare ESP32-S3-Touch-LCD-7 — 800x480 RGB565 (ST7262) + GT911`
- **Dir:** `esp32-s3-touch-lcd-7`
- **Resolution:** 800×480 @ 16 MHz PCLK (same timings/pins as 4.3″ sibling)
- **Display:** ST7262 RGB DotClock
- **Touch:** GT911 @ `0x5D`; **identity** coords (no diagonal remap — unlike 4.3″)
- **Expander:** CH422G (EXIO2 = backlight / DISP)
- **USB:** Two ports — use **UART (CH343)** for flash/REPL; native USB may not
  enumerate CDC until firmware is up. Session example: CH343 serial
  `578E020986`.
- **Interesting:** Symptom that forced restoring double-FB: `lv_test_timer` drew
  UI then went mostly black with one short edge (live bounce-source paint).

### LILYGO T-Embed (SPI ST7789)

- **board_config title:** `LILYGO T-Embed ST7789 170x320 SPI + rotary`
- **Dir:** `board_configs/busdisplay/spi/t-embed` (native displayif `spibus`)
- **Resolution:** 170×320, `colstart=35`, `rowstart=0`, `invert=True`, `bgr=True`
- **Orientation:** `rotation=180`, `mirrored=True` → MADCTL **MX|MY|BGR
  (`0xC8`)**. Encoder at the physical bottom → origin upper-left. Matches
  russhughes rot2 / TFT_eSPI `setRotation(2)` for 170×320 (plain rot0 `0x08`
  put the origin wrong on this panel).
- **Bus:** `SPIBus(id=2, sck=12, mosi=11, miso=-1, dc=13, cs=10, reset=9)` @
  40 MHz — always pass explicit pins (`SPI(2)` defaults hit Octal PSRAM pads)
- **Power / BL:** GPIO46 `PIN_POWER_ON` (must be high), backlight GPIO15
- **Input:** `RotaryIRQ` A=2, B=1, button=0 (`half_step=True`); no touch panel
- **Flash / USB:** Native USB-Serial/JTAG; **BOOT+RESET** → ROM, flash
  `ESP32_GENERIC_S3` + `SPIRAM_OCT`, plain **RESET** for CDC. Example unit
  serial `3485186BFCAC0000` (Windows `COM55` in one WSL session).
- **Setup:** `/setup t-embed lv_test_timer` (or mip the `package.json` deps).
  Do **not** leave a Python `/lib/spibus.py` — it shadows native `spibus`.
- **displayif notes:** `SPI.init` on each `send` must re-pass sck/mosi/miso or
  ESP32-S3 drops the GPIO matrix; command byte via buffer protocol (not
  `MP_OBJ_TO_PTR` on the bytearray); soft-reset re-init must not leave CS/DC
  stuck.
- **Interesting:** Solid fills looked fine, but `BusDisplay.fill_rect` using
  ST7789 `RAMCONT` (`0x3C`) with CS dropping between strips produced dots /
  garbage. Fixed in pydisplay `displaysys/busdisplay.py`: per-strip window +
  `RAMWR` (`0x2C`) only. Verified with an L geometry under MADCTL `0xC8`.

### LILYGO T-HMI (I80 ST7789)

- **board_config title:** `LILYGO T-HMI 240x320 ST7789 I80 + XPT2046`
- **Dir:** `board_configs/busdisplay/i80/t-hmi` (native displayif `i80bus`)
- **Resolution:** 240×320, `colstart=0`, `invert=False`, `bgr=True`
- **Orientation:** `rotation=0`, `mirrored=True` → MADCTL **BGR (`0x08`)**
  (TFT_eSPI Setup207 / ST7789 rot0). `mirrored=False` added MX (`0x48`) and
  looked left/right mirrored.
- **Bus:** 8-bit I80 — `dc=7`, `cs=6`, `wr=8`,
  `data=[48, 47, 39, 40, 41, 42, 45, 46]` (parallel LCD, not SPI)
- **Power / BL:** GPIO14 `PWR_ON`, GPIO10 `PWR_EN` (reed-switch / battery path
  — both must be high); backlight GPIO38
- **Touch:** XPT2046 on dedicated SPI1 (LilyGO `pins.h`): SCK=1, MOSI=3,
  MISO=4, CS=2, IRQ=9 (active-low, pull-up). Baud 2 MHz, MODE0. Driver
  framing matches LilyGO `transfer16` (`drivers/touch/xpt2046.py`). Press =
  IRQ low or `|z| ≥ 25`. Cal defaults from LilyGO `touch.ino`
  (`xmin=1788, xmax=285, ymin=1877, ymax=311`); pass
  `width=height_disp, height=width_disp` into `calibrate(orientation=0)` so
  the map size ends up 240×320. `touch_read_func` returns **`None` when up**
  (always returning coords looked like a permanent press). Short release
  holdoff (3 polls) for resistive dropouts. UI vs panel needed
  `touch_rotation_table` all **`REVERSE_Y`** so `BOTTOM_MID` taps hit the
  visible button.
- **Flash / USB:** same `ESP32_GENERIC_S3` + `SPIRAM_OCT` family as T-Embed;
  native USB. Example unit serial `ECDA3B9956DC0000` (Windows `COM57` in one
  WSL session).
- **Setup:** mip / `/setup` the `package.json` (notes: firmware-native
  `i80bus` — do not mip-install `packages/i80bus.json`).
- **Interesting:** Resistive touch is usable for `lv_test_timer` but remains
  flaky (missed taps) even with IRQ/Z press detect — expect firm presses.
  Thin noisy vertical lines after geometry tests were often **stale GRAM**
  (clear with `display_drv.fill(0)`), not an active draw bug. I80
  `max_transfer_bytes` vs full-frame size is a displayif hardening note if
  oneshot full-frame transfers misbehave.

### Waveshare RP2040-Touch-LCD-1.28 (round GC9A01A)

- **board_config titles:** `Waveshare RP2040-Touch-LCD-1.28 GC9A01 240x240`
  (MicroPython); same board under CircuitPython
- **Dirs:** `board_configs/busdisplay/spi/rp2040-touch-lcd-1.28` (MP);
  `board_configs/busdisplay/spi/cp_rp2040-touch-lcd-1.28` (CP)
- **Resolution:** 240×240 round GC9A01A, `bgr=True`,
  `reverse_bytes_in_word=True`, `invert=True`
- **Bus (MP):** `SPIBus(id=1, sck=10, mosi=11, dc=8, cs=9)` @ **10 MHz**
  (60 MHz was flaky on cold boot). Reset GPIO13 (active-low), backlight
  GPIO25.
- **Bus (CP):** `busio.SPI(clock=LCD_CLK, MOSI=LCD_DIN)` + `FourWire` on
  `LCD_DC` / `LCD_CS` / `LCD_RST` @ 10 MHz. Official CP board build
  (`waveshare_rp2040_touch_lcd_1_28`) exposes `LCD_*` / `IMU_*` aliases —
  **no** `board.SPI()` / `board.I2C()`.
- **Panel init:** Waveshare full GC9A01A sequence in `drivers/display/gc9a01.py`
  with **MADCTL `0x98`** and **COLMOD `0x05`**. Short Adafruit-style init +
  BusDisplay’s post-init COLMOD **`0x55`** → backlight on, **no pixels**.
  Reinforce `0x36`/`0x3A` after construct.
- **Backlight:** sticky **GPIO** (not PWM). PWM/BL dies across soft-reset and
  looks like a blank panel.
- **Touch:** CST816 family @ `0x15` on I2C1 **SDA=6 / SCL=7** (CP:
  `IMU_SDA`/`IMU_SCL`); RST=GP22, IRQ=GP21 (polled). MP: `cst8xx.CST8XX`;
  CP: `drivers/touch/circuitpython/cst816.py`.
- **Flash / USB:** RP2040 ROM UF2 bootloader (`RPI-RP2`). Example unit serial
  `E462A052C73E4A29`. MicroPython `RPI_PICO` → CDC `VID_2E8A`/`PID_0005`;
  CircuitPython board UF2 → `PID_1057` + **CIRCUITPY**. Adafruit TinyUF2
  does **not** ship RP2040 builds (ROM UF2 only).
- **displayif notes:** native `spibus` `SPI.init` must **not** re-pass
  sck/mosi/miso on **rp2** (`extra keyword arguments given`); pin kwargs are
  ESP-only. Same guard in Python `drivers/bus/spibus.py`.
- **Demos:** LVGL/`lv_test_timer` is too RAM-heavy on this board. Prefer
  `src/examples/simon.py` (MP graphics + eventsys) or
  `src/examples/circuitpython/simon_waveshare_rp2040_touch_lcd_1_28.py`
  (standalone CP). Soft-reset / USB attach after heavy SPI init can wedge
  CDC — prefer cold boot / power cycle for recovery; keep `boot.py`
  USB-settle + try/except if auto-launching Simon.
- **Setup:** mip / `/setup` the matching `package.json`. MP notes: prefer
  firmware-native displayif `spibus` when present; otherwise Python
  `spibus.py` with the rp2 pin-kwargs fix.

### Adafruit Metro M7 + 2.8″ TFT Touch Shield (cap, product 1947)

- **board_config title:** `Metro M7 + Adafruit 2.8" TFT Touch Shield (cap) product 1947`
- **Dir:** `board_configs/busdisplay/spi/metro_m7_tft_touch_shield_1947` (MP;
  package `v0.2`)
- **Resolution:** 240×320 ILI9341, `bgr=True`, `reverse_bytes_in_word=True`,
  `rotation=0`
- **Shield SPI jumpers (one toggle in board_config):** `USE_SOFTSPI`
  - **`True` (default bring-up):** solder **11/SO/SI** (Arduino D11–D13),
    **cut ICSP** → `SPIBus(soft=True, sck="D13", mosi="D11", miso="D12",
    dc="D9", cs="D10")` @ 2 MHz. Metro **cannot** remap `machine.SPI(0)` onto
    D11–D13 (`extra keyword arguments given`).
  - **`False`:** solder **ICSP**, cut 11/SO/SI → `SPIBus(id=0, dc="D9",
    cs="D10")` @ 24 MHz (LPSPI1 / ICSP = AirLift SPI bus).
- **Pins:** CS=`D10`, DC=`D9`; hold `ESP_CS` and SD `D4` high when unused.
  Named pins (`"D9"`) work; numeric GPIO ids often fail on mimxrt.
- **Touch:** FT6206 @ `0x38` on `I2C(0)`; `touch_rotation_table = (6, 3, 0, 5)`.
  `runtime = None` — OCRAM heap ~**64 KiB**; eventsys / LVGL / full `.py`
  stacks do not fit.
- **AirLift (onboard NINA):** MicroPython `network.WLAN` = `nina` over
  **SPI(0)** @ 8 MHz (`ESP_CS` / `ESP_BUSY` / `ESP_RESET` / `ESP_GPIO0`).
  Join + DNS work; **TCP sockets failed** with `fd=-1` / `OSError: 0` on
  nina-fw **1.7.4** (ssl/mip modules present but unusable). On
  **CircuitPython 10.2.1** the same AirLift works via frozen
  `adafruit_esp32spi` + `adafruit_requests` (HTTP/HTTPS OK). SoftSPI display
  on D11–D13 coexists with AirLift; HW display SPI0 **shares** the AirLift bus.
- **Flash / USB:** TinyUF2 **`METROM7BOOT`**. Custom MP build:
  displayif + graphics (+ optional LVGL), `bundle-networking`,
  `MICROPY_HW_FLASH_VFS_OFFSET=0x180000` (moving VFS **wipes** `/flash` —
  redeploy board_config / secrets). Example CDC after MP: `VID_F055`/`PID_9802`
  (e.g. `COM12`); after CP board UF2: Adafruit `VID_239A`/`PID_80E2` +
  **CIRCUITPY** (e.g. `COM60`).
- **displayif notes:** native `spibus` accepts pin **int / name str / Pin** and
  **`soft=True`** (SoftSPI; skip `init()` on each send). Firmware must include
  that SoftSPI path (`SPIBus loaded (SoftSPI)`).
- **CircuitPython caveats (same shield wiring):** `displaysys` **MemoryError**
  on ~35 KiB free heap. `bitbangio.SPI.write` → `OSError: 5`; GPIO SoftSPI
  paint works but is very slow. Prefer MP + native SoftSPI for pydisplay;
  CP is fine for AirLift networking.
- **Demos:** stripes / `ili9341` over SoftSPI; Simon needs a slim path (no
  eventsys). LVGL unlikely without frozen/slim runtime.
- **Setup:** mip / `/setup` `metro_m7_tft_touch_shield_1947` after WiFi is up
  (or `mpftp put` over serial if NINA sockets are still broken).

---

## DotClock knobs (cross-cutting)

Do not conflate these (full write-up in displayif `SOFT_RESET_AND_BRINGUP.md`):

| Knob | Role |
|------|------|
| **Bounce buffer** | DRAM staging for PSRAM DPI — always on for large panels |
| **Double panel FBs** | Tear-free present when LVGL blits the panel FB |
| **`auto_refresh=False` (MP)** | `FBDisplay.show()` → `refresh()` promote; CP Qualia’s `True` needs a separate Bitmap |

Smoke: after `fill_rect`, call `display_drv.show()`. LVGL via `display_driver`
already sets `refresh_cb=display_drv.show`.

---

*Seeded 2026-07-20 from the pydisplay + displayif bring-up chat; T-Embed /
T-HMI busdisplay notes expanded 2026-07-21; Waveshare RP2040-Touch-LCD-1.28
added 2026-07-21; Adafruit Metro M7 + TFT Touch Shield 1947 added 2026-07-21.
Add a row when a new display board is verified; link inventory fixture numbers
when Detect has captured the silicon.*
