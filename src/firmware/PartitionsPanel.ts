import * as vscode from "vscode";
import { ActivityLog } from "../activityLog";
import { FirmwareEngine } from "./engine";

/**
 * ESP32 partition-table editor. Loads the board's stock CSV (or an existing
 * workspace override), lets the user edit rows, and saves the result as a
 * workspace override — the MicroPython clone is never modified.
 */
export class PartitionsPanel {
  private panel: vscode.WebviewPanel | undefined;
  private readonly engine: FirmwareEngine;
  private mp = "";
  private board = "";
  private variant = "";

  constructor(
    private readonly extensionUri: vscode.Uri,
    extensionPath: string,
    private readonly activity: ActivityLog
  ) {
    this.engine = new FirmwareEngine(extensionPath);
  }

  reveal(mp: string, board: string, variant: string): void {
    this.mp = mp;
    this.board = board;
    this.variant = variant;
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Active);
      void this.load();
      return;
    }
    this.panel = vscode.window.createWebviewPanel(
      "mpftp.partitions",
      "ESP32 Partitions",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
      }
    );
    this.panel.iconPath = vscode.Uri.joinPath(this.extensionUri, "resources", "mpftp.svg");
    this.panel.webview.html = this.getHtml(this.panel.webview);
    this.panel.webview.onDidReceiveMessage((m) => void this.onMessage(m));
    this.panel.onDidDispose(() => (this.panel = undefined));
  }

  private post(msg: Record<string, unknown>): void {
    void this.panel?.webview.postMessage(msg);
  }

  private args(): Record<string, string> {
    return { mp: this.mp, board: this.board, variant: this.variant };
  }

  private async onMessage(msg: any): Promise<void> {
    switch (msg?.type) {
      case "ready":
      case "load":
        await this.load();
        break;
      case "save":
        await this.save(msg.rows || []);
        break;
      case "reset":
        await this.reset();
        break;
      default:
        break;
    }
  }

  private async load(): Promise<void> {
    try {
      const res = await this.engine.run(
        "partitions",
        this.args(),
        ["get"]
      );
      this.post({
        type: "data",
        board: this.board,
        variant: this.variant,
        ...(res as Record<string, unknown>),
      });
    } catch (e: any) {
      this.post({ type: "error", error: String(e?.message || e) });
    }
  }

  private async save(rows: unknown[]): Promise<void> {
    try {
      const res = await this.engine.run(
        "partitions",
        { ...this.args(), rows: JSON.stringify(rows) },
        ["set"]
      );
      this.activity.event("firmware_partitions_save", {
        data: { board: this.board, variant: this.variant },
      });
      this.post({ type: "saved", ...(res as Record<string, unknown>) });
      await this.load();
    } catch (e: any) {
      this.post({ type: "error", error: String(e?.message || e) });
    }
  }

  private async reset(): Promise<void> {
    try {
      await this.engine.run("partitions", this.args(), ["reset"]);
      await this.load();
    } catch (e: any) {
      this.post({ type: "error", error: String(e?.message || e) });
    }
  }

  private getHtml(webview: vscode.Webview): string {
    const css = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "partitions.css")
    );
    const js = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "partitions.js")
    );
    const codicons = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "codicons", "codicon.css")
    );
    const csp = [
      `default-src 'none'`,
      `style-src ${webview.cspSource}`,
      `font-src ${webview.cspSource}`,
      `script-src ${webview.cspSource}`,
    ].join("; ");
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="${csp}" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="stylesheet" href="${codicons}" />
  <link rel="stylesheet" href="${css}" />
  <title>ESP32 Partitions</title>
</head>
<body>
  <div id="app"></div>
  <script src="${js}"></script>
</body>
</html>`;
  }
}
