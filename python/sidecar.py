#!/usr/bin/env python3
"""
mpftp sidecar — long-lived mpremote session for VS Code / Cursor.

Speaks newline-delimited JSON on stdin/stdout.
Uses the official mpremote package (same backend as mpremote.exe).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any, Optional


def split_fs_path(path: str) -> tuple[bool, str]:
    """Return (is_remote, path). Board paths use mpremote ':' prefix."""
    if path.startswith(":"):
        return True, path[1:]
    return False, path


def host_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_runtime_name(name: Any) -> str:
    """Map ``sys.implementation.name`` to ``micropython`` | ``circuitpython``."""
    n = str(name or "").strip().lower()
    if n in ("circuitpython", "circuit"):
        return "circuitpython"
    return "micropython"


# CircuitPython "Press any key…" banners (Thonny cirpy_back._ENTER_REPL_PHRASES).
# Substring match — localization variants share distinctive fragments.
_CP_ENTER_REPL_MARKERS = (
    b"Press any key to enter the REPL",
    b"Appuyez sur n'importe quelle touche pour utiliser le REPL",
    b"Presiona cualquier tecla para entrar al REPL",
    b"Dr\xc3\xbccke eine beliebige Taste um REPL",
    b"Druk een willekeurige toets om de REPL",
    b"Tekan sembarang tombol untuk masuk ke REPL",
    b"Pressione qualquer tecla para entrar no REPL",
    b"Tryck p\xc3\xa5 valfri tangent f\xc3\xb6r att g\xc3\xa5 in i REPL",
    "Нажмите любую клавишу чтобы зайти в REPL".encode("utf-8"),
)


def data_has_enter_repl_prompt(data: bytes) -> bool:
    """True if UART output is waiting for a key to enter the friendly REPL."""
    if not data:
        return False
    return any(m in data for m in _CP_ENTER_REPL_MARKERS)


def circup_boot_out_text(*, cpy_version: str, board_id: str = "unknown") -> str:
    """Minimal boot_out.txt so circup --path can resolve board/version."""
    ver = (cpy_version or "9.0.0").strip()
    bid = (board_id or "unknown").strip() or "unknown"
    return (
        f"Adafruit CircuitPython {ver} on 2020-01-01; "
        f"{bid} with unknown\n"
        f"Board ID:{bid}\n"
    )


def build_circup_argv(
    *,
    circup_exe: str,
    stage_path: str,
    packages: list[str],
    cpy_version: str,
    board_id: str = "unknown",
    py: bool = False,
) -> list[str]:
    """CLI argv for host-side circup install into a staging directory."""
    argv = [
        circup_exe,
        "--path",
        stage_path,
        "--board-id",
        board_id or "unknown",
        "--cpy-version",
        cpy_version or "9.0.0",
        "install",
    ]
    if py:
        argv.append("--py")
    argv.extend(packages)
    return argv


def build_circup_web_argv(
    *,
    circup_exe: str,
    host: str,
    password: str,
    packages: list[str],
    py: bool = False,
) -> list[str]:
    """CLI argv for circup install over CircuitPython Web Workflow."""
    argv = [
        circup_exe,
        "--host",
        host,
        "--password",
        password,
        "install",
    ]
    if py:
        argv.append("--py")
    argv.extend(packages)
    return argv


# Ensure UTF-8 stdio on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _notify(method: str, params: Optional[dict[str, Any]] = None) -> None:
    _emit({"type": "notify", "method": method, "params": params or {}})


def _result(req_id: Any, result: Any = None) -> None:
    _emit({"type": "result", "id": req_id, "result": result})


def _error(req_id: Any, message: str, data: Any = None) -> None:
    err: dict[str, Any] = {"type": "error", "id": req_id, "error": message}
    if data is not None:
        err["data"] = data
    _emit(err)


def _mpftp_dir() -> Path:
    d = Path.home() / ".mpftp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sidecar_pid_path() -> Path:
    return _mpftp_dir() / "sidecar.pid"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _force_kill_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if sys.platform == "win32":
        import subprocess

        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False,
            capture_output=True,
        )
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _iter_sidecar_pids_win() -> list[int]:
    """Find other Windows python processes running this sidecar script."""
    import subprocess

    script_marker = "sidecar.py"
    my_pid = os.getpid()
    try:
        out = subprocess.check_output(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -like '*sidecar.py*' "
                "-and $_.CommandLine -like '*mpftp*' } | "
                "Select-Object -ExpandProperty ProcessId",
            ],
            stderr=subprocess.DEVNULL,
            timeout=8,
        ).decode("utf-8", "replace")
    except Exception:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        pid = int(line)
        if pid != my_pid:
            pids.append(pid)
    # Prefer marker match even if mpftp not in command line (WSL path forms).
    if not pids:
        try:
            out = subprocess.check_output(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    f"Get-CimInstance Win32_Process | "
                    f"Where-Object {{ $_.CommandLine -like '*{script_marker}*' "
                    f"-and $_.Name -match 'python' }} | "
                    f"Select-Object -ExpandProperty ProcessId",
                ],
                stderr=subprocess.DEVNULL,
                timeout=8,
            ).decode("utf-8", "replace")
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit() and int(line) != my_pid:
                    pids.append(int(line))
        except Exception:
            pass
    return pids


def cleanup_stale_sidecars() -> list[int]:
    """Kill orphaned sidecar processes that would keep COM ports locked."""
    killed: list[int] = []
    pid_path = _sidecar_pid_path()
    try:
        prev = int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        prev = 0
    if prev and prev != os.getpid() and _pid_alive(prev):
        _force_kill_pid(prev)
        killed.append(prev)
    if sys.platform == "win32":
        for pid in _iter_sidecar_pids_win():
            if pid not in killed:
                _force_kill_pid(pid)
                killed.append(pid)
    try:
        if pid_path.exists():
            pid_path.unlink()
    except OSError:
        pass
    return killed


def claim_sidecar_pid() -> None:
    _sidecar_pid_path().write_text(f"{os.getpid()}\n", encoding="utf-8")


def release_sidecar_pid() -> None:
    path = _sidecar_pid_path()
    try:
        cur = int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return
    if cur == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


# MicroPython inisetup / _boot spam when the vfs partition is corrupt.
_FS_CORRUPT_MARKERS = (
    b"filesystem appears to be corrupted",
    b"factory reprogramming of MicroPython",
    b"fs_corrupted",
)


class Session:
    def __init__(self) -> None:
        self.transport = None
        self.device: Optional[str] = None
        self.last_device: Optional[str] = None
        self.baud = 115200
        self._lock = threading.RLock()
        self._repl_mode = False
        self._repl_stop = threading.Event()
        self._repl_thread: Optional[threading.Thread] = None
        self._mounted_path: Optional[str] = None
        # "micropython" | "circuitpython" — set on successful probe/connect.
        self.runtime: Optional[str] = None
        # Set when connect had to skip raw soft-reset because boot loops on a
        # corrupt filesystem (soft-reset would never re-enter raw REPL).
        self.filesystem_warning: Optional[str] = None

    def list_ports(self) -> list[dict[str, Any]]:
        import serial.tools.list_ports

        ports = []
        for p in serial.tools.list_ports.comports():
            iface = getattr(p, "interface", None) or ""
            # CircuitPython CDC2 is the data interface, not the REPL.
            repl = "CircuitPython CDC2" not in iface
            ports.append(
                {
                    "device": p.device,
                    "serial_number": p.serial_number,
                    "vid": p.vid if isinstance(p.vid, int) else None,
                    "pid": p.pid if isinstance(p.pid, int) else None,
                    "manufacturer": p.manufacturer,
                    "product": p.product,
                    "description": p.description,
                    "interface": iface or None,
                    "hwid": getattr(p, "hwid", None) or None,
                    "repl": repl,
                }
            )
        # REPL-capable first, then by device name; CDC2 (repl=false) last.
        ports.sort(key=lambda x: (0 if x.get("repl", True) else 1, x["device"] or ""))
        return ports

    def connect(
        self, device: str, baud: int = 115200, attempts: int = 3
    ) -> dict[str, Any]:
        from mpremote.transport_serial import SerialTransport
        from mpremote.transport import TransportError
        import time

        # The raw-REPL handshake (and sometimes opening the port itself) can fail
        # transiently right after the board enumerates or if the REPL is momentarily
        # busy — the classic "could not enter raw repl". A fresh reopen + reprobe
        # almost always succeeds, so retry a few times before surfacing the error
        # (mirrors what the user would do by clicking connect again).
        retry_delay = 0.4

        with self._lock:
            self.disconnect()
            self.baud = baud
            self.filesystem_warning = None
            last_probe_err: Optional[Exception] = None
            for attempt in range(1, max(1, attempts) + 1):
                last = attempt >= max(1, attempts)
                # (Re)open the serial transport for this attempt.
                try:
                    self.transport = SerialTransport(device, baudrate=baud)
                except TransportError as e:
                    if not last:
                        time.sleep(retry_delay)
                        continue
                    raise RuntimeError(self._friendly_port_open_error(device, e)) from e
                except OSError as e:
                    if not last:
                        time.sleep(retry_delay)
                        continue
                    raise RuntimeError(self._friendly_port_open_error(device, e)) from e
                # Opening the COM port succeeds in UF2/bootloader mode too — require a
                # MicroPython raw-REPL handshake before we report connected.
                try:
                    rtc = self._probe_micropython(self.transport)
                except Exception as e:
                    try:
                        self.transport.close()
                    except Exception:
                        pass
                    self.transport = None
                    last_probe_err = e
                    if not last:
                        time.sleep(retry_delay)
                        continue
                    self.device = None
                    raise RuntimeError(
                        self._friendly_probe_error(device, e)
                    ) from e
                self.device = device
                self.last_device = device
                return self._connect_result(device, baud, rtc, retries=attempt - 1)
            # Unreachable: the loop always returns or raises on the last attempt.
            raise RuntimeError(
                f"{device} could not be connected: {last_probe_err}"
            )

    def _connect_result(
        self,
        device: str,
        baud: int,
        rtc: Optional[list[int]],
        *,
        retries: int = 0,
        resumed: bool = False,
    ) -> dict[str, Any]:
        runtime = self.runtime or "micropython"
        result: dict[str, Any] = {
            "device": device,
            "baud": baud,
            "runtime": runtime,
            # Legacy bool kept for older UI/agents.
            "micropython": runtime == "micropython",
        }
        if rtc is not None:
            result["rtc"] = rtc
        if retries > 0:
            result["retries"] = retries
        if resumed:
            result["resumed"] = True
        if self.filesystem_warning:
            result["filesystem_warning"] = self.filesystem_warning
        return result

    def resume(self, baud: Optional[int] = None) -> dict[str, Any]:
        """Reconnect to the last device without requiring the caller to re-pick a port."""
        device = self.device or self.last_device
        if not device:
            raise RuntimeError("no previous device to resume")
        if self.transport and self.device == device:
            # Already connected — re-probe / refresh RTC like a fresh connect would.
            with self._lock:
                try:
                    rtc = self._probe_micropython(self.transport)
                except Exception as e:
                    raise RuntimeError(
                        f"resume failed (still connected but board not responding): {e}"
                    ) from e
                return self._connect_result(
                    device, self.baud, rtc, resumed=True
                )
        return self.connect(device, baud if baud is not None else self.baud)

    def _host_rtc_tuple(self) -> tuple[int, ...]:
        import time

        tnow = time.localtime()
        # MicroPython RTC: (year, month, day, weekday, hour, minute, second, subsecond)
        # weekday: Monday=0 (matches time.struct_time.tm_wday)
        return (
            tnow.tm_year,
            tnow.tm_mon,
            tnow.tm_mday,
            tnow.tm_wday,
            tnow.tm_hour,
            tnow.tm_min,
            tnow.tm_sec,
            0,
        )

    def _apply_rtc(self, t: Any) -> list[int]:
        tup = self._host_rtc_tuple()
        # MicroPython: machine.RTC; CircuitPython often exposes rtc.RTC as well.
        t.exec(
            "try:\n"
            " import machine\n"
            f" machine.RTC().datetime({tup})\n"
            "except Exception:\n"
            " import rtc\n"
            f" rtc.RTC().datetime({tup})\n"
        )
        return list(tup)

    @staticmethod
    def _serial_flush(serial: Any) -> None:
        try:
            n = serial.inWaiting()
            while n > 0:
                serial.read(n)
                n = serial.inWaiting()
        except Exception:
            pass

    def _reset_esp_to_app(self, serial: Any) -> None:
        """Pulse EN while IO0 is released so the chip boots firmware (not ROM download).

        esptool Detect/flash leave many ESP boards in ``waiting for download``;
        Connect must recover without requiring a physical RESET button.
        """
        import time

        try:
            serial.dtr = False  # IO0 released (not held for download)
            serial.rts = True  # EN low
            time.sleep(0.1)
            serial.rts = False  # EN high → boot app
            time.sleep(0.05)
        except Exception:
            try:
                serial.setDTR(False)
                serial.setRTS(True)
                time.sleep(0.1)
                serial.setRTS(False)
            except Exception:
                pass
        time.sleep(1.6)  # USB-UART boards need time for ROM + MP boot banner
        self._serial_flush(serial)

    def _serial_peek(self, serial: Any, wait: float = 0.15) -> bytes:
        import time

        time.sleep(wait)
        try:
            n = serial.inWaiting()
            return serial.read(n) if n else b""
        except Exception:
            return b""

    @staticmethod
    def _serial_shows_fs_corrupt(data: bytes) -> bool:
        return any(m in data for m in _FS_CORRUPT_MARKERS)

    def _take_control(
        self, t: Any, *, clean: bool = True, timeout_overall: float = 20.0
    ) -> None:
        """Interrupt any running program and enter raw REPL.

        Always sends Ctrl-C (Thonny-style interrupt-on-connect). When ``clean``
        is True:

        - MicroPython: soft-reset *while in raw REPL* so ``main.py`` does not run.
        - CircuitPython: friendly↔raw toggle (no Ctrl-D). CP runs ``code.py`` after
          a raw soft-reboot; switching REPL modes refreshes the VM instead.

        Exception: a corrupt on-board filesystem makes soft-reset re-run
        ``_boot``/``inisetup``, which loops printing the corruption banner and
        never returns to raw REPL. In that case we interrupt + enter raw
        without soft-reset and set ``filesystem_warning`` (MicroPython only).
        """
        import time

        serial = getattr(t, "serial", None)
        if serial is None:
            raise RuntimeError("transport has no serial port")

        saw_fs_corrupt = False

        def note_bytes(data: bytes) -> None:
            nonlocal saw_fs_corrupt
            if data and self._serial_shows_fs_corrupt(data):
                saw_fs_corrupt = True

        def ctrl_c() -> None:
            try:
                serial.write(b"\r\x03")
            except Exception:
                pass

        def poke_enter_repl_key(data: bytes) -> None:
            # CircuitPython waits for any key before showing the friendly REPL.
            if data_has_enter_repl_prompt(data):
                try:
                    serial.write(b"\r")
                except Exception:
                    pass
                time.sleep(0.15)
                try:
                    n = serial.inWaiting()
                    if n:
                        note_bytes(serial.read(n))
                except Exception:
                    pass

        def interrupt_storm() -> None:
            # Thorough interrupt (see micropython#7867 / Thonny pipkin).
            # Busy timer/display loops (common on P4 boards) need a longer storm.
            for delay in (0.0, 0.05, 0.05, 0.1, 0.1, 0.15, 0.2):
                if delay:
                    time.sleep(delay)
                ctrl_c()
            time.sleep(0.05)
            try:
                n = serial.inWaiting()
                if n:
                    data = serial.read(n)
                    note_bytes(data)
                    poke_enter_repl_key(data)
            except Exception:
                pass

        # If a prior esptool session left the chip in ROM download mode, reboot
        # into firmware before trying the raw-REPL handshake.
        peek = self._serial_peek(serial, 0.2)
        note_bytes(peek)
        poke_enter_repl_key(peek)
        if b"waiting for download" in peek or b"DOWNLOAD(" in peek:
            self._reset_esp_to_app(serial)
            after = self._serial_peek(serial, 0.3)
            note_bytes(after)
            poke_enter_repl_key(after)

        interrupt_storm()

        def try_raw(soft_reset: bool, per: float) -> bool:
            try:
                t.enter_raw_repl(soft_reset=soft_reset, timeout_overall=per)
                return True
            except Exception:
                try:
                    n = serial.inWaiting()
                    if n:
                        data = serial.read(n)
                        note_bytes(data)
                        poke_enter_repl_key(data)
                except Exception:
                    pass
                raise

        last_err: Optional[Exception] = None
        # Always enter raw *without* soft-reset first so CircuitPython does not
        # auto-run code.py. Clean behavior is applied after runtime detect.
        if clean and saw_fs_corrupt:
            try:
                if try_raw(False, 5.0):
                    self.filesystem_warning = (
                        "On-board filesystem is corrupted. Connect succeeded without "
                        "soft-reset; erase flash and reflash MicroPython (Firmware > "
                        "Erase) to restore a usable filesystem."
                    )
                    self._finish_clean_after_raw(t, saw_fs_corrupt=True)
                    return
            except Exception as e:
                last_err = e

        # Two short tries, then one app-mode reset (covers silent download mode
        # where the banner was already drained), then two more tries.
        schedule = [
            ("try", 3.5),
            ("try", 3.5),
            ("reset", 0.0),
            ("try", 5.0),
            ("try", 5.0),
        ]
        for action, per in schedule:
            if action == "reset":
                self._reset_esp_to_app(serial)
                after = self._serial_peek(serial, 0.3)
                note_bytes(after)
                poke_enter_repl_key(after)
                interrupt_storm()
                # After reset the board may start the corrupt-FS boot loop.
                if clean and saw_fs_corrupt:
                    try:
                        if try_raw(False, 5.0):
                            self.filesystem_warning = (
                                "On-board filesystem is corrupted. Connect succeeded "
                                "without soft-reset; erase flash and reflash MicroPython "
                                "(Firmware > Erase) to restore a usable filesystem."
                            )
                            self._finish_clean_after_raw(t, saw_fs_corrupt=True)
                            return
                    except Exception as e:
                        last_err = e
                    continue
            try:
                if try_raw(False, per):
                    self._finish_clean_after_raw(
                        t, saw_fs_corrupt=saw_fs_corrupt, want_clean=clean
                    )
                    return
            except Exception as e:
                last_err = e
                try:
                    t.in_raw_repl = False
                except Exception:
                    pass
                for _ in range(4):
                    ctrl_c()
                    time.sleep(0.08)
                try:
                    n = serial.inWaiting()
                    if n:
                        data = serial.read(n)
                        note_bytes(data)
                        poke_enter_repl_key(data)
                except Exception:
                    pass
                try:
                    serial.write(b"\r\x01")  # poke raw REPL (Thonny)
                except Exception:
                    pass
                time.sleep(0.1)

        # Last resort: interrupt-only enter raw.
        if clean:
            interrupt_storm()
            try:
                if try_raw(False, 5.0):
                    if saw_fs_corrupt:
                        self.filesystem_warning = (
                            "On-board filesystem is corrupted. Connect succeeded without "
                            "soft-reset; erase flash and reflash MicroPython (Firmware > "
                            "Erase) to restore a usable filesystem."
                        )
                    self._finish_clean_after_raw(
                        t, saw_fs_corrupt=saw_fs_corrupt, want_clean=clean
                    )
                    return
            except Exception as e:
                last_err = e

        detail = str(last_err.args[0] if last_err and getattr(last_err, "args", None) else last_err)
        raise RuntimeError(
            self._friendly_take_control_error(detail, fs_corrupt=saw_fs_corrupt)
        ) from last_err

    def _detect_runtime(self, t: Any) -> str:
        """Read ``sys.implementation.name`` while already in raw REPL."""
        try:
            t.exec("import sys")
            name = t.eval("sys.implementation.name")
            runtime = normalize_runtime_name(name)
        except Exception:
            runtime = self.runtime or "micropython"
        self.runtime = runtime
        return runtime

    def _finish_clean_after_raw(
        self,
        t: Any,
        *,
        saw_fs_corrupt: bool = False,
        want_clean: bool = True,
    ) -> None:
        """After raw REPL is open: detect runtime and apply clean semantics."""
        runtime = self._detect_runtime(t)
        if not want_clean:
            return
        if runtime == "circuitpython":
            # Thonny CP clear_repl: exit raw → enter raw (no Ctrl-D soft-reboot).
            try:
                if t.in_raw_repl:
                    t.exit_raw_repl()
            except Exception:
                pass
            try:
                t.in_raw_repl = False
            except Exception:
                pass
            t.enter_raw_repl(soft_reset=False, timeout_overall=10.0)
            try:
                t.exec(
                    "try:\n"
                    " import supervisor\n"
                    " if hasattr(supervisor, 'disable_autoreload'):\n"
                    "  supervisor.disable_autoreload()\n"
                    " elif hasattr(supervisor, 'runtime'):\n"
                    "  supervisor.runtime.autoreload = False\n"
                    "except Exception:\n"
                    " pass\n"
                )
            except Exception:
                pass
            return
        # MicroPython: raw soft-reset skips main.py (unless corrupt FS).
        if saw_fs_corrupt:
            return
        try:
            if t.in_raw_repl:
                t.exit_raw_repl()
        except Exception:
            pass
        try:
            t.in_raw_repl = False
        except Exception:
            pass
        t.enter_raw_repl(soft_reset=True, timeout_overall=10.0)
    @staticmethod
    def _friendly_port_open_error(device: str, exc: BaseException) -> str:
        raw = str(exc.args[0] if getattr(exc, "args", None) else exc)
        low = raw.lower()
        locked = (
            "exclusively lock" in low
            or "exclusive" in low and "lock" in low
            or "permissionerror" in low
            or "access is denied" in low
            or "permission denied" in low
            or "failed to access" in low
            or "busy" in low
        )
        if locked:
            return (
                f"failed to open {device}: port is busy or locked. "
                f"Close Thonny, a serial monitor, another mpftp session, or any tool "
                f"holding the port, then try again. ({raw})"
            )
        return f"failed to access {device}: {raw}"

    @staticmethod
    def _friendly_take_control_error(detail: str, *, fs_corrupt: bool = False) -> str:
        if fs_corrupt or any(
            s in detail.lower()
            for s in ("filesystem appears to be corrupted", "fs_corrupted")
        ):
            return (
                f"could not take control of the board: on-board filesystem is corrupted "
                f"({detail}). Erase flash and reflash firmware "
                f"(Firmware panel > Erase for MicroPython), then Connect again."
            )
        return (
            f"could not take control of the board (interrupt/raw REPL failed): {detail}\n"
            f"Device is busy or does not respond. Your options:\n"
            f"  - wait until it completes current work;\n"
            f"  - hard-reset the board and try again;\n"
            f"  - check for a runaway main.py / code.py;\n"
            f"  - confirm firmware is MicroPython or CircuitPython (not bootloader/UF2);\n"
            f"  - close other serial tools holding the port."
        )

    @staticmethod
    def _friendly_probe_error(device: str, exc: BaseException) -> str:
        detail = str(exc.args[0] if getattr(exc, "args", None) else exc)
        if "filesystem is corrupted" in detail.lower():
            return f"{device}: {detail}"
        base = (
            f"{device} is not responding as MicroPython/CircuitPython "
            f"(likely bootloader/UF2 mode, wrong port, or busy REPL): {detail}"
        )
        # Take-control failures already include the checklist.
        if "could not take control" in detail or "Your options:" in detail:
            return base
        return (
            f"{base}\n"
            f"  - wait / hard-reset / check main.py or code.py / confirm firmware / close other tools."
        )

    def _sync_remote_fs(self, t: Any) -> None:
        """Flush on-board filesystem after writes (mpremote fs_writefile does not sync)."""
        try:
            t.exec("import os\nif hasattr(os, 'sync'):\n os.sync()")
        except Exception:
            pass

    def _probe_micropython(self, t: Any) -> Optional[list[int]]:
        """Take control, detect runtime, set RTC, leave raw REPL.

        Connect never leaves user code running: interrupt is unconditional.
        MicroPython gets a raw soft-reset (skip main.py); CircuitPython gets a
        friendly↔raw toggle (Ctrl-D would run code.py).
        """
        self._take_control(t, clean=True, timeout_overall=20.0)
        rtc: Optional[list[int]] = None
        try:
            rtc = self._apply_rtc(t)
        except Exception:
            # Some ports lack machine.RTC / rtc.RTC; don't fail the connection.
            pass
        try:
            if t.in_raw_repl:
                t.exit_raw_repl()
        except Exception:
            pass
        return rtc

    def disconnect(self) -> None:
        with self._lock:
            self._stop_repl_reader()
            if not self.transport:
                self.runtime = None
                return
            try:
                if getattr(self.transport, "mounted", False):
                    if not self.transport.in_raw_repl:
                        self.transport.enter_raw_repl(soft_reset=False)
                    self.transport.umount_local()
                if self.transport.in_raw_repl:
                    self.transport.exit_raw_repl()
            except Exception:
                pass
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None
            self.device = None
            self.runtime = None
            self._repl_mode = False
    def _require(self):
        if not self.transport:
            raise RuntimeError("not connected")
        return self.transport

    def _enter_raw(self, soft_reset: bool = False) -> None:
        t = self._require()
        was_repl = self._repl_mode
        if was_repl:
            self._stop_repl_reader()
        if not t.in_raw_repl:
            t.enter_raw_repl(soft_reset=soft_reset)
        return  # type: ignore[return-value]

    def _leave_raw_to_repl(self) -> None:
        t = self._require()
        if t.in_raw_repl:
            t.exit_raw_repl()
        if self._repl_mode:
            self._start_repl_reader()

    def with_raw(self, fn, soft_reset: bool = False):
        with self._lock:
            self._enter_raw(soft_reset=soft_reset)
            try:
                return fn(self._require())
            finally:
                # Prefer staying ready for more fs ops; exit raw only if REPL wanted
                if self._repl_mode:
                    try:
                        if self.transport and self.transport.in_raw_repl:
                            self.transport.exit_raw_repl()
                    except Exception:
                        pass
                    self._start_repl_reader()

    # --- filesystem ---

    def _require_micropython(self, feature: str) -> None:
        runtime = self.runtime or "micropython"
        if runtime != "micropython":
            raise RuntimeError(
                f"{feature} is MicroPython-only (connected runtime is {runtime})"
            )

    def _board_listdir(self, t: Any, path: str) -> list[Any]:
        """List directory entries on the board (listdir, then ilistdir).

        Returns mpremote-like objects with ``.name``, ``.st_mode``, ``.st_size``.
        Prefers ``os.listdir`` + ``os.stat`` (universal), falls back to
        ``os.ilistdir`` when listdir is missing — Thonny's order.
        """
        list_path = "" if path in ("", "/", None) else path
        # Prefer transport helper when present and working (MicroPython).
        try:
            return list(t.fs_listdir(list_path))
        except Exception:
            pass

        path_lit = repr(list_path if list_path else "/")
        code = f"""
