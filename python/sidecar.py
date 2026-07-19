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
import sys
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

    def list_ports(self) -> list[dict[str, Any]]:
        import serial.tools.list_ports

        ports = []
        for p in sorted(serial.tools.list_ports.comports(), key=lambda x: x.device):
            ports.append(
                {
                    "device": p.device,
                    "serial_number": p.serial_number,
                    "vid": p.vid if isinstance(p.vid, int) else None,
                    "pid": p.pid if isinstance(p.pid, int) else None,
                    "manufacturer": p.manufacturer,
                    "product": p.product,
                    "description": p.description,
                }
            )
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
                    raise RuntimeError(str(e.args[0] if e.args else e)) from e
                except OSError as e:
                    if not last:
                        time.sleep(retry_delay)
                        continue
                    raise RuntimeError(f"failed to access {device}: {e}") from e
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
                        f"{device} is not responding as MicroPython "
                        f"(likely bootloader/UF2 mode, wrong port, or busy REPL): {e}"
                    ) from e
                self.device = device
                self.last_device = device
                result: dict[str, Any] = {
                    "device": device,
                    "baud": baud,
                    "micropython": True,
                }
                if rtc is not None:
                    result["rtc"] = rtc
                if attempt > 1:
                    result["retries"] = attempt - 1
                return result
            # Unreachable: the loop always returns or raises on the last attempt.
            raise RuntimeError(
                f"{device} could not be connected: {last_probe_err}"
            )

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
                    raise RuntimeError(f"resume failed (still connected but not MicroPython): {e}") from e
                result: dict[str, Any] = {
                    "device": device,
                    "baud": self.baud,
                    "micropython": True,
                    "resumed": True,
                }
                if rtc is not None:
                    result["rtc"] = rtc
                return result
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
        t.exec(f"import machine; machine.RTC().datetime({tup})")
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

    def _take_control(
        self, t: Any, *, clean: bool = True, timeout_overall: float = 20.0
    ) -> None:
        """Interrupt any running program and enter raw REPL.

        Always sends Ctrl-C (Thonny-style interrupt-on-connect). When ``clean``
        is True, soft-resets *while in raw REPL* so ``main.py`` does not run
        (MicroPython skips auto-start after a raw soft-reset).
        """
        import time

        serial = getattr(t, "serial", None)
        if serial is None:
            raise RuntimeError("transport has no serial port")

        def ctrl_c() -> None:
            try:
                serial.write(b"\r\x03")
            except Exception:
                pass

        # Thorough interrupt before raw mode (see micropython#7867 / Thonny pipkin).
        for delay in (0.0, 0.1, 0.1, 0.2):
            if delay:
                time.sleep(delay)
            ctrl_c()
        time.sleep(0.05)
        self._serial_flush(serial)

        last_err: Optional[Exception] = None
        attempts = 4
        per = max(5.0, float(timeout_overall) / attempts)
        for attempt in range(attempts):
            try:
                t.enter_raw_repl(soft_reset=clean, timeout_overall=per)
                return
            except Exception as e:
                last_err = e
                try:
                    t.in_raw_repl = False
                except Exception:
                    pass
                ctrl_c()
                ctrl_c()
                time.sleep(0.15 + 0.1 * attempt)
                self._serial_flush(serial)
                try:
                    serial.write(b"\r\x01")  # poke raw REPL (Thonny)
                except Exception:
                    pass
                time.sleep(0.1)

        detail = str(last_err.args[0] if last_err and getattr(last_err, "args", None) else last_err)
        raise RuntimeError(
            f"could not take control of MicroPython (interrupt/raw REPL failed): {detail}"
        ) from last_err

    def _probe_micropython(self, t: Any) -> Optional[list[int]]:
        """Take control (interrupt + raw soft-reset), set RTC, leave raw REPL.

        Connect never leaves ``main.py`` running: interrupt is unconditional and
        the raw soft-reset refreshes the heap without auto-starting main.
        """
        self._take_control(t, clean=True, timeout_overall=20.0)
        rtc: Optional[list[int]] = None
        try:
            rtc = self._apply_rtc(t)
        except Exception:
            # Some ports lack machine.RTC; don't fail the connection for that.
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

    def fs_listdir(self, path: str = "/") -> list[dict[str, Any]]:
        # mpremote uses "" for the device root; "/" also works on most ports.
        list_path = "" if path in ("", "/", None) else path

        def op(t):
            entries = []
            for e in t.fs_listdir(list_path):
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
            t.fs_writefile(path, data)
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
                for e in t.fs_listdir(p):
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
                entries = t.fs_listdir(p if p != "/" else "")
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
        """Fresh heap via raw soft-reset. Does not run main.py."""
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            self._take_control(t, clean=True, timeout_overall=20.0)
            if self._repl_mode:
                try:
                    if t.in_raw_repl:
                        t.exit_raw_repl()
                except Exception:
                    pass
                self._start_repl_reader()
            return {"ok": True, "main_skipped": True}

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
            t.exec("import machine")
            out = t.eval("machine.RTC().datetime()")
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

    def df(self) -> dict[str, Any]:
        code = r"""
import os,vfs
rows=[]
try:
 _ms=vfs.mount()
except:
 _ms=[]
 for _m in ['']+os.listdir('/'):
  _m='/'+_m if _m else '/'
  try:
   _s=os.stat(_m)
  except:
   continue
  if _s[0]&(1<<14):
   _ms.append(('<unknown>',_m))
for _v,_p in _ms:
 _s=os.statvfs(_p)
 _sz=_s[0]*_s[2]
 _av=_s[0]*_s[3]
 rows.append({'fs':str(_v),'size':_sz,'used':_sz-_av,'avail':_av,'mounted':_p})
print(repr(rows))
"""

        def op(t):
            out = t.exec(code)
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
            # last line should be repr(rows)
            line = text.strip().splitlines()[-1] if text.strip() else "[]"
            import ast

            rows = ast.literal_eval(line)
            return {"mounts": rows}

        return self.with_raw(op)

    def mount(self, path: str, unsafe_links: bool = False) -> dict[str, Any]:
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            t.mount_local(path, unsafe_links=unsafe_links)
            self._mounted_path = path
            return {"path": path, "mount": getattr(t, "fs_hook_mount", "/remote")}

    def umount(self) -> dict[str, Any]:
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

    def _cp_remote_to_local(
        self, t, src: str, dest: str, verify: bool, copied: list[str], verified: list[str]
    ) -> None:
        dest_p = Path(dest)
        if self._remote_isdir(t, src):
            root = dest_p / Path(src).name if dest_p.exists() and dest_p.is_dir() else dest_p
            root.mkdir(parents=True, exist_ok=True)

            def walk(remote: str, local: Path) -> None:
                for e in t.fs_listdir(remote if remote != "/" else ""):
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
                for e in t.fs_listdir(remote if remote != "/" else ""):
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
