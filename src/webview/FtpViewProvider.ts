import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { DirEntry, SidecarBridge } from "../bridge/SidecarBridge";

function joinRemote(base: string, name: string): string {
  if (!base || base === "/") {
    return "/" + name.replace(/^\/+/, "");
  }
  return base.replace(/\/+$/, "") + "/" + name;
}

export class FtpViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "mpftp.ftpView";

  private view?: vscode.WebviewView;
  private localPath: string;
  private remotePath = "/";

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly bridge: SidecarBridge,
    private readonly onOpenRepl: () => void,
    private readonly onConnect: () => Promise<void>,
    private readonly onDisconnect: () => Promise<void>
  ) {
    const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    this.localPath = folder || os.homedir();
  }

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this.view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
    };
    webviewView.webview.html = this.getHtml(webviewView.webview);

    webviewView.webview.onDidReceiveMessage(async (msg) => {
      try {
        await this.handleMessage(msg);
      } catch (e: any) {
        this.status(String(e?.message || e));
      }
    });

    this.bridge.on("connected", () => void this.pushState());
    this.bridge.on("disconnected", () => void this.pushState());
  }

  async reveal(): Promise<void> {
    await vscode.commands.executeCommand("mpftp.ftpView.focus");
    await this.pushState();
  }

  private status(text: string): void {
    void this.view?.webview.postMessage({ type: "status", text });
  }

  private async handleMessage(msg: any): Promise<void> {
    switch (msg.type) {
      case "ready":
        await this.pushState();
        break;
      case "connect":
        await this.onConnect();
        await this.pushState();
        break;
      case "disconnect":
        await this.onDisconnect();
        await this.pushState();
        break;
      case "openRepl":
        this.onOpenRepl();
        break;
      case "refreshLocal":
        await this.pushState();
        break;
      case "refreshRemote":
        await this.pushState();
        break;
      case "pickLocal": {
        const uris = await vscode.window.showOpenDialog({
          canSelectFiles: false,
          canSelectFolders: true,
          canSelectMany: false,
          openLabel: "Select local folder",
          defaultUri: vscode.Uri.file(this.localPath),
        });
        if (uris?.[0]) {
          this.localPath = uris[0].fsPath;
          await this.pushState();
        }
        break;
      }
      case "localCd":
        if (msg.path && fs.existsSync(msg.path) && fs.statSync(msg.path).isDirectory()) {
          this.localPath = msg.path;
          await this.pushState();
        } else {
          this.status(`Not a directory: ${msg.path}`);
        }
        break;
      case "localUp": {
        const parent = path.dirname(this.localPath);
        if (parent && parent !== this.localPath) {
          this.localPath = parent;
          await this.pushState();
        }
        break;
      }
      case "remoteCd":
        this.remotePath = msg.path || "/";
        await this.pushState();
        break;
      case "upload":
        await this.uploadMany(msg.localPaths || []);
        await this.pushState();
        break;
      case "download":
        await this.downloadMany(msg.remotePaths || []);
        await this.pushState();
        break;
      case "mkdir": {
        const name = await vscode.window.showInputBox({
          prompt: "New remote directory name",
          placeHolder: "lib",
        });
        if (!name) {
          return;
        }
        const dest = joinRemote(this.remotePath, name);
        await this.bridge.request("fs_mkdir", { path: dest });
        this.status(`mkdir ${dest}`);
        await this.pushState();
        break;
      }
      case "newFile": {
        const name = await vscode.window.showInputBox({
          prompt: "New remote file name",
          placeHolder: "main.py",
        });
        if (!name) {
          return;
        }
        const dest = joinRemote(this.remotePath, name);
        await this.bridge.request("fs_touch", { path: dest });
        this.status(`touch ${dest}`);
        await this.pushState();
        break;
      }
      case "rm":
        await this.rmMany(msg.remotePaths || []);
        await this.pushState();
        break;
      default:
        break;
    }
  }

  private listLocal(dir: string): DirEntry[] {
    try {
      return fs
        .readdirSync(dir, { withFileTypes: true })
        .filter((d) => d.name !== "." && d.name !== "..")
        .map((d) => {
          let size = 0;
          try {
            if (d.isFile()) {
              size = fs.statSync(path.join(dir, d.name)).size;
            }
          } catch {
            /* ignore */
          }
          return { name: d.name, isDir: d.isDirectory(), size };
        })
        .sort((a, b) => Number(b.isDir) - Number(a.isDir) || a.name.localeCompare(b.name));
    } catch (e: any) {
      this.status(`local list failed: ${e.message}`);
      return [];
    }
  }

  private async listRemote(dir: string): Promise<DirEntry[]> {
    if (!this.bridge.connected) {
      return [];
    }
    const p = !dir || dir === "/" ? "/" : dir;
    // sidecar treats "" as cwd/root listing for "/"
    const entries = await this.bridge.request<DirEntry[]>("fs_listdir", {
      path: p === "/" ? "/" : p,
    });
    return entries;
  }

  private async pushState(): Promise<void> {
    if (!this.view) {
      return;
    }
    let remoteEntries: DirEntry[] = [];
    if (this.bridge.connected) {
      try {
        remoteEntries = await this.listRemote(this.remotePath);
      } catch (e: any) {
        this.status(`remote list failed: ${e.message}`);
      }
    }
    await this.view.webview.postMessage({
      type: "state",
      connected: this.bridge.connected,
      device: this.bridge.connectedDevice || "",
      localPath: this.localPath,
      remotePath: this.remotePath,
      localEntries: this.listLocal(this.localPath),
      remoteEntries,
    });
  }

  private async uploadMany(localPaths: string[]): Promise<void> {
    if (!this.bridge.connected) {
      throw new Error("not connected");
    }
    for (const local of localPaths) {
      await this.uploadPath(local, this.remotePath);
    }
    this.status(`Uploaded ${localPaths.length} item(s)`);
  }

  private async uploadPath(local: string, remoteDir: string): Promise<void> {
    const base = path.basename(local);
    const st = fs.statSync(local);
    if (st.isDirectory()) {
      const destDir = joinRemote(remoteDir, base);
      try {
        await this.bridge.request("fs_mkdir", { path: destDir });
      } catch {
        /* may exist */
      }
      for (const name of fs.readdirSync(local)) {
        await this.uploadPath(path.join(local, name), destDir);
      }
      return;
    }
    const data = fs.readFileSync(local);
    const dest = joinRemote(remoteDir, base);
    this.status(`Uploading ${base}…`);
    await this.bridge.request("fs_write", {
      path: dest,
      data_b64: data.toString("base64"),
    });
  }

  private async downloadMany(remotePaths: string[]): Promise<void> {
    if (!this.bridge.connected) {
      throw new Error("not connected");
    }
    for (const remote of remotePaths) {
      await this.downloadPath(remote, this.localPath);
    }
    this.status(`Downloaded ${remotePaths.length} item(s)`);
  }

  private async downloadPath(remote: string, localDir: string): Promise<void> {
    const name = remote.split("/").filter(Boolean).pop() || "file";
    const st = await this.bridge.request<{ isDir: boolean; size: number }>("fs_stat", {
      path: remote,
    });
    if (st.isDir) {
      const destDir = path.join(localDir, name);
      fs.mkdirSync(destDir, { recursive: true });
      const entries = await this.bridge.request<DirEntry[]>("fs_listdir", { path: remote });
      for (const e of entries) {
        await this.downloadPath(joinRemote(remote, e.name), destDir);
      }
      return;
    }
    this.status(`Downloading ${name}…`);
    const res = await this.bridge.request<{ data_b64: string }>("fs_read", { path: remote });
    fs.writeFileSync(path.join(localDir, name), Buffer.from(res.data_b64, "base64"));
  }

  private async rmMany(remotePaths: string[]): Promise<void> {
    const ok = await vscode.window.showWarningMessage(
      `Delete ${remotePaths.length} item(s) on the board?`,
      { modal: true },
      "Delete"
    );
    if (ok !== "Delete") {
      return;
    }
    for (const p of remotePaths) {
      this.status(`Removing ${p}…`);
      await this.bridge.request("fs_rm_rf", { path: p });
    }
    this.status("Deleted");
  }

  private getHtml(webview: vscode.Webview): string {
    const css = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "ftp.css"));
    const js = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "ftp.js"));
    const csp = [
      `default-src 'none'`,
      `style-src ${webview.cspSource}`,
      `script-src ${webview.cspSource}`,
    ].join("; ");

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="${csp}" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="stylesheet" href="${css}" />
  <title>mpftp</title>
