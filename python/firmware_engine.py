#!/usr/bin/env python3
"""
mpftp firmware engine — build & flash MicroPython (and variants) from the
parent workspace of a ``micropython`` checkout, with user C modules / frozen
manifest auto-discovered like a cmods-style tree, but WITHOUT depending on
build_mp.sh.

This is a stdlib-only script driven by the mpftp extension (and the mpftp CLI /
agent RPC). Each subcommand runs in its own process:

  discover     resolve MicroPython / ESP-IDF / emsdk / workspace paths
  tree         list ports -> boards -> variants
  cmods        list discovered user C modules in the workspace
  artifact     report the built firmware for a port/board/variant (Ready state)
  build        make submodules + all (streams NDJSON log lines)
  clean        make clean for the selection
  flash        flash a built artifact to a device (esp32 / rp2 / samd)
  flashers     report which ports have a known flasher
  partitions   get / set / reset an esp32 partition-table override

Long-running commands (build/flash) stream newline-delimited JSON on stdout:
  {"type":"log","line":"..."}          incremental output
  {"type":"result","ok":true, ...}     final result (always last)
Short commands print a single JSON object.

Cancellation: the parent kills this process (group); child make/flash processes
are spawned in the same group and die with it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def emit(obj: dict) -> None:
    """Stream one NDJSON record (build/flash)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_log(line: str) -> None:
    emit({"type": "log", "line": line.rstrip("\n")})


def emit_result(ok: bool, **kw: Any) -> None:
    emit({"type": "result", "ok": ok, **kw})