import os
_p = {path_lit}
_out = []
_ok = False
if hasattr(os, 'listdir'):
 try:
  for _n in os.listdir(_p):
   if _n in ('.', '..'):
    continue
   _fp = (_p.rstrip('/') + '/' + _n) if _p not in ('', '/') else ('/' + _n)
   try:
    _st = os.stat(_fp)
    _out.append((_n, _st[0], 0, _st[6] if len(_st) > 6 else 0))
   except Exception:
    _out.append((_n, 0x8000, 0, 0))
  _ok = True
 except Exception:
  _out = []
if not _ok:
 for _e in os.ilistdir(_p):
  _n = _e[0]
  if _n in ('.', '..'):
   continue
  _mode = _e[1] if len(_e) > 1 else 0x8000
  _size = _e[3] if len(_e) > 3 else 0
  _out.append((_n, _mode, 0, _size))
print(repr(_out))
"""
        out = t.exec(code)
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        line = text.strip().splitlines()[-1] if text.strip() else "[]"
        import ast

        rows = ast.literal_eval(line)

        class _Ent:
            __slots__ = ("name", "st_mode", "st_ino", "st_size")

            def __init__(self, name: str, mode: int, ino: int, size: int) -> None:
                self.name = name
                self.st_mode = mode
                self.st_ino = ino
                self.st_size = size

        return [_Ent(*r) if len(r) >= 4 else _Ent(r[0], r[1] if len(r) > 1 else 0x8000, 0, 0) for r in rows]

    def fs_listdir(self, path: str = "/") -> list[dict[str, Any]]:
        # mpremote uses "" for the device root; "/" also works on most ports.
        list_path = "" if path in ("", "/", None) else path

        def op(t):
            entries = []
            for e in self._board_listdir(t, list_path):
                is_dir = bool(e.st_mode & 0x4000)
                entries.append(
                    {
                        "name": e.name,
                        "isDir": is_dir,
                        "size": e.st_size,
                        "mode": e.st_mode,
                    }
                )
            entries.sort(key=lambda x: (not x["isDir"], x["name"].lower()))
            return entries

        return self.with_raw(op)

    def fs_stat(self, path: str) -> dict[str, Any]:
        def op(t):
            st = t.fs_stat(path)
            return {
                "mode": st.st_mode,
                "size": st.st_size,
                "isDir": bool(st.st_mode & 0x4000),
            }

        return self.with_raw(op)

    def fs_read(self, path: str) -> dict[str, Any]:
        def op(t):
            data = t.fs_readfile(path)
            return {
                "path": path,
                "size": len(data),
                "data_b64": base64.b64encode(bytes(data)).decode("ascii"),
            }

        return self.with_raw(op)

    def fs_write(self, path: str, data_b64: str) -> dict[str, Any]:
        data = base64.b64decode(data_b64)

        def op(t):
            self._ensure_cp_writable(t)
            t.fs_writefile(path, data)
            self._sync_remote_fs(t)
            return {"path": path, "size": len(data)}

        return self.with_raw(op)

    def fs_mkdir(self, path: str) -> dict[str, Any]:
        def op(t):
            t.fs_mkdir(path)
            return {"path": path}

        return self.with_raw(op)

    def fs_rm(self, path: str) -> dict[str, Any]:
        def op(t):
            if t.fs_isdir(path):
                raise RuntimeError(f"is a directory: {path} (use fs_rmdir or fs_rm_rf)")
            t.fs_rmfile(path)
            return {"path": path}

        return self.with_raw(op)

    def fs_rmdir(self, path: str) -> dict[str, Any]:
        def op(t):
            t.fs_rmdir(path)
            return {"path": path}

        return self.with_raw(op)

    def fs_rm_rf(self, path: str) -> dict[str, Any]:
        def _rm(t, p: str) -> None:
            if t.fs_isdir(p):
                for e in self._board_listdir(t, p):
                    child = p.rstrip("/") + "/" + e.name if p not in ("", ".") else e.name
                    if p == "/":
                        child = "/" + e.name
                    _rm(t, child)
                if p not in ("", "/", "."):
                    t.fs_rmdir(p)
            else:
                t.fs_rmfile(p)

        def op(t):
            _rm(t, path)
            return {"path": path}

        return self.with_raw(op)

    def fs_touch(self, path: str) -> dict[str, Any]:
        def op(t):
            t.fs_touchfile(path)
            return {"path": path}

        return self.with_raw(op)

    def fs_rename(self, src: str, dest: str) -> dict[str, Any]:
        def op(t):
            # mpremote Transport has no fs_rename; use device os.rename.
            t.exec(f"import os\nos.rename({src!r}, {dest!r})")
            return {"src": src, "dest": dest}

        return self.with_raw(op)

    def fs_hash(self, path: str, algo: str = "sha256") -> dict[str, Any]:
        def op(t):
            digest = t.fs_hashfile(path, algo)
            if isinstance(digest, bytes):
                digest_hex = digest.hex()
            else:
                digest_hex = str(digest)
            return {"path": path, "algo": algo, "hash": digest_hex}

        return self.with_raw(op)

    def fs_tree(self, path: str = "/") -> dict[str, Any]:
        def walk(t, p: str, depth: int = 0) -> list[dict[str, Any]]:
            nodes = []
            try:
                entries = self._board_listdir(t, p if p != "/" else "")
            except Exception:
                return nodes
            for e in sorted(entries, key=lambda x: (not bool(x.st_mode & 0x4000), x.name.lower())):
                is_dir = bool(e.st_mode & 0x4000)
                child_path = (p.rstrip("/") + "/" + e.name) if p != "/" else "/" + e.name
                node = {
                    "name": e.name,
                    "path": child_path,
                    "isDir": is_dir,
                    "size": e.st_size,
                }
                if is_dir and depth < 8:
                    node["children"] = walk(t, child_path, depth + 1)
                nodes.append(node)
            return nodes

        def op(t):
            return {"path": path, "children": walk(t, path)}

        return self.with_raw(op)

    # --- exec / control ---

    def exec(self, code: str, follow: bool = True) -> dict[str, Any]:
        def op(t):
            out = t.exec(code) if follow else t.exec_raw_no_follow(code.encode())
            if out is None:
                text = ""
            elif isinstance(out, bytes):
                text = out.decode("utf-8", "replace")
            else:
                text = str(out)
            return {"output": text}

        return self.with_raw(op, soft_reset=False)

    def eval(self, expr: str) -> dict[str, Any]:
        def op(t):
            val = t.eval(expr)
            return {"value": repr(val)}

        return self.with_raw(op)

    def run_script(self, source: str, follow: bool = True) -> dict[str, Any]:
        """Run source on the board after interrupt + raw soft-reset (skip main.py)."""
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            self._take_control(t, clean=True, timeout_overall=20.0)
            try:
                if follow:
                    out = t.exec(source)
                    if out is None:
                        text = ""
                    elif isinstance(out, bytes):
                        text = out.decode("utf-8", "replace")
                    else:
                        text = str(out)
                    return {"output": text}
                t.exec_raw_no_follow(source.encode())
                try:
                    if t.in_raw_repl:
                        t.exit_raw_repl()
                except Exception:
                    pass
                if self._repl_mode:
                    self._start_repl_reader()
                return {"output": ""}
            except Exception:
                if self._repl_mode:
                    try:
                        if self.transport and self.transport.in_raw_repl:
                            self.transport.exit_raw_repl()
                    except Exception:
                        pass
                    self._start_repl_reader()
                raise

    def run_path(self, path: str, follow: bool = False) -> dict[str, Any]:
        """
        Run a .py file already on the board.

        Takes control (interrupt + raw soft-reset, skipping main.py), then execs
        the file with __name__/__file__ set like a normal script launch. This is
        how user code is started — Soft Reset alone never runs main.py.
        Default follow=False so UI apps / loops do not block the sidecar; output
        is left on the UART (open REPL to watch).
        """
        path_lit = json.dumps(path)
        code = (
            f"_p = {path_lit}\n"
            "exec(compile(open(_p).read(), _p, \"exec\"), "
            "{\"__name__\": \"__main__\", \"__file__\": _p})\n"
        )
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            self._take_control(t, clean=True, timeout_overall=20.0)
            try:
                if follow:
                    out = t.exec(code)
                    if out is None:
                        text = ""
                    elif isinstance(out, bytes):
                        text = out.decode("utf-8", "replace")
                    else:
                        text = str(out)
                    return {"output": text, "path": path, "followed": True}
                t.exec_raw_no_follow(code.encode())
                # Leave raw REPL so the script keeps running and prints are visible.
                try:
                    if t.in_raw_repl:
                        t.exit_raw_repl()
                except Exception:
                    pass
                if self._repl_mode:
                    self._start_repl_reader()
                return {"output": "", "path": path, "followed": False}
            except Exception:
                if self._repl_mode:
                    try:
                        if self.transport and self.transport.in_raw_repl:
                            self.transport.exit_raw_repl()
                    except Exception:
                        pass
                    self._start_repl_reader()
                raise

    def interrupt(self) -> dict[str, Any]:
        """Send Ctrl-C without resetting or entering raw REPL."""
        with self._lock:
            t = self._require()
            serial = getattr(t, "serial", None)
            if serial is None:
                raise RuntimeError("transport has no serial port")
            try:
                serial.write(b"\r\x03")
            except Exception as e:
                raise RuntimeError(f"interrupt failed: {e}") from e
            return {"ok": True}

    def soft_reset(self) -> dict[str, Any]:
        """Fresh session without running user startup scripts the MP way.

        MicroPython: raw soft-reset (does not run main.py).
        CircuitPython: friendly↔raw toggle (Ctrl-D would run code.py).
        """
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            self._take_control(t, clean=True, timeout_overall=20.0)
            runtime = self.runtime or "micropython"
            if self._repl_mode:
                try:
                    if t.in_raw_repl:
                        t.exit_raw_repl()
                except Exception:
                    pass
                self._start_repl_reader()
            return {
                "ok": True,
                "runtime": runtime,
                "main_skipped": runtime == "micropython",
            }

    def hard_reset(self) -> dict[str, Any]:
        code = "import time, machine; time.sleep_ms(100); machine.reset()"
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            try:
                self._take_control(t, clean=False, timeout_overall=15.0)
            except Exception:
                pass
            try:
                if not t.in_raw_repl:
                    t.enter_raw_repl(soft_reset=False, timeout_overall=5)
                t.exec_raw_no_follow(code.encode())
            except Exception:
                pass
            # Board will reboot; close transport
            try:
                t.close()
            except Exception:
                pass
            self.transport = None
            self._repl_mode = False
            return {"ok": True, "note": "device resetting; reconnect required"}

    def bootloader(self) -> dict[str, Any]:
        code = "import time, machine; time.sleep_ms(100); machine.bootloader()"
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            try:
                self._take_control(t, clean=False, timeout_overall=15.0)
            except Exception:
                pass
            try:
                if not t.in_raw_repl:
                    t.enter_raw_repl(soft_reset=False, timeout_overall=5)
                t.exec_raw_no_follow(code.encode())
            except Exception:
                pass
            try:
                t.close()
            except Exception:
                pass
            self.transport = None
            self._repl_mode = False
            return {"ok": True}

    def rtc_get(self) -> dict[str, Any]:
        def op(t):
            t.exec(
                "def _mpftp_rtc():\n"
                " try:\n"
                "  import machine\n"
                "  return machine.RTC().datetime()\n"
                " except Exception:\n"
                "  import rtc\n"
                "  return rtc.RTC().datetime()\n"
            )
            out = t.eval("_mpftp_rtc()")
            return {"datetime": list(out) if isinstance(out, (tuple, list)) else repr(out)}

        return self.with_raw(op)

    def rtc_set(self) -> dict[str, Any]:
        def op(t):
            return {"datetime": self._apply_rtc(t)}

        return self.with_raw(op)

    def mip_install(
        self,
        packages: list[str],
        target: Optional[str] = None,
        mpy: bool = True,
        index: Optional[str] = None,
    ) -> dict[str, Any]:
        """Host-side mip install via mpremote (downloads on host, writes to board)."""
        if (self.runtime or "micropython") == "circuitpython":
            raise RuntimeError(
                "mip is MicroPython-only; use circup_install / mpftp circup on CircuitPython"
            )
        from mpremote import mip as mp_mip

        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            try:
                pkg_index = (index or mp_mip._PACKAGE_INDEX).rstrip("/")
                resolved_target = target
                if resolved_target is None:
                    t.exec("import sys")
                    lib_paths = [
                        p
                        for p in t.eval("sys.path")
                        if isinstance(p, str) and not p.startswith("/rom") and p.endswith("/lib")
                    ]
                    if not lib_paths or not lib_paths[0]:
                        raise RuntimeError(
                            "Unable to find lib dir in sys.path; pass target= (e.g. /lib)"
                        )
                    resolved_target = lib_paths[0]

                logs: list[str] = []
                installed: list[str] = []
                for package in packages:
                    version = None
                    pkg = package
                    if "@" in pkg:
                        pkg, version = pkg.split("@", 1)
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        print(f"Install {package}")
                        mp_mip._install_package(
                            t, pkg, pkg_index, resolved_target, version, mpy
                        )
                        print("Done")
                    logs.append(buf.getvalue().strip())
                    installed.append(package)
                return {
                    "output": "\n".join(logs),
                    "packages": installed,
                    "target": resolved_target,
                    "index": pkg_index,
                    "mpy": mpy,
                }
            finally:
                if self._repl_mode:
                    try:
                        if self.transport and self.transport.in_raw_repl:
                            self.transport.exit_raw_repl()
                    except Exception:
                        pass
                    self._start_repl_reader()

    def _find_circuitpy_host_roots(self) -> list[str]:
        """Host paths for a mounted CIRCUITPY drive (Windows letters or /media)."""
        roots: list[str] = []
        # Windows: query volume label via PowerShell (works from WSL too).
        try:
            ps = (
                "Get-CimInstance Win32_LogicalDisk | "
                "Where-Object { $_.VolumeName -eq 'CIRCUITPY' } | "
                "ForEach-Object { $_.DeviceID }"
            )
            out = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                text=True,
                timeout=15,
                stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                letter = line.strip().rstrip("\\/")
                if len(letter) >= 2 and letter[1] == ":":
                    # Prefer WSL mount if present, else Windows path for Windows python.
                    wsl = f"/mnt/{letter[0].lower()}"
                    if Path(wsl).is_dir():
                        roots.append(wsl)
                    else:
                        roots.append(letter + "\\")
        except Exception:
            pass
        # Linux / macOS common mounts
        for base in ("/media", "/Volumes", str(Path.home() / "media")):
            try:
                p = Path(base)
                if not p.is_dir():
                    continue
                for child in p.iterdir():
                    if child.name.upper() == "CIRCUITPY" and child.is_dir():
                        roots.append(str(child))
            except Exception:
                pass
        # Dedupe preserve order
        seen: set[str] = set()
        uniq: list[str] = []
        for r in roots:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return uniq

    def _push_lib_tree_host(self, lib_src: Path, dest_lib: Path) -> list[str]:
        """Copy staged lib/ contents onto a host CIRCUITPY/lib tree."""
        dest_lib.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for child in sorted(lib_src.iterdir(), key=lambda p: p.name.lower()):
            target = dest_lib / child.name
            if child.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
            copied.append(str(target))
        return copied

    def _ensure_cp_writable(self, t: Any) -> None:
        """Remount CIRCUITPY root read-write when USB MSC left it read-only."""
        if (self.runtime or "") != "circuitpython":
            return
        try:
            t.exec(
                "try:\n"
                " import storage\n"
                " storage.remount('/', False)\n"
                "except Exception:\n"
                " pass\n"
            )
        except Exception:
            pass

    def _resolve_circup_exe(self) -> tuple[str, bool]:
        """Return (circup_executable_or_python, use_module_invocation)."""
        circup_exe = shutil.which("circup") or shutil.which("circup.exe")
        if circup_exe:
            return circup_exe, False
        try:
            import circup as _circup  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "circup is not installed for the sidecar Python. "
                "Install with: python -m pip install circup "
                "(on WSL use the Windows python.exe that runs the sidecar)"
            ) from e
        return sys.executable, True

    def _run_circup(self, argv: list[str], *, use_module: bool = False) -> str:
        del use_module  # argv is fully formed by callers
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        out_text = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            raise RuntimeError(
                f"circup failed (exit {proc.returncode}): {out_text.strip() or 'no output'}"
            )
        return out_text.strip()

    def _cp_web_workflow_info(self, t: Any) -> dict[str, Any]:
        """Read Wi-Fi IP + web API password from the board (no secrets logged)."""
        code = r"""
