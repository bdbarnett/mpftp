import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import * as path from "path";
import { EventEmitter } from "events";
import * as vscode from "vscode";
import { ActivityLog, summarizeParams } from "../activityLog";
import {
  GS_LAST_DEVICE,
  GS_LAST_VIDPID,
  getConfig,
  pathForPythonProcess,
  portVidPidKey,
  resolvePython,
} from "../platform";

export type PortInfo = {
  device: string;
  serial_number?: string | null;
  vid?: number | null;
  pid?: number | null;
  manufacturer?: string | null;
  product?: string | null;
  description?: string | null;
  interface?: string | null;
  hwid?: string | null;
  /** false for CircuitPython CDC2 (data) interfaces — not for REPL connect. */
  repl?: boolean;
};

export type DirEntry = {
  name: string;
  isDir: boolean;
  size: number;
  mode?: number;
};

type Pending = {
  resolve: (v: unknown) => void;
  reject: (e: Error) => void;
};

/**
 * Long-lived JSON-line bridge to python/sidecar.py (mpremote-backed).
 */
export class SidecarBridge extends EventEmitter {
  private proc: ChildProcessWithoutNullStreams | undefined;
  private buf = "";
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private starting: Promise<void> | undefined;
  private _connectedDevice: string | undefined;
  private _lastDevice: string | undefined;

  constructor(
    private readonly extensionPath: string,
    private readonly log: vscode.OutputChannel,
    private readonly activity?: ActivityLog,
    private readonly globalState?: vscode.Memento
  ) {
    super();
  }

  get connectedDevice(): string | undefined {
    return this._connectedDevice;
  }

  get lastDevice(): string | undefined {
    return this._lastDevice || this._connectedDevice;
  }

  get connected(): boolean {
    return !!this._connectedDevice;
  }

  /** Seed in-memory last device from globalState (after window reload). */
  seedLastDeviceFromGlobalState(): void {
    const saved = this.globalState?.get<string>(GS_LAST_DEVICE);
    if (saved && !this._lastDevice) {
      this._lastDevice = saved;
    }
  }

  get rememberedVidPid(): string | undefined {
    return this.globalState?.get<string>(GS_LAST_VIDPID);
  }

  private async persistLastDevice(device: string): Promise<void> {
    if (!this.globalState) {
      return;
    }
    await this.globalState.update(GS_LAST_DEVICE, device);
    try {
      const ports = await this.listPorts();
      const match = ports.find((p) => p.device === device);
      const key = match ? portVidPidKey(match) : undefined;
      if (key) {
        await this.globalState.update(GS_LAST_VIDPID, key);
      }
    } catch {
      /* port list optional for persistence */
    }
  }

  async ensureStarted(): Promise<void> {
    if (this.proc && !this.proc.killed) {
      return;
    }
    if (this.starting) {
      return this.starting;
    }
    this.starting = this.start().finally(() => {
      this.starting = undefined;
    });
    return this.starting;
  }