def print_json(obj: Any) -> None:
    """Single-shot JSON result (discover/tree/...)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Host detection
# --------------------------------------------------------------------------- #

def detect_host() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        try:
            v = Path("/proc/version").read_text("utf-8", "replace").lower()
            if "microsoft" in v or "wsl" in v:
                return "wsl"
        except Exception:
            pass
        return "linux"
    return "linux"


HOST = detect_host()
HOME = Path.home()
MPFTP_DIR = HOME / ".mpftp"
STATE_FILE = MPFTP_DIR / "firmware.json"
ACTIVITY_LOG = MPFTP_DIR / "activity.log"


def log_activity(kind: str, message: str = "", data: Optional[dict] = None) -> None:
    """Append an NDJSON activity record (best effort), matching activityLog.ts."""
    try:
        MPFTP_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "source": "agent",
            "kind": kind,
            "message": message,
            "data": data or {},
        }
        with ACTIVITY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# State (last selection / device / prefs — paths live in VS Code settings)
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_state(patch: dict) -> None:
    state = load_state()
    state.update(patch)
    try:
        MPFTP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Path discovery
# --------------------------------------------------------------------------- #

def _is_mp_tree(p: Path) -> bool:
    return (p / "ports").is_dir() and (p / "py").is_dir()


def find_micropython(hint: Optional[str]) -> Optional[Path]:
    """Resolve the MicroPython tree: hint -> MP_DIR env -> search -> saved."""
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint).expanduser())
    env = os.environ.get("MP_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    saved = load_state().get("micropythonPath")
    if saved:
        candidates.append(Path(saved).expanduser())

    # Common cmods-style layouts and workspace neighbours.
    search_roots = [
        HOME / "gh" / "pydevices" / "cmods",
        HOME / "github" / "cmods",
        HOME / "gh" / "pydevices",
        Path.cwd(),
        Path.cwd().parent,
    ]
    for root in search_roots:
        candidates.append(root / "micropython")

    for c in candidates:
        try:
            if c and _is_mp_tree(c):
                return c.resolve()
        except Exception:
            continue

    # Shallow scan of a couple of roots for any */micropython.
    for root in [HOME / "gh" / "pydevices", HOME / "github", HOME / "gh"]:
        try:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                mp = child / "micropython"
                if _is_mp_tree(mp):
                    return mp.resolve()
        except Exception:
            continue
    return None


def find_idf(hint: Optional[str], workspace: Optional[Path]) -> Optional[Path]:
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint).expanduser())
    for key in ("IDF_DIR", "IDF_PATH"):
        v = os.environ.get(key)
        if v:
            candidates.append(Path(v).expanduser())
    saved = load_state().get("idfPath")
    if saved:
        candidates.append(Path(saved).expanduser())
    if workspace:
        candidates.append(workspace.parent.parent / "other" / "esp-idf")
        candidates.append(workspace / "esp-idf")
    candidates += [
        HOME / "esp" / "esp-idf",
        HOME / "esp-idf",
        HOME / ".espressif" / "esp-idf",
    ]
    for c in candidates:
        try:
            if c and (c / "export.sh").is_file():
                return c.resolve()
        except Exception:
            continue
    return None


def find_emsdk(hint: Optional[str], workspace: Optional[Path]) -> Optional[Path]:
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint).expanduser())
    v = os.environ.get("EMSDK_DIR") or os.environ.get("EMSDK")
    if v:
        candidates.append(Path(v).expanduser())
    if workspace:
        candidates.append(workspace.parent.parent / "other" / "emsdk")
    candidates.append(HOME / "emsdk")
    for c in candidates:
        try:
            if c and (c / "emsdk_env.sh").is_file():
                return c.resolve()
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# Tree / port model
# --------------------------------------------------------------------------- #

# Ports we can flash (others are build-only in the UI).
FLASHERS = {
    "esp32": "esptool",
    "rp2": "uf2/picotool",
    "samd": "uf2",
}


def port_kind(port_dir: Path) -> str:
    if (port_dir / "boards").is_dir():
        return "boards"
    if (port_dir / "variants").is_dir():
        return "variants"
    return "plain"


def _has_board(d: Path) -> bool:
    return (d / "mpconfigboard.mk").is_file() or (d / "mpconfigboard.cmake").is_file()


def list_board_variants(board_dir: Path) -> list[str]:
    out: list[str] = []
    for f in sorted(board_dir.glob("mpconfigvariant_*")):
        if f.suffix in (".mk", ".cmake"):
            name = f.name[len("mpconfigvariant_"):]
            name = name.rsplit(".", 1)[0]
            out.append(name)
    return sorted(set(out))


def list_port_variants(port_dir: Path) -> list[str]:
    out: list[str] = []
    vdir = port_dir / "variants"
    if vdir.is_dir():
        for d in sorted(vdir.iterdir()):
            if (d / "mpconfigvariant.mk").is_file():
                out.append(d.name)
    return out


def list_ports(mp: Path) -> list[str]:
    out: list[str] = []
    ports = mp / "ports"
    if not ports.is_dir():
        return out
    for p in sorted(ports.iterdir()):
        if (p / "Makefile").is_file():
            out.append(p.name)
    return out


def build_tree(mp: Path) -> list[dict]:
    tree: list[dict] = []
    for port in list_ports(mp):
        port_dir = mp / "ports" / port
        kind = port_kind(port_dir)
        node: dict = {
            "port": port,
            "kind": kind,
            "flashable": port in FLASHERS,
            "flasher": FLASHERS.get(port),
            "boards": [],
            "variants": [],
        }
        if kind == "boards":
            bdir = port_dir / "boards"
            for d in sorted(bdir.iterdir()):
                if d.is_dir() and _has_board(d):
                    node["boards"].append(
                        {"board": d.name, "variants": list_board_variants(d)}
                    )
        elif kind == "variants":
            node["variants"] = list_port_variants(port_dir)
        tree.append(node)
    return tree


# --------------------------------------------------------------------------- #
# cmods / user modules discovery
# --------------------------------------------------------------------------- #

def workspace_of(mp: Path) -> Path:
    return mp.parent


def discover_cmods(workspace: Path) -> dict:
    """Find user C modules (micropython.cmake / */micropython.mk) + manifest."""
    cmods: list[dict] = []
    seen: set[str] = set()
    try:
        for child in sorted(workspace.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            has_cmake = (child / "micropython.cmake").is_file()
            has_mk = (child / "micropython.mk").is_file()
            if has_cmake or has_mk:
                if child.name in seen:
                    continue
                seen.add(child.name)
                cmods.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "hasManifest": (child / "manifest.py").is_file(),
                        "kind": "cmake" if has_cmake else "make",
                    }
                )
    except Exception:
        pass
    aggregator = (workspace / "micropython.cmake").is_file()
    manifest = (workspace / "manifest.py").is_file()
    return {
        "workspaceDir": str(workspace),
        "cmods": cmods,
        "hasAggregator": aggregator,
        "hasManifest": manifest,
        "manifest": str(workspace / "manifest.py") if manifest else None,
    }


# --------------------------------------------------------------------------- #
# Frozen manifest / build dir / artifact resolution
# --------------------------------------------------------------------------- #

def resolve_upstream_frozen_manifest(
    port_dir: Path, kind: str, board: str, variant: str
) -> Optional[Path]:
    """Same file MicroPython would select for FROZEN_MANIFEST (parity w/ build_mp.sh)."""
    if kind == "variants":
        if variant and (port_dir / "variants" / variant / "manifest.py").is_file():
            return port_dir / "variants" / variant / "manifest.py"
        if (port_dir / "variants" / "manifest.py").is_file():
            return port_dir / "variants" / "manifest.py"
    elif kind == "boards":
        if board and variant and (port_dir / "boards" / board / f"manifest_{variant}.py").is_file():
            return port_dir / "boards" / board / f"manifest_{variant}.py"
        if board and (port_dir / "boards" / board / "manifest.py").is_file():
            return port_dir / "boards" / board / "manifest.py"
        if (port_dir / "boards" / "manifest.py").is_file():
            return port_dir / "boards" / "manifest.py"
    else:
        if (port_dir / "manifest.py").is_file():
            return port_dir / "manifest.py"
    return None


def build_dir(port_dir: Path, kind: str, board: str, variant: str) -> Optional[Path]:
    if kind == "boards":
        if board and variant:
            return port_dir / f"build-{board}-{variant}"
        if board:
            return port_dir / f"build-{board}"
        return None
    if kind == "variants":
        if variant:
            return port_dir / f"build-{variant}"
        return port_dir / "build"
    return port_dir / "build"


ARTIFACT_NAMES = ["firmware.uf2", "firmware.bin", "firmware.hex", "micropython.bin", "micropython"]


def find_artifact(bdir: Path) -> Optional[Path]:
    if not bdir or not bdir.is_dir():
        return None
    for name in ARTIFACT_NAMES:
        f = bdir / name
        if f.is_file():
            return f
    for pat in ("firmware.*", "*.uf2"):
        matches = sorted(bdir.glob(pat))
        if matches:
            return matches[0]
    return None


def artifact_info(mp: Path, port: str, board: str, variant: str) -> dict:
    port_dir = mp / "ports" / port
    kind = port_kind(port_dir)
    bdir = build_dir(port_dir, kind, board, variant)
    art = find_artifact(bdir) if bdir else None
    if art and art.is_file():
        st = art.stat()
        return {
            "ready": True,
            "artifact": str(art),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "buildDir": str(bdir),
        }
    return {"ready": False, "artifact": None, "buildDir": str(bdir) if bdir else None}


# --------------------------------------------------------------------------- #
# Partition override paths
# --------------------------------------------------------------------------- #

def partition_override_path(workspace: Path, board: str, variant: str) -> Path:
    name = board + (f"_{variant}" if variant else "")
    return workspace / "mpftp-partitions" / "esp32" / f"{name}.csv"


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def stream_process(cmd: list[str], cwd: Path, env: dict) -> int:
    """Run a process, streaming merged stdout/stderr as log lines. Returns rc."""
    emit_log(f"$ (cd {cwd}) {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout
    for line in proc.stdout:
        emit_log(line)
    proc.wait()
    return proc.returncode or 0


def do_build(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve()
    if not _is_mp_tree(mp):
        emit_result(False, error=f"Not a MicroPython tree: {mp}")
        return
    workspace = workspace_of(mp)
    port = ns.port
    board = ns.board or ""
    variant = ns.variant or ""
    port_dir = mp / "ports" / port
    if not (port_dir / "Makefile").is_file():
        emit_result(False, error=f"Invalid port: {port}")
        return
    kind = port_kind(port_dir)

    jobs = str(ns.jobs) if ns.jobs else ""
    j_arg = ["-j", jobs] if jobs else ["-j"]

    # Environment: unset inherited overrides (parity with build_mp.sh).
    env = dict(os.environ)
    env.pop("USER_C_MODULES", None)
    env.pop("FROZEN_MANIFEST", None)
    env["PYTHONUNBUFFERED"] = "1"

    make_args: list[str] = []

    cmods = discover_cmods(workspace)
    use_user_modules = cmods["hasAggregator"] or any(
        c["kind"] == "make" for c in cmods["cmods"]
    )
    if use_user_modules:
        make_args.append(f"USER_C_MODULES={workspace}")
        emit_log(f"[mpftp] USER_C_MODULES={workspace}")
        if cmods["cmods"]:
            names = ", ".join(c["name"] for c in cmods["cmods"])
            emit_log(f"[mpftp] user C modules: {names}")
    else:
        emit_log("[mpftp] no workspace user C modules found; building vanilla")

    if cmods["hasManifest"]:
        make_args.append(f"FROZEN_MANIFEST={workspace / 'manifest.py'}")
        upstream = resolve_upstream_frozen_manifest(port_dir, kind, board, variant)
        if upstream:
            env["FROZEN_MANIFEST_UPSTREAM"] = str(upstream)
            emit_log(f"[mpftp] FROZEN_MANIFEST_UPSTREAM={upstream}")

    if kind == "boards":
        if board:
            make_args.append(f"BOARD={board}")
        if variant:
            make_args.append(f"BOARD_VARIANT={variant}")
    elif kind == "variants":
        if variant:
            make_args.append(f"VARIANT={variant}")

    # Partition override (esp32): patch the build-dir sdkconfig after reconfigure.
    part_override = None
    if port == "esp32":
        ovr = partition_override_path(workspace, board, variant)
        if ovr.is_file():
            part_override = ovr
            emit_log(f"[mpftp] partition override: {ovr}")

    # Assemble the shell script (esp32/webassembly need a sourced env).
    script_lines = ["set -e", "set -o pipefail"]
    if port == "esp32":
        idf = find_idf(ns.idf, workspace)
        if not idf:
            emit_result(False, error="ESP-IDF not found. Set mpftp.idfPath or IDF_PATH.")
            return
        emit_log(f"[mpftp] ESP-IDF: {idf}")
        script_lines.append(f'. "{idf}/export.sh"')
    elif port == "webassembly":
        emsdk = find_emsdk(ns.emsdk, workspace)
        if not emsdk:
            emit_result(False, error="emsdk not found. Set mpftp.emsdkPath or EMSDK.")
            return
        emit_log(f"[mpftp] emsdk: {emsdk}")
        script_lines.append(f'. "{emsdk}/emsdk_env.sh"')

    q_args = " ".join(_shq(a) for a in make_args)

    # Host mpy-cross with cleared user modules (parity with build_mp.sh).
    script_lines.append(
        f'make -C "{mp}/mpy-cross" USER_C_MODULES= FROZEN_MANIFEST='
    )

    if ns.clean:
        script_lines.append(f'make {" ".join(j_arg)} clean {q_args}')

    if part_override is not None:
        # Reconfigure to create the build dir + sdkconfig, patch it, then build.
        bdir = build_dir(port_dir, kind, board, variant)
        script_lines.append(f'make {" ".join(j_arg)} submodules {q_args}')
        emit_log("[mpftp] applying partition override to build sdkconfig")
        _run_shell(script_lines, port_dir, env)  # run prep so build dir exists
        if bdir:
            _patch_sdkconfig_partition(bdir, part_override)
        rc = _run_shell(
            ["set -e", "set -o pipefail", *_env_prefix(port, ns, workspace),
             f'make {" ".join(j_arg)} all {q_args}'],
            port_dir, env,
        )
        _finish_build(rc, mp, port, board, variant)
        return

    script_lines.append(f'make {" ".join(j_arg)} submodules {q_args}')
    script_lines.append(f'make {" ".join(j_arg)} all {q_args}')

    rc = _run_shell(script_lines, port_dir, env)
    _finish_build(rc, mp, port, board, variant)


def _env_prefix(port: str, ns: argparse.Namespace, workspace: Path) -> list[str]:
    lines: list[str] = []
    if port == "esp32":
        idf = find_idf(ns.idf, workspace)
        if idf:
            lines.append(f'. "{idf}/export.sh"')
    elif port == "webassembly":
        emsdk = find_emsdk(ns.emsdk, workspace)
        if emsdk:
            lines.append(f'. "{emsdk}/emsdk_env.sh"')
    return lines


def _run_shell(script_lines: list[str], cwd: Path, env: dict) -> int:
    script = "\n".join(script_lines)
    proc = subprocess.Popen(
        ["bash", "-c", script],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout
    for line in proc.stdout:
        emit_log(line)
    proc.wait()
    return proc.returncode or 0


def _finish_build(rc: int, mp: Path, port: str, board: str, variant: str) -> None:
    if rc != 0:
        emit_result(False, error=f"make failed (exit {rc})", returncode=rc)
        log_activity("firmware_build", f"failed {port}/{board}/{variant}", {"rc": rc})
        return
    info = artifact_info(mp, port, board, variant)
    save_state({"lastSelection": {"port": port, "board": board, "variant": variant}})
    emit_result(True, **info)
    log_activity("firmware_build", f"ok {port}/{board}/{variant}", {"artifact": info.get("artifact")})


def _patch_sdkconfig_partition(bdir: Path, override_csv: Path) -> None:
    """Point the esp32 build's sdkconfig at an absolute custom partition CSV."""
    sdk = bdir / "sdkconfig"
    try:
        lines = sdk.read_text("utf-8").splitlines() if sdk.is_file() else []
    except Exception:
        lines = []
    filtered = [
        ln for ln in lines
        if not ln.startswith("CONFIG_PARTITION_TABLE_CUSTOM")
        and not ln.startswith("CONFIG_PARTITION_TABLE_FILENAME")
    ]
    filtered.append("CONFIG_PARTITION_TABLE_CUSTOM=y")
    filtered.append(f'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="{override_csv}"')
    filtered.append(f'CONFIG_PARTITION_TABLE_FILENAME="{override_csv}"')
    try:
        sdk.write_text("\n".join(filtered) + "\n", encoding="utf-8")
    except Exception as e:
        emit_log(f"[mpftp] warning: could not patch sdkconfig: {e}")


