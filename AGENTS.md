# AGENTS.md — using mpftp with MicroPython boards

This document is for **coding agents** (and humans driving the same CLI) that need
to talk to a board, push Python, rebuild firmware with user C modules, or flash
for recovery. Prefer the **extension TCP RPC** so you share the UI’s serial
session and do not open a second connection on the same port.

## Session model

| Path | Purpose |
|------|---------|
| `~/.mpftp/rpc.port` | Usually `127.0.0.1:7429` — JSON-line RPC to the live extension |
| `~/.mpftp/activity.log` | NDJSON of connects, transfers, RPC, errors |
| `~/.mpftp/repl.log` | REPL I/O when a REPL is open |
| `<workspace>/.mpftp/activity.log` | Same activity mirrored into the open workspace |

The Cursor/VS Code window must have **mpftp loaded** for the socket to exist.
On WSL, serial and esp32 flash use **Windows Python** so `COM` ports work.

```bash
chmod +x scripts/mpftp
./scripts/mpftp status          # rpc up? connected device?
./scripts/mpftp watch           # follow activity.log
```

Standalone (no extension): pass `-d/--device` after the subcommand. Prefer RPC
while the UI is connected.

---

## Board workflow (day-to-day)

### Connect

```bash
./scripts/mpftp ports
./scripts/mpftp connect COM4        # or /dev/ttyACM0
./scripts/mpftp resume              # last device
```

Connect **interrupts** any running program and enters raw REPL with a **soft
reset that skips `main.py`**. If connect fails with a filesystem-corruption
banner, the board may need erase + reflash (see Troubleshooting).

### MicroPython `.py` on the board

Treat the board like a small filesystem. Prefer verified transfers for anything
that must land intact.

```bash
./scripts/mpftp ls /
./scripts/mpftp tree /
./scripts/mpftp put ./main.py /main.py --verify
./scripts/mpftp get /main.py ./main.py --verify
./scripts/mpftp cp ./lib :/lib --verify     # : = board path
./scripts/mpftp mkdir /lib
./scripts/mpftp rm /junk.py
./scripts/mpftp eval '1+1'
./scripts/mpftp exec 'import main'
./scripts/mpftp run ./script.py             # local file via exec
./scripts/mpftp interrupt                   # Ctrl-C; no reset
./scripts/mpftp soft-reset                  # fresh heap; does not run main.py
./scripts/mpftp hard-reset
./scripts/mpftp mip github:org/repo         # host-side mip install onto board
```

**Rules of thumb**

- Debug with `exec` / `eval` / `run` before rewriting `main.py`.
- Soft-reset after bad imports; hard-reset if the port is wedged.
- Dotfiles / `__pycache__` are skipped by the File Transfer UI; CLI `put` of a
  single path does what you ask.
- Do not put secrets in scripts that will show up in `activity.log`.

---

## Firmware: diagnose, download, build, flash

Firmware commands are **host-side** (separate from the sidecar). They do not
hold the serial lock for the whole build.

### Detect (troubleshooting first step)

Works on a bare board (no MicroPython). Releases a live session briefly if needed.

```bash
./scripts/mpftp firmware detect -d COM4
```

Use chip / flash size / secure-boot / flash-encryption state before flashing.
If secure boot or flash encryption is enabled, stop and confirm before erase.

### Official download (no local checkout)

```bash
./scripts/mpftp firmware download-tree
# UI: Firmware → Download → pick board/version → Download → Flash
```

### Build from a firmware workspace

A **firmware workspace** must provide MicroPython: `micropython/` (dir or
symlink) or the folder *is* the tree (`ports/` + `py/`). Port SDKs
(`esp-idf`, `emsdk`, …) must be **in that workspace (or symlinked)** or set via
env vars — same contract for every dependency, no special home-path hunts.

```bash
./scripts/mpftp firmware discover
./scripts/mpftp firmware list
./scripts/mpftp firmware cmods
./scripts/mpftp firmware build --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI
./scripts/mpftp firmware artifact --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI
./scripts/mpftp firmware flash --port esp32 --board ESP32_GENERIC_P4 --variant C6_WIFI -d COM4
# erase when recovering a corrupt filesystem / wrong partition layout:
# (Firmware UI → Erase, or engine --erase)
```

Flash without rebuild to the next board:

```bash
./scripts/mpftp firmware flash -d COM5
```

Supported flashers: `esp32` (esptool), `rp2` / `samd` (UF2; BOOTSEL first).

---

