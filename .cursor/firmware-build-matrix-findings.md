# mpftp firmware build matrix — discovery findings

**Discovery only. No mpftp code was modified.** This documents an experiment:
run the mpftp firmware `build` engine once for a single board/variant in each
available port and record what happens.

- **Date:** 2026-07-19
- **Host:** WSL (Linux)
- **MicroPython tree:** `/home/brad/gh/pydevices/cmods/micropython`
- **Workspace (parent):** `/home/brad/gh/pydevices/cmods`
- **Auto-discovered env:** ESP-IDF `…/other/esp-idf`, emsdk `…/other/emsdk`, SDL2_DEV `…/other/SDL2-2.30.10`
- **Driver:** `/tmp/mpftp_buildmatrix/run.py` (outside the repo); per-build cap 600 s
- **Per-port logs:** `/tmp/mpftp_buildmatrix/<port>.log`, machine summary `summary.json`

## Method

For each port the driver picked one representative target:

- `boards` ports → the **first board alphabetically** (variant default)
- `variants` ports (unix/webassembly/windows) → the **`standard`** variant
- `plain` ports (bare-arm/minimal/cc3200/pic16bit) → no board

Each target was built with:

```
python3 python/firmware_engine.py build --mp <mp> --port <port> [--board <b>] [--variant <v>]
```

ESP-IDF / emsdk / SDL2_DEV were resolved by the engine's normal auto-discovery
(no explicit paths passed). Builds were **not** run with `clean`.

## Results

| Port | Target (auto-picked) | Result | Time | Root cause |
|------|----------------------|--------|------|-----------|
| alif | ALIF_ENSEMBLE | ❌ fail | 18 s | cmods: LVGL OpenGLES shader source fails `-Werror` |
| bare-arm | (plain) | ❌ fail | 3 s | toolchain: `arm-none-eabi-ld: cannot find -lm` |
| cc3200 | (plain / WIPY) | ❌ fail | 11 s | cmods: `graphics` uses `mp_obj_get_float` (implicit decl) |
| esp32 | ARDUINO_NANO_ESP32 (S3) | ❌ fail | 33 s | IDF: missing `esp32s3/.../mspi_timing_tuning/…/include` (IDF version mismatch) |
| esp8266 | ESP8266_GENERIC | ❌ fail | 3 s | toolchain: `xtensa-lx106-elf-gcc` not installed |
| mimxrt | ADAFRUIT_METRO_M7 | ❌ fail | 13 s | cmods: LVGL OpenGLES shader source fails `-Werror` |
| minimal | (plain) | ❌ fail | 3 s | port/cmods: `MICROPY_MODULE_FROZEN_MPY` redefined `-Werror` |
| nrf | ACTINIUS_ICARUS | ❌ fail | 7 s | cmods: LVGL OpenGLES shader source fails `-Werror` |
| pic16bit | (plain) | ❌ fail | 2 s | toolchain: `/opt/microchip/xc16/.../xc16-gcc` missing |
| qemu | MICROBIT | ❌ fail | 14 s | cmods: builds LVGL OpenGLES desktop driver for the target |
| renesas-ra | ARDUINO_PORTENTA_C33 | ❌ fail | 2 s | system dep: `protoc-c` missing (Error 127) |
| rp2 | ADAFRUIT_FEATHER_RP2040 | ❌ fail | 80 s | cmods: `graphics` link errors (undefined `free`/`strrchr`/`fopen`) |
| samd | ADAFRUIT_FEATHER_M0_EXPRESS | ❌ fail | 7 s | cmods: `displayif` API mismatch (too few args) |
| stm32 | ADAFRUIT_F405_EXPRESS | ❌ fail | 23 s | cmods: `graphics` link errors (undefined libc symbols) |
| **unix** | **standard** | ✅ **ok** | **4 s** | **built `…/ports/unix/build-standard/micropython`** |
| webassembly | standard | ❌ fail | 17 s | `-Werror` `unused-but-set-global` in `main.c` (emsdk clang) |
| windows | standard | ❌ fail | 12 s | built with host gcc → `windows.h: No such file` (no MinGW cross set) |

**Score: 1 / 17 succeeded (unix/standard).**

## Analysis

### 1. The biggest cause: workspace C modules are force-included in *every* build

Every build logged:

```
[mpftp] USER_C_MODULES=/home/brad/gh/pydevices/cmods
[mpftp] user C modules: displayif, graphics, lv_micropython_cmod, usdl2
```

