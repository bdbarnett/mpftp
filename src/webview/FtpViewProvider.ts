import * as crypto from "crypto";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { DirEntry, SidecarBridge } from "../bridge/SidecarBridge";
import { openBoardFileInEditor } from "../editRemote";
import { getConfig } from "../platform";

function joinRemote(base: string, name: string): string {
  if (!base || base === "/") {
    return "/" + name.replace(/^\/+/, "");
  }
  return base.replace(/\/+$/, "") + "/" + name;
}

/** Skip VCS/env/bytecode noise: .git, .venv, __pycache__, *.pyc, etc. */
function shouldSkipTransferEntry(name: string): boolean {
  if (!name || name === "." || name === "..") {
    return true;
  }
  if (name.startsWith(".")) {
    return true;
  }
  if (name === "__pycache__") {
    return true;
  }
  const lower = name.toLowerCase();
  return lower.endsWith(".pyc") || lower.endsWith(".pyo");
}

type TransferPhase = "active" | "stalled" | "done" | "idle";

/**
 * Tracks per-file transfer progress and periodically reports whether work is
 * still advancing or appears hung (no completion for stallMs).
 */
class TransferMonitor {
  private done = 0;
  private current = "";
  private startedAt = Date.now();
  private lastActivityAt = Date.now();
  private timer: ReturnType<typeof setInterval> | undefined;
  private disposed = false;

  constructor(
    private readonly verb: "Uploading" | "Downloading",
    private readonly total: number,
    private readonly onUpdate: (text: string, phase: TransferPhase) => void,
    private readonly progress: vscode.Progress<{ message?: string; increment?: number }>,
    private readonly stallMs = 15000,
    private readonly tickMs = 2000
  ) {}

  start(): void {
    this.startedAt = Date.now();
    this.lastActivityAt = Date.now();
    this.timer = setInterval(() => this.emit(), this.tickMs);
    this.emit();
  }

  beginFile(label: string): void {
    this.current = label;
    this.lastActivityAt = Date.now();
    this.emit();
  }

  finishFile(): void {
    this.done += 1;
    this.lastActivityAt = Date.now();
    const increment = this.total > 0 ? 100 / this.total : 0;
    this.progress.report({
      increment,
      message: this.current ? `${this.done}/${this.total} ${this.current}` : `${this.done}/${this.total}`,
    });
    this.emit();
  }

  finish(summary: string): void {
    this.dispose();
    this.onUpdate(summary, "done");
  }

  dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  private emit(): void {
    if (this.disposed) {
      return;
    }
    const idleMs = Date.now() - this.lastActivityAt;
    const elapsedSec = Math.max(0, Math.round((Date.now() - this.startedAt) / 1000));
    const idleSec = Math.max(0, Math.round(idleMs / 1000));
    const filePart = this.current || "…";
    const counts = this.total > 0 ? `${this.done}/${this.total}` : `${this.done}`;
    if (idleMs >= this.stallMs && this.current) {
      this.onUpdate(
        `${this.verb} ${filePart} (${counts}) — no activity ${idleSec}s (may be hung) · ${elapsedSec}s total`,
        "stalled"
      );
      this.progress.report({
        message: `${filePart} — stalled ${idleSec}s?`,
      });
      return;
    }
    this.onUpdate(
      `${this.verb} ${filePart} (${counts}) — in progress · ${elapsedSec}s`,
      "active"
    );
  }
}

