import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { ActivityLog } from "../activityLog";
import { SidecarBridge } from "../bridge/SidecarBridge";
import { detectHost, getConfig } from "../platform";
import { FirmwareEngine, StreamHandle } from "./engine";
import { PartitionsPanel } from "./PartitionsPanel";

interface Selection {
  port: string;
  board: string;
  variant: string;
}

interface Prefs {
  reconnectAfterFlash: boolean;
  alsoFlashAfterBuild: boolean;
  device: string;
}

const STATE_FILE = path.join(os.homedir(), ".mpftp", "firmware.json");

/**
 * The Firmware build/flash webview (editor tab). Guides the user through
 * selecting a target, building once, then flashing one or many boards.
 */
export class FirmwarePanel {
  private panel: vscode.WebviewPanel | undefined;
  private readonly engine: FirmwareEngine;
  private discovery: Record<string, unknown> = {};
  private tree: any[] = [];
  private cmods: Record<string, unknown> = {};
  private flashers: Record<string, string> = {};
  private selection: Selection = { port: "", board: "", variant: "" };
  private prefs: Prefs = { reconnectAfterFlash: false, alsoFlashAfterBuild: false, device: "" };
  private activeStream: StreamHandle | undefined;
  private busy = false;
  private partitions: PartitionsPanel | undefined;
  private lastDetect: Record<string, unknown> | undefined;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly extensionPath: string,
    private readonly bridge: SidecarBridge,
    private readonly activity: ActivityLog
  ) {
    this.engine = new FirmwareEngine(extensionPath);
    this.loadPrefs();
  }

  reveal(): void {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Active);
      return;
    }
    this.panel = vscode.window.createWebviewPanel(
      "mpftp.firmware",
      "mpftp Firmware",
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
    this.panel.onDidDispose(() => {
      this.activeStream?.cancel();
      this.activeStream = undefined;
      this.panel = undefined;
    });
  }

  // ---------------------------------------------------------------------- //
  // Preferences (mirror of ~/.mpftp/firmware.json written by the engine)
  // ---------------------------------------------------------------------- //

  private loadPrefs(): void {
    try {
      const s = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
      if (s.lastSelection) {
        this.selection = {
          port: s.lastSelection.port || "",
          board: s.lastSelection.board || "",
          variant: s.lastSelection.variant || "",
        };
      }
      this.prefs.device = s.lastDevice || "";
      if (typeof s.reconnectAfterFlash === "boolean") {
        this.prefs.reconnectAfterFlash = s.reconnectAfterFlash;
      }
      if (typeof s.alsoFlashAfterBuild === "boolean") {
        this.prefs.alsoFlashAfterBuild = s.alsoFlashAfterBuild;
      }
    } catch {
      /* first run */
    }
  }

  private savePrefs(): void {
    let s: Record<string, unknown> = {};
    try {
      s = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
    } catch {
      /* ignore */
    }
    s.lastSelection = this.selection;
    s.lastDevice = this.prefs.device;
    s.reconnectAfterFlash = this.prefs.reconnectAfterFlash;
    s.alsoFlashAfterBuild = this.prefs.alsoFlashAfterBuild;
    try {
      fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
      fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2), "utf8");
    } catch {
      /* ignore */
    }
  }

  // ---------------------------------------------------------------------- //
  // Engine path args
  // ---------------------------------------------------------------------- //

  private mpDir(): string {
    return (
      getConfig().micropythonPath ||
      (this.discovery.micropython as string) ||
      ""
    );
  }

  private pathArgs(): Record<string, string> {
    const cfg = getConfig();
    const args: Record<string, string> = {};
    const mp = this.mpDir();
    if (mp) {
      args.mp = mp;
    }
    if (cfg.idfPath) {
      args.idf = cfg.idfPath;
    }
    if (cfg.emsdkPath) {
      args.emsdk = cfg.emsdkPath;
    }
    return args;
  }

  // ---------------------------------------------------------------------- //
  // Messages
  // ---------------------------------------------------------------------- //

  private post(msg: Record<string, unknown>): void {
    void this.panel?.webview.postMessage(msg);
  }

  private log(line: string): void {
    this.post({ type: "log", line });
  }

  private phase(phase: string, text = ""): void {
    this.post({ type: "phase", phase, text });
  }

  private async onMessage(msg: any): Promise<void> {
    switch (msg?.type) {
      case "ready":
        await this.refreshAll();
        break;
      case "refresh":
        await this.refreshAll();
        break;
      case "changePath":
        await this.changePath();
        break;
      case "select":
        this.selection = {
          port: msg.port || "",
          board: msg.board || "",
          variant: msg.variant || "",
        };
        this.savePrefs();
        await this.refreshArtifact();
        break;
      case "build":
        await this.doBuild(!!msg.clean);
        break;
      case "clean":
        await this.doClean();
        break;
      case "flash":
        await this.doFlash();
        break;
      case "detect":
        await this.detectDevice();
        break;
      case "cancel":
        this.activeStream?.cancel();
        this.log("[mpftp] cancel requested");
        break;
      case "refreshDevices":
        await this.pushDevices();
        break;
      case "changeDevice":
        await this.changeDevice();
        break;
      case "setPref":
        if (msg.key === "reconnectAfterFlash") {
          this.prefs.reconnectAfterFlash = !!msg.value;
        } else if (msg.key === "alsoFlashAfterBuild") {
          this.prefs.alsoFlashAfterBuild = !!msg.value;
        }
        this.savePrefs();
        break;
      case "openPartitions":
        this.openPartitions();
        break;
      case "loadPartitions":
        await this.loadPartitions();
        break;
      case "applySplit":
        await this.applySplit(Number(msg.storageMb) || 0);
        break;
      default:
        break;
    }
  }

  // ---------------------------------------------------------------------- //
  // Discovery / state push
  // ---------------------------------------------------------------------- //

  private async refreshAll(): Promise<void> {
    try {
      this.discovery = await this.engine.run("discover", this.pathArgs());
    } catch (e: any) {
      this.log(`[mpftp] discover failed: ${e?.message || e}`);
    }
    if (!this.mpDir()) {
      this.pushState();
      return;
    }
    try {
      const t = await this.engine.run<{ ports: any[] }>("tree", this.pathArgs());
      this.tree = t.ports || [];
    } catch (e: any) {
      this.log(`[mpftp] tree failed: ${e?.message || e}`);
    }
    try {
      this.cmods = await this.engine.run("cmods", this.pathArgs());
    } catch {
      /* ignore */
    }
    try {
      const f = await this.engine.run<{ flashers: Record<string, string> }>("flashers");
      this.flashers = f.flashers || {};
    } catch {
      /* ignore */
    }
    // Auto-select from a connected device if we have no selection yet.
    await this.autoSelectFromDevice();
    this.pushState();
    await this.refreshArtifact();
    await this.pushDevices();
  }

  // ---------------------------------------------------------------------- //
  // Detect (esptool-first chip / flash / security probe)
  // ---------------------------------------------------------------------- //

  /**
   * One-click device probe. esptool-first (works on a bare board), with
   * optional MicroPython enrichment only if a session was already active.
   */
  private async detectDevice(): Promise<void> {
    if (this.busy) {
      return;
    }
    const device = this.prefs.device || this.bridge.connectedDevice || "";
    if (!device) {
      void vscode.window.showWarningMessage(
        "Select a device (serial port) to detect."
      );
      return;
    }

    this.busy = true;
    this.phase("detecting", "Detecting…");
    this.post({ type: "clearLog" });
    this.log(`[mpftp] detecting ${device}…`);

    // Enrichment only: gather MicroPython hints while still connected.
    let mpHints: Record<string, unknown> = {};
    const wasConnected =
      !!this.bridge.connectedDevice && this.bridge.connectedDevice === device;
    if (wasConnected) {
      mpHints = await this.gatherMpHints();
      try {
        await this.bridge.disconnect();
        this.log(`[mpftp] released ${device} for esptool`);
      } catch {
        /* ignore */
      }
    }

    let res: Record<string, unknown> | undefined;
    try {
      res = await this.engine.run("detect", {
        ...this.pathArgs(),
        device,
        esptool: this.engine.esptoolCommand() || undefined,
        mpHints: Object.keys(mpHints).length ? JSON.stringify(mpHints) : undefined,
      });
    } catch (e: any) {
      this.log(`[mpftp] detect failed: ${e?.message || e}`);
    }

    this.busy = false;

    if (res && res.ok !== false) {
      this.lastDetect = res;
      this.applyDetect(res);
      const chip = String((res as any).chip || "");
      const board = String(((res as any).match || {}).board || "");
      this.phase(
        "ready",
        res.espressif === false
          ? "Detected — not an Espressif chip"
          : `Detected ${chip}${board ? " · " + board : ""}`
      );
      this.activity.event("firmware_detect", {
        message: chip || "detect",
        data: { device, board },
      });
    } else {
      this.phase("failed", "Detect failed");
    }

    if (wasConnected) {
      try {
        await this.bridge.connect(device);
        this.log(`[mpftp] reconnected ${device}`);
        if (res && res.ok !== false && !Object.keys(mpHints).length) {
          const fresh = await this.gatherMpHints();
          if (Object.keys(fresh).length && this.lastDetect) {
            (this.lastDetect as any).mp = fresh;
          }
        }
      } catch (e: any) {
        this.log(`[mpftp] reconnect failed (no MicroPython?): ${e?.message || e}`);
      }
    }

    this.post({ type: "detect", detect: this.lastDetect || null });
    this.pushState();
    await this.refreshArtifact();
    await this.pushDevices();
  }

  /** Best-effort MicroPython/CircuitPython runtime hints (enrichment only). */
  private async gatherMpHints(): Promise<Record<string, unknown>> {
    const code = [
      "import json as _j",
      "_d = {}",
      "try:\n import sys\n _d['platform']=sys.platform\n _d['impl']=sys.implementation.name\n _d['build']=getattr(sys.implementation,'_build','')\nexcept Exception: pass",
      "try:\n import os as _os\n _d['machine']=_os.uname().machine\nexcept Exception: pass",
      "try:\n import machine as _m\n _d['freq']=_m.freq()\nexcept Exception: pass",
      "try:\n import gc as _g\n _d['memfree']=_g.mem_free()\nexcept Exception: pass",
      "try:\n import esp as _e\n _d['flash']=_e.flash_size()\nexcept Exception: pass",
      "print('MPHINTS:'+_j.dumps(_d))",
    ].join("\n");
    try {
      const res = await this.bridge.request<{ output?: string }>("exec", {
        code,
        follow: true,
      });
      const out = String((res as any)?.output ?? "");
      const line = out.split(/\r?\n/).find((l) => l.startsWith("MPHINTS:"));
      if (line) {
        return JSON.parse(line.slice("MPHINTS:".length)) as Record<string, unknown>;
      }
    } catch {
      /* board busy / not MicroPython */
    }
    return {};
  }

  /** Apply Detect's suggested port/board/variant when confidence allows. */
  private applyDetect(res: Record<string, unknown>): void {
    const m = (res.match || {}) as Record<string, unknown>;
    const conf = String(m.confidence || "");
    const port = String(m.port || "");
    if (!port) {
      return;
    }
    if (res.espressif === false) {
      // Non-Espressif: only select the firmware port, never force an ESP board.
      if (conf === "family-only") {
        this.selectPort(port);
      }
      return;
    }
    if (conf === "matched" || conf === "family-only") {
      this.selection = {
        port,
        board: String(m.board || ""),
        variant: String(m.variant || ""),
      };
      this.savePrefs();
    }
  }

  /** Select a port node, picking a GENERIC board when the board is unknown. */
  private selectPort(port: string, board = "", variant = ""): void {
    let b = board;
    const node = this.tree.find((p: any) => p.port === port);
    if (!b && node?.boards?.length) {
      const generic = node.boards.find((x: any) => /GENERIC/.test(x.board));
      b = (generic || node.boards[0]).board;
    }
    this.selection = { port, board: b, variant };
    this.savePrefs();
  }

  private async autoSelectFromDevice(): Promise<void> {
    if (this.selection.port) {
      return;
    }
    // Best-effort chip hint: ask the connected board for its platform.
    let hintPort = "";
    if (this.bridge.connected) {
      try {
        const res = await this.bridge.request<{ value?: string }>("eval", {
          expr: "__import__('sys').platform",
        });
        const plat = String((res as any)?.value ?? res ?? "").replace(/['"]/g, "").trim();
        const map: Record<string, string> = {
          esp32: "esp32",
          rp2: "rp2",
          samd: "samd",
          pyboard: "stm32",
          mimxrt: "mimxrt",
          nrf52: "nrf",
        };
        hintPort = map[plat] || "";
      } catch {
        /* board may be busy */
      }
    }
    const flashable = this.tree.filter((p) => p.flashable);
    const hinted = hintPort ? this.tree.find((p) => p.port === hintPort) : undefined;
    const pick = hinted || flashable.find((p) => p.port === "esp32") || flashable[0];
    if (pick) {
      this.selection.port = pick.port;
      if (pick.boards?.length) {
        const generic = pick.boards.find((b: any) => /GENERIC/.test(b.board));
        this.selection.board = (generic || pick.boards[0]).board;
      } else if (pick.variants?.length) {
        this.selection.variant = "";
      }
    }
  }

  private async refreshArtifact(): Promise<void> {
    if (!this.selection.port || !this.mpDir()) {
      this.post({ type: "artifact", artifact: { ready: false } });
      return;
    }
    try {
      const info = await this.engine.run("artifact", {
        ...this.pathArgs(),
        port: this.selection.port,
        board: this.selection.board,
        variant: this.selection.variant,
      });
      this.post({ type: "artifact", artifact: info });
    } catch {
      this.post({ type: "artifact", artifact: { ready: false } });
    }
  }

  private async pushDevices(): Promise<void> {
    let devices: Array<{ device: string; description?: string }> = [];
    try {
      await this.bridge.ensureStarted();
      const ports = await this.bridge.listPorts();
      devices = ports.map((p) => ({
        device: p.device,
        description: p.product || p.description || p.manufacturer || "",
      }));
    } catch {
      /* ignore */
    }
    if (!this.prefs.device && this.bridge.connectedDevice) {
      this.prefs.device = this.bridge.connectedDevice;
    }
    this.post({
      type: "devices",
      devices,
      device: this.prefs.device,
      connectedDevice: this.bridge.connectedDevice || "",
    });
  }

  private pushState(): void {
    const cfg = getConfig();
    this.post({
      type: "state",
      host: detectHost(),
      micropython: this.mpDir(),
      workspace: this.discovery.workspace || null,
      idf: cfg.idfPath || this.discovery.idf || null,
      emsdk: cfg.emsdkPath || this.discovery.emsdk || null,
      tree: this.tree,
      cmods: this.cmods,
      flashers: this.flashers,
      selection: this.selection,
      prefs: this.prefs,
      connectedDevice: this.bridge.connectedDevice || "",
      detect: this.lastDetect || null,
      busy: this.busy,
    });
  }

  // ---------------------------------------------------------------------- //
  // Change MicroPython path
  // ---------------------------------------------------------------------- //

  private async changePath(): Promise<void> {
    const uris = await vscode.window.showOpenDialog({
      canSelectFiles: false,
      canSelectFolders: true,
      canSelectMany: false,
      openLabel: "Select MicroPython folder",
      title: "Select the MicroPython checkout (contains ports/ and py/)",
    });
    if (!uris?.[0]) {
      return;
    }
    const p = uris[0].fsPath;
    if (!fs.existsSync(path.join(p, "ports")) || !fs.existsSync(path.join(p, "py"))) {
      void vscode.window.showErrorMessage(
        "That folder is not a MicroPython tree (missing ports/ or py/)."
      );
      return;
    }
    await vscode.workspace
      .getConfiguration("mpftp")
      .update("micropythonPath", p, vscode.ConfigurationTarget.Global);
    await this.refreshAll();
  }

  // ---------------------------------------------------------------------- //
  // Build / Clean
  // ---------------------------------------------------------------------- //

  private guardMp(): boolean {
    if (!this.mpDir()) {
      void vscode.window.showErrorMessage(
        "MicroPython tree not found. Use Change… to select it."
      );
      return false;
    }
    if (!this.selection.port) {
      void vscode.window.showWarningMessage("Select a target port/board first.");
      return false;
    }
    return true;
  }

  private async doBuild(clean: boolean): Promise<void> {
    if (this.busy || !this.guardMp()) {
      return;
    }
    this.busy = true;
    this.phase("building", clean ? "Clean build…" : "Building…");
    this.post({ type: "clearLog" });
    this.activity.event("firmware_build_start", {
      data: { ...this.selection, clean },
    });
    const handle = this.engine.stream(
      "build",
      {
        ...this.pathArgs(),
        port: this.selection.port,
        board: this.selection.board,
        variant: this.selection.variant,
        clean,
      },
      (line) => this.log(line)
    );
    this.activeStream = handle;
    const result = await handle.done;
    this.activeStream = undefined;
    this.busy = false;
    if (result.ok) {
      this.phase("ready", "Build ready");
      this.post({ type: "artifact", artifact: result });
      this.activity.event("firmware_build_ok", { data: { ...this.selection } });
      if (this.prefs.alsoFlashAfterBuild) {
        await this.doFlash();
      }
    } else {
      this.phase("failed", String(result.error || "Build failed"));
      this.activity.event("firmware_build_fail", {
        data: { ...this.selection, error: result.error },
      });
    }
  }

  private async doClean(): Promise<void> {
    if (this.busy || !this.guardMp()) {
      return;
    }
    this.busy = true;
    this.phase("building", "Cleaning…");
    this.post({ type: "clearLog" });
    const handle = this.engine.stream(
      "clean",
      {
        ...this.pathArgs(),
        port: this.selection.port,
        board: this.selection.board,
        variant: this.selection.variant,
      },
      (line) => this.log(line)
    );
    this.activeStream = handle;
    const result = await handle.done;
    this.activeStream = undefined;
    this.busy = false;
    this.phase(result.ok ? "idle" : "failed", result.ok ? "Cleaned" : "Clean failed");
    await this.refreshArtifact();
  }

  // ---------------------------------------------------------------------- //
  // Flash
  // ---------------------------------------------------------------------- //

  private async doFlash(): Promise<void> {
    if (this.busy || !this.guardMp()) {
      return;
    }
    const port = this.selection.port;
    if (!this.flashers[port]) {
      void vscode.window.showWarningMessage(`Flashing is not supported for '${port}'.`);
      return;
    }
    const device = this.prefs.device;
    const isSerial = port === "esp32";
    if (isSerial && !device) {
      void vscode.window.showWarningMessage("Select a device (serial port) to flash.");
      return;
    }

    if (!(await this.securityGuard(port, device))) {
      return;
    }

    this.busy = true;
    this.phase("flashing", "Flashing…");
    this.post({ type: "clearLog" });

    // Release the port if we are connected to the flash target.
    let wasConnected = false;
    const connected = this.bridge.connectedDevice;
    if (connected && (connected === device || !isSerial)) {
      wasConnected = true;
      try {
        if (port === "rp2" || port === "samd") {
          this.log("[mpftp] rebooting board into bootloader…");
          try {
            await this.bridge.request("bootloader", {});
          } catch {
            /* board may already be in bootloader */
          }
        }
        await this.bridge.disconnect();
        this.log(`[mpftp] disconnected ${connected} for flashing`);
      } catch {
        /* ignore */
      }
    }

    const esptool = this.engine.esptoolCommand();
    const handle = this.engine.stream(
      "flash",
      {
        ...this.pathArgs(),
        port,
        board: this.selection.board,
        variant: this.selection.variant,
        device,
        esptool: esptool || undefined,
      },
      (line) => this.log(line)
    );
    this.activeStream = handle;
    const result = await handle.done;
    this.activeStream = undefined;
    this.busy = false;

    if (result.ok) {
      this.phase("ready", "Flashed — ready for the next board");
      this.activity.event("firmware_flash_ok", {
        data: { ...this.selection, device },
      });
      this.post({ type: "flashed", device });
    } else {
      this.phase("failed", String(result.error || "Flash failed"));
      this.activity.event("firmware_flash_fail", {
        data: { ...this.selection, device, error: result.error },
      });
    }

    if (wasConnected && this.prefs.reconnectAfterFlash && device) {
      try {
        await this.bridge.connect(device);
        this.log(`[mpftp] reconnected ${device}`);
      } catch (e: any) {
        this.log(`[mpftp] reconnect failed: ${e?.message || e}`);
      }
    }
    await this.pushDevices();
    await this.refreshArtifact();
  }

  /**
   * Block esp32 flashing when the last Detect reported flash encryption or
   * secure boot enabled — writing a fresh image can brick or fail to boot.
   * Returns false to abort the flash.
   */
  private async securityGuard(port: string, device: string): Promise<boolean> {
    if (port !== "esp32" || !this.lastDetect) {
      return true;
    }
    const det = this.lastDetect as any;
    if (det.device !== device || !det.security) {
      return true;
    }
    const enc = /enable/i.test(String(det.security.flashEncryption || ""));
    const sb = /enable/i.test(String(det.security.secureBoot || ""));
    if (!enc && !sb) {
      return true;
    }
    const what = [enc ? "flash encryption" : "", sb ? "secure boot" : ""]
      .filter(Boolean)
      .join(" and ");
    const pick = await vscode.window.showWarningMessage(
      `This device reports ${what} enabled. Flashing a new image may brick it ` +
        `or fail to boot. Continue anyway?`,
      { modal: true },
      "Flash anyway"
    );
    return pick === "Flash anyway";
  }

  private async changeDevice(): Promise<void> {
    const port = this.selection.port;
    if (port === "rp2" || port === "samd") {
      const uris = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        openLabel: "Select UF2 drive",
        title: "Select the board's UF2 bootloader drive (RPI-RP2, etc.)",
      });
      if (uris?.[0]) {
        this.prefs.device = uris[0].fsPath;
        this.savePrefs();
        await this.pushDevices();
      }
      return;
    }
    try {
      await this.bridge.ensureStarted();
      const ports = await this.bridge.listPorts();
      const items = ports.map((p) => ({
        label: p.device,
        description: p.product || p.description || "",
      }));
      const manual = { label: "$(edit) Enter manually…", description: "" };
      const pick = await vscode.window.showQuickPick([...items, manual], {
        title: "Select device to flash",
      });
      if (!pick) {
        return;
      }
      if (pick === manual) {
        const v = await vscode.window.showInputBox({ prompt: "Device (e.g. COM5)" });
        if (v) {
          this.prefs.device = v;
        }
      } else {
        this.prefs.device = pick.label;
      }
      this.savePrefs();
      await this.pushDevices();
    } catch (e: any) {
      void vscode.window.showErrorMessage(`Could not list ports: ${e?.message || e}`);
    }
  }

  // ---------------------------------------------------------------------- //
  // Partitions
  // ---------------------------------------------------------------------- //

  private partArgs(): Record<string, string> {
    return {
      ...this.pathArgs(),
      board: this.selection.board,
      variant: this.selection.variant,
    };
  }

  /** Enumerate stock/override partition tables for the firmware/storage split. */
  private async loadPartitions(): Promise<void> {
    if (this.selection.port !== "esp32") {
      this.post({ type: "partitions", partitions: null, flashMb: 0 });
      return;
    }
    try {
      const res = await this.engine.run("partitions", this.partArgs(), ["candidates"]);
      const flashMb = Number((this.lastDetect as any)?.flashMb || 0);
      this.post({ type: "partitions", partitions: res, flashMb });
    } catch (e: any) {
      this.log(`[mpftp] partitions load failed: ${e?.message || e}`);
      this.post({ type: "partitions", partitions: null, flashMb: 0 });
    }
  }

  /** Save a firmware/storage split by resizing (or adding) the storage partition. */
  private async applySplit(storageMb: number): Promise<void> {
    if (this.selection.port !== "esp32") {
      return;
    }
    const flashMb = Number((this.lastDetect as any)?.flashMb || 0);
    const args: Record<string, string | number> = {
      ...this.partArgs(),
      storageBytes: Math.round(storageMb * 1024 * 1024),
    };
    if (flashMb) {
      args.flashBytes = flashMb * 1024 * 1024;
      args.flashMb = flashMb;
    }
    try {
      const res = await this.engine.run("partitions", args, ["split"]);
      this.activity.event("firmware_partitions_split", {
        data: { ...this.selection, storageMb },
      });
      this.post({ type: "splitApplied", ...(res as Record<string, unknown>) });
      await this.loadPartitions();
      await this.refreshArtifact();
    } catch (e: any) {
      this.log(`[mpftp] split failed: ${e?.message || e}`);
    }
  }

  private openPartitions(): void {
    if (this.selection.port !== "esp32") {
      void vscode.window.showInformationMessage(
        "The partition editor is for esp32 boards."
      );
      return;
    }
    if (!this.partitions) {
      this.partitions = new PartitionsPanel(
        this.extensionUri,
        this.extensionPath,
        this.activity
      );
    }
    this.partitions.reveal(this.mpDir(), this.selection.board, this.selection.variant);
  }

  // ---------------------------------------------------------------------- //
  // HTML
  // ---------------------------------------------------------------------- //

  private getHtml(webview: vscode.Webview): string {
    const css = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "firmware.css")
    );
    const js = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "firmware.js")
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
  <title>mpftp Firmware</title>
</head>
<body>
  <div id="app"></div>
  <script src="${js}"></script>
</body>
</html>`;
  }
}