_host = None
_pwd = None
_ssid = None
try:
 import os as _os
 _pwd = _os.getenv('CIRCUITPY_WEB_API_PASSWORD')
 _ssid = _os.getenv('CIRCUITPY_WIFI_SSID')
except Exception:
 pass
try:
 import wifi
 _ip = wifi.radio.ipv4_address
 if _ip is not None:
  _host = str(_ip)
except Exception:
 pass
print(repr((_host, bool(_pwd), _ssid)))
"""
        out = t.exec(code)
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        line = text.strip().splitlines()[-1] if text.strip() else "(None, False, None)"
        import ast

        host, has_pwd, ssid = ast.literal_eval(line)
        password = None
        if has_pwd:
            # Fetch password in a second eval so we can avoid printing it in exec stdout
            # that might land in logs; still needed for circup argv.
            try:
                t.exec("import os")
                password = t.eval("os.getenv('CIRCUITPY_WEB_API_PASSWORD')")
                if password is not None:
                    password = str(password)
            except Exception:
                password = None
        env_pwd = os.environ.get("CIRCUP_WEBWORKFLOW_PASSWORD") or os.environ.get(
            "MPFTP_CIRCUITPY_WEB_PASSWORD"
        )
        if env_pwd:
            password = env_pwd
        return {
            "host": host,
            "password": password,
            "ssid": ssid,
            "ready": bool(host and password),
        }

    def _cp_web_remount_writable(self, host: str, password: str) -> bool:
        """Ask Web Workflow to remount the FS writable to the device."""
        import urllib.error
        import urllib.request

        url = f"http://{host}/fs/"
        req = urllib.request.Request(
            url,
            data=b'{"writable":true}',
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        # Empty username, password as web API password (Basic auth).
        import base64 as _b64

        token = _b64.b64encode(f":{password}".encode()).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
            return "USB storage active" not in body
        except Exception:
            return False

    def _eject_circuitpy_host(self) -> bool:
        """Best-effort eject of CIRCUITPY so web workflow can write."""
        try:
            ps = r"""