export class FtpViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "mpftp.ftpView";

  private view?: vscode.WebviewView;
  private editorPanel: vscode.WebviewPanel | undefined;
  private readonly webviews = new Set<vscode.Webview>();
  private localPath: string;
  private remotePath = "/";
  private transferBusy = false;
  private bridgeEventsBound = false;

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
    this.attachWebview(webviewView.webview);
    webviewView.onDidDispose(() => {
      this.webviews.delete(webviewView.webview);
      if (this.view === webviewView) {
        this.view = undefined;
      }
    });
    this.bindBridgeEvents();
  }

  /** Focus the sidebar Board Files view. */
  async reveal(): Promise<void> {
    await vscode.commands.executeCommand("mpftp.ftpView.focus");
    await this.pushState();
  }

  /**
   * Open (or focus) the FTP UI as an editor tab — movable, splittable,
   * and can be dragged to another editor group or window.
   */
  openInEditor(): void {
    if (this.editorPanel) {
      this.editorPanel.reveal(vscode.ViewColumn.Beside);
      void this.pushState();
      return;
    }
    this.editorPanel = vscode.window.createWebviewPanel(
      "mpftp.ftpEditor",
      "mpftp Board Files",
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
      }
    );
    this.editorPanel.iconPath = vscode.Uri.joinPath(this.extensionUri, "resources", "mpftp.svg");
    this.attachWebview(this.editorPanel.webview);
    this.editorPanel.onDidDispose(() => {
      if (this.editorPanel) {
        this.webviews.delete(this.editorPanel.webview);
      }
      this.editorPanel = undefined;
    });
    this.bindBridgeEvents();
  }

  private attachWebview(webview: vscode.Webview): void {
    webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
    };
    webview.html = this.getHtml(webview);
    this.webviews.add(webview);
    webview.onDidReceiveMessage(async (msg) => {
      try {
        await this.handleMessage(msg);
      } catch (e: any) {
        this.status(String(e?.message || e));
      }
    });
  }

  private bindBridgeEvents(): void {
    if (this.bridgeEventsBound) {
      return;
    }
    this.bridgeEventsBound = true;
    this.bridge.on("connected", () => void this.pushState());
    this.bridge.on("disconnected", () => void this.pushState());
  }

  private postAll(msg: Record<string, unknown>): void {
    for (const webview of this.webviews) {
      void webview.postMessage(msg);
    }
  }

  private status(text: string, phase: TransferPhase = "idle"): void {
    this.postAll({ type: "status", text, phase });
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
      case "command": {
        const cmd = String(msg.command || "");
        if (cmd.startsWith("mpftp.")) {
          await vscode.commands.executeCommand(cmd);
          await this.pushState();
        }
        break;
      }
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
      case "openRemote": {
        const remote = String(msg.path || "");
        if (!remote) {
          return;
        }
        await openBoardFileInEditor(this.bridge, remote);
        this.status(`Editing ${remote}`);
        break;
      }
      case "runRemote": {
        const remote = String(msg.path || "");
        if (!remote) {
          return;
        }
        if (!/\.py$/i.test(remote)) {
          this.status("Run is only for .py files");
          return;
        }
        this.status(`Running ${remote}…`, "active");
        try {
          // Soft-reset + exec board file; do not follow (UI apps loop forever).
          // Output appears on the REPL UART — open it so the user can see prints/tracebacks.
          await this.bridge.request("run_path", { path: remote, follow: false });
          this.status(`Running ${remote} — see REPL`, "done");
          this.onOpenRepl();
        } catch (e: any) {
          const err = String(e?.message || e);
          this.status(`Run failed: ${err}`, "stalled");
          void vscode.window.showErrorMessage(`mpftp run ${remote}: ${err}`);
        }
        break;
      }
      case "hashRemote": {
        const remote = String(msg.path || "");
        if (!remote) {
          return;
        }
        const res = await this.bridge.request<{ hash: string; algo: string }>("fs_hash", {
          path: remote,
          algo: "sha256",
        });
        this.status(`${remote}: ${res.algo} ${res.hash}`);
        void vscode.window.showInformationMessage(`${remote}\n${res.hash}`);
        break;
      }
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
      case "localMkdir": {
        const name = await vscode.window.showInputBox({
          prompt: "New local directory name",
          placeHolder: "lib",
        });
        if (!name) {
          return;
        }
        if (name.includes("/") || name.includes("\\")) {
          this.status("Name cannot contain path separators");
          return;
        }
        const dest = path.join(this.localPath, name);
        fs.mkdirSync(dest, { recursive: false });
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
      case "localRm":
        await this.localRmMany(msg.localPaths || []);
        await this.pushState();
        break;
      case "localRename": {
        const src = String(msg.path || "");
        if (!src || !fs.existsSync(src)) {
          this.status(`Not found: ${src}`);
          return;
        }
        const oldName = path.basename(src);
        const newName = await vscode.window.showInputBox({
          prompt: "Rename local item",
          value: oldName,
          valueSelection: [0, oldName.lastIndexOf(".") > 0 ? oldName.lastIndexOf(".") : oldName.length],
        });
        if (!newName || newName === oldName) {
          return;
        }
        if (newName.includes("/") || newName.includes("\\")) {
          this.status("Name cannot contain path separators");
          return;
        }
        const dest = path.join(path.dirname(src), newName);
        fs.renameSync(src, dest);
        this.status(`Renamed ${oldName} → ${newName}`);
        await this.pushState();
        break;
      }
      case "remoteRename": {
        const src = String(msg.path || "");
        if (!src) {
          return;
        }
        const oldName = src.split("/").filter(Boolean).pop() || "";
        const newName = await vscode.window.showInputBox({
          prompt: "Rename board item",
          value: oldName,
          valueSelection: [0, oldName.lastIndexOf(".") > 0 ? oldName.lastIndexOf(".") : oldName.length],
        });
        if (!newName || newName === oldName) {
          return;
        }
        if (newName.includes("/")) {
          this.status("Name cannot contain '/'");
          return;
        }
        const parent = src.includes("/") ? src.slice(0, src.lastIndexOf("/")) || "/" : "/";
        const dest = joinRemote(parent === "" ? "/" : parent, newName);
        await this.bridge.request("fs_rename", { src, dest });
        this.status(`Renamed ${oldName} → ${newName}`);
        await this.pushState();
        break;
      }
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
    if (!this.webviews.size) {
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
    const connected = this.bridge.connected;
    this.postAll({
      type: "state",
      connected,
      device: this.bridge.connectedDevice || "",
      localPath: this.localPath,
      // Hide board path while disconnected; keep this.remotePath for reconnect.
      remotePath: connected ? this.remotePath : "",
      localEntries: this.listLocal(this.localPath),
      remoteEntries: connected ? remoteEntries : [],
    });
  }

  private countLocalFiles(local: string): { files: number; skipped: number } {
    const base = path.basename(local);
    if (shouldSkipTransferEntry(base)) {
      return { files: 0, skipped: 1 };
    }
    let files = 0;
    let skipped = 0;
    const st = fs.statSync(local);
    if (st.isDirectory()) {
      for (const name of fs.readdirSync(local)) {
        if (shouldSkipTransferEntry(name)) {
          skipped += 1;
          continue;
        }
        const sub = this.countLocalFiles(path.join(local, name));
        files += sub.files;
        skipped += sub.skipped;
      }
      return { files, skipped };
    }
    return { files: 1, skipped: 0 };
  }

  private async countRemoteFiles(remote: string): Promise<{ files: number; skipped: number }> {
    const name = remote.split("/").filter(Boolean).pop() || "";
    if (name && shouldSkipTransferEntry(name)) {
      return { files: 0, skipped: 1 };
    }
    const st = await this.bridge.request<{ isDir: boolean; size: number }>("fs_stat", {
      path: remote,
    });
    if (!st.isDir) {
      return { files: 1, skipped: 0 };
    }
    let files = 0;
    let skipped = 0;
    const entries = await this.bridge.request<DirEntry[]>("fs_listdir", { path: remote });
    for (const e of entries) {
      if (shouldSkipTransferEntry(e.name)) {
        skipped += 1;
        continue;
      }
      const sub = await this.countRemoteFiles(joinRemote(remote, e.name));
      files += sub.files;
      skipped += sub.skipped;
    }
    return { files, skipped };
  }

  private async uploadMany(localPaths: string[]): Promise<void> {
    if (!this.bridge.connected) {
      throw new Error("not connected");
    }
    if (this.transferBusy) {
      this.status("A transfer is already in progress", "stalled");
      return;
    }
    let totalFiles = 0;
    let skippedDots = 0;
    for (const local of localPaths) {
      const c = this.countLocalFiles(local);
      totalFiles += c.files;
      skippedDots += c.skipped;
    }
    if (totalFiles === 0) {
      this.status(
        skippedDots
          ? `Nothing to upload (skipped ${skippedDots} ignored entr${skippedDots === 1 ? "y" : "ies"}: .git, __pycache__, …)`
          : "Nothing to upload"
      );
      return;
    }

    this.transferBusy = true;
    try {
      await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: "mpftp upload",
          cancellable: false,
        },
        async (progress) => {
          const monitor = new TransferMonitor(
            "Uploading",
            totalFiles,
            (text, phase) => this.status(text, phase),
            progress
          );
          monitor.start();
          try {
            for (const local of localPaths) {
              await this.uploadPath(local, this.remotePath, monitor);
            }
            const skipNote = skippedDots
              ? `, skipped ${skippedDots} ignored entr${skippedDots === 1 ? "y" : "ies"}`
              : "";
            monitor.finish(`Uploaded ${totalFiles} file(s)${skipNote}`);
          } catch (e) {
            monitor.dispose();
            throw e;
          }
        }
      );
    } finally {
      this.transferBusy = false;
    }
  }

  private async uploadPath(local: string, remoteDir: string, monitor: TransferMonitor): Promise<void> {
    const base = path.basename(local);
    if (shouldSkipTransferEntry(base)) {
      return;
    }
    const st = fs.statSync(local);
    if (st.isDirectory()) {
      const destDir = joinRemote(remoteDir, base);
      try {
        await this.bridge.request("fs_mkdir", { path: destDir });
      } catch {
        /* may exist */
      }
      for (const name of fs.readdirSync(local)) {
        if (shouldSkipTransferEntry(name)) {
          continue;
        }
        await this.uploadPath(path.join(local, name), destDir, monitor);
      }
      return;
    }
    const data = fs.readFileSync(local);
    const dest = joinRemote(remoteDir, base);
    monitor.beginFile(dest);
    await this.bridge.request("fs_write", {
      path: dest,
      data_b64: data.toString("base64"),
    });
    if (getConfig().verifyTransfers) {
      await this.verifyRemoteHash(dest, data);
    }
    monitor.finishFile();
  }

  private async verifyRemoteHash(remote: string, data: Buffer): Promise<void> {
    const expect = crypto.createHash("sha256").update(data).digest("hex");
    const res = await this.bridge.request<{ hash: string }>("fs_hash", {
      path: remote,
      algo: "sha256",
    });
    if (res.hash !== expect) {
      throw new Error(`hash mismatch for ${remote}: expected ${expect}, got ${res.hash}`);
    }
  }

  private async downloadMany(remotePaths: string[]): Promise<void> {
    if (!this.bridge.connected) {
      throw new Error("not connected");
    }
    if (this.transferBusy) {
      this.status("A transfer is already in progress", "stalled");
      return;
    }
    let totalFiles = 0;
    let skippedDots = 0;
    for (const remote of remotePaths) {
      const c = await this.countRemoteFiles(remote);
      totalFiles += c.files;
      skippedDots += c.skipped;
    }
    if (totalFiles === 0) {
      this.status(
        skippedDots
          ? `Nothing to download (skipped ${skippedDots} ignored entr${skippedDots === 1 ? "y" : "ies"})`
          : "Nothing to download"
      );
      return;
    }

    this.transferBusy = true;
    try {
      await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: "mpftp download",
          cancellable: false,
        },
        async (progress) => {
          const monitor = new TransferMonitor(
            "Downloading",
            totalFiles,
            (text, phase) => this.status(text, phase),
            progress
          );
          monitor.start();
          try {
            for (const remote of remotePaths) {
              await this.downloadPath(remote, this.localPath, monitor);
            }
            const skipNote = skippedDots
              ? `, skipped ${skippedDots} ignored entr${skippedDots === 1 ? "y" : "ies"}`
              : "";
            monitor.finish(`Downloaded ${totalFiles} file(s)${skipNote}`);
          } catch (e) {
            monitor.dispose();
            throw e;
          }
        }
      );
    } finally {
      this.transferBusy = false;
    }
  }

  private async downloadPath(
    remote: string,
    localDir: string,
    monitor: TransferMonitor
  ): Promise<void> {
    const name = remote.split("/").filter(Boolean).pop() || "file";
    if (shouldSkipTransferEntry(name)) {
      return;
    }
    const st = await this.bridge.request<{ isDir: boolean; size: number }>("fs_stat", {
      path: remote,
    });
    if (st.isDir) {
      const destDir = path.join(localDir, name);
      fs.mkdirSync(destDir, { recursive: true });
      const entries = await this.bridge.request<DirEntry[]>("fs_listdir", { path: remote });
      for (const e of entries) {
        if (shouldSkipTransferEntry(e.name)) {
          continue;
        }
        await this.downloadPath(joinRemote(remote, e.name), destDir, monitor);
      }
      return;
    }
    monitor.beginFile(remote);
    const res = await this.bridge.request<{ data_b64: string }>("fs_read", { path: remote });
    const buf = Buffer.from(res.data_b64, "base64");
    fs.writeFileSync(path.join(localDir, name), buf);
    if (getConfig().verifyTransfers) {
      await this.verifyRemoteHash(remote, buf);
    }
    monitor.finishFile();
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

  private async localRmMany(localPaths: string[]): Promise<void> {
    const ok = await vscode.window.showWarningMessage(
      `Delete ${localPaths.length} local item(s)?`,
      { modal: true },
      "Delete"
    );
    if (ok !== "Delete") {
      return;
    }
    for (const p of localPaths) {
      this.status(`Removing ${p}…`);
      fs.rmSync(p, { recursive: true, force: true });
    }
    this.status("Deleted");
  }

  private getHtml(webview: vscode.Webview): string {
    const css = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "ftp.css"));
    const js = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "ftp.js"));
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
  <title>mpftp</title>