  private async start(): Promise<void> {
    // Drop any prior child handle before spawn. The new sidecar.py claims
    // ~/.mpftp/sidecar.pid and force-kills orphans that still hold COM ports.
    if (this.proc && !this.proc.killed) {
      await this.killSidecarProcess(this.proc.pid);
      this.proc = undefined;
    }

    const cfg = getConfig();
    const python = resolvePython(this.extensionPath, cfg.pythonPath);
    const scriptLinux = path.join(this.extensionPath, "python", "sidecar.py");
    const script = pathForPythonProcess(python, scriptLinux);
    this.log.appendLine(`[mpftp] starting sidecar: ${python} ${script}`);
    this.activity?.event("sidecar_start", {
      message: `${python} ${script}`,
      data: { python, script },
    });

    this.proc = spawn(python, [script], {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      windowsHide: true,
    });

    this.proc.stdout.setEncoding("utf8");
    this.proc.stderr.setEncoding("utf8");

    this.proc.stdout.on("data", (chunk: string) => this.onStdout(chunk));
    this.proc.stderr.on("data", (chunk: string) => {
      this.log.appendLine(`[sidecar:err] ${chunk.trimEnd()}`);
      this.activity?.event("sidecar_stderr", {
        source: "sidecar",
        message: chunk.trimEnd(),
      });
    });
    this.proc.on("exit", (code, signal) => {
      this.log.appendLine(`[mpftp] sidecar exited code=${code} signal=${signal}`);
      this.activity?.event("sidecar_exit", {
        message: `code=${code} signal=${signal}`,
        data: { code, signal },
      });
      this.proc = undefined;
      this._connectedDevice = undefined;
      for (const [, p] of this.pending) {
        p.reject(new Error("sidecar exited"));
      }
      this.pending.clear();
      this.emit("exit");
      void vscode.commands.executeCommand("setContext", "mpftp.connected", false);
    });

    await new Promise<void>((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("sidecar ready timeout")), 20000);
      const onReady = () => {
        clearTimeout(t);
        this.off("ready", onReady);
        resolve();
      };
      this.on("ready", onReady);
    });

    await this.request("ping");
    this.activity?.event("sidecar_ready", { message: "sidecar ready" });
  }

  private onStdout(chunk: string): void {
    this.buf += chunk;
    let idx: number;
    while ((idx = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, idx).trim();
      this.buf = this.buf.slice(idx + 1);
      if (!line) {
        continue;
      }
      let msg: any;
      try {
        msg = JSON.parse(line);
      } catch {
        this.log.appendLine(`[sidecar:bad] ${line}`);
        continue;
      }
      if (msg.type === "notify") {
        if (msg.method === "ready") {
          this.emit("ready", msg.params);
        } else if (msg.method === "repl_data") {
          this.emit("repl_data", msg.params);
        } else if (msg.method === "repl_error") {
          this.emit("repl_error", msg.params);
          this.activity?.event("repl_error", {
            source: "repl",
            message: msg.params?.message,
          });
        }
        continue;
      }
      if (msg.type === "result") {
        const p = this.pending.get(msg.id);
        if (p) {
          this.pending.delete(msg.id);
          p.resolve(msg.result);
        }
        continue;
      }
      if (msg.type === "error") {
        const p = this.pending.get(msg.id);
        if (p) {
          this.pending.delete(msg.id);
          p.reject(new Error(msg.error || "sidecar error"));
        }
        continue;
      }
    }
  }

  async request<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    await this.ensureStarted();
    if (!this.proc) {
      throw new Error("sidecar not running");
    }
    const id = this.nextId++;
    const payload = JSON.stringify({ id, method, params }) + "\n";
    this.activity?.event("rpc", {
      message: method,
      data: summarizeParams(method, params),
    });
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (v) => {
          this.activity?.event("rpc_ok", { message: method, data: { method } });
          resolve(v as T);
        },
        reject: (e) => {
          this.activity?.event("rpc_err", {
            message: `${method}: ${e.message}`,
            data: { method, error: e.message },
          });
          reject(e);
        },
      });
      this.proc!.stdin.write(payload, (err) => {
        if (err) {
          this.pending.delete(id);
          reject(err);
        }
      });
    });
  }

  async listPorts(): Promise<PortInfo[]> {
    return this.request<PortInfo[]>("list_ports");
  }

  async connect(
    device: string,
    baud?: number,
    opts?: { silent?: boolean }
  ): Promise<void> {
    const cfg = getConfig();
    await this.request("connect", { device, baud: baud ?? cfg.defaultBaud });
    this._connectedDevice = device;
    this._lastDevice = device;
    await this.persistLastDevice(device);
    await vscode.commands.executeCommand("setContext", "mpftp.connected", true);
    this.activity?.event("connected", { message: device, data: { device } });
    // `silent` reconnects (e.g. after detect/flash) restore the link without
    // firing user-facing side effects such as auto-opening File Transfer.
    this.emit("connected", device, { silent: !!opts?.silent });
  }

  /** Reconnect to the last device (after hard-reset / port flicker / reload). */
  async resume(baud?: number): Promise<void> {
    // After a window reload the sidecar is fresh (no last_device); use
    // in-memory / globalState last device and connect directly.
    if (!this._connectedDevice && this._lastDevice) {
      await this.connect(this._lastDevice, baud);
      return;
    }
    const cfg = getConfig();
    const res = await this.request<{ device: string }>("resume", {
      baud: baud ?? cfg.defaultBaud,
    });
    const device = res.device || this._lastDevice;
    if (!device) {
      throw new Error("resume: no device");
    }
    this._connectedDevice = device;
    this._lastDevice = device;
    await this.persistLastDevice(device);
    await vscode.commands.executeCommand("setContext", "mpftp.connected", true);
    this.activity?.event("connected", { message: `resume ${device}`, data: { device } });
    this.emit("connected", device);
  }

  /**
   * After hard-reset / disconnect, wait for the previous COM port and reconnect.
   * Connect always interrupts (and raw soft-resets) so main.py is not left running.
   */
  async reconnectAfterReset(
    opts: {
      attempts?: number;
      delayMs?: number;
      device?: string;
      token?: vscode.CancellationToken;
    } = {}
  ): Promise<boolean> {
    const device = opts.device || this._lastDevice;
    if (!device) {
      return false;
    }
    const attempts = opts.attempts ?? 20;
    const delayMs = opts.delayMs ?? 1000;
    await this.disconnect().catch(() => undefined);
    for (let i = 0; i < attempts; i++) {
      if (opts.token?.isCancellationRequested) {
        this.log.appendLine("reconnect cancelled");
        return false;
      }
      await new Promise((r) => setTimeout(r, delayMs));
      if (opts.token?.isCancellationRequested) {
        this.log.appendLine("reconnect cancelled");
        return false;
      }
      try {
        const ports = await this.listPorts();
        if (!ports.some((p) => p.device === device)) {
          continue;
        }
        await this.connect(device);
        return true;
      } catch (e) {
        this.log.appendLine(`reconnect attempt ${i + 1}/${attempts}: ${e}`);
      }
    }
    return false;
  }

  /**
   * Drop the logical connection immediately (UI / status), then best-effort
   * tell the sidecar. Order matters: after bootloader/hard-reset the serial
   * port is often gone and a blocking disconnect RPC would leave the UI stuck
   * "connected".
   */
  async disconnect(): Promise<void> {
    const wasConnected = !!this._connectedDevice;
    this._connectedDevice = undefined;
    await vscode.commands.executeCommand("setContext", "mpftp.connected", false);
    if (wasConnected) {
      this.activity?.event("disconnected", { message: "disconnected" });
      this.emit("disconnected");
    }
    try {
      await Promise.race([
        this.request("disconnect"),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error("disconnect timeout")), 2000)
        ),
      ]);
    } catch {
      /* board/port may already be gone */
    }
  }

  /** Force-kill sidecar (and Windows process tree) so COM ports are released. */
  private async killSidecarProcess(pid: number | undefined): Promise<void> {
    if (!pid) {
      return;
    }
    const { execFile } = await import("child_process");
    const { promisify } = await import("util");
    const execFileAsync = promisify(execFile);
    try {
      // WSL Node → Windows python.exe: SIGTERM often leaves orphans holding COM.
      await execFileAsync("taskkill.exe", ["/F", "/T", "/PID", String(pid)], {
        timeout: 5000,
        windowsHide: true,
      });
    } catch {
      try {
        process.kill(pid, "SIGKILL");
      } catch {
        /* already gone */
      }
    }
  }

  dispose(): void {
    const pid = this.proc?.pid;
    try {
      this.proc?.stdin.write(JSON.stringify({ id: 0, method: "disconnect", params: {} }) + "\n");
    } catch {
      /* ignore */
    }
    void this.killSidecarProcess(pid);
    this.proc?.kill();
    this.proc = undefined;
    this._connectedDevice = undefined;
    this.removeAllListeners();
  }
}