$shell = New-Object -ComObject Shell.Application
$usb = $shell.NameSpace(17)
$n = 0
foreach ($item in $usb.Items()) {
  if ($item.Name -match 'CIRCUITPY') {
    $item.InvokeVerb('Eject')
    $n++
  }
}
Write-Output $n
"""
            out = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                text=True,
                timeout=20,
                stderr=subprocess.DEVNULL,
            )
            return int((out or "0").strip().splitlines()[-1] or "0") > 0
        except Exception:
            return False

    def _cp_prepare_web_writable(self, t: Any, host: str, password: str) -> bool:
        """Eject CIRCUITPY on the host and remount writable for Web Workflow.

        Returns True if ``/fs/`` reports writable. Host eject alone is often not
        enough while the USB MSC *interface* is still enabled in firmware
        (CircuitPython keeps the FS read-only until ``storage.disable_usb_drive()``
        in boot.py, or USB is unplugged). We still try eject + remount for the
        cases where the host held the volume open.
        """
        import time as _time

        self._eject_circuitpy_host()
        _time.sleep(0.8)
        self._ensure_cp_writable(t)
        self._cp_web_remount_writable(host, password)
        try:
            import base64 as _b64
            import json as _json
            import urllib.request

            req = urllib.request.Request(
                f"http://{host}/fs/",
                headers={
                    "Accept": "application/json",
                    "Authorization": "Basic "
                    + _b64.b64encode(f":{password}".encode()).decode("ascii"),
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
            return bool(data.get("writable"))
        except Exception:
            return False

    def circup_install(
        self,
        packages: list[str],
        *,
        py: bool = False,
        target: str = "/lib",
        host: Optional[str] = None,
        password: Optional[str] = None,
        prefer_web: bool = True,
    ) -> dict[str, Any]:
        """Install packages via circup.

        Preference order (no board ``boot.py`` required):

        1. **CIRCUITPY mount** on the host — ``circup --path`` (fast USB disk).
        2. **Web Workflow** — ``circup --host`` when Wi‑Fi is up *and* the FS is
           device-writable (USB MSC not locking it).
        3. Host stage + serial put, then MSC copy fallback.
        """
        if (self.runtime or "micropython") != "circuitpython":
            raise RuntimeError(
                "circup is CircuitPython-only; use mip_install / mpftp mip on MicroPython"
            )
        if not packages:
            raise RuntimeError("circup_install: packages required")

        circup_exe, use_module = self._resolve_circup_exe()

        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            try:
                cpy_version, board_id = self._cp_version_and_board(t)
                web = self._cp_web_workflow_info(t)
                web_host = host or web.get("host")
                web_password = password or web.get("password")
                web_fallback_err: Optional[BaseException] = None

                # --- 1. Prefer mounted CIRCUITPY (no board edits needed) ---
                roots = self._find_circuitpy_host_roots()
                if roots:
                    root = roots[0]
                    # Prefer a Windows drive path for circup.exe under WSL.
                    path_for_circup = root
                    if root.startswith("/mnt/") and len(root) >= 6:
                        path_for_circup = f"{root[5].upper()}:\\"
                    try:
                        if use_module:
                            argv = [
                                circup_exe,
                                "-c",
                                (
                                    "import sys; from circup import main; "
                                    "sys.argv=['circup']+sys.argv[1:]; "
                                    "raise SystemExit(main())"
                                ),
                                "--path",
                                path_for_circup,
                                "install",
                            ]
                        else:
                            argv = [circup_exe, "--path", path_for_circup, "install"]
                        if py:
                            argv.append("--py")
                        argv.extend(packages)
                        out_text = self._run_circup(argv, use_module=use_module)
                        return {
                            "output": out_text,
                            "packages": list(packages),
                            "target": target if target.startswith("/") else "/" + target,
                            "cpy_version": cpy_version,
                            "board_id": board_id,
                            "py": py,
                            "transport": "circuitpy-path",
                            "path": path_for_circup,
                        }
                    except Exception as e:
                        web_fallback_err = e  # try web / serial next

                # --- 2. Web Workflow when FS is device-writable ---
                if prefer_web and web_host and web_password:
                    if self._cp_prepare_web_writable(t, str(web_host), str(web_password)):
                        if use_module:
                            argv = [
                                circup_exe,
                                "-c",
                                (
                                    "import sys; from circup import main; "
                                    "sys.argv=['circup']+sys.argv[1:]; "
                                    "raise SystemExit(main())"
                                ),
                                "--host",
                                str(web_host),
                                "--password",
                                str(web_password),
                                "install",
                            ]
                            if py:
                                argv.append("--py")
                            argv.extend(packages)
                        else:
                            argv = build_circup_web_argv(
                                circup_exe=circup_exe,
                                host=str(web_host),
                                password=str(web_password),
                                packages=packages,
                                py=py,
                            )
                        try:
                            out_text = self._run_circup(argv, use_module=use_module)
                            return {
                                "output": out_text,
                                "packages": list(packages),
                                "target": target if target.startswith("/") else "/" + target,
                                "cpy_version": cpy_version,
                                "board_id": board_id,
                                "py": py,
                                "transport": "web-workflow",
                                "host": str(web_host),
                            }
                        except Exception as web_err:
                            web_fallback_err = web_err
                    else:
                        web_fallback_err = RuntimeError(
                            "Web Workflow FS is not writable while USB mass storage "
                            "is active. Mount CIRCUITPY and mpftp will use circup "
                            "--path, or unplug USB / disable MSC in boot.py for "
                            "Wi-Fi installs."
                        )

                # --- 3. Fallback: stage on host, serial put / MSC ---
                self._ensure_cp_writable(t)
                stage = tempfile.mkdtemp(prefix="mpftp-circup-")
                try:
                    boot = Path(stage) / "boot_out.txt"
                    boot.write_text(
                        circup_boot_out_text(cpy_version=cpy_version, board_id=board_id),
                        encoding="utf-8",
                    )
                    (Path(stage) / "lib").mkdir(exist_ok=True)
                    stage_for_circup = stage
                    if sys.platform == "win32" or (
                        os.name == "nt"
                        or (
                            os.environ.get("WSL_DISTRO_NAME")
                            and str(circup_exe).lower().endswith(".exe")
                        )
                    ):
                        try:
                            win = subprocess.check_output(
                                ["wslpath", "-w", stage], text=True
                            ).strip()
                            if win:
                                stage_for_circup = win
                        except Exception:
                            pass

                    if use_module:
                        argv = [
                            circup_exe,
                            "-c",
                            (
                                "import sys; from circup import main; "
                                "sys.argv=['circup']+sys.argv[1:]; raise SystemExit(main())"
                            ),
                            "--path",
                            stage_for_circup,
                            "--board-id",
                            board_id,
                            "--cpy-version",
                            cpy_version,
                            "install",
                        ]
                        if py:
                            argv.append("--py")
                        argv.extend(packages)
                    else:
                        argv = build_circup_argv(
                            circup_exe=circup_exe,
                            stage_path=stage_for_circup,
                            packages=packages,
                            cpy_version=cpy_version,
                            board_id=board_id,
                            py=py,
                        )
                    out_text = self._run_circup(argv, use_module=use_module)

                    lib_src = Path(stage) / "lib"
                    if not lib_src.is_dir():
                        raise RuntimeError("circup did not create a lib/ staging directory")
                    dest_root = target if target.startswith("/") else "/" + target
                    copied: list[str] = []
                    transport = "serial"
                    serial_err: Optional[BaseException] = None
                    try:
                        self._ensure_cp_writable(t)
                        try:
                            t.fs_mkdir(dest_root if dest_root != "/" else "/lib")
                        except Exception:
                            pass
                        for child in sorted(lib_src.iterdir(), key=lambda p: p.name.lower()):
                            remote = dest_root.rstrip("/") + "/" + child.name
                            self._cp_local_to_remote(
                                t,
                                str(child),
                                remote,
                                verify=False,
                                copied=copied,
                                verified=[],
                            )
                        self._sync_remote_fs(t)
                    except Exception as e:
                        serial_err = e
                        err_s = str(e).lower()
                        if (
                            "read-only" not in err_s
                            and "errno 30" not in err_s
                            and "erofs" not in err_s
                        ):
                            raise
                        roots = self._find_circuitpy_host_roots()
                        if not roots:
                            hint = ""
                            if web_fallback_err is not None:
                                hint = f" ({web_fallback_err})"
                            raise RuntimeError(
                                "circup staged libraries, but the CircuitPython filesystem "
                                "is read-only over serial and no CIRCUITPY drive is mounted."
                                + hint
                                + f" Serial error: {e}"
                            ) from e
                        host_lib = Path(roots[0]) / "lib"
                        host_root = roots[0]
                        if host_root.startswith("/mnt/") and len(host_root) >= 6:
                            letter = host_root[5].upper()
                            win_lib = Path(f"{letter}:/lib")
                            try:
                                win_lib.mkdir(parents=True, exist_ok=True)
                                host_lib = win_lib
                            except Exception:
                                pass
                        copied = self._push_lib_tree_host(lib_src, host_lib)
                        transport = "circuitpy-msc"
                    result = {
                        "output": out_text,
                        "packages": list(packages),
                        "target": dest_root,
                        "cpy_version": cpy_version,
                        "board_id": board_id,
                        "py": py,
                        "files": len(copied),
                        "transport": transport,
                        "serial_error": str(serial_err) if serial_err else None,
                    }
                    if web_fallback_err is not None:
                        result["web_workflow_error"] = str(web_fallback_err)
                    return result
                finally:
                    shutil.rmtree(stage, ignore_errors=True)
            finally:
                if self._repl_mode:
                    try:
                        if self.transport and self.transport.in_raw_repl:
                            self.transport.exit_raw_repl()
                    except Exception:
                        pass
                    self._start_repl_reader()

    def _cp_version_and_board(self, t: Any) -> tuple[str, str]:
        """Best-effort CircuitPython version + board id for circup."""
        code = r"""
