import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import * as path from "path";
import { EventEmitter } from "events";
import * as vscode from "vscode";
import { getConfig, resolvePython } from "../platform";

export type PortInfo = {
  device: string;
  serial_number?: string | null;
  vid?: number | null;
  pid?: number | null;
  manufacturer?: string | null;
  product?: string | null;
  description?: string | null;
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

  constructor(private readonly extensionPath: string, private readonly log: vscode.OutputChannel) {
    super();
  }

  get connectedDevice(): string | undefined {
    return this._connectedDevice;
  }

  get connected(): boolean {
    return !!this._connectedDevice;
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
    const cfg = getConfig();
    const python = resolvePython(this.extensionPath, cfg.pythonPath);
    const script = path.join(this.extensionPath, "python", "sidecar.py");
    this.log.appendLine(`[mpftp] starting sidecar: ${python} ${script}`);

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
    });
    this.proc.on("exit", (code, signal) => {
      this.log.appendLine(`[mpftp] sidecar exited code=${code} signal=${signal}`);
      this.proc = undefined;
      this._connectedDevice = undefined;
      for (const [, p] of this.pending) {
        p.reject(new Error("sidecar exited"));
      }
      this.pending.clear();
      this.emit("exit");
      void vscode.commands.executeCommand("setContext", "mpftp.connected", false);
    });

    // Wait for ready notify (with timeout)
    await new Promise<void>((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("sidecar ready timeout")), 20000);
      const onReady = () => {
        clearTimeout(t);
        this.off("ready", onReady);
        resolve();
      };
      this.on("ready", onReady);
    });

    // Sanity ping
    await this.request("ping");
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
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (v) => resolve(v as T),
        reject,
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

  async connect(device: string, baud?: number): Promise<void> {
    const cfg = getConfig();
    await this.request("connect", { device, baud: baud ?? cfg.defaultBaud });
    this._connectedDevice = device;
    await vscode.commands.executeCommand("setContext", "mpftp.connected", true);
    this.emit("connected", device);
  }

  async disconnect(): Promise<void> {
    try {
      await this.request("disconnect");
    } finally {
      this._connectedDevice = undefined;
      await vscode.commands.executeCommand("setContext", "mpftp.connected", false);
      this.emit("disconnected");
    }
  }

  dispose(): void {
    try {
      this.proc?.stdin.write(JSON.stringify({ id: 0, method: "disconnect", params: {} }) + "\n");
    } catch {
      /* ignore */
    }
    this.proc?.kill();
    this.proc = undefined;
    this.removeAllListeners();
  }
}
