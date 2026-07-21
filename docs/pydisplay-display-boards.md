# Pydisplay display boards (bring-up notes)

Sister doc to [`board-inventory.md`](board-inventory.md) (esptool / Detect
hardware inventory). This file tracks **pydisplay `board_config` bring-ups** —
panel resolution, touch, interface module, and quirks — for boards exercised in
the July 2026 DotClock / `mipidsi` + LVGL (`lv_test_timer`) campaign.

Paths are under pydisplay `board_configs/fbdisplay/<name>/` unless noted.
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

*Seeded 2026-07-20 from the pydisplay + displayif bring-up chat. Add a row when
a new display board is verified; link inventory fixture numbers when Detect has
captured the silicon.*