</head>
<body>
  <div id="app">
    <div class="toolbar">
      <button id="btnMore" class="secondary icon-btn more-btn" title="mpftp commands" aria-label="mpftp commands">
        <i class="codicon codicon-ellipsis"></i>
      </button>
      <button id="btnConnect">Connect</button>
      <button id="btnRepl" class="secondary" disabled>REPL</button>
      <button id="btnFirmware" class="secondary" title="Build & flash firmware">Firmware</button>
      <span class="status" id="status">Not connected</span>
    </div>
    <div class="panes">
      <section class="pane" id="localPane">
        <div class="pane-header"><span>Local</span><button id="btnPickLocal" class="secondary">Browse…</button></div>
        <div class="pathbar">
          <button id="btnRefreshLocal" class="secondary icon-btn" title="Refresh"><i class="codicon codicon-refresh"></i></button>
          <input id="localPath" spellcheck="false" />
        </div>
        <div class="listing" id="localListing"></div>
      </section>
      <div class="xfer">
        <button id="btnXferUp" disabled title="Upload"><i class="codicon codicon-arrow-right"></i></button>
        <button id="btnXferDown" disabled title="Download"><i class="codicon codicon-arrow-left"></i></button>
      </div>
      <section class="pane" id="remotePane">
        <div class="pane-header"><span>Board</span></div>
        <div class="pathbar">
          <button id="btnRefreshRemote" class="secondary icon-btn" disabled title="Refresh"><i class="codicon codicon-refresh"></i></button>
          <input id="remotePath" spellcheck="false" />
        </div>
        <div class="listing" id="remoteListing"></div>
      </section>
    </div>
    <div class="footer" id="footer">Ready</div>
  </div>
  <div id="ctxMenu" class="ctx-menu" hidden></div>
  <script src="${js}"></script>
</body>
</html>`;
  }
}
