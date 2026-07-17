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
  └─ Webview FTP UI (media/ftp.*)
```

On WSL the sidecar is intentionally a **Windows** Python process so it opens `COMx` the same way `mpremote.exe` does.
