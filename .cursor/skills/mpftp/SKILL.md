---
name: mpftp
description: >-
  Drive MicroPython boards via mpftp (mpremote-backed): connect, filesystem,
  REPL, resets, mip, romfs, and watch live activity. Use when working with mpftp,
  MicroPython serial boards, COM ports on WSL, or board file transfer/REPL.
---

# mpftp — agent guide

mpftp is a Cursor/VS Code extension plus CLI for MicroPython over USB serial.
Prefer the **CLI through the extension TCP RPC** (`127.0.0.1:7429`) so you share
the UI session and do not fight over the serial port.

## Paths (always available)

| Path | Purpose |
|------|---------|
| `~/.mpftp/rpc.port` (`127.0.0.1:7429`) | TCP JSON-RPC to the live extension session |
| `~/.mpftp/activity.log` | NDJSON of connects, transfers, RPC, errors |
| `~/.mpftp/repl.log` | Mirrored REPL I/O when REPL is open |
| `<workspace>/.mpftp/activity.log` | Same activity mirrored into the workspace |

Extension must be loaded (window open with mpftp installed) for the socket.

## Setup tooling

```bash
chmod +x scripts/mpftp
./scripts/mpftp status
ln -sf "$(pwd)/scripts/mpftp" ~/bin/mpftp
```

On WSL, the CLI uses Windows `python.exe` + mpremote for COM ports (same as the UI).

## Watch activity

```bash
./scripts/mpftp watch              # activity.log
./scripts/mpftp watch --repl       # repl.log
tail -f ~/.mpftp/activity.log
```

## Board ops

```bash
./scripts/mpftp ports
./scripts/mpftp connect COM4       # probes MicroPython + sets RTC
./scripts/mpftp resume             # reconnect last device
./scripts/mpftp ls /
./scripts/mpftp tree /
./scripts/mpftp put ./main.py /main.py --verify
./scripts/mpftp get /main.py ./main.py --verify
./scripts/mpftp cp ./lib :/lib --verify          # : = board path
./scripts/mpftp cp :/a.py :/b.py
./scripts/mpftp hash /main.py
./scripts/mpftp mkdir /lib
./scripts/mpftp touch /boot.py
./scripts/mpftp rename /old.py /new.py
./scripts/mpftp rm /junk.py
./scripts/mpftp rm -r /olddir
./scripts/mpftp edit /main.py      # requires $EDITOR
./scripts/mpftp eval '1+1'
./scripts/mpftp exec 'print(42)'
./scripts/mpftp run ./script.py
./scripts/mpftp soft-reset
./scripts/mpftp hard-reset
./scripts/mpftp bootloader
./scripts/mpftp rtc
./scripts/mpftp rtc --set
./scripts/mpftp df
./scripts/mpftp mip github:org/repo   # host-side mip (downloads on PC)
./scripts/mpftp mount /some/local/path
./scripts/mpftp umount
./scripts/mpftp romfs query
./scripts/mpftp romfs build ./romdir -o ./out.romfs
./scripts/mpftp romfs deploy ./out.romfs --partition 0
./scripts/mpftp disconnect
```

Raw RPC:

```bash
./scripts/mpftp rpc fs_listdir '{"path":"/"}'
./scripts/mpftp rpc ping
```

Standalone (no extension): pass `-d/--device` after the subcommand.

## Firmware builder (host-side build & flash)

Builds MicroPython from a local checkout (no `build_mp.sh`). User C modules
(`micropython.cmake` / `*/micropython.mk`) and a frozen `manifest.py` are
auto-discovered from the checkout's **parent** workspace. Runs as its own
process (native Linux python on WSL for `make`), so it never blocks the serial
session. All MP ports build; flash is supported for `esp32`, `rp2`, `samd`.

```bash
./scripts/mpftp firmware discover                 # resolved MP / IDF / emsdk paths
./scripts/mpftp firmware list                     # ports -> boards -> variants tree
./scripts/mpftp firmware cmods                    # discovered user C modules
./scripts/mpftp firmware build --port esp32 --board ESP32_GENERIC   # streams log
./scripts/mpftp firmware build --port unix --variant standard --clean
./scripts/mpftp firmware artifact --port esp32 --board ESP32_GENERIC # Ready?/path
./scripts/mpftp firmware flash --port esp32 --board ESP32_GENERIC -d COM4
./scripts/mpftp firmware flash -d COM5            # same artifact, next board (no rebuild)
./scripts/mpftp firmware partitions get --board ESP32_GENERIC
./scripts/mpftp firmware partitions set --board ESP32_GENERIC --rows '[{...}]'
./scripts/mpftp firmware partitions reset --board ESP32_GENERIC
```

- `--mp` is auto-discovered (setting `mpftp.micropythonPath` → `MP_DIR` →
  common layouts). Pass `--mp PATH` to override.
- esp32 needs ESP-IDF (`mpftp.idfPath` / `IDF_PATH`); flashing on WSL uses the
  Windows python esptool so it can see `COMx`.
- rp2/samd flash copies the `.uf2` to the bootloader drive (rp2 falls back to
  `picotool`); put the board in BOOTSEL/bootloader mode first.
- ESP32 partition edits save to `<workspace>/mpftp-partitions/esp32/<board>.csv`
  (workspace override — the MicroPython clone is never modified) and are injected
  at build time.
- UI: **Firmware** button in the Board Files toolbar, or `mpftp: Build & Flash
  Firmware`. Same operations over Agent RPC as `firmware_*` methods
  (`firmware_list`, `firmware_build`, `firmware_flash`, `firmware_partitions`, …).

## Rules

- Prefer the TCP RPC session over spawning a second sidecar while the UI is connected.
- Connect fails clearly if the port is bootloader/UF2-only (no MicroPython raw REPL).
- Dotfiles / `__pycache__` / `*.pyc` are skipped by the FTP UI upload; CLI `put` of a single file does what you ask.
- Do not commit secrets into activity logs; they may contain paths and script snippets.
