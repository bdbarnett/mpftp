# mpftp — MicroPython board tools for VS Code / Cursor

FTP-style dual-pane file browser and full ANSI REPL for MicroPython boards over USB serial.

Works on:

- **Native Linux** — `/dev/ttyACM*`, `/dev/ttyUSB*` via local Python + `mpremote`
- **Native Windows** — `COMx` via Windows Python + `mpremote`
- **WSL / Cursor Remote-WSL** — uses **Windows `python.exe`** (same stack as `mpremote.exe`) so **COM ports** are visible without `usbipd`

## Features

- Dual-pane local ↔ board file browser (upload, download, mkdir, new file, delete, rename, drag-and-drop)
- Open board files in the editor (save writes back); SHA-256 verify on transfers (configurable)
- Integrated REPL with ANSI/VT color codes
- Connect probes MicroPython raw REPL (rejects UF2/bootloader-only ports) and sets RTC from the host
- Hard-reset auto-reconnect; **Resume** reconnects the last device
- mpremote-backed ops: eval/exec/run, soft/hard reset, bootloader, RTC, host-side **mip**, df, mount/umount, romfs, recursive `cp`, hash
- Agent CLI + TCP RPC (`127.0.0.1:7429`) sharing the UI serial session
- **Firmware builder**: guided build/flash UI (build MicroPython once, flash one or many boards), auto-discovered user C modules / frozen manifest, esp32/rp2/samd flashing, esptool-first **Detect** (chip/flash/security + board autoset), and an ESP32 firmware/storage split with workspace partition overrides

## Requirements

