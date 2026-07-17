# mpftp — MicroPython board tools for VS Code / Cursor

FTP-style dual-pane file browser and full ANSI REPL for MicroPython boards over USB serial.

Works on:

- **Native Linux** — `/dev/ttyACM*`, `/dev/ttyUSB*` via local Python + `mpremote`
- **Native Windows** — `COMx` via Windows Python + `mpremote`
- **WSL / Cursor Remote-WSL** — uses **Windows `python.exe`** (same stack as `mpremote.exe`) so **COM ports** are visible without `usbipd`

## Features

- Dual-pane local ↔ board file browser (upload, download, mkdir, new file, delete, navigate)
- Integrated REPL with ANSI/VT color codes (VS Code terminal / xterm.js)
- mpremote-backed session: connect, fs ops, eval/exec/run, soft/hard reset, bootloader, RTC, mip, df, mount/umount
- Single long-lived sidecar owns the serial port (REPL and file ops share one connection)

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

# Linux serial support (optional if you use WSL+COM):
python3 -m venv .venv
.venv/bin/pip install mpremote
```

In Cursor/VS Code:

1. **Run → Start Debugging** (or `F5`) on this folder, **or**
2. Install from VSIX: `npx @vscode/vsce package` then **Extensions: Install from VSIX…**
3. Or symlink into your extensions dir and reload.

Command palette: **Developer: Install Extension from Location…** → select this folder (after `npm run compile`).

## Usage

1. Click the **mpftp** activity-bar icon (or status-bar **mpftp**)
2. **Connect** and pick a port (`COM4`, `/dev/ttyACM0`, …)
3. Use the dual-pane browser; **REPL** opens an ANSI terminal

Useful commands:

| Command | Action |
|--------|--------|
| `mpftp: Connect to Board` | Port picker / disconnect menu |
| `mpftp: Open File Browser` | Focus FTP view |
| `mpftp: Open REPL` | ANSI REPL terminal |
| `mpftp: Run Current File on Board` | exec buffer via raw REPL |
| `mpftp: Soft Reset` / `Hard Reset` | reset device |

## Settings

- `mpftp.pythonPath` — override Python (on WSL, leave empty to auto-pick `python.exe`)
- `mpftp.mpremotePath` — optional CLI path for diagnostics
- `mpftp.defaultBaud` — default `115200`
- `mpftp.autoConnectDevice` — e.g. `COM4` to skip the picker

## Architecture

```
Extension (TypeScript)
  ├─ SidecarBridge  ──stdin/stdout JSON──►  python/sidecar.py
  │                                            └─ mpremote SerialTransport
  ├─ Pseudoterminal REPL (ANSI passthrough)
  └─ Webview FTP UI (media/ftp.*)
```

On WSL the sidecar is intentionally a **Windows** Python process so it opens `COMx` the same way `mpremote.exe` does.
