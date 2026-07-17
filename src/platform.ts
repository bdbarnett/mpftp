import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { execFileSync } from "child_process";
import * as vscode from "vscode";

export type HostKind = "wsl" | "linux" | "windows";

export function detectHost(): HostKind {
  if (process.platform === "win32") {
    return "windows";
  }
  if (process.platform === "linux") {
    try {
      const v = fs.readFileSync("/proc/version", "utf8").toLowerCase();
      if (v.includes("microsoft") || v.includes("wsl")) {
        return "wsl";
      }
    } catch {
      /* ignore */
    }
    return "linux";
  }
  return "linux";
}

function existsFile(p: string): boolean {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

function which(cmd: string): string | undefined {
  try {
    const out = execFileSync(process.platform === "win32" ? "where" : "which", [cmd], {
      encoding: "utf8",
      timeout: 3000,
    })
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean)[0];
    return out;
  } catch {
    return undefined;
  }
}

/** Resolve a Python that can import mpremote and open the host's serial ports. */
export function resolvePython(extensionPath: string, configured?: string): string {
  if (configured && configured.trim()) {
    return configured.trim();
  }

  const host = detectHost();
  const candidates: string[] = [];

  if (host === "wsl") {
    // Windows Python sees COM ports; WSL Python typically does not.
    const winCandidates = [
      which("python.exe"),
      path.join(os.homedir(), "bin", "python.exe"),
      ...discoverWindowsPythons(),
      "/mnt/c/Windows/py.exe",
    ].filter(Boolean) as string[];
    candidates.push(...winCandidates);
    // Fall back to extension venv (Linux serial / usbipd devices).
    candidates.push(path.join(extensionPath, ".venv", "bin", "python"));
    candidates.push(which("python3") || "", which("python") || "");
  } else if (host === "windows") {
    candidates.push(
      which("python.exe") || "",
      which("python") || "",
      which("py") || "",
      path.join(extensionPath, ".venv", "Scripts", "python.exe")
    );
  } else {
    candidates.push(
      path.join(extensionPath, ".venv", "bin", "python"),
      which("python3") || "",
      which("python") || ""
    );
  }

  for (const c of candidates) {
    if (c && existsFile(c)) {
      if (canImportMpremote(c)) {
        return c;
      }
    }
  }

  // Last resort: return best guess even if import check failed (user may fix env).
  for (const c of candidates) {
    if (c && existsFile(c)) {
      return c;
    }
  }
  return host === "windows" ? "python" : "python3";
}

function discoverWindowsPythons(): string[] {
  const found: string[] = [];
  const usersDir = "/mnt/c/Users";
  try {
    for (const user of fs.readdirSync(usersDir)) {
      if (user === "Public" || user === "Default" || user.startsWith(".")) {
        continue;
      }
      const bases = [
        path.join(usersDir, user, "AppData", "Local", "Programs", "Python"),
        path.join(usersDir, user, "AppData", "Roaming", "Python"),
      ];
      for (const base of bases) {
        if (!fs.existsSync(base)) {
          continue;
        }
        try {
          for (const ent of fs.readdirSync(base)) {
            const py = path.join(base, ent, "python.exe");
            if (existsFile(py)) {
              found.push(py);
            }
            // Roaming layout: Python314/site-packages — interpreter is under Local
          }
        } catch {
          /* ignore */
        }
      }
    }
  } catch {
    /* ignore */
  }
  // Prefer newer version-looking paths last → reverse sort by name
  return found.sort().reverse();
}

function canImportMpremote(python: string): boolean {
  try {
    execFileSync(python, ["-c", "import mpremote, serial; print('ok')"], {
      encoding: "utf8",
      timeout: 15000,
      stdio: ["ignore", "pipe", "pipe"],
    });
    return true;
  } catch {
    return false;
  }
}

/** True when `python` is a Windows interpreter (needs Windows-style paths on WSL). */
export function isWindowsPython(python: string): boolean {
  const p = python.toLowerCase();
  return p.endsWith(".exe") || p.includes("/mnt/c/") || /^[a-z]:\\/.test(python);
}

/**
 * Convert a WSL/Linux path into a form Windows python.exe can open.
 * Without this, Node spawn often passes `/home/...` and Windows Python
 * mis-resolves it as `C:\home\...` (ENOENT).
 */
export function pathForPythonProcess(python: string, filePath: string): string {
  if (detectHost() !== "wsl" || !isWindowsPython(python)) {
    return filePath;
  }
  if (/^[a-zA-Z]:[\\/]/.test(filePath) || filePath.startsWith("\\\\")) {
    return filePath;
  }
  try {
    const win = execFileSync("wslpath", ["-w", filePath], {
      encoding: "utf8",
      timeout: 3000,
    }).trim();
    if (win) {
      return win;
    }
  } catch {
    /* fall through */
  }
  const distro = process.env.WSL_DISTRO_NAME || "Ubuntu";
  return `\\\\wsl.localhost\\${distro}${filePath.replace(/\//g, "\\")}`;
}

export function resolveMpremoteCli(configured?: string): string | undefined {
  if (configured && configured.trim()) {
    return configured.trim();
  }
  const host = detectHost();
  if (host === "wsl") {
    return (
      which("mpremote.exe") ||
      "/mnt/c/Users/bradb/AppData/Roaming/Python/Python314/Scripts/mpremote.exe" ||
      which("mpremote")
    );
  }
  if (host === "windows") {
    return which("mpremote.exe") || which("mpremote");
  }
  return which("mpremote");
}

export function getConfig() {
  const cfg = vscode.workspace.getConfiguration("mpftp");
  return {
    pythonPath: cfg.get<string>("pythonPath") || "",
    mpremotePath: cfg.get<string>("mpremotePath") || "",
    defaultBaud: cfg.get<number>("defaultBaud") || 115200,
    autoConnectDevice: cfg.get<string>("autoConnectDevice") || "",
  };
}
