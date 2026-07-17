import * as vscode from "vscode";
import { SidecarBridge, PortInfo } from "./bridge/SidecarBridge";
import { openRepl } from "./terminal/ReplTerminal";
import { FtpViewProvider } from "./webview/FtpViewProvider";
import { detectHost, getConfig, resolvePython } from "./platform";

let bridge: SidecarBridge;
let log: vscode.OutputChannel;
let ftpProvider: FtpViewProvider;
let statusBar: vscode.StatusBarItem;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  log = vscode.window.createOutputChannel("mpftp");
  bridge = new SidecarBridge(context.extensionPath, log);

  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 80);
  statusBar.command = "mpftp.connect";
  statusBar.text = "$(plug) mpftp";
  statusBar.tooltip = "Connect to MicroPython board";
  statusBar.show();

  const ensureConnected = async (): Promise<boolean> => {
    if (bridge.connected) {
      return true;
    }
    await connectCommand();
    return bridge.connected;
  };

  const connectCommand = async (): Promise<void> => {
    if (bridge.connected) {
      const choice = await vscode.window.showQuickPick(
        [
          { label: "Disconnect", id: "disconnect" },
          { label: "Open REPL", id: "repl" },
          { label: "Open file browser", id: "ftp" },
        ],
        { title: `Connected to ${bridge.connectedDevice}` }
      );
      if (choice?.id === "disconnect") {
        await bridge.disconnect();
        updateStatus();
      } else if (choice?.id === "repl") {
        openRepl(bridge);
      } else if (choice?.id === "ftp") {
        await ftpProvider.reveal();
      }
      return;
    }

    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "mpftp: listing serial ports…" },
      async () => {
        await bridge.ensureStarted();
      }
    );

    const ports = await bridge.listPorts();
    const items = ports.map((p) => portQuickPick(p));
    if (!items.length) {
      const host = detectHost();
      void vscode.window.showWarningMessage(
        host === "wsl"
          ? "No serial ports found. On WSL, mpftp uses Windows python.exe / COM ports. Is the board plugged in?"
          : "No serial ports found."
      );
      return;
    }

    const cfg = getConfig();
    let device = cfg.autoConnectDevice;
    if (!device) {
      const pick = await vscode.window.showQuickPick(items, {
        title: "Select MicroPython board",
        placeHolder: "Serial port",
      });
      if (!pick) {
        return;
      }
      device = pick.device;
    }

    try {
      await bridge.connect(device);
      updateStatus();
      void vscode.window.showInformationMessage(`mpftp connected: ${device}`);
      await ftpProvider.reveal();
    } catch (e: any) {
      void vscode.window.showErrorMessage(`mpftp connect failed: ${e.message || e}`);
      log.appendLine(String(e));
    }
  };

  ftpProvider = new FtpViewProvider(
    context.extensionUri,
    bridge,
    () => {
      if (!bridge.connected) {
        void vscode.window.showWarningMessage("Connect to a board first");
        return;
      }
      openRepl(bridge);
    },
    connectCommand,
    async () => {
      await bridge.disconnect();
      updateStatus();
    }
  );

  context.subscriptions.push(
    log,
    statusBar,
    bridge,
    vscode.window.registerWebviewViewProvider(FtpViewProvider.viewType, ftpProvider),
    vscode.commands.registerCommand("mpftp.connect", connectCommand),
    vscode.commands.registerCommand("mpftp.disconnect", async () => {
      await bridge.disconnect();
      updateStatus();
    }),
    vscode.commands.registerCommand("mpftp.openFtp", () => ftpProvider.reveal()),
    vscode.commands.registerCommand("mpftp.openRepl", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      openRepl(bridge);
    }),
    vscode.commands.registerCommand("mpftp.softReset", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      await bridge.request("soft_reset");
      void vscode.window.showInformationMessage("Soft reset sent");
    }),
    vscode.commands.registerCommand("mpftp.hardReset", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      await bridge.request("hard_reset");
      await bridge.disconnect().catch(() => undefined);
      updateStatus();
      void vscode.window.showInformationMessage("Hard reset sent — reconnect when ready");
    }),
    vscode.commands.registerCommand("mpftp.bootloader", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      await bridge.request("bootloader");
      await bridge.disconnect().catch(() => undefined);
      updateStatus();
    }),
    vscode.commands.registerCommand("mpftp.runFile", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const ed = vscode.window.activeTextEditor;
      if (!ed) {
        return;
      }
      const source = ed.document.getText();
      const res = await bridge.request<{ output: string }>("run_script", { source, follow: true });
      log.appendLine(res.output || "");
      log.show(true);
    }),
    vscode.commands.registerCommand("mpftp.eval", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const expr = await vscode.window.showInputBox({ prompt: "Expression to eval on board" });
      if (!expr) {
        return;
      }
      const res = await bridge.request<{ value: string }>("eval", { expr });
      void vscode.window.showInformationMessage(res.value);
    }),
    vscode.commands.registerCommand("mpftp.exec", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const code = await vscode.window.showInputBox({ prompt: "Code to exec on board" });
      if (!code) {
        return;
      }
      const res = await bridge.request<{ output: string }>("exec", { code, follow: true });
      log.appendLine(res.output || "");
      log.show(true);
    }),
    vscode.commands.registerCommand("mpftp.rtcGet", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const res = await bridge.request<{ datetime: string }>("rtc_get");
      void vscode.window.showInformationMessage(`RTC: ${res.datetime}`);
    }),
    vscode.commands.registerCommand("mpftp.rtcSet", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const res = await bridge.request<{ datetime: number[] }>("rtc_set");
      void vscode.window.showInformationMessage(`RTC set: ${JSON.stringify(res.datetime)}`);
    }),
    vscode.commands.registerCommand("mpftp.mipInstall", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const pkg = await vscode.window.showInputBox({
        prompt: "Package to install via mip (board needs network)",
        placeHolder: "github:org/repo",
      });
      if (!pkg) {
        return;
      }
      const res = await bridge.request<{ output: string }>("mip_install", {
        packages: [pkg],
        mpy: true,
      });
      log.appendLine(res.output || "");
      log.show(true);
    }),
    vscode.commands.registerCommand("mpftp.df", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const res = await bridge.request<{ mounts: any[] }>("df");
      log.appendLine(JSON.stringify(res.mounts, null, 2));
      log.show(true);
    }),
    vscode.commands.registerCommand("mpftp.refreshPorts", async () => {
      await bridge.ensureStarted();
      const ports = await bridge.listPorts();
      log.appendLine(ports.map((p) => `${p.device}\t${p.product || ""}\t${p.serial_number || ""}`).join("\n"));
      log.show(true);
    })
  );

  bridge.on("connected", updateStatus);
  bridge.on("disconnected", updateStatus);

  const host = detectHost();
  const py = resolvePython(context.extensionPath, getConfig().pythonPath);
  log.appendLine(`[mpftp] host=${host} python=${py}`);
  updateStatus();

  if (getConfig().autoConnectDevice) {
    void connectCommand();
  }
}

function portQuickPick(p: PortInfo) {
  const vidpid =
    p.vid != null && p.pid != null
      ? `${p.vid.toString(16).padStart(4, "0")}:${p.pid.toString(16).padStart(4, "0")}`
      : "";
  return {
    label: p.device,
    description: [p.product, p.manufacturer, vidpid].filter(Boolean).join(" · "),
    detail: p.serial_number || p.description || undefined,
    device: p.device,
  };
}

function updateStatus(): void {
  if (bridge.connected) {
    statusBar.text = `$(check) mpftp: ${bridge.connectedDevice}`;
    statusBar.tooltip = "mpftp connected — click for actions";
  } else {
    statusBar.text = "$(plug) mpftp";
    statusBar.tooltip = "Connect to MicroPython board";
  }
}

export function deactivate(): void {
  bridge?.dispose();
}
