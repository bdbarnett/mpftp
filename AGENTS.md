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

User modules and aggregators: see **[docs/aggregator.md](docs/aggregator.md)**.

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

### ESP32 partition autosize

If an esp32 build fails because the app image is larger than the `factory` (or
other app) partition, mpftp **parses the overflow**, writes a grown table to
`<workspace>/esp32_partitions/<board>.csv` (sibling of `micropython/` — the
MicroPython tree is never edited), patches the build-dir `sdkconfig`, and
**rebuilds once**. Disable with `--no-autosize`.

Details: [user guide — Autosize](docs/user-guide.md#esp32-partition-autosize).

---

## Troubleshooting playbook

| Symptom | Agent action |
|---------|----------------|
| Port busy / exclusive lock | Disconnect UI tools; `./scripts/mpftp disconnect`; kill stale `sidecar.py` / `python.exe` holding the COM port |
| `could not enter raw repl` after flash | Detect; erase + reflash MicroPython; corrupt FS boot loops block soft-reset |
| Wrong board / no Wi-Fi on P4 | Detect + MicroPython hints; pick `C5_WIFI` / `C6_WIFI` explicitly if needed |
| Build: required tree not found | Symlink under firmware workspace or set env (`IDF_PATH`, `EMSDK`, …); Locate… in UI |
| App partition too small | Let autosize rebuild once, or adjust `esp32_partitions/<board>.csv` |
| Module missing from firmware | See [aggregator.md](docs/aggregator.md); `firmware cmods` |

---

## RPC reminder

```bash
./scripts/mpftp rpc ping
./scripts/mpftp rpc fs_listdir '{"path":"/"}'
# Firmware methods are also exposed over the same agent RPC when the extension runs.
```

See [docs/user-guide.md](docs/user-guide.md),
[docs/aggregator.md](docs/aggregator.md), and
[docs/developers-guide.md](docs/developers-guide.md). Keep this file aligned when
CLI or discovery contracts change.