The engine aggregates **all** workspace modules for **every** port. Those modules
are desktop/LVGL/SDL-oriented and are not portable to bare-metal/embedded ports,
so they break the build regardless of the toolchain:

- **`lv_micropython_cmod`** pulls in LVGL, including its **OpenGLES desktop
  driver**. `lv_bindings/lvgl/src/drivers/opengles/assets/lv_opengles_shader.c`
  fails to compile under the cross toolchains with
  `error: missing terminating " character [-Werror]`.
  → alif, mimxrt, nrf, qemu (qemu was actively compiling `lv_opengles_*.c` for MICROBIT).
- **`graphics`** relies on host libc (`free`, `strrchr`, `fopen`) and desktop-only
  MicroPython APIs (`mp_obj_get_float`). On bare-metal ports this yields
  `undefined reference` link errors (stm32, rp2) or implicit-declaration errors (cc3200).
- **`displayif`** hit an API mismatch on samd
  (`too few arguments to function 'displayif_i80bus_gpio_pins_sequential'`).

**Implication:** mpftp has no notion of *which* workspace modules are compatible
with the selected port. For a workspace of desktop-class modules, only desktop-class
targets (unix, and — per earlier manual runs — esp32-P4/S3-class boards with LVGL)
are expected to build. Bare-metal ARM/xtensa/pic ports will fail on the modules
before the toolchain even matters.

### 2. Genuinely missing toolchains / system deps (independent of cmods)

- **esp8266** — `xtensa-lx106-elf-gcc` not installed.
- **pic16bit** — Microchip `xc16-gcc` not installed.
- **renesas-ra** — `protoc-c` not installed (upstream extmod needs it to generate `esp_hosted.pb-c.c`).
- **bare-arm** — `arm-none-eabi-ld: cannot find -lm` (installed ARM GCC lacks the needed newlib libm variant).

Toolchains that **are** present: `arm-none-eabi-gcc` (14.2), `x86_64-w64-mingw32-gcc`,
plus `emcc` via the sourced emsdk. Missing: xtensa (lx106 + esp32), riscv, xc16, protoc-c.

### 3. Toolchain present but build still fails

- **esp32 / ARDUINO_NANO_ESP32 (ESP32-S3):** CMake aborts because
  `…/esp-idf/components/esp_hw_support/mspi_timing_tuning/port/esp32s3/include`
  isn't a directory — an **ESP-IDF version/layout mismatch** against this
  MicroPython checkout. (Note: earlier manual runs built ESP32-P4 fine, so this
  looks board/IDF-version specific rather than a blanket esp32 failure.)
- **webassembly / standard:** `emcc` runs, but the build dies on
  `main.c:50: error: variable 'external_call_depth' set but not used
  [-Werror,-Wunused-but-set-global]` — the emsdk's clang treats a new warning as
  an error. A toolchain/source version skew, not a missing SDK.
- **windows / standard:** compiled with **host gcc** (`windows.h: No such file`,
  `_fmode`/`O_BINARY` undeclared). MinGW **is installed**
  (`x86_64-w64-mingw32-gcc`), but the engine didn't invoke the windows port with
  `CROSS_COMPILE=x86_64-w64-mingw32-`, so it tried a native build.

### 4. The one success

- **unix / standard** built cleanly to
  `…/ports/unix/build-standard/micropython` in ~4 s — full host libc, and the
  port the workspace modules actually target.

## Caveats

- **Discovery only** — nothing in mpftp was changed based on these results.
- **Target choice matters.** Targets were the *first board alphabetically* per
  port. A different board (or the correct IDF version for esp32) could change
  individual outcomes — notably esp32, where a P4 board built manually before.
- Builds were incremental (no `clean`); a couple of ports had prior build dirs.
- Findings reflect the current host's installed toolchains and the specific
  ESP-IDF/emsdk checkouts under `…/other/`.

## Suggested follow-ups (not implemented)

- Consider per-port/per-module compatibility gating so incompatible workspace
  modules aren't force-fed into ports that can't build them (or an opt-out).
- For the windows port, wire `CROSS_COMPILE=x86_64-w64-mingw32-` when MinGW is
  detected (mirrors how ESP-IDF/emsdk/SDL2_DEV are discovered).
- Investigate the ESP-IDF version pinned by this MicroPython checkout vs. the
  discovered `…/other/esp-idf` (S3 component path mismatch).
