import * as vscode from "vscode";
import { SidecarBridge } from "../bridge/SidecarBridge";

/**
 * VS Code Pseudoterminal backed by the mpremote sidecar.
 * Bytes from the board are written as-is so ANSI/VT color sequences render
 * in the integrated terminal (xterm.js).
 */
export class ReplTerminal implements vscode.Pseudoterminal {
  private readonly writeEmitter = new vscode.EventEmitter<string>();
  private readonly closeEmitter = new vscode.EventEmitter<number | void>();
  onDidWrite = this.writeEmitter.event;
  onDidClose = this.closeEmitter.event;

  private onData?: (params: { data_b64: string }) => void;
  private onErr?: (params: { message: string }) => void;
  private onExit?: () => void;

  constructor(private readonly bridge: SidecarBridge) {}

  open(): void {
    const device = this.bridge.connectedDevice || "board";
    this.writeEmitter.fire(`\x1b[90mmpftp REPL — ${device} (Ctrl+] to close)\x1b[0m\r\n`);

    this.onData = (params) => {
      const buf = Buffer.from(params.data_b64, "base64");
      // Pseudoterminal expects string; pass through Latin1 to preserve all bytes including ESC.
      this.writeEmitter.fire(buf.toString("latin1"));
    };
    this.onErr = (params) => {
      this.writeEmitter.fire(`\r\n\x1b[31m[repl error] ${params.message}\x1b[0m\r\n`);
    };
    this.onExit = () => {
      this.writeEmitter.fire("\r\n\x1b[90m[disconnected]\x1b[0m\r\n");
      this.closeEmitter.fire(0);
    };

    this.bridge.on("repl_data", this.onData);
    this.bridge.on("repl_error", this.onErr);
    this.bridge.on("exit", this.onExit);
    this.bridge.on("disconnected", this.onExit);

    void this.bridge.request("repl_start").catch((e) => {
      this.writeEmitter.fire(`\x1b[31mFailed to start REPL: ${e}\x1b[0m\r\n`);
    });
  }

  close(): void {
    if (this.onData) {
      this.bridge.off("repl_data", this.onData);
    }
    if (this.onErr) {
      this.bridge.off("repl_error", this.onErr);
    }
    if (this.onExit) {
      this.bridge.off("exit", this.onExit);
      this.bridge.off("disconnected", this.onExit);
    }
    void this.bridge.request("repl_stop").catch(() => undefined);
  }

  handleInput(data: string): void {
    // Ctrl+] closes
    if (data === "\x1d") {
      this.closeEmitter.fire(0);
      return;
    }
    const b64 = Buffer.from(data, "latin1").toString("base64");
    void this.bridge.request("repl_write", { data_b64: b64 }).catch((e) => {
      this.writeEmitter.fire(`\r\n\x1b[31mwrite failed: ${e}\x1b[0m\r\n`);
    });
  }
}

export function openRepl(bridge: SidecarBridge): vscode.Terminal {
  const pty = new ReplTerminal(bridge);
  const term = vscode.window.createTerminal({
    name: `mpftp: ${bridge.connectedDevice || "REPL"}`,
    pty,
  });
  term.show();
  return term;
}
