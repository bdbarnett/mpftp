#!/usr/bin/env python3
"""
mpftp sidecar — long-lived mpremote session for VS Code / Cursor.

Speaks newline-delimited JSON on stdin/stdout.
Uses the official mpremote package (same backend as mpremote.exe).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import traceback
from typing import Any, Optional

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


class Session:
    def __init__(self) -> None:
        self.transport = None
        self.device: Optional[str] = None
        self.baud = 115200
        self._lock = threading.RLock()
        self._repl_mode = False
        self._repl_stop = threading.Event()
        self._repl_thread: Optional[threading.Thread] = None

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

    def connect(self, device: str, baud: int = 115200) -> dict[str, Any]:
        from mpremote.transport_serial import SerialTransport
        from mpremote.transport import TransportError

        with self._lock:
            self.disconnect()
            self.baud = baud
            try:
                self.transport = SerialTransport(device, baudrate=baud)
            except TransportError as e:
                raise RuntimeError(str(e.args[0] if e.args else e)) from e
            except OSError as e:
                raise RuntimeError(f"failed to access {device}: {e}") from e
            self.device = device
            return {"device": device, "baud": baud}

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
        return self.exec(source, follow=follow)

    def soft_reset(self) -> dict[str, Any]:
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            t.enter_raw_repl(soft_reset=True)
            if self._repl_mode:
                t.exit_raw_repl()
                self._start_repl_reader()
            return {"ok": True}

    def hard_reset(self) -> dict[str, Any]:
        code = "import time, machine; time.sleep_ms(100); machine.reset()"
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            try:
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
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            try:
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
            out = t.eval("import machine; machine.RTC().datetime()")
            return {"datetime": repr(out)}

        return self.with_raw(op)

    def rtc_set(self) -> dict[str, Any]:
        import time

        tnow = time.localtime()
        # MicroPython RTC: (year, month, day, weekday, hour, minute, second, subsecond)
        # weekday: Monday=0 in MP for some ports; use time.struct_time tm_wday (Mon=0)
        tup = (
            tnow.tm_year,
            tnow.tm_mon,
            tnow.tm_mday,
            tnow.tm_wday,
            tnow.tm_hour,
            tnow.tm_min,
            tnow.tm_sec,
            0,
        )
        code = f"import machine; machine.RTC().datetime({tup})"

        def op(t):
            t.exec(code)
            return {"datetime": list(tup)}

        return self.with_raw(op)

    def mip_install(self, packages: list[str], target: Optional[str] = None, mpy: bool = True) -> dict[str, Any]:
        # Reuse mpremote mip by exec'ing mip on device after ensuring connectivity.
        # Prefer calling into mpremote.mip if available.
        try:
            from mpremote import mip as mp_mip
        except Exception:
            mp_mip = None

        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            try:
                if mp_mip is not None and hasattr(mp_mip, "install"):
                    # mpremote.mip.do_mip expects state; fall back to device-side mip
                    pass
                # Device-side mip (works when network available on board)
                pkgs = ", ".join(repr(p) for p in packages)
                tgt = f", target={target!r}" if target else ""
                mpy_flag = "True" if mpy else "False"
                code = (
                    "import mip\n"
                    f"for _p in [{pkgs}]:\n"
                    f" mip.install(_p, mpy={mpy_flag}{tgt})\n"
                )
                out = t.exec(code)
                text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
                return {"output": text, "packages": packages}
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
            return {"path": path, "mount": getattr(t, "fs_hook_mount", "/remote")}

    def umount(self) -> dict[str, Any]:
        with self._lock:
            t = self._require()
            self._stop_repl_reader()
            if not t.in_raw_repl:
                t.enter_raw_repl(soft_reset=False)
            t.umount_local()
            if self._repl_mode:
                t.exit_raw_repl()
                self._start_repl_reader()
            return {"ok": True}

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
    "fs_listdir": lambda p: SESSION.fs_listdir(p.get("path", "/")),
    "fs_stat": lambda p: SESSION.fs_stat(p["path"]),
    "fs_read": lambda p: SESSION.fs_read(p["path"]),
    "fs_write": lambda p: SESSION.fs_write(p["path"], p["data_b64"]),
    "fs_mkdir": lambda p: SESSION.fs_mkdir(p["path"]),
    "fs_rm": lambda p: SESSION.fs_rm(p["path"]),
    "fs_rmdir": lambda p: SESSION.fs_rmdir(p["path"]),
    "fs_rm_rf": lambda p: SESSION.fs_rm_rf(p["path"]),
    "fs_touch": lambda p: SESSION.fs_touch(p["path"]),
    "fs_hash": lambda p: SESSION.fs_hash(p["path"], p.get("algo", "sha256")),
    "fs_tree": lambda p: SESSION.fs_tree(p.get("path", "/")),
    "exec": lambda p: SESSION.exec(p["code"], bool(p.get("follow", True))),
    "eval": lambda p: SESSION.eval(p["expr"]),
    "run_script": lambda p: SESSION.run_script(p["source"], bool(p.get("follow", True))),
    "soft_reset": lambda _p: SESSION.soft_reset(),
    "hard_reset": lambda _p: SESSION.hard_reset(),
    "bootloader": lambda _p: SESSION.bootloader(),
    "rtc_get": lambda _p: SESSION.rtc_get(),
    "rtc_set": lambda _p: SESSION.rtc_set(),
    "mip_install": lambda p: SESSION.mip_install(
        list(p.get("packages") or []), p.get("target"), bool(p.get("mpy", True))
    ),
    "df": lambda _p: SESSION.df(),
    "mount": lambda p: SESSION.mount(p["path"], bool(p.get("unsafe_links", False))),
    "umount": lambda _p: SESSION.umount(),
    "repl_start": lambda _p: SESSION.repl_start(),
    "repl_stop": lambda _p: SESSION.repl_stop(),
    "repl_write": lambda p: SESSION.repl_write(p["data_b64"]),
}


def main() -> None:
    _notify("ready", {"version": 1})
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


if __name__ == "__main__":
    main()