import sys
_ver = '.'.join(str(x) for x in sys.implementation.version[:3])
_bid = 'unknown'
try:
 _t = open('/boot_out.txt').read()
 for _line in _t.split('\n'):
  if _line.startswith('Board ID:'):
   _bid = _line.split(':',1)[1].strip() or _bid
   break
except Exception:
 pass
if _bid == 'unknown':
 try:
  import os as _os
  _u = _os.uname()
  _bid = getattr(_u, 'machine', 'unknown') or 'unknown'
 except Exception:
  pass
print(repr((_ver, _bid)))
"""
        out = t.exec(code)
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        line = text.strip().splitlines()[-1] if text.strip() else "('9.0.0', 'unknown')"
        import ast

        ver, bid = ast.literal_eval(line)
        return str(ver), str(bid)

    def df(self) -> dict[str, Any]:
        # Prefer vfs.mount enrichment (MicroPython); fall back to os.statvfs only.
        code = r"""
import os
rows=[]
_ms=[]
try:
 import vfs
 _ms=list(vfs.mount())
except Exception:
 _ms=[]
if not _ms:
 try:
  _ms=[('<root>', '/')]
 except Exception:
  _ms=[]
for _v,_p in _ms:
 try:
  _s=os.statvfs(_p)
 except Exception:
  continue
 _sz=_s[0]*_s[2]
 _av=_s[0]*_s[3]
 rows.append({'fs':str(_v),'size':_sz,'used':_sz-_av,'avail':_av,'mounted':_p})