def _shq(s: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./=:+-]+", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def do_clean(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve()
    workspace = workspace_of(mp)
    port = ns.port
    board = ns.board or ""
    variant = ns.variant or ""
    port_dir = mp / "ports" / port
    kind = port_kind(port_dir)
    make_args: list[str] = []
    if kind == "boards":
        if board:
            make_args.append(f"BOARD={board}")
        if variant:
            make_args.append(f"BOARD_VARIANT={variant}")
    elif kind == "variants" and variant:
        make_args.append(f"VARIANT={variant}")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    lines = ["set -e", *_env_prefix(port, ns, workspace),
             f'make -j clean {" ".join(_shq(a) for a in make_args)}']
    rc = _run_shell(lines, port_dir, env)
    emit_result(rc == 0, returncode=rc)


# --------------------------------------------------------------------------- #
# Flash
# --------------------------------------------------------------------------- #

def esp32_flash_offset(port_dir: Path, board: str) -> str:
    if not board:
        return "0x0"
    bj = port_dir / "boards" / board / "board.json"
    try:
        data = json.loads(bj.read_text("utf-8"))
        return str(data.get("deploy_options", {}).get("flash_offset", "0x0"))
    except Exception:
        return "0x0"


def _wslpath_w(p: str) -> str:
    try:
        out = subprocess.run(
            ["wslpath", "-w", p], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return p


def _esptool_cmd(ns: argparse.Namespace) -> list[str]:
    """Resolve an esptool invocation for this host."""
    if ns.esptool:
        # Explicit: could be a python interpreter or an esptool executable.
        val = ns.esptool
        if val.endswith((".exe", "python", "python3")) or "python" in Path(val).name:
            return [val, "-m", "esptool"]
        return [val]
    if HOST == "wsl":
        # COM ports need Windows esptool.
        for cand in (str(HOME / "bin" / "python.exe"), "python.exe"):
            if shutil.which(cand) or Path(cand).is_file():
                return [cand, "-m", "esptool"]
        return ["python.exe", "-m", "esptool"]
    if shutil.which("esptool"):
        return ["esptool"]
    if shutil.which("esptool.py"):
        return ["esptool.py"]
    return [sys.executable, "-m", "esptool"]


def flash_esp32(ns: argparse.Namespace, mp: Path, artifact: Path) -> None:
    port_dir = mp / "ports" / ns.port
    offset = esp32_flash_offset(port_dir, ns.board or "")
    fw = str(artifact)
    if HOST == "wsl":
        fw = _wslpath_w(fw)
    cmd = _esptool_cmd(ns)
    base = cmd + ["-b", str(ns.baud or 460800), "-p", ns.device]
    if getattr(ns, "erase", False):
        emit_log("[mpftp] erasing flash…")
        rc = stream_process(base + ["erase_flash"], Path.cwd(), dict(os.environ))
        if rc != 0:
            emit_result(False, error=f"erase_flash failed (exit {rc})")
            return
    full = base + ["--before", "default_reset", "--after", "hard_reset",
                   "write_flash", offset, fw]
    rc = stream_process(full, Path.cwd(), dict(os.environ))
    if rc != 0:
        emit_result(False, error=f"esptool failed (exit {rc})")
        return
    emit_result(True, device=ns.device, offset=offset, artifact=str(artifact))
    log_activity("firmware_flash", f"esp32 {ns.board} -> {ns.device}", {"offset": offset})


def _find_uf2_drive() -> Optional[str]:
    """Find a mounted UF2 bootloader drive (RPI-RP2, etc.)."""
    roots: list[Path] = []
    if HOST in ("wsl", "linux"):
        roots += [Path("/media"), Path("/mnt"), Path("/run/media")]
    if HOST == "windows":
        for letter in "DEFGHIJKLMNOP":
            roots.append(Path(f"{letter}:/"))
    checked: list[Path] = []
    for root in roots:
        try:
            if root.name.endswith(":") or str(root).endswith(":/"):
                checked.append(root)
            elif root.is_dir():
                for sub in root.iterdir():
                    if sub.is_dir():
                        # one more level (/media/<user>/<LABEL>)
                        checked.append(sub)
                        for sub2 in sub.iterdir() if sub.is_dir() else []:
                            checked.append(sub2)
        except Exception:
            continue
    for d in checked:
        try:
            if (d / "INFO_UF2.TXT").is_file():
                return str(d)
        except Exception:
            continue
    return None


def flash_uf2(ns: argparse.Namespace, artifact: Path) -> None:
    """rp2 / samd: copy .uf2 to the bootloader drive; rp2 falls back to picotool."""
    target = ns.device if ns.device and _looks_like_mount(ns.device) else _find_uf2_drive()
    if target:
        dest = Path(target) / artifact.name
        emit_log(f"[mpftp] copying {artifact.name} -> {dest}")
        try:
            shutil.copyfile(str(artifact), str(dest))
            try:
                # Flush; some FS need it before the board reboots.
                with open(dest, "rb+") as f:
                    os.fsync(f.fileno())
            except Exception:
                pass
            emit_result(True, device=target, artifact=str(artifact), method="uf2")
            log_activity("firmware_flash", f"uf2 -> {target}", {"artifact": str(artifact)})
            return
        except Exception as e:
            emit_log(f"[mpftp] UF2 copy failed: {e}")

    if ns.port == "rp2" and shutil.which("picotool"):
        emit_log("[mpftp] no UF2 drive; trying picotool load")
        rc = stream_process(
            ["picotool", "load", "-f", "-x", str(artifact)], Path.cwd(), dict(os.environ)
        )
        emit_result(rc == 0, method="picotool", artifact=str(artifact),
                    error=None if rc == 0 else f"picotool failed (exit {rc})")
        return

    emit_result(
        False,
        error="No UF2 bootloader drive found. Put the board in BOOTSEL/bootloader "
        "mode (double-tap reset) and select its drive, or install picotool (rp2).",
    )


def _looks_like_mount(dev: str) -> bool:
    return "/" in dev or dev.endswith(":") or dev.endswith(":/")


def do_flash(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve()
    port = ns.port
    board = ns.board or ""
    variant = ns.variant or ""
    if port not in FLASHERS:
        emit_result(False, error=f"Flashing not supported for port '{port}'.")
        return
    if ns.artifact:
        artifact = Path(ns.artifact).expanduser()
    else:
        info = artifact_info(mp, port, board, variant)
        if not info["ready"]:
            emit_result(False, error="No build found for this selection. Build first.")
            return
        artifact = Path(info["artifact"])
    if not artifact.is_file():
        emit_result(False, error=f"Artifact not found: {artifact}")
        return

    if port == "esp32":
        if not ns.device:
            emit_result(False, error="No device selected.")
            return
        flash_esp32(ns, mp, artifact)
    elif port in ("rp2", "samd"):
        flash_uf2(ns, artifact)
    else:
        emit_result(False, error=f"No flasher for '{port}'.")
    save_state({"lastDevice": ns.device or ""})


# --------------------------------------------------------------------------- #
# Partitions (esp32)
# --------------------------------------------------------------------------- #

def _sdkconfig_partition_csv(port_dir: Path, board: str, variant: str) -> Optional[Path]:
    """Best-effort resolve the stock partition CSV filename from sdkconfig files."""
    board_dir = port_dir / "boards" / board
    candidates: list[Path] = []
    bdir = build_dir(port_dir, "boards", board, variant)
    if bdir:
        candidates.append(bdir / "sdkconfig")
    for name in ("sdkconfig.board", "sdkconfig.defaults"):
        candidates.append(board_dir / name)
    candidates.append(port_dir / "boards" / "sdkconfig.base")
    for c in candidates:
        try:
            if not c.is_file():
                continue
            for ln in c.read_text("utf-8").splitlines():
                m = re.match(r'\s*CONFIG_PARTITION_TABLE_CUSTOM_FILENAME\s*=\s*"(.+)"', ln)
                if m:
                    csv = m.group(1)
                    p = (port_dir / csv)
                    if p.is_file():
                        return p
                    p2 = (board_dir / csv)
                    if p2.is_file():
                        return p2
        except Exception:
            continue
    # Fallback: any partitions*.csv in the board dir, else the port default.
    for p in sorted(board_dir.glob("partitions*.csv")):
        return p
    default = port_dir / "partitions-4MiBplus.csv"
    return default if default.is_file() else None


def parse_partitions_csv(text: str) -> list[dict]:
    rows: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [c.strip() for c in line.split(",")]
        while len(parts) < 6:
            parts.append("")
        rows.append(
            {
                "name": parts[0],
                "type": parts[1],
                "subtype": parts[2],
                "offset": parts[3],
                "size": parts[4],
                "flags": parts[5],
            }
        )
    return rows


def rows_to_csv(rows: list[dict]) -> str:
    header = "# Name, Type, SubType, Offset, Size, Flags\n"
    out = [header]
    for r in rows:
        out.append(
            ", ".join(
                [
                    str(r.get("name", "")),
                    str(r.get("type", "")),
                    str(r.get("subtype", "")),
                    str(r.get("offset", "")),
                    str(r.get("size", "")),
                    str(r.get("flags", "")),
                ]
            ).rstrip(", ")
        )
    return "\n".join(out) + "\n"


def _parse_size(v: str) -> Optional[int]:
    v = (v or "").strip()
    if not v:
        return None
    try:
        if v.lower().endswith("k"):
            return int(v[:-1], 0) * 1024
        if v.lower().endswith("m"):
            return int(v[:-1], 0) * 1024 * 1024
        return int(v, 0)
    except Exception:
        return None


def validate_partitions(rows: list[dict]) -> list[str]:
    warnings: list[str] = []
    prev_end: Optional[int] = None
    for i, r in enumerate(rows):
        off = _parse_size(r.get("offset", ""))
        size = _parse_size(r.get("size", ""))
        name = r.get("name", f"row{i}")
        if size is None:
            warnings.append(f"{name}: missing/invalid size")
            continue
        if off is not None:
            if prev_end is not None and off < prev_end:
                warnings.append(f"{name}: offset 0x{off:x} overlaps previous end 0x{prev_end:x}")
            if r.get("type") == "app" and off % 0x10000 != 0:
                warnings.append(f"{name}: app offset 0x{off:x} not 64K-aligned")
            prev_end = off + size
        elif prev_end is not None:
            prev_end = prev_end + size
    return warnings


def do_partitions(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve()
    workspace = workspace_of(mp)
    port_dir = mp / "ports" / "esp32"
    board = ns.board or ""
    variant = ns.variant or ""
    override = partition_override_path(workspace, board, variant)

    if ns.action == "get":
        using_override = override.is_file()
        if using_override:
            text = override.read_text("utf-8")
            source = str(override)
        else:
            stock = _sdkconfig_partition_csv(port_dir, board, variant)
            if not stock:
                print_json({"error": "No partition CSV found for this board."})
                return
            text = stock.read_text("utf-8")
            source = str(stock)
        rows = parse_partitions_csv(text)
        print_json(
            {
                "rows": rows,
                "source": source,
                "usingOverride": using_override,
                "overridePath": str(override),
                "warnings": validate_partitions(rows),
            }
        )
        return

    if ns.action == "set":
        if ns.csv_file:
            text = Path(ns.csv_file).read_text("utf-8")
        elif ns.rows:
            rows = json.loads(ns.rows)
            text = rows_to_csv(rows)
        else:
            print_json({"error": "set requires --rows or --csv-file"})
            return
        rows = parse_partitions_csv(text)
        warnings = validate_partitions(rows)
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        log_activity("firmware_partitions", f"set {board}/{variant}", {"path": str(override)})
        print_json({"ok": True, "overridePath": str(override), "warnings": warnings})
        return

    if ns.action == "reset":
        if override.is_file():
            try:
                override.unlink()
            except Exception as e:
                print_json({"error": str(e)})
                return
        print_json({"ok": True, "reset": True})
        return


# --------------------------------------------------------------------------- #
# Discover / dispatch
# --------------------------------------------------------------------------- #

def do_discover(ns: argparse.Namespace) -> None:
    mp = find_micropython(ns.mp)
    workspace = workspace_of(mp) if mp else None
    idf = find_idf(ns.idf, workspace)
    emsdk = find_emsdk(ns.emsdk, workspace)
    result = {
        "host": HOST,
        "micropython": str(mp) if mp else None,
        "workspace": str(workspace) if workspace else None,
        "idf": str(idf) if idf else None,
        "emsdk": str(emsdk) if emsdk else None,
        "state": load_state(),
    }
    if mp:
        save_state({"micropythonPath": str(mp)})
    print_json(result)


def do_tree(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve() if ns.mp else find_micropython(None)
    if not mp or not _is_mp_tree(mp):
        print_json({"error": "MicroPython tree not found", "ports": []})
        return
    print_json(
        {
            "micropython": str(mp),
            "workspace": str(workspace_of(mp)),
            "ports": build_tree(mp),
        }
    )


def do_cmods(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve() if ns.mp else find_micropython(None)
    if not mp:
        print_json({"error": "MicroPython tree not found", "cmods": []})
        return
    print_json(discover_cmods(workspace_of(mp)))


def do_artifact(ns: argparse.Namespace) -> None:
    mp = Path(ns.mp).expanduser().resolve()
    print_json(artifact_info(mp, ns.port, ns.board or "", ns.variant or ""))


def do_flashers(_ns: argparse.Namespace) -> None:
    print_json({"flashers": FLASHERS})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="firmware_engine", description="mpftp firmware engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_mp(sp: argparse.ArgumentParser, required: bool = False) -> None:
        sp.add_argument("--mp", default=None, required=required, help="MicroPython tree path")
        sp.add_argument("--idf", default=None, help="ESP-IDF path")
        sp.add_argument("--emsdk", default=None, help="emsdk path")

    d = sub.add_parser("discover")
    add_mp(d)
    d.set_defaults(func=do_discover)

    t = sub.add_parser("tree")
    add_mp(t)
    t.set_defaults(func=do_tree)

    c = sub.add_parser("cmods")
    add_mp(c)
    c.set_defaults(func=do_cmods)

    a = sub.add_parser("artifact")
    add_mp(a, required=True)
    a.add_argument("--port", required=True)
    a.add_argument("--board", default="")
    a.add_argument("--variant", default="")
    a.set_defaults(func=do_artifact)

    b = sub.add_parser("build")
    add_mp(b, required=True)
    b.add_argument("--port", required=True)
    b.add_argument("--board", default="")
    b.add_argument("--variant", default="")
    b.add_argument("--clean", action="store_true")
    b.add_argument("--jobs", type=int, default=0)
    b.set_defaults(func=do_build)

    cl = sub.add_parser("clean")
    add_mp(cl, required=True)
    cl.add_argument("--port", required=True)
    cl.add_argument("--board", default="")
    cl.add_argument("--variant", default="")
    cl.set_defaults(func=do_clean)

    f = sub.add_parser("flash")
    add_mp(f, required=True)
    f.add_argument("--port", required=True)
    f.add_argument("--board", default="")
    f.add_argument("--variant", default="")
    f.add_argument("--device", default="")
    f.add_argument("--artifact", default="")
    f.add_argument("--baud", type=int, default=460800)
    f.add_argument("--erase", action="store_true")
    f.add_argument("--esptool", default=None, help="esptool interpreter/executable")
    f.set_defaults(func=do_flash)

    fl = sub.add_parser("flashers")
    fl.set_defaults(func=do_flashers)

    pt = sub.add_parser("partitions")
    add_mp(pt, required=True)
    pt.add_argument("action", choices=["get", "set", "reset"])
    pt.add_argument("--board", default="")
    pt.add_argument("--variant", default="")
    pt.add_argument("--rows", default=None, help="JSON array of partition rows (set)")
    pt.add_argument("--csv-file", dest="csv_file", default=None, help="CSV file to import (set)")
    pt.set_defaults(func=do_partitions)

    return p


def main(argv: Optional[list[str]] = None) -> None:
    ns = build_parser().parse_args(argv)
    try:
        ns.func(ns)
    except BrokenPipeError:
        pass
    except Exception as e:  # noqa: BLE001
        if ns.cmd in ("build", "clean", "flash"):
            emit_result(False, error=str(e))
        else:
            print_json({"error": str(e)})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
