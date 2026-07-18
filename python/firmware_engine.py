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


def _patch_sdkconfig_partition(
    bdir: Path, override_csv: Path, flash_mb: Optional[int] = None
) -> None:
    """Point the esp32 build's sdkconfig at an absolute custom partition CSV.

    Optionally set ``CONFIG_ESPTOOLPY_FLASHSIZE_*`` (from Detect/autoset) and
    append a companion ``.sdkconfig`` fragment saved beside the CSV. Only the
    build-dir sdkconfig is touched — never files under ``ports/**``.
    """
    sdk = bdir / "sdkconfig"
    try:
        lines = sdk.read_text("utf-8").splitlines() if sdk.is_file() else []
    except Exception:
        lines = []
    drop = ["CONFIG_PARTITION_TABLE_CUSTOM", "CONFIG_PARTITION_TABLE_FILENAME"]
    if flash_mb:
        drop.append("CONFIG_ESPTOOLPY_FLASHSIZE")
    filtered = [ln for ln in lines if not ln.startswith(tuple(drop))]
    filtered.append("CONFIG_PARTITION_TABLE_CUSTOM=y")
    filtered.append(f'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="{override_csv}"')
    filtered.append(f'CONFIG_PARTITION_TABLE_FILENAME="{override_csv}"')
    if flash_mb:
        filtered.append(f'CONFIG_ESPTOOLPY_FLASHSIZE="{flash_mb}MB"')
        filtered.append(f"CONFIG_ESPTOOLPY_FLASHSIZE_{flash_mb}MB=y")
    frag = override_csv.with_suffix(".sdkconfig")
    try:
        if frag.is_file():
            for ln in frag.read_text("utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    filtered.append(ln)
    except Exception:
        pass
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


def _table_target_size(rows: list[dict]) -> int:
    """Total flash consumed by a partition table (max offset+size)."""
    end = 0
    cursor = 0
    for r in rows:
        off = _parse_size(r.get("offset", ""))
        size = _parse_size(r.get("size", "")) or 0
        start = off if off is not None else cursor
        end = max(end, start + size)
        cursor = start + size
    return end


# Data partitions that hold the user filesystem ("storage").
_STORAGE_SUBTYPES = {"fat", "spiffs", "littlefs"}
_STORAGE_NAMES = {"vfs", "storage", "ffat", "user"}


def _is_storage_row(r: dict) -> bool:
    return r.get("type") == "data" and (
        r.get("subtype", "") in _STORAGE_SUBTYPES
        or r.get("name", "").lower() in _STORAGE_NAMES
    )


def _fmt_hex(n: int) -> str:
    return hex(int(n))


def _reflow_offsets(rows: list[dict], from_idx: int) -> None:
    """Recompute contiguous offsets for rows at/after ``from_idx`` (keep sizes)."""
    if from_idx <= 0 or from_idx > len(rows):
        return
    prev = rows[from_idx - 1]
    prev_off = _parse_size(prev.get("offset", ""))
    prev_size = _parse_size(prev.get("size", "")) or 0
    cursor = (prev_off or 0) + prev_size
    for r in rows[from_idx:]:
        size = _parse_size(r.get("size", "")) or 0
        r["offset"] = _fmt_hex(cursor)
        cursor += size


def compute_split(
    rows: list[dict], storage_bytes: int, flash_bytes: Optional[int] = None
) -> tuple[list[dict], list[str]]:
    """Resize (or create) the storage partition to ``storage_bytes``.

    Grows/shrinks an existing vfs/fat/littlefs partition; if the table has only
    firmware (e.g. P4 factory-only), appends a trailing storage partition.
    Returns (rows, warnings). Sizes are aligned down to 4 KB.
    """
    rows = [dict(r) for r in rows]
    warnings: list[str] = []
    storage_bytes = max(0, int(storage_bytes) & ~0xFFF)
    idx = next((i for i, r in enumerate(rows) if _is_storage_row(r)), None)

    if idx is None:
        end = _table_target_size(rows)
        start = (end + 0xFFFF) & ~0xFFFF  # 64 KB align
        if storage_bytes <= 0 and flash_bytes:
            storage_bytes = (flash_bytes - start) & ~0xFFF
        rows.append(
            {
                "name": "vfs",
                "type": "data",
                "subtype": "fat",
                "offset": _fmt_hex(start),
                "size": _fmt_hex(storage_bytes),
                "flags": "",
            }
        )
        idx = len(rows) - 1
        warnings.append("No storage partition in the stock table; added a trailing vfs.")
    else:
        rows[idx]["size"] = _fmt_hex(storage_bytes)

    _reflow_offsets(rows, idx)

    if flash_bytes:
        total = _table_target_size(rows)
        if total > flash_bytes:
            warnings.append(
                f"Table end 0x{total:x} exceeds flash 0x{flash_bytes:x}; reduce storage."
            )
    return rows, warnings


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

    if ns.action == "split":
        # Base table: current override if present, else stock.
        if override.is_file():
            base_text = override.read_text("utf-8")
        else:
            stock = _sdkconfig_partition_csv(port_dir, board, variant)
            if not stock:
                print_json({"error": "No partition CSV found for this board."})
                return
            base_text = stock.read_text("utf-8")
        base_rows = parse_partitions_csv(base_text)
        storage_bytes = int(ns.storage_bytes or 0)
        flash_bytes = int(ns.flash_bytes) if ns.flash_bytes else None
        rows, warnings = compute_split(base_rows, storage_bytes, flash_bytes)
        warnings += validate_partitions(rows)
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(rows_to_csv(rows), encoding="utf-8")
        # Companion sdkconfig fragment carries the flash size for the build.
        if ns.flash_mb:
            frag = override.with_suffix(".sdkconfig")
            frag.write_text(
                f'CONFIG_ESPTOOLPY_FLASHSIZE="{ns.flash_mb}MB"\n'
                f"CONFIG_ESPTOOLPY_FLASHSIZE_{ns.flash_mb}MB=y\n",
                encoding="utf-8",
            )
        log_activity("firmware_partitions", f"split {board}/{variant}", {"path": str(override)})
        print_json(
            {
                "ok": True,
                "rows": rows,
                "overridePath": str(override),
                "warnings": warnings,
            }
        )
        return

    if ns.action == "candidates":
        stock = _sdkconfig_partition_csv(port_dir, board, variant)
        board_dir = port_dir / "boards" / board
        candidates: list[dict] = []
        if override.is_file():
            try:
                rows = parse_partitions_csv(override.read_text("utf-8"))
                candidates.append(
                    {
                        "label": "Current override",
                        "path": str(override),
                        "rows": rows,
                        "targetSize": _table_target_size(rows),
                        "isOverride": True,
                        "isStock": False,
                    }
                )
            except Exception:
                pass
        seen: set[str] = set()
        globbed: list[Path] = []
        if stock:
            globbed.append(stock)
        globbed += sorted(board_dir.glob("partitions*.csv"))
        globbed += sorted(port_dir.glob("partitions*.csv"))
        for p in globbed:
            rp = str(p)
            if rp in seen:
                continue
            seen.add(rp)
            try:
                rows = parse_partitions_csv(p.read_text("utf-8"))
            except Exception:
                continue
            candidates.append(
                {
                    "label": p.name,
                    "path": rp,
                    "rows": rows,
                    "targetSize": _table_target_size(rows),
                    "isOverride": False,
                    "isStock": bool(stock and p == stock),
                }
            )
        print_json(
            {
                "candidates": candidates,
                "overridePath": str(override),
                "usingOverride": override.is_file(),
            }
        )
        return


# --------------------------------------------------------------------------- #
# Detect (esptool-first chip / flash / security probe)
# --------------------------------------------------------------------------- #

# Generic MicroPython board name per ESP32 family code.
ESP_FAMILIES = {
    "S2": "ESP32_GENERIC_S2",
    "S3": "ESP32_GENERIC_S3",
    "C2": "ESP32_GENERIC_C2",
    "C3": "ESP32_GENERIC_C3",
    "C5": "ESP32_GENERIC_C5",
    "C6": "ESP32_GENERIC_C6",
    "H2": "ESP32_GENERIC_H2",
    "P4": "ESP32_GENERIC_P4",
}

# Static per-family facts esptool does not report (SRAM etc.). "" == classic ESP32.
CHIP_SPECS = {
    "":   {"label": "ESP32",    "sramKb": 520, "cores": 2, "lp": False, "maxMhz": 240},
    "S2": {"label": "ESP32-S2", "sramKb": 320, "cores": 1, "lp": False, "maxMhz": 240},
    "S3": {"label": "ESP32-S3", "sramKb": 512, "cores": 2, "lp": False, "maxMhz": 240},
    "C2": {"label": "ESP32-C2", "sramKb": 272, "cores": 1, "lp": False, "maxMhz": 120},
    "C3": {"label": "ESP32-C3", "sramKb": 400, "cores": 1, "lp": False, "maxMhz": 160},
    "C5": {"label": "ESP32-C5", "sramKb": 384, "cores": 1, "lp": True,  "maxMhz": 240},
    "C6": {"label": "ESP32-C6", "sramKb": 512, "cores": 1, "lp": True,  "maxMhz": 160},
    "H2": {"label": "ESP32-H2", "sramKb": 320, "cores": 1, "lp": False, "maxMhz": 96},
    "P4": {"label": "ESP32-P4", "sramKb": 768, "cores": 2, "lp": True,  "maxMhz": 400},
}


def family_from_chip(chip: str) -> str:
    """Family code (S3/C6/P4/...) from an esptool chip name; '' for classic ESP32."""
    m = re.search(r"ESP32-?(S2|S3|C2|C3|C5|C6|H2|P4)\b", (chip or "").upper())
    return m.group(1) if m else ""


def family_from_text(text: str) -> str:
    """Family code from a MicroPython machine/uname string; '' for classic ESP32."""
    m = re.search(r"ESP32-?(S2|S3|C2|C3|C5|C6|H2|P4)", (text or "").upper())
    return m.group(1) if m else ""


def parse_esptool_flash_id(text: str) -> dict:
    """Parse ``esptool flash_id`` output (works with or without MicroPython)."""
    out: dict = {
        "chip": "",
        "revision": "",
        "features": [],
        "cores": None,
        "lpCore": False,
        "maxMhz": None,
        "crystalMhz": None,
        "mac": "",
        "flashMb": None,
        "psram": {"present": False, "octal": False, "label": ""},
        "usbMode": "",
    }
    m = re.search(r"Chip is (.+?)(?: \(QFN\d+\))? \(revision (v?[^)]+)\)", text)
    if m:
        out["chip"] = m.group(1).strip()
        out["revision"] = m.group(2).strip()
    else:
        m2 = re.search(r"Chip is (ESP32\S*)", text)
        if m2:
            out["chip"] = m2.group(1).strip()
    fm = re.search(r"Features:\s*(.+)", text)
    if fm:
        feats = [f.strip() for f in fm.group(1).split(",") if f.strip()]
        out["features"] = feats
        for f in feats:
            low = f.lower()
            if "single core" in low:
                out["cores"] = 1
            elif "dual core" in low:
                out["cores"] = 2
            if "lp core" in low:
                out["lpCore"] = True
            mh = re.search(r"(\d+)\s*MHz", f)
            if mh and out["maxMhz"] is None:
                out["maxMhz"] = int(mh.group(1))
            if "psram" in low:
                out["psram"]["present"] = True
                out["psram"]["label"] = f
                if "octal" in low:
                    out["psram"]["octal"] = True
    cm = re.search(r"Crystal is (\d+)\s*MHz", text)
    if cm:
        out["crystalMhz"] = int(cm.group(1))
    mac = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", text)
    if mac:
        out["mac"] = mac.group(1).lower()
    fl = re.search(r"[Ff]lash size:\s*(\d+)\s*MB", text)
    if fl:
        out["flashMb"] = int(fl.group(1))
    if "USB-Serial/JTAG" in text:
        out["usbMode"] = "USB-Serial/JTAG"
    return out


def parse_esptool_security(text: str) -> dict:
    """Parse ``esptool get_security_info``; tolerate 'not implemented' chips."""
    out = {"available": False, "secureBoot": "", "flashEncryption": ""}
    sb = re.search(r"Secure Boot:\s*(\w+)", text)
    fe = re.search(r"Flash Encryption:\s*(\w+)", text)
    if sb:
        out["secureBoot"] = sb.group(1)
        out["available"] = True
    if fe:
        out["flashEncryption"] = fe.group(1)
        out["available"] = True
    return out


def esp32_board_variants(tree: Optional[list], board: str) -> Optional[list]:
    """Variants for an esp32 board in the tree, or None if the board is absent."""
    for node in tree or []:
        if node.get("port") == "esp32":
            for b in node.get("boards", []):
                if b.get("board") == board:
                    return b.get("variants", [])
    return None


def match_esp_target(
    family: str, psram: dict, flash_mb: Optional[int], mp_hints: dict, tree: Optional[list]
) -> dict:
    """Suggest board / variant / flash-size for an ESP32 family (fixture rules)."""
    notes: list[str] = []
    generic = ESP_FAMILIES.get(family, "ESP32_GENERIC")
    variants = esp32_board_variants(tree, generic)
    matched = variants is not None
    avail = variants or []

    def has(v: str) -> bool:
        return v in avail

    build = str(mp_hints.get("build") or "").upper()
    machine = str(mp_hints.get("machine") or "")
    memfree = mp_hints.get("memfree") or 0
    variant = ""
    variant_options: list[str] = []

    if family == "P4":
        # External Wi-Fi co-processor (C5/C6) is invisible to esptool: MP only.
        for w in ("C6_WIFI", "C5_WIFI"):
            if w in build or w in machine.upper():
                if has(w):
                    variant = w
                    break
        variant_options = [v for v in avail if v.upper().endswith("WIFI")]
        if not variant and variant_options:
            notes.append(
                "P4 external Wi-Fi (C5/C6) cannot be detected from esptool alone — "
                "pick the variant if your board has an external radio."
            )
    else:
        if psram.get("octal") or "octal-spiram" in machine.lower():
            variant = "SPIRAM_OCT" if has("SPIRAM_OCT") else ("SPIRAM" if has("SPIRAM") else "")
        elif psram.get("present"):
            variant = "SPIRAM" if has("SPIRAM") else ("SPIRAM_OCT" if has("SPIRAM_OCT") else "")
        elif isinstance(memfree, (int, float)) and memfree > 1_000_000:
            if has("SPIRAM"):
                variant = "SPIRAM"
                notes.append("Large MicroPython heap suggests PSRAM; selected SPIRAM.")
        variant_options = [v for v in avail if "SPIRAM" in v]

    confidence = "matched"
    if not matched:
        confidence = "family-only"
        notes.append(
            f"{generic} not found in this MicroPython tree; using the family default."
        )

    return {
        "port": "esp32",
        "board": generic,
        "variant": variant,
        "variantOptions": variant_options,
        "flashSize": f"{flash_mb}MB" if flash_mb else "",
        "flashConfig": f"CONFIG_ESPTOOLPY_FLASHSIZE_{flash_mb}MB" if flash_mb else "",
        "confidence": confidence,
        "notes": notes,
    }


def _mp_indicates_esp(h: dict) -> bool:
    plat = str(h.get("platform") or "").lower()
    machine = str(h.get("machine") or "").lower()
    return plat in ("esp32", "espressif") or "esp32" in machine


def suggested_port_from_mp(h: dict) -> str:
    """Best firmware port for a non-Espressif board from MicroPython/CP hints."""
    low = str(h.get("platform") or "").lower()
    machine = str(h.get("machine") or "").lower()
    mapping = {
        "rp2": "rp2",
        "rp2040": "rp2",
        "pyboard": "stm32",
        "mimxrt": "mimxrt",
        "samd": "samd",
        "nrf52": "nrf",
        "nrf52840": "nrf",
        "esp32": "esp32",
        "espressif": "esp32",
    }
    if low in mapping:
        return mapping[low]
    if "rp2" in low or "rp2040" in machine:
        return "rp2"
    if "nrf" in low or "nrf" in machine:
        return "nrf"
    if "samd" in low or "samd" in machine:
        return "samd"
    if "imxrt" in low or "imxrt" in machine:
        return "mimxrt"
    if "stm32" in machine or low == "pyboard":
        return "stm32"
    if "espressif" in low or "esp32" in machine:
        return "esp32"
    return ""


def _esptool_fail_reason(out: str, rc: int) -> str:
    for key in (
        "No serial data received",
        "Invalid head of packet",
        "Failed to connect",
        "could not open",
        "Timed out",
    ):
        if key.lower() in (out or "").lower():
            return f"esptool: {key}"
    if rc == 127:
        return "esptool not available"
    return "Not an Espressif chip (esptool did not detect an ESP)"


def _esptool_capture(ns: argparse.Namespace, sub_args: list[str], timeout: int = 60):
    """Run one esptool subcommand, returning (rc, combined_output)."""
    cmd = _esptool_cmd(ns) + ["-p", ns.device] + sub_args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"esptool timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, f"esptool not found: {e}"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def do_detect(ns: argparse.Namespace) -> None:
    device = ns.device
    if not device:
        print_json({"ok": False, "error": "No device specified for detect."})
        return

    mp_hints: dict = {}
    if ns.mp_hints:
        try:
            mp_hints = json.loads(ns.mp_hints)
        except Exception:
            mp_hints = {}

    tree = None
    if ns.mp:
        mp = Path(ns.mp).expanduser().resolve()
        if _is_mp_tree(mp):
            tree = build_tree(mp)

    rc, fout = _esptool_capture(ns, ["flash_id"])
    flash = parse_esptool_flash_id(fout)
    sec = {"available": False, "secureBoot": "", "flashEncryption": ""}
    if flash.get("chip"):
        _src, srout = _esptool_capture(ns, ["get_security_info"])
        sec = parse_esptool_security(srout)

    esp_from_mp = _mp_indicates_esp(mp_hints)
    espressif = bool(flash.get("chip")) or esp_from_mp
    if not espressif:
        suggested = suggested_port_from_mp(mp_hints)
        print_json(
            {
                "ok": True,
                "espressif": False,
                "device": device,
                "reason": _esptool_fail_reason(fout, rc),
                "suggestedPort": suggested,
                "mp": mp_hints,
                "match": {
                    "port": suggested,
                    "confidence": "family-only" if suggested else "unknown",
                    "notes": [],
                },
            }
        )
        return

    if flash.get("chip"):
        family = family_from_chip(flash["chip"])
    else:
        family = family_from_text(
            str(mp_hints.get("machine") or mp_hints.get("platform") or "")
        )

    spec = CHIP_SPECS.get(family or "", CHIP_SPECS[""])
    flash_mb = flash.get("flashMb")
    if not flash_mb and mp_hints.get("flash"):
        try:
            flash_mb = int(int(mp_hints["flash"]) / (1024 * 1024))
        except Exception:
            flash_mb = None
    match = match_esp_target(family or "", flash.get("psram", {}), flash_mb, mp_hints, tree)

    result = {
        "ok": True,
        "espressif": True,
        "device": device,
        "esptoolFailed": not flash.get("chip"),
        "chip": flash.get("chip") or spec["label"],
        "revision": flash.get("revision", ""),
        "features": flash.get("features", []),
        "cores": flash.get("cores") or spec["cores"],
        "lpCore": bool(flash.get("lpCore") or spec["lp"]),
        "maxMhz": flash.get("maxMhz") or spec["maxMhz"],
        "crystalMhz": flash.get("crystalMhz"),
        "mac": flash.get("mac", ""),
        "flashMb": flash_mb,
        "psram": flash.get("psram", {"present": False, "octal": False, "label": ""}),
        "sramKb": spec["sramKb"],
        "security": sec,
        "match": match,
        "mp": mp_hints,
    }
    save_state(
        {
            "lastDetect": {
                "device": device,
                "chip": result["chip"],
                "secureBoot": sec.get("secureBoot", ""),
                "flashEncryption": sec.get("flashEncryption", ""),
            }
        }
    )
    log_activity(
        "firmware_detect",
        f"{result['chip']} on {device}",
        {"board": match.get("board"), "flashMb": flash_mb},
    )
    print_json(result)


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

    dt = sub.add_parser("detect")
    add_mp(dt)
    dt.add_argument("--device", required=True)
    dt.add_argument("--baud", type=int, default=460800)
    dt.add_argument("--esptool", default=None, help="esptool interpreter/executable")
    dt.add_argument("--mp-hints", dest="mp_hints", default=None,
                    help="JSON of MicroPython runtime hints (optional enrichment)")
    dt.set_defaults(func=do_detect)

    pt = sub.add_parser("partitions")
    add_mp(pt, required=True)
    pt.add_argument("action", choices=["get", "set", "reset", "candidates", "split"])
    pt.add_argument("--board", default="")
    pt.add_argument("--variant", default="")
    pt.add_argument("--rows", default=None, help="JSON array of partition rows (set)")
    pt.add_argument("--csv-file", dest="csv_file", default=None, help="CSV file to import (set)")
    pt.add_argument("--storage-bytes", dest="storage_bytes", type=int, default=0,
                    help="storage partition size in bytes (split)")
    pt.add_argument("--flash-bytes", dest="flash_bytes", type=int, default=0,
                    help="total flash in bytes (split, for validation)")
    pt.add_argument("--flash-mb", dest="flash_mb", type=int, default=0,
                    help="flash size in MB for the sdkconfig fragment (split)")
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