if not rows:
 try:
  _s=os.statvfs('/')
  _sz=_s[0]*_s[2]
  _av=_s[0]*_s[3]
  rows.append({'fs':'/','size':_sz,'used':_sz-_av,'avail':_av,'mounted':'/'})
 except Exception:
  pass
print(repr(rows))
"""

        def op(t):
            out = t.exec(code)
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
            line = text.strip().splitlines()[-1] if text.strip() else "[]"
            import ast

            rows = ast.literal_eval(line)
            return {"mounts": rows}

        return self.with_raw(op)

    def mount(self, path: str, unsafe_links: bool = False) -> dict[str, Any]:
        self._require_micropython("mount")
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            t.mount_local(path, unsafe_links=unsafe_links)
            self._mounted_path = path
            return {"path": path, "mount": getattr(t, "fs_hook_mount", "/remote")}

    def umount(self) -> dict[str, Any]:
        self._require_micropython("umount")
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            t.umount_local()
            self._mounted_path = None
            if self._repl_mode:
                t.exit_raw_repl()
                self._start_repl_reader()
            return {"ok": True}

    def _mp_state(self):
        """Minimal mpremote State shim for romfs helpers."""
        t = self._require()

        class _State:
            def __init__(self, transport):
                self.transport = transport

            def ensure_raw_repl(self):
                if not self.transport.in_raw_repl:
                    self.transport.enter_raw_repl(soft_reset=False)

            def did_action(self):
                pass

        return _State(t)

    def romfs_query(self) -> dict[str, Any]:
        self._require_micropython("romfs")
        from mpremote import commands as mp_cmd

        with self._lock:
            self._stop_repl_reader()
            state = self._mp_state()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mp_cmd._do_romfs_query(state, argparse.Namespace())
            if self._repl_mode:
                try:
                    if self.transport and self.transport.in_raw_repl:
                        self.transport.exit_raw_repl()
                except Exception:
                    pass
                self._start_repl_reader()
            return {"output": buf.getvalue().strip()}

    def romfs_build(
        self, path: str, output: Optional[str] = None, mpy: bool = True
    ) -> dict[str, Any]:
        self._require_micropython("romfs")
        from mpremote import commands as mp_cmd

        args = argparse.Namespace(path=path, output=output, mpy=mpy)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mp_cmd._do_romfs_build(None, args)
        out_file = output or (path + ".romfs")
        size = Path(out_file).stat().st_size if Path(out_file).is_file() else 0
        return {"output": buf.getvalue().strip(), "output_file": out_file, "size": size}

    def romfs_deploy(
        self, path: str, partition: int = 0, mpy: bool = True
    ) -> dict[str, Any]:
        self._require_micropython("romfs")
        from mpremote import commands as mp_cmd

        with self._lock:
            self._stop_repl_reader()
            state = self._mp_state()
            args = argparse.Namespace(path=path, partition=partition, mpy=mpy)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mp_cmd._do_romfs_deploy(state, args)
            if self._repl_mode:
                try:
                    if self.transport and self.transport.in_raw_repl:
                        self.transport.exit_raw_repl()
                except Exception:
                    pass
                self._start_repl_reader()
            return {"output": buf.getvalue().strip(), "path": path, "partition": partition}

    def fs_cp(self, src: str, dest: str, verify: bool = False) -> dict[str, Any]:
        """
        Recursive copy using mpremote ':' prefix for board paths.
        Examples: local→board  ./a.py :/a.py
                  board→local  :/a.py ./a.py
                  board→board  :/a.py :/b.py
        """
        src_remote, src_path = split_fs_path(src)
        dest_remote, dest_path = split_fs_path(dest)
        copied: list[str] = []
        verified: list[str] = []

        def op(t):
            nonlocal copied, verified
            self._ensure_cp_writable(t)
            if src_remote and dest_remote:
                self._cp_remote_to_remote(t, src_path, dest_path, verify, copied, verified)
            elif not src_remote and dest_remote:
                self._cp_local_to_remote(t, src_path, dest_path, verify, copied, verified)
            elif src_remote and not dest_remote:
                self._cp_remote_to_local(t, src_path, dest_path, verify, copied, verified)
            else:
                raise RuntimeError("fs_cp: at least one path must be a board path (prefix with :)")
            return {
                "src": src,
                "dest": dest,
                "files": len(copied),
                "copied": copied,
                "verified": verified if verify else None,
            }

        return self.with_raw(op)

    def _remote_isdir(self, t, path: str) -> bool:
        try:
            st = t.fs_stat(path)
            return bool(st.st_mode & 0x4000)
        except Exception:
            return False

    def _cp_local_to_remote(
        self, t, src: str, dest: str, verify: bool, copied: list[str], verified: list[str]
    ) -> None:
        src_p = Path(src)
        if not src_p.exists():
            raise RuntimeError(f"local path not found: {src}")
        if src_p.is_dir():
            # If dest exists as dir, copy into it as basename; else create dest as the tree root.
            dest_exists = False
            try:
                dest_exists = self._remote_isdir(t, dest)
            except Exception:
                dest_exists = False
            root = (dest.rstrip("/") + "/" + src_p.name) if dest_exists else dest
            try:
                t.fs_mkdir(root)
            except Exception:
                pass
            for dirpath, dirnames, filenames in os.walk(src):
                rel = os.path.relpath(dirpath, src)
                remote_dir = root if rel == "." else root.rstrip("/") + "/" + rel.replace("\\", "/")
                if rel != ".":
                    try:
                        t.fs_mkdir(remote_dir)
                    except Exception:
                        pass
                for name in dirnames:
                    try:
                        t.fs_mkdir(remote_dir.rstrip("/") + "/" + name)
                    except Exception:
                        pass
                for name in filenames:
                    if name.startswith(".") or name in ("__pycache__",) or name.endswith((".pyc", ".pyo")):
                        continue
                    local_file = Path(dirpath) / name
                    remote_file = remote_dir.rstrip("/") + "/" + name
                    data = local_file.read_bytes()
                    t.fs_writefile(remote_file, data)
                    copied.append(remote_file)
                    if verify:
                        digest = t.fs_hashfile(remote_file, "sha256")
                        hx = digest.hex() if isinstance(digest, bytes) else str(digest)
                        if hx != host_sha256(data):
                            raise RuntimeError(f"hash mismatch after upload: {remote_file}")
                        verified.append(remote_file)
        else:
            # File copy: if dest is/exists as directory, place basename inside.
            final = dest
            if dest.endswith("/") or self._remote_isdir(t, dest):
                final = dest.rstrip("/") + "/" + src_p.name
            data = src_p.read_bytes()
            t.fs_writefile(final, data)
            copied.append(final)
            if verify:
                digest = t.fs_hashfile(final, "sha256")
                hx = digest.hex() if isinstance(digest, bytes) else str(digest)
                if hx != host_sha256(data):
                    raise RuntimeError(f"hash mismatch after upload: {final}")
                verified.append(final)
        if copied:
            self._sync_remote_fs(t)

    def _cp_remote_to_local(
        self, t, src: str, dest: str, verify: bool, copied: list[str], verified: list[str]
    ) -> None:
        dest_p = Path(dest)
        if self._remote_isdir(t, src):
            root = dest_p / Path(src).name if dest_p.exists() and dest_p.is_dir() else dest_p
            root.mkdir(parents=True, exist_ok=True)

            def walk(remote: str, local: Path) -> None:
                for e in self._board_listdir(t, remote if remote != "/" else ""):
                    name = e.name
                    if name.startswith(".") or name == "__pycache__" or name.endswith((".pyc", ".pyo")):
                        continue
                    rpath = (remote.rstrip("/") + "/" + name) if remote != "/" else "/" + name
                    lpath = local / name
                    if e.st_mode & 0x4000:
                        lpath.mkdir(parents=True, exist_ok=True)
                        walk(rpath, lpath)
                    else:
                        data = t.fs_readfile(rpath)
                        lpath.write_bytes(data)
                        copied.append(str(lpath))
                        if verify:
                            digest = t.fs_hashfile(rpath, "sha256")
                            hx = digest.hex() if isinstance(digest, bytes) else str(digest)
                            if hx != host_sha256(data):
                                raise RuntimeError(f"hash mismatch after download: {rpath}")
                            verified.append(str(lpath))

            walk(src, root)
        else:
            data = t.fs_readfile(src)
            if dest_p.exists() and dest_p.is_dir() or str(dest).endswith(("/", "\\")):
                dest_p = dest_p / Path(src).name
            dest_p.parent.mkdir(parents=True, exist_ok=True)
            dest_p.write_bytes(data)
            copied.append(str(dest_p))
            if verify:
                digest = t.fs_hashfile(src, "sha256")
                hx = digest.hex() if isinstance(digest, bytes) else str(digest)
                if hx != host_sha256(data):
                    raise RuntimeError(f"hash mismatch after download: {src}")
                verified.append(str(dest_p))

    def _cp_remote_to_remote(
        self, t, src: str, dest: str, verify: bool, copied: list[str], verified: list[str]
    ) -> None:
        if self._remote_isdir(t, src):
            root = dest.rstrip("/") + "/" + Path(src).name if self._remote_isdir(t, dest) else dest
            try:
                t.fs_mkdir(root)
            except Exception:
                pass

            def walk(remote: str, dest_dir: str) -> None:
                for e in self._board_listdir(t, remote if remote != "/" else ""):
                    name = e.name
                    rpath = (remote.rstrip("/") + "/" + name) if remote != "/" else "/" + name
                    dpath = dest_dir.rstrip("/") + "/" + name
                    if e.st_mode & 0x4000:
                        try:
                            t.fs_mkdir(dpath)
                        except Exception:
                            pass
                        walk(rpath, dpath)
                    else:
                        data = t.fs_readfile(rpath)
                        t.fs_writefile(dpath, data)
                        copied.append(dpath)
                        if verify:
                            d1 = t.fs_hashfile(rpath, "sha256")
                            d2 = t.fs_hashfile(dpath, "sha256")
                            h1 = d1.hex() if isinstance(d1, bytes) else str(d1)
                            h2 = d2.hex() if isinstance(d2, bytes) else str(d2)
                            if h1 != h2:
                                raise RuntimeError(f"hash mismatch after copy: {dpath}")
                            verified.append(dpath)

            walk(src, root)
        else:
            final = dest
            if dest.endswith("/") or self._remote_isdir(t, dest):
                final = dest.rstrip("/") + "/" + Path(src).name
            data = t.fs_readfile(src)
            t.fs_writefile(final, data)
            copied.append(final)
            if verify:
                d1 = t.fs_hashfile(src, "sha256")
                d2 = t.fs_hashfile(final, "sha256")
                h1 = d1.hex() if isinstance(d1, bytes) else str(d1)
                h2 = d2.hex() if isinstance(d2, bytes) else str(d2)
                if h1 != h2:
                    raise RuntimeError(f"hash mismatch after copy: {final}")
                verified.append(final)

    def edit_pull(self, path: str) -> dict[str, Any]:
        """Read a board file for host-side editing (extension / CLI)."""
        return self.fs_read(path)

    def edit_push(self, path: str, data_b64: str) -> dict[str, Any]:
        return self.fs_write(path, data_b64)

    # --- REPL ---

    def repl_start(self) -> dict[str, Any]:
        with self._lock:
            t = self._require()
            if t.in_raw_repl:
                t.exit_raw_repl()
            self._repl_mode = True
            self._start_repl_reader()
            return {"device": self.device}

    def repl_stop(self) -> dict[str, Any]:
        with self._lock:
            self._repl_mode = False
            self._stop_repl_reader()
            return {"ok": True}

    def repl_write(self, data_b64: str) -> dict[str, Any]:
        data = base64.b64decode(data_b64)
        with self._lock:
            t = self._require()
            if t.in_raw_repl:
                t.exit_raw_repl()
            if not self._repl_mode:
                self._repl_mode = True
                self._start_repl_reader()
            t.serial.write(data)
            return {"bytes": len(data)}

    def _start_repl_reader(self) -> None:
        if self._repl_thread and self._repl_thread.is_alive():
            return
        self._repl_stop.clear()
        self._repl_thread = threading.Thread(target=self._repl_reader_loop, daemon=True)
        self._repl_thread.start()

    def _stop_repl_reader(self) -> None:
        self._repl_stop.set()
        th = self._repl_thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=1.0)
        self._repl_thread = None

    def _repl_reader_loop(self) -> None:
        while not self._repl_stop.is_set():
            t = self.transport
            if not t or t.in_raw_repl:
                self._repl_stop.wait(0.05)
                continue
            try:
                n = t.serial.inWaiting()
                if n and not self._lock.acquire(blocking=False):
                    self._repl_stop.wait(0.02)
                    continue
                try:
                    if n:
                        data = t.serial.read(n)
                        if data:
                            _notify(
                                "repl_data",
                                {"data_b64": base64.b64encode(data).decode("ascii")},
                            )
                finally:
                    if n:
                        self._lock.release()
                if not n:
                    self._repl_stop.wait(0.02)
            except Exception as e:
                _notify("repl_error", {"message": str(e)})
                break


SESSION = Session()

METHODS = {
    "ping": lambda _p: {"pong": True, "pid": os.getpid(), "platform": sys.platform},
    "list_ports": lambda _p: SESSION.list_ports(),
    "connect": lambda p: SESSION.connect(p["device"], int(p.get("baud", 115200))),
    "disconnect": lambda _p: (SESSION.disconnect() or {"ok": True}),
    "resume": lambda p: SESSION.resume(p.get("baud")),
    "fs_listdir": lambda p: SESSION.fs_listdir(p.get("path", "/")),
    "fs_stat": lambda p: SESSION.fs_stat(p["path"]),
    "fs_read": lambda p: SESSION.fs_read(p["path"]),
    "fs_write": lambda p: SESSION.fs_write(p["path"], p["data_b64"]),
    "fs_mkdir": lambda p: SESSION.fs_mkdir(p["path"]),
    "fs_rm": lambda p: SESSION.fs_rm(p["path"]),
    "fs_rmdir": lambda p: SESSION.fs_rmdir(p["path"]),
    "fs_rm_rf": lambda p: SESSION.fs_rm_rf(p["path"]),
    "fs_touch": lambda p: SESSION.fs_touch(p["path"]),
    "fs_rename": lambda p: SESSION.fs_rename(p["src"], p["dest"]),
    "fs_hash": lambda p: SESSION.fs_hash(p["path"], p.get("algo", "sha256")),
    "fs_tree": lambda p: SESSION.fs_tree(p.get("path", "/")),
    "fs_cp": lambda p: SESSION.fs_cp(p["src"], p["dest"], bool(p.get("verify", False))),
    "exec": lambda p: SESSION.exec(p["code"], bool(p.get("follow", True))),
    "eval": lambda p: SESSION.eval(p["expr"]),
    "run_script": lambda p: SESSION.run_script(p["source"], bool(p.get("follow", True))),
    "run_path": lambda p: SESSION.run_path(p["path"], bool(p.get("follow", False))),
    "interrupt": lambda _p: SESSION.interrupt(),
    "soft_reset": lambda _p: SESSION.soft_reset(),
    "hard_reset": lambda _p: SESSION.hard_reset(),
    "bootloader": lambda _p: SESSION.bootloader(),
    "rtc_get": lambda _p: SESSION.rtc_get(),
    "rtc_set": lambda _p: SESSION.rtc_set(),
    "mip_install": lambda p: SESSION.mip_install(
        list(p.get("packages") or []),
        p.get("target"),
        bool(p.get("mpy", True)),
        p.get("index"),
    ),
    "circup_install": lambda p: SESSION.circup_install(
        list(p.get("packages") or []),
        py=bool(p.get("py", False)),
        target=str(p.get("target") or "/lib"),
        host=p.get("host"),
        password=p.get("password"),
        prefer_web=bool(p.get("prefer_web", True)),
    ),
    "df": lambda _p: SESSION.df(),
    "mount": lambda p: SESSION.mount(p["path"], bool(p.get("unsafe_links", False))),
    "umount": lambda _p: SESSION.umount(),
    "romfs_query": lambda _p: SESSION.romfs_query(),
    "romfs_build": lambda p: SESSION.romfs_build(
        p["path"], p.get("output"), bool(p.get("mpy", True))
    ),
    "romfs_deploy": lambda p: SESSION.romfs_deploy(
        p["path"], int(p.get("partition", 0)), bool(p.get("mpy", True))
    ),
    "edit_pull": lambda p: SESSION.edit_pull(p["path"]),
    "edit_push": lambda p: SESSION.edit_push(p["path"], p["data_b64"]),
    "repl_start": lambda _p: SESSION.repl_start(),
    "repl_stop": lambda _p: SESSION.repl_stop(),
    "repl_write": lambda p: SESSION.repl_write(p["data_b64"]),
}


def main() -> None:
    import atexit

    killed = cleanup_stale_sidecars()
    claim_sidecar_pid()
    atexit.register(release_sidecar_pid)
    ready: dict[str, Any] = {"version": 1, "pid": os.getpid()}
    if killed:
        ready["killed_stale"] = killed
    _notify("ready", ready)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            msg = json.loads(line)
            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params") or {}
            if method not in METHODS:
                _error(req_id, f"unknown method: {method}")
                continue
            result = METHODS[method](params)
            _result(req_id, result)
        except Exception as e:
            _error(req_id, str(e), traceback.format_exc())
    try:
        SESSION.disconnect()
    except Exception:
        pass
    release_sidecar_pid()


if __name__ == "__main__":
    main()