- VS Code or Cursor
- Python 3 with [`mpremote`](https://pypi.org/project/mpremote/) installed
  - **WSL:** Windows Python + `pip install mpremote` (you already have `mpremote.exe`)
  - **Linux:** create the extension venv (see below) or point `mpftp.pythonPath` at a suitable interpreter

## Install (development)

```bash
cd ~/gh/bdbarnett/mpftp
export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh"
npm install
npm run compile

# WSL / Cursor remote: install into the remote extension host
./scripts/install-cursor-wsl.sh
# then: Developer: Reload Window

# Linux serial support (optional if you use WSL+COM):
python3 -m venv .venv
.venv/bin/pip install mpremote
```

### Package a VSIX

```bash
./scripts/package-vsix.sh
# or: npm run package
```

Then **Extensions: Install from VSIX…** and pick the generated `.vsix`.

## Usage

1. Click the **mpftp** activity-bar icon (or status-bar **mpftp**)
2. **Connect** and pick a port (`COM4`, `/dev/ttyACM0`, …)
3. Use the dual-pane browser (drag local → board / board → local); **REPL** opens an ANSI terminal
4. Double-click a board file to edit it in VS Code; save pushes it back

Useful commands:

| Command | Action |
|--------|--------|
| `mpftp: Connect to Board` | Port picker / disconnect menu |
| `mpftp: Resume Last Device` | Reconnect previous COM port |
| `mpftp: Open File Browser` | Focus FTP view |
| `mpftp: Edit Board File` | Pull → edit → save back |
| `mpftp: Open REPL` | ANSI REPL terminal |
| `mpftp: Run Current File on Board` | exec buffer via raw REPL |
| `mpftp: Soft Reset` / `Hard Reset` | reset (hard reset auto-reconnects by default) |
| `mpftp: mip Install Package` | Host-side mip install onto the board |

## Settings

- `mpftp.pythonPath` — override Python (on WSL, leave empty to auto-pick `python.exe`)
- `mpftp.mpremotePath` — optional CLI path for diagnostics
- `mpftp.defaultBaud` — default `115200`
- `mpftp.autoConnectDevice` — e.g. `COM4` to skip the picker
- `mpftp.verifyTransfers` — SHA-256 check after each file transfer (default `true`)
- `mpftp.autoReconnectAfterReset` — reconnect after hard reset (default `true`)
- `mpftp.micropythonPath` — MicroPython checkout used by the Firmware builder (auto-discovered if empty)
- `mpftp.idfPath` / `mpftp.emsdkPath` — ESP-IDF / emsdk locations (auto-discovered if empty)
- `mpftp.buildPythonPath` — native (Linux on WSL) python3 to run the build engine + `make`
- `mpftp.esptoolCommand` — override esptool for flashing (e.g. a Windows `python.exe` on WSL so it sees COM ports)

## Firmware builder

Open **Firmware** from the Board Files toolbar (or `mpftp: Build & Flash Firmware`).
It reimplements the useful parts of a cmods-style build without shelling out to
`build_mp.sh`:

1. **Target** — pick a port → board → variant from the tree (all MP ports are
   listed; flashing is enabled for `esp32`, `rp2`, and `samd`).
2. **Modules** — user C modules (`micropython.cmake` / `*/micropython.mk`) and a
   frozen `manifest.py` are auto-discovered from the checkout's parent workspace
   and shown before you build.
3. **Build** — incremental `make submodules` + `make all` (with a separate
   **Clean**), streaming log, and a **Ready** state with the artifact path/size.
4. **Flash** — build once, then flash many: swap boards and click **Flash**
   again with no rebuild. esp32 uses `esptool` (Windows python on WSL for COM
   ports); rp2/samd copy the `.uf2` to the bootloader drive (rp2 falls back to
   `picotool`).

**Detect** reads the board with esptool first, so it works even on a bare board
with no MicroPython: chip, revision, flash size, PSRAM, and flash-encryption /
secure-boot state populate a Device Info card, and the board / variant / flash
size are auto-selected. If a MicroPython session was active it is briefly
released, probed, then reconnected to enrich the card (clock, free heap,
`_build`). Non-Espressif boards get only a suggested firmware port — never a
forced `ESP32_GENERIC_*` — and ESP32-P4 external Wi-Fi variants (`C5_WIFI` /
`C6_WIFI`) are chosen only from MicroPython hints. If a device reports flash
encryption or secure boot enabled, flashing is guarded behind a warning.

For esp32 the Firmware page offers a simple **firmware / storage** split (resize
or add the storage partition), and **Advanced…** opens the full **Partitions…**
editor (presets, a flash-usage bar, and overlap/alignment validation). Both save
a **workspace override** at `<workspace>/mpftp-partitions/esp32/<board>.csv`
— the MicroPython clone is never modified. Builds inject the override (and its
companion flash-size fragment) into the build-dir `sdkconfig` automatically.

```bash
./scripts/mpftp firmware list
./scripts/mpftp firmware detect -d COM4         # esptool-first chip/flash/security probe
./scripts/mpftp firmware build --port esp32 --board ESP32_GENERIC
./scripts/mpftp firmware flash --port esp32 --board ESP32_GENERIC -d COM4
./scripts/mpftp firmware flash -d COM5          # same artifact, next board
./scripts/mpftp firmware partitions get --board ESP32_GENERIC
```

## Agents / CLI

| Path | Purpose |
|------|---------|
| `~/.mpftp/rpc.port` (`127.0.0.1:7429`) | JSON-RPC into the running extension (shared serial session) |
| `~/.mpftp/activity.log` | NDJSON activity (connect, transfers, RPC, errors) |
| `~/.mpftp/repl.log` | Mirrored REPL I/O |

```bash
./scripts/mpftp status
./scripts/mpftp ports
./scripts/mpftp connect COM4
./scripts/mpftp ls /
./scripts/mpftp put ./main.py /main.py --verify
./scripts/mpftp cp ./lib :/lib --verify
./scripts/mpftp hash /main.py
./scripts/mpftp resume
./scripts/mpftp romfs query
./scripts/mpftp eval '1+1'
./scripts/mpftp watch
```

See `.cursor/skills/mpftp/SKILL.md` for the full command surface.

## Tests

```bash
npm run test:python
# or: python3 -m unittest discover -s python/tests -v
```

## Architecture

```
Extension (TypeScript)
  ├─ SidecarBridge  ──stdin/stdout JSON──►  python/sidecar.py
  │                                            └─ mpremote SerialTransport
  ├─ AgentRpcServer ──TCP :7429──►  scripts/mpftp / agents
  ├─ ActivityLog     ~/.mpftp/activity.log + repl.log
  ├─ Pseudoterminal REPL (ANSI passthrough)
  ├─ Webview FTP UI (media/ftp.*)
  └─ Firmware builder (src/firmware/*, media/firmware.*, media/partitions.*)
       └─ python/firmware_engine.py  ──make / esptool / uf2──►  build & flash
```

The firmware engine runs as its own extension-host child process (native Linux
python on WSL for `make`), so builds never block the serial REPL/filesystem
session. It is also driven by `mpftp firmware …` (CLI) and `firmware_*` Agent
RPC methods.

On WSL the sidecar is intentionally a **Windows** Python process so it opens `COMx` the same way `mpremote.exe` does.
