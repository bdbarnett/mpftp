#!/usr/bin/env python3
"""
mpftp CLI — agent-friendly front-end to the mpftp sidecar / extension RPC.

Prefer the Cursor extension's Unix socket (~/.mpftp/rpc.sock) so CLI and UI
share one serial session. If the socket is missing, spawn sidecar.py directly
(standalone; requires --device for board ops).

Examples:
  mpftp status
  mpftp ports
  mpftp connect COM4
  mpftp ls /
  mpftp put ./main.py /main.py
  mpftp get /main.py ./main.py
  mpftp eval '1+1'
  mpftp exec 'print(42)'
  mpftp soft-reset
  mpftp watch          # tail activity log
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

def _linux_home() -> Path:
    """Prefer the WSL/Linux home even if this script is run under Windows Python."""
    for key in ("HOME", "USERPROFILE"):
        pass
    # If we're Windows Python launched from WSL, USERPROFILE is Windows; agents use Linux paths.
    wsl = os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")
    linux_home = os.environ.get("HOME")
    if linux_home and (wsl or sys.platform.startswith("linux")):
        return Path(linux_home)
    # When python.exe runs with HOME unset to Linux, try /home/<user>
    if sys.platform == "win32":
        for cand in (
            os.environ.get("HOME"),
            "/home/" + os.environ.get("USER", ""),
            "/home/" + os.environ.get("USERNAME", "").lower(),
        ):
            if cand and cand.startswith("/home/") and Path(cand).is_dir():
                return Path(cand)
    return Path.home()


HOME_MPFTP = _linux_home() / ".mpftp"
# Also check Windows-side mirror when needed
WIN_MPFTP = Path.home() / ".mpftp"
RPC_PORT_FILES = [
    HOME_MPFTP / "rpc.port",
    HOME_MPFTP / "rpc.path",
    Path.cwd() / ".mpftp" / "rpc.port",
    WIN_MPFTP / "rpc.port",
    WIN_MPFTP / "rpc.path",
]
ACTIVITY_LOG = HOME_MPFTP / "activity.log"
REPL_LOG = HOME_MPFTP / "repl.log"

HERE = Path(__file__).resolve().parent
SIDECAR = HERE / "sidecar.py"
FIRMWARE_ENGINE = HERE / "firmware_engine.py"


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def find_rpc_addr() -> Optional[tuple[str, int]]:
    """Return (host, port) for the extension AgentRpcServer, if running."""
    for f in RPC_PORT_FILES:
        try:
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8").strip()
            if not text:
                continue
            # "127.0.0.1:7429" or legacy socket path
            if ":" in text and not text.startswith("/"):
                host, _, port_s = text.rpartition(":")
                return host.strip() or "127.0.0.1", int(port_s)
        except Exception:
            continue
    # Probe default port
    try:
        with socket.create_connection(("127.0.0.1", 7429), timeout=0.3):
            return "127.0.0.1", 7429
    except Exception:
        return None


class RpcClient:
    def call(self, method: str, params: Optional[dict] = None) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        pass


class TcpClient(RpcClient):
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._id = 0

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        self._id += 1
        req = {"id": self._id, "method": method, "params": params or {}}
        with socket.create_connection((self.host, self.port), timeout=120) as s:
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
        line = buf.split(b"\n", 1)[0].decode("utf-8", "replace")
        msg = json.loads(line)
        if msg.get("type") == "error":
            raise RuntimeError(msg.get("error") or "rpc error")
        return msg.get("result")


class SidecarClient(RpcClient):
    """One-shot sidecar process; connect yourself before board ops."""

    def __init__(self, python: str) -> None:
        self.python = python
        self.proc = subprocess.Popen(
            [python, str(SIDECAR)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._id = 0
        assert self.proc.stdout
        # wait for ready
        deadline = time.time() + 20
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                _die(f"sidecar exited early: {err}")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "notify" and msg.get("method") == "ready":
                break
        else:
            _die("sidecar ready timeout")

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        assert self.proc.stdin and self.proc.stdout
        self._id += 1
        self.proc.stdin.write(json.dumps({"id": self._id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"sidecar closed: {err}")
            msg = json.loads(line)
            if msg.get("type") == "notify":
                continue
            if msg.get("id") != self._id:
                continue
            if msg.get("type") == "error":
                raise RuntimeError(msg.get("error") or "sidecar error")
            return msg.get("result")

    def close(self) -> None:
        try:
            self.call("disconnect")
        except Exception:
            pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()


def resolve_python() -> str:
    env = os.environ.get("MPFTP_PYTHON")
    if env:
        return env
    # Prefer Windows python on WSL for COM ports
    for cand in (
        str(Path.home() / "bin" / "python.exe"),
        "python.exe",
        str(HERE.parent / ".venv" / "bin" / "python"),
        "python3",
        "python",
    ):
        try:
            r = subprocess.run(
                [cand, "-c", "import mpremote, serial; print('ok')"],
                capture_output=True,
                timeout=15,
            )
            if r.returncode == 0:
                return cand
        except Exception:
            continue
    return "python3"


def get_client(prefer_rpc: bool = True) -> tuple[RpcClient, str]:
    if prefer_rpc:
        addr = find_rpc_addr()
        if addr:
            host, port = addr
            return TcpClient(host, port), f"tcp:{host}:{port}"
    return SidecarClient(resolve_python()), "sidecar"


def out(obj: Any) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(obj)


def cmd_status(_: argparse.Namespace) -> None:
    addr = find_rpc_addr()
    info = {
        "rpc": f"{addr[0]}:{addr[1]}" if addr else None,
        "activity_log": str(ACTIVITY_LOG),
        "repl_log": str(REPL_LOG),
        "extension_running": bool(addr),
    }
    if addr:
        try:
            client: RpcClient = TcpClient(*addr)
            info["session"] = client.call("agent_status")
        except Exception as e:
            info["session_error"] = str(e)
    out(info)


def cmd_ports(_: argparse.Namespace) -> None:
    client, _ = get_client()
    try:
        ports = client.call("list_ports")
        out(ports)
    finally:
        client.close()


def cmd_connect(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        res = client.call("connect", {"device": ns.device, "baud": ns.baud})
        print(f"connected via {mode}: {res}", file=sys.stderr)
        out(res)
    finally:
        if mode.startswith("sidecar"):
            # keep process? one-shot connect is useless in sidecar mode without linger
            client.close()


def cmd_disconnect(_: argparse.Namespace) -> None:
    client, _ = get_client()
    try:
        out(client.call("disconnect"))
    finally:
        client.close()


def cmd_resume(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        params: dict[str, Any] = {}
        if ns.baud:
            params["baud"] = ns.baud
        out(client.call("resume", params))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def ensure_device(client: RpcClient, device: Optional[str], baud: int) -> None:
    if not device:
        return
    client.call("connect", {"device": device, "baud": baud})


def cmd_ls(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        entries = client.call("fs_listdir", {"path": ns.path})
        if ns.json:
            out(entries)
            return
        for e in entries or []:
            kind = "d" if e.get("isDir") else "-"
            print(f"{kind} {e.get('size', 0):8}  {e.get('name')}")
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_tree(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_tree", {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_put(ns: argparse.Namespace) -> None:
    data = Path(ns.local).read_bytes()
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        dest = ns.remote
        if getattr(ns, "recursive", False) or Path(ns.local).is_dir():
            out(
                client.call(
                    "fs_cp",
                    {
                        "src": str(Path(ns.local).resolve()),
                        "dest": ":" + dest if not dest.startswith(":") else dest,
                        "verify": bool(getattr(ns, "verify", False)),
                    },
                )
            )
            return
        res = client.call(
            "fs_write",
            {"path": dest, "data_b64": base64.b64encode(data).decode("ascii")},
        )
        if getattr(ns, "verify", False):
            import hashlib

            expect = hashlib.sha256(data).hexdigest()
            got = client.call("fs_hash", {"path": dest, "algo": "sha256"})["hash"]
            if got != expect:
                raise SystemExit(f"hash mismatch: expected {expect}, got {got}")
            res = {**res, "verified": got}
        out(res)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_get(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        remote = ns.remote
        if getattr(ns, "recursive", False):
            out(
                client.call(
                    "fs_cp",
                    {
                        "src": ":" + remote if not remote.startswith(":") else remote,
                        "dest": str(Path(ns.local).resolve()),
                        "verify": bool(getattr(ns, "verify", False)),
                    },
                )
            )
            return
        res = client.call("fs_read", {"path": remote})
        raw = base64.b64decode(res["data_b64"])
        Path(ns.local).write_bytes(raw)
        if getattr(ns, "verify", False):
            import hashlib

            expect = client.call("fs_hash", {"path": remote, "algo": "sha256"})["hash"]
            got = hashlib.sha256(raw).hexdigest()
            if got != expect:
                raise SystemExit(f"hash mismatch: expected {expect}, got {got}")
        print(f"wrote {len(raw)} bytes → {ns.local}", file=sys.stderr)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_cp(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(
            client.call(
                "fs_cp",
                {"src": ns.src, "dest": ns.dest, "verify": bool(ns.verify)},
            )
        )
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_hash(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_hash", {"path": ns.path, "algo": ns.algo}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_edit(ns: argparse.Namespace) -> None:
    import os
    import tempfile

    editor = os.environ.get("EDITOR")
    if not editor:
        raise SystemExit("edit: $EDITOR not set")
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        path = ns.path
        client.call("fs_touch", {"path": path})
        res = client.call("edit_pull", {"path": path})
        raw = base64.b64decode(res["data_b64"])
        fd, tmp = tempfile.mkstemp(suffix="-" + Path(path).name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            rc = os.system(f'{editor} "{tmp}"')
            if rc != 0:
                raise SystemExit(f"editor exited {rc}")
            data = Path(tmp).read_bytes()
            out(
                client.call(
                    "edit_push",
                    {"path": path, "data_b64": base64.b64encode(data).decode("ascii")},
                )
            )
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_romfs(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        if ns.romfs_cmd == "build":
            # build is host-only; still needs a client for method dispatch
            out(
                client.call(
                    "romfs_build",
                    {"path": ns.path, "output": ns.output, "mpy": not ns.no_mpy},
                )
            )
            return
        ensure_device(client, ns.device, ns.baud)
        if ns.romfs_cmd == "query":
            out(client.call("romfs_query"))
        elif ns.romfs_cmd == "deploy":
            out(
                client.call(
                    "romfs_deploy",
                    {
                        "path": ns.path,
                        "partition": ns.partition,
                        "mpy": not ns.no_mpy,
                    },
                )
            )
        else:
            raise SystemExit(f"unknown romfs command: {ns.romfs_cmd}")
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_mkdir(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_mkdir", {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rm(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        method = "fs_rm_rf" if ns.recursive else "fs_rm"
        out(client.call(method, {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_touch(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_touch", {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rename(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_rename", {"src": ns.src, "dest": ns.dest}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_eval(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("eval", {"expr": ns.expr}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_exec(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("exec", {"code": ns.code, "follow": True}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_run(ns: argparse.Namespace) -> None:
    source = Path(ns.file).read_text(encoding="utf-8")
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("run_script", {"source": source, "follow": True}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_soft_reset(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("soft_reset"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_hard_reset(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("hard_reset"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_bootloader(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("bootloader"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rtc(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        if ns.set:
            out(client.call("rtc_set"))
        else:
            out(client.call("rtc_get"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_df(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("df"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_mip(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(
            client.call(
                "mip_install",
                {"packages": ns.packages, "target": ns.target, "mpy": not ns.no_mpy},
            )
        )
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_mount(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("mount", {"path": ns.path, "unsafe_links": ns.unsafe_links}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_umount(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("umount"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rpc(ns: argparse.Namespace) -> None:
    """Raw JSON-RPC: mpftp rpc METHOD [JSON_PARAMS]"""
    params = json.loads(ns.params) if ns.params else {}
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call(ns.method, params))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def resolve_build_python() -> str:
    """A native (Linux on WSL) python3 to run the firmware engine + make."""
    env = os.environ.get("MPFTP_BUILD_PYTHON")
    if env:
        return env
    import shutil

    if sys.platform != "win32":
        for cand in ("python3", "python"):
            p = shutil.which(cand)
            if p:
                return p
    return sys.executable or "python3"


def _engine_argv(cmd: str, extra: list[str]) -> list[str]:
    return [resolve_build_python(), str(FIRMWARE_ENGINE), cmd, *extra]


def _engine_json(cmd: str, extra: list[str]) -> Any:
    r = subprocess.run(_engine_argv(cmd, extra), capture_output=True, text=True)
    if r.returncode != 0 and not r.stdout.strip():
        _die(r.stderr.strip() or f"engine {cmd} failed")
    try:
        return json.loads(r.stdout)
    except Exception:
        _die(r.stderr.strip() or r.stdout.strip() or f"engine {cmd}: bad output")


def _engine_stream(cmd: str, extra: list[str]) -> dict:
    """Run a streaming engine command; log lines -> stderr, return final result."""
    proc = subprocess.Popen(
        _engine_argv(cmd, extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    result: dict = {}
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            print(line, file=sys.stderr)
            continue
        if msg.get("type") == "log":
            print(msg.get("line", ""), file=sys.stderr)
        elif msg.get("type") == "result":
            result = msg
    proc.wait()
    return result or {"ok": proc.returncode == 0, "returncode": proc.returncode}


def _resolve_mp(ns: argparse.Namespace) -> Optional[str]:
    if getattr(ns, "mp", None):
        return ns.mp
    info = _engine_json("discover", [])
    return info.get("micropython")


def _sel_args(ns: argparse.Namespace) -> list[str]:
    args: list[str] = []
    mp = _resolve_mp(ns)
    if mp:
        args += ["--mp", mp]
    if getattr(ns, "port", None):
        args += ["--port", ns.port]
    if getattr(ns, "board", None):
        args += ["--board", ns.board]
    if getattr(ns, "variant", None):
        args += ["--variant", ns.variant]
    return args


def cmd_firmware(ns: argparse.Namespace) -> None:
    sub = ns.fw_cmd
    if sub == "list":
        extra = ["--mp", ns.mp] if getattr(ns, "mp", None) else []
        out(_engine_json("tree", extra))
        return
    if sub == "discover":
        extra = ["--mp", ns.mp] if getattr(ns, "mp", None) else []
        out(_engine_json("discover", extra))
        return
    if sub == "cmods":
        extra = ["--mp", ns.mp] if getattr(ns, "mp", None) else []
        out(_engine_json("cmods", extra))
        return
    if sub == "artifact":
        out(_engine_json("artifact", _sel_args(ns)))
        return
    if sub == "build":
        extra = _sel_args(ns)
        if ns.clean:
            extra.append("--clean")
        res = _engine_stream("build", extra)
        out(res)
        if not res.get("ok"):
            raise SystemExit(1)
        return
    if sub == "clean":
        res = _engine_stream("clean", _sel_args(ns))
        out(res)
        return
    if sub == "flash":
        extra = _sel_args(ns)
        if ns.device:
            extra += ["--device", ns.device]
        if getattr(ns, "artifact", None):
            extra += ["--artifact", ns.artifact]
        if getattr(ns, "erase", False):
            extra.append("--erase")
        res = _engine_stream("flash", extra)
        out(res)
        if not res.get("ok"):
            raise SystemExit(1)
        return
    if sub == "partitions":
        extra = [ns.part_action]
        mp = _resolve_mp(ns)
        if mp:
            extra += ["--mp", mp]
        if getattr(ns, "board", None):
            extra += ["--board", ns.board]
        if getattr(ns, "variant", None):
            extra += ["--variant", ns.variant]
        if ns.part_action == "set":
            if getattr(ns, "csv_file", None):
                extra += ["--csv-file", ns.csv_file]
            elif getattr(ns, "rows", None):
                extra += ["--rows", ns.rows]
            else:
                _die("partitions set requires --csv-file or --rows")
        out(_engine_json("partitions", extra))
        return
    _die(f"unknown firmware command: {sub}")


def cmd_watch(ns: argparse.Namespace) -> None:
    path = Path(ns.file) if ns.file else (REPL_LOG if ns.repl else ACTIVITY_LOG)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    print(f"watching {path}", file=sys.stderr)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        if not ns.from_start:
            f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                time.sleep(0.25)


def build_parser() -> argparse.ArgumentParser:
    device_opts = argparse.ArgumentParser(add_help=False)
    device_opts.add_argument(
        "--device",
        "-d",
        dest="device",
        default=None,
        help="Serial device (standalone / force connect)",
    )
    device_opts.add_argument("--baud", type=int, default=115200)

    p = argparse.ArgumentParser(prog="mpftp", description="mpftp agent CLI (mpremote via sidecar)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="RPC socket + session status").set_defaults(func=cmd_status)
    sub.add_parser("ports", parents=[device_opts], help="List serial ports").set_defaults(func=cmd_ports)

    c = sub.add_parser("connect", parents=[device_opts], help="Connect to device")
    c.add_argument("device_pos", metavar="DEVICE", help="e.g. COM4 or /dev/ttyACM0")
    c.set_defaults(func=cmd_connect)

    sub.add_parser("disconnect", parents=[device_opts], help="Disconnect").set_defaults(func=cmd_disconnect)
    sub.add_parser("resume", parents=[device_opts], help="Reconnect to last device").set_defaults(
        func=cmd_resume
    )

    ls = sub.add_parser("ls", parents=[device_opts], help="List board directory")
    ls.add_argument("path", nargs="?", default="/")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)

    tr = sub.add_parser("tree", parents=[device_opts], help="Tree board directory")
    tr.add_argument("path", nargs="?", default="/")
    tr.set_defaults(func=cmd_tree)

    put = sub.add_parser("put", parents=[device_opts], help="Upload local file to board")
    put.add_argument("local")
    put.add_argument("remote")
    put.add_argument("-r", "--recursive", action="store_true", help="Copy directories via fs_cp")
    put.add_argument("--verify", action="store_true", help="SHA-256 verify after transfer")
    put.set_defaults(func=cmd_put)

    get = sub.add_parser("get", parents=[device_opts], help="Download board file to local")
    get.add_argument("remote")
    get.add_argument("local")
    get.add_argument("-r", "--recursive", action="store_true", help="Copy directories via fs_cp")
    get.add_argument("--verify", action="store_true", help="SHA-256 verify after transfer")
    get.set_defaults(func=cmd_get)

    cp = sub.add_parser(
        "cp",
        parents=[device_opts],
        help="Copy (use : prefix for board paths, e.g. ./a.py :/a.py)",
    )
    cp.add_argument("src")
    cp.add_argument("dest")
    cp.add_argument("--verify", action="store_true")
    cp.set_defaults(func=cmd_cp)

    hx = sub.add_parser("hash", parents=[device_opts], help="SHA-256 (or algo) of board file")
    hx.add_argument("path")
    hx.add_argument("--algo", default="sha256")
    hx.set_defaults(func=cmd_hash)

    ed = sub.add_parser("edit", parents=[device_opts], help="Edit board file with $EDITOR")
    ed.add_argument("path")
    ed.set_defaults(func=cmd_edit)

    mk = sub.add_parser("mkdir", parents=[device_opts], help="Create board directory")
    mk.add_argument("path")
    mk.set_defaults(func=cmd_mkdir)

    rm = sub.add_parser("rm", parents=[device_opts], help="Remove board file (or -r tree)")
    rm.add_argument("path")
    rm.add_argument("-r", "--recursive", action="store_true")
    rm.set_defaults(func=cmd_rm)

    touch = sub.add_parser("touch", parents=[device_opts], help="Create empty board file")
    touch.add_argument("path")
    touch.set_defaults(func=cmd_touch)

    ren = sub.add_parser("rename", parents=[device_opts], help="Rename board path")
    ren.add_argument("src")
    ren.add_argument("dest")
    ren.set_defaults(func=cmd_rename)

    ev = sub.add_parser("eval", parents=[device_opts], help="Eval expression on board")
    ev.add_argument("expr")
    ev.set_defaults(func=cmd_eval)

    ex = sub.add_parser("exec", parents=[device_opts], help="Exec code on board")
    ex.add_argument("code")
    ex.set_defaults(func=cmd_exec)

    run = sub.add_parser("run", parents=[device_opts], help="Run local script on board")
    run.add_argument("file")
    run.set_defaults(func=cmd_run)

    sub.add_parser("soft-reset", parents=[device_opts], help="Soft reset").set_defaults(func=cmd_soft_reset)
    sub.add_parser("hard-reset", parents=[device_opts], help="Hard reset").set_defaults(func=cmd_hard_reset)
    sub.add_parser("bootloader", parents=[device_opts], help="Enter bootloader").set_defaults(
        func=cmd_bootloader
    )

    rtc = sub.add_parser("rtc", parents=[device_opts], help="Get or set RTC")
    rtc.add_argument("--set", action="store_true", help="Set RTC from host")
    rtc.set_defaults(func=cmd_rtc)

    sub.add_parser("df", parents=[device_opts], help="Disk free").set_defaults(func=cmd_df)

    mip = sub.add_parser("mip", parents=[device_opts], help="mip install package(s)")
    mip.add_argument("packages", nargs="+")
    mip.add_argument("--target")
    mip.add_argument("--no-mpy", action="store_true")
    mip.set_defaults(func=cmd_mip)

    mnt = sub.add_parser("mount", parents=[device_opts], help="Mount local path on board")
    mnt.add_argument("path")
    mnt.add_argument("--unsafe-links", action="store_true")
    mnt.set_defaults(func=cmd_mount)
    sub.add_parser("umount", parents=[device_opts], help="Umount local mount").set_defaults(func=cmd_umount)

    rom = sub.add_parser("romfs", parents=[device_opts], help="ROMFS query/build/deploy")
    rom.add_argument("romfs_cmd", choices=["query", "build", "deploy"])
    rom.add_argument("path", nargs="?", help="Source dir or .romfs image (build/deploy)")
    rom.add_argument("-o", "--output", help="Output file for build")
    rom.add_argument("--partition", type=int, default=0)
    rom.add_argument("--no-mpy", action="store_true")
    rom.set_defaults(func=cmd_romfs)

    rpc = sub.add_parser("rpc", parents=[device_opts], help="Raw RPC method")
    rpc.add_argument("method")
    rpc.add_argument("params", nargs="?", help='JSON object, e.g. {"path":"/"}')
    rpc.set_defaults(func=cmd_rpc)

    fw = sub.add_parser("firmware", help="Build & flash MicroPython firmware (host-side)")
    fwsub = fw.add_subparsers(dest="fw_cmd", required=True)

    fw_sel = argparse.ArgumentParser(add_help=False)
    fw_sel.add_argument("--mp", help="MicroPython tree path (auto-discovered if omitted)")
    fw_sel.add_argument("--port", help="MicroPython port, e.g. esp32")
    fw_sel.add_argument("--board", default="", help="Board name")
    fw_sel.add_argument("--variant", default="", help="Board/port variant")

    fwsub.add_parser("list", parents=[fw_sel], help="List ports/boards/variants").set_defaults(
        func=cmd_firmware
    )
    fwsub.add_parser("discover", parents=[fw_sel], help="Show resolved MP/IDF/emsdk paths").set_defaults(
        func=cmd_firmware
    )
    fwsub.add_parser("cmods", parents=[fw_sel], help="List discovered user C modules").set_defaults(
        func=cmd_firmware
    )
    fwsub.add_parser("artifact", parents=[fw_sel], help="Report built firmware for a selection").set_defaults(
        func=cmd_firmware
    )

    fwb = fwsub.add_parser("build", parents=[fw_sel], help="Build firmware (streams log)")
    fwb.add_argument("--clean", action="store_true", help="Clean before building")
    fwb.set_defaults(func=cmd_firmware)

    fwsub.add_parser("clean", parents=[fw_sel], help="Clean a selection").set_defaults(
        func=cmd_firmware
    )

    fwf = fwsub.add_parser("flash", parents=[fw_sel, device_opts], help="Flash a built artifact")
    fwf.add_argument("--artifact", help="Explicit firmware file (else last build)")
    fwf.add_argument("--erase", action="store_true", help="esp32: erase flash first")
    fwf.set_defaults(func=cmd_firmware)

    fwp = fwsub.add_parser("partitions", parents=[fw_sel], help="esp32 partition override")
    fwp.add_argument("part_action", choices=["get", "set", "reset"])
    fwp.add_argument("--rows", help="JSON array of partition rows (set)")
    fwp.add_argument("--csv-file", dest="csv_file", help="CSV file to import (set)")
    fwp.set_defaults(func=cmd_firmware)

    w = sub.add_parser("watch", help="Tail activity or REPL log")
    w.add_argument("--repl", action="store_true", help="Watch REPL log instead of activity")
    w.add_argument("--file", help="Custom log path")
    w.add_argument("--from-start", action="store_true")
    w.set_defaults(func=cmd_watch)

    return p


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if getattr(ns, "cmd", None) == "connect":
        ns.device = ns.device_pos
    elif not hasattr(ns, "device"):
        ns.device = None
    if not hasattr(ns, "baud"):
        ns.baud = 115200
    try:
        ns.func(ns)
    except BrokenPipeError:
        pass
    except Exception as e:
        _die(str(e))


if __name__ == "__main__":
    main()