</head>
<body>
  <div id="app">
    <div class="toolbar">
      <button id="btnConnect">Connect</button>
      <button id="btnRepl" class="secondary" disabled>REPL</button>
      <span class="status" id="status">Not connected</span>
    </div>
    <div class="panes">
      <section class="pane" id="localPane">
        <div class="pane-header"><span>Local</span><button id="btnPickLocal" class="secondary">Browse…</button></div>
        <div class="pathbar">
          <button id="btnRefreshLocal" class="secondary" title="Refresh">↻</button>
          <input id="localPath" spellcheck="false" />
        </div>
        <div class="listing" id="localListing"></div>
        <div class="actions">
          <button id="btnUpload" disabled title="Upload selected to board">Upload →</button>
        </div>
      </section>
      <div class="xfer">
        <button id="btnXferUp" disabled title="Upload">→</button>
        <button id="btnXferDown" disabled title="Download">←</button>
      </div>
      <section class="pane" id="remotePane">
        <div class="pane-header"><span>Board</span></div>
        <div class="pathbar">
          <button id="btnRefreshRemote" class="secondary" disabled title="Refresh">↻</button>
          <input id="remotePath" spellcheck="false" />
        </div>
        <div class="listing" id="remoteListing"></div>
        <div class="actions">
          <button id="btnDownload" disabled>← Download</button>
          <button id="btnMkdir" disabled>New folder</button>
          <button id="btnNewFile" disabled>New file</button>
          <button id="btnRm" class="danger" disabled>Delete</button>
        </div>
      </section>
    </div>
    <div class="footer" id="footer">Ready</div>
  </div>
  <script src="${js}"></script>
</body>
</html>`;
  }
}
