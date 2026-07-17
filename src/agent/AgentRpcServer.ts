import * as fs from "fs";
import * as net from "net";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { ActivityLog } from "../activityLog";
import { SidecarBridge } from "../bridge/SidecarBridge";

const DEFAULT_PORT = 7429;
const HOST = "127.0.0.1";

/**
 * Local JSON-line TCP RPC so agents/CLI share the extension's sidecar session.
 * Uses TCP (not Unix sockets) so WSL UI + Windows python.exe CLI can both talk.
 * Protocol matches sidecar.py: {"id","method","params"} → result|error.
 * Extra methods: agent_status, agent_paths, status.
 */
export class AgentRpcServer {
  private server: net.Server | undefined;
  private port = DEFAULT_PORT;

  constructor(
    private readonly bridge: SidecarBridge,
    private readonly activity: ActivityLog
  ) {}

  get path(): string {
    return `${HOST}:${this.port}`;
  }

  start(): void {
    this.server = net.createServer((socket) => {
      let buf = "";
      socket.setEncoding("utf8");
      socket.on("data", (chunk: string) => {
        buf += chunk;
        let idx: number;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line) {
            continue;
          }
          void this.handleLine(socket, line);
        }
      });
    });

    const tryListen = (port: number, attemptsLeft: number) => {
      this.server!.once("error", (err: NodeJS.ErrnoException) => {
        if (err.code === "EADDRINUSE" && attemptsLeft > 0) {
          tryListen(port + 1, attemptsLeft - 1);
          return;
        }
        this.activity.event("rpc_error", {
          message: String(err),
          data: { host: HOST, port },
        });
      });
      this.server!.listen(port, HOST, () => {
        this.port = port;
        const addr = `${HOST}:${port}`;
        this.activity.writeRpcPath(addr);
        this.writePortFiles(port);
        this.activity.event("rpc_listen", {
          message: `agent RPC listening on ${addr}`,
          data: { host: HOST, port },
        });
      });
    };

    tryListen(DEFAULT_PORT, 20);
  }

  private writePortFiles(port: number): void {
    const line = `${HOST}:${port}\n`;
    try {
      fs.writeFileSync(path.join(os.homedir(), ".mpftp", "rpc.port"), line, "utf8");
    } catch {
      /* ignore */
    }
    const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (folder) {
      try {
        const dir = path.join(folder, ".mpftp");
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(path.join(dir, "rpc.port"), line, "utf8");
      } catch {
        /* ignore */
      }
    }
  }

  private async handleLine(socket: net.Socket, line: string): Promise<void> {
    let msg: { id?: number; method?: string; params?: Record<string, unknown> };
    try {
      msg = JSON.parse(line);
    } catch {
      socket.write(JSON.stringify({ type: "error", id: null, error: "invalid json" }) + "\n");
      return;
    }
    const id = msg.id ?? 0;
    const method = msg.method || "";
    const params = msg.params || {};
    try {
      const result = await this.dispatch(method, params);
      socket.write(JSON.stringify({ type: "result", id, result }) + "\n");
    } catch (e: any) {
      socket.write(
        JSON.stringify({ type: "error", id, error: e?.message || String(e) }) + "\n"
      );
    }
  }

  private async dispatch(method: string, params: Record<string, unknown>): Promise<unknown> {
    this.activity.event("agent_rpc", {
      source: "agent",
      message: method,
      data: { method, keys: Object.keys(params) },
    });

    if (method === "agent_status" || method === "status") {
      await this.bridge.ensureStarted();
      return {
        connected: this.bridge.connected,
        device: this.bridge.connectedDevice || null,
        rpc: this.path,
        activityLog: this.activity.activityPath,
        replLog: this.activity.replPath,
      };
    }
    if (method === "agent_paths") {
      return {
        rpc: this.path,
        activityLog: this.activity.activityPath,
        replLog: this.activity.replPath,
        home: this.activity.dir,
      };
    }
    if (method === "connect") {
      const device = String(params.device || "");
      if (!device) {
        throw new Error("device required");
      }
      await this.bridge.connect(device, params.baud as number | undefined);
      return { device, baud: params.baud ?? 115200 };
    }
    if (method === "resume") {
      await this.bridge.resume(params.baud as number | undefined);
      return { device: this.bridge.connectedDevice, resumed: true };
    }
    if (method === "disconnect") {
      await this.bridge.disconnect();
      return { ok: true };
    }
    return this.bridge.request(method, params);
  }

  dispose(): void {
    try {
      this.server?.close();
    } catch {
      /* ignore */
    }
    this.server = undefined;
    this.activity.clearRpcPath();
    for (const f of [
      path.join(os.homedir(), ".mpftp", "rpc.port"),
      path.join(os.homedir(), ".mpftp", "rpc.path"),
    ]) {
      try {
        fs.unlinkSync(f);
      } catch {
        /* ignore */
      }
    }
  }
}