## User C modules and the workspace aggregator

### Layout

```
<firmware-workspace>/
  micropython/                 # or symlink
  micropython.cmake            # AGGREGATOR (required for USER_C_MODULES)
  manifest.py                  # AGGREGATOR for frozen Python (optional but usual)
  graphics/                    # example user module
    micropython.cmake          # and/or micropython.mk
    manifest.py                # optional frozen Python for this module
  usdl2/
    micropython.cmake
    ...
  esp-idf -> ...               # optional SDK symlink
  emsdk -> ...
```

Create missing aggregators from the Firmware UI (**Create stubs…**) or copy
`resources/templates/micropython.cmake` and `resources/templates/manifest.py`.

### Aggregator contract (`micropython.cmake`)

When mpftp builds with `USER_C_MODULES=<firmware-workspace>`, MicroPython’s
CMake includes the **workspace-root** `micropython.cmake`. That file is an
**aggregator only**: it does not define modules itself. It finds every
`*/micropython.cmake` under the workspace (following symlinks) and `include()`s
them.

mpftp discovers modules the same way:

- A **user module** is a **direct child directory** of the firmware workspace
  that has **`micropython.cmake` and/or `micropython.mk` in its root**.
- Optional: **`manifest.py` in that same module root** for frozen Python.
- The workspace-root aggregator / root `manifest.py` are **not** modules.
- Hidden directories (`.git`, …) are skipped.
- mpftp passes **every** discovered module into the build for **every** port;
  it does **not** gate by board compatibility. Each module’s own
  `micropython.cmake` / `micropython.mk` must opt in/out and resolve its own
  dependencies (for example SDL2 for a desktop usermod).

### Module root contract (what you put in each repo)

| File | Role |
|------|------|
| `micropython.cmake` | CMake usermod registration for esp32 and other CMake ports |
| `micropython.mk` | Make-based usermod registration for Make ports |
| `manifest.py` | Optional frozen Python for this module (`freeze(...)`, `package(...)`, …) |

At least one of `micropython.cmake` or `micropython.mk` must sit in the
**module root** (not only nested deeper) for mpftp’s discovery and for the
usual aggregator `find`/`include` pattern.

### Frozen Python aggregator (`manifest.py`)

The workspace-root `manifest.py`:

1. Optionally includes `my-manifest.py` (local overrides).
2. Includes each child `*/manifest.py`.
3. Includes upstream via `FROZEN_MANIFEST_UPSTREAM` (mpftp sets this to the
   port/board/variant manifest MicroPython would have used).

Without a root `manifest.py`, the build is still valid but will not freeze
workspace Python packages unless you set `FROZEN_MANIFEST` yourself.

### Agent checklist when changing a usermod

1. Confirm the module directory is a **sibling of `micropython/`** (or symlinked
   there) and has `micropython.cmake` and/or `micropython.mk` at its root.
2. `./scripts/mpftp firmware cmods` — module name should appear.
3. Rebuild the target port/board/variant; flash; connect; exercise the binding
   from the REPL (`exec` / `eval`).
4. Pure Python changes that are **not** frozen can be `put` to the board
   without a firmware rebuild; **native / frozen** changes need Build + Flash.

---

## Troubleshooting playbook

| Symptom | Agent action |
|---------|----------------|
| Port busy / exclusive lock | Disconnect UI tools; `./scripts/mpftp disconnect`; kill stale `sidecar.py` / `python.exe` holding the COM port |
| `could not enter raw repl` after flash | Detect; erase + reflash MicroPython; corrupt FS boot loops block soft-reset |
| Wrong board / no Wi-Fi on P4 | Detect + MicroPython hints; pick `C5_WIFI` / `C6_WIFI` explicitly if needed |
| Build: required tree not found | Symlink under firmware workspace or set env (`IDF_PATH`, `EMSDK`, …); Locate… in UI |
| App partition too small | Let autosize rebuild once, or adjust `esp32_partitions/<board>.csv` |
| Module missing from firmware | Check aggregator + module-root `micropython.cmake`/`micropython.mk`; `firmware cmods` |

---

## RPC reminder

```bash
./scripts/mpftp rpc ping
./scripts/mpftp rpc fs_listdir '{"path":"/"}'
# Firmware methods are also exposed over the same agent RPC when the extension runs.
```

See [docs/user-guide.md](docs/user-guide.md) and
[docs/developers-guide.md](docs/developers-guide.md) for product and packaging
detail. Keep this file aligned when CLI or discovery contracts change.
