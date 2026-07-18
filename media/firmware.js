/* global acquireVsCodeApi */
(function () {
  const vscode = acquireVsCodeApi();

  const model = {
    host: "",
    micropython: "",
    workspace: "",
    idf: null,
    emsdk: null,
    tree: [],
    cmods: { cmods: [], hasAggregator: false, hasManifest: false },
    flashers: {},
    selection: { port: "", board: "", variant: "" },
    prefs: { reconnectAfterFlash: false, alsoFlashAfterBuild: false, device: "" },
    connectedDevice: "",
    devices: [],
    detect: null,
    partitions: null,
    partitionsFlashMb: 0,
    artifact: { ready: false },
    phase: "idle",
    phaseText: "Idle",
    filter: "",
    busy: false,
  };

  let splitRequested = false;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const el = (tag, cls, txt) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  };

  function humanSize(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }

  function relTime(mtime) {
    if (!mtime) return "";
    const secs = Math.max(0, Math.floor(Date.now() / 1000 - mtime));
    if (secs < 60) return "just now";
    if (secs < 3600) return Math.floor(secs / 60) + " min ago";
    if (secs < 86400) return Math.floor(secs / 3600) + " h ago";
    return Math.floor(secs / 86400) + " d ago";
  }

  // ---------------------------------------------------------------- render

  function render() {
    const app = $("#app");
    app.innerHTML = "";

    app.appendChild(renderHeader());

    if (!model.micropython) {
      app.appendChild(renderNoMp());
      app.appendChild(renderLog());
      return;
    }

    app.appendChild(renderDeviceInfo());

    const grid = el("div", "grid");
    grid.appendChild(renderTarget());
    grid.appendChild(renderModules());
    grid.appendChild(renderBuild());
    grid.appendChild(renderFlash());
    app.appendChild(grid);
    app.appendChild(renderLog());
  }

  function renderHeader() {
    const h = el("header", "hdr");
    const left = el("div", "hdr-left");
    const brand = el("div", "brand");
    brand.appendChild(el("span", "brand-mark", "mpftp"));
    brand.appendChild(el("span", "brand-title", "Firmware"));
    left.appendChild(brand);
    left.appendChild(
      el("p", "subtitle", "Build MicroPython once, then flash one or many boards.")
    );
    h.appendChild(left);

    const right = el("div", "hdr-right");
    const pathRow = el("div", "path-row");
    const icon = el("i", "codicon codicon-repo");
    pathRow.appendChild(icon);
    const pathText = el("span", "path-text", model.micropython || "No MicroPython found");
    pathText.title = model.micropython || "";
    pathRow.appendChild(pathText);
    const change = el("button", "btn ghost sm", "Change…");
    change.onclick = () => vscode.postMessage({ type: "changePath" });
    pathRow.appendChild(change);
    right.appendChild(pathRow);

    const meta = el("div", "meta-row");
    if (model.idf) meta.appendChild(pill("ESP-IDF", "ok"));
    if (model.emsdk) meta.appendChild(pill("emsdk", "ok"));
    meta.appendChild(pill(model.host, "muted"));
    right.appendChild(meta);
    h.appendChild(right);
    return h;
  }

  function pill(text, kind) {
    return el("span", "pill " + (kind || ""), text);
  }

  function renderNoMp() {
    const c = el("div", "card empty");
    c.appendChild(el("h2", null, "MicroPython not found"));
    c.appendChild(
      el(
        "p",
        "muted",
        "Select the folder that contains your MicroPython checkout (with ports/ and py/). User C modules and a frozen manifest are auto-discovered from its parent workspace."
      )
    );
    const b = el("button", "btn primary", "Select MicroPython folder…");
    b.onclick = () => vscode.postMessage({ type: "changePath" });
    c.appendChild(b);
    return c;
  }

  // Device Info — esptool-first Detect
  function mpMachine(d) {
    const mp = (d && d.mp) || {};
    return mp.machine || mp.platform || "";
  }

  function chipTitle(d) {
    if (d.espressif === false) {
      const port = (d.match && d.match.port) || d.suggestedPort || "";
      const name = mpMachine(d) || "Non-Espressif device";
      return name + (port ? " · firmware port: " + port : "");
    }
    const parts = [];
    let head = d.chip || "ESP32";
    if (d.revision) head += " (rev " + d.revision + ")";
    parts.push(head);
    parts.push((d.cores === 1 ? "single" : "dual") + (d.lpCore ? " + LP" : ""));
    if (d.maxMhz) parts.push(d.maxMhz + " MHz");
    if (d.flashMb) parts.push(d.flashMb + " MB flash");
    if (d.psram && d.psram.present) parts.push(d.psram.octal ? "Octal PSRAM" : "PSRAM");
    const m = d.match || {};
    if (m.board) parts.push(m.board + (m.variant ? " / " + m.variant : ""));
    return parts.join(" · ");
  }

  function infoRow(label, value) {
    if (value == null || value === "") return null;
    const row = el("div", "info-row");
    row.appendChild(el("span", "info-label", label));
    row.appendChild(el("span", "info-value", String(value)));
    return row;
  }

  function infoGroup(title, rows) {
    const kept = rows.filter(Boolean);
    if (!kept.length) return null;
    const g = el("div", "info-group");
    g.appendChild(el("div", "info-group-title", title));
    for (const r of kept) g.appendChild(r);
    return g;
  }

  function renderDeviceInfo() {
    const card = el("section", "card device-info");
    const head = el("div", "info-head");
    head.appendChild(el("span", "step-title", "Device Info"));
    const btn = el("button", "btn accent sm", model.busy ? "Detecting…" : "Detect");
    btn.disabled = model.busy;
    btn.title = "Probe the selected device with esptool (bare board OK)";
    btn.onclick = () => vscode.postMessage({ type: "detect" });
    head.appendChild(btn);
    card.appendChild(head);

    const d = model.detect;
    if (!d) {
      card.appendChild(
        el("p", "muted", "No device selected — click Detect to inspect.")
      );
      return card;
    }

    card.appendChild(el("div", "chip-title", chipTitle(d)));

    const mp = d.mp || {};
    const hasMp = !!(mp.platform || mp.machine);
    const grid = el("div", "info-grid");

    if (d.espressif === false) {
      grid.appendChild(
        infoGroup("Identity", [
          infoRow("Board", mpMachine(d)),
          infoRow("Platform", mp.platform),
          infoRow("Firmware port", (d.match && d.match.port) || d.suggestedPort),
        ])
      );
      grid.appendChild(
        infoGroup("Runtime", [
          infoRow("MicroPython", hasMp ? mp.impl || "yes" : null),
          infoRow("Frequency", mp.freq ? fmtFreq(mp.freq) : null),
          infoRow("Free heap", mp.memfree ? humanSize(mp.memfree) : null),
        ])
      );
      card.appendChild(grid);
      card.appendChild(
        el(
          "p",
          "muted sm",
          "Not an Espressif chip — " +
            (d.reason || "esptool did not detect an ESP") +
            ". Build/flash uses the suggested port."
        )
      );
      return card;
    }

    grid.appendChild(
      infoGroup("Identity", [
        infoRow("Chip", d.chip),
        infoRow("Revision", d.revision),
        infoRow("MAC", d.mac),
      ])
    );
    grid.appendChild(
      infoGroup("Performance", [
        infoRow("Cores", (d.cores || "") + (d.lpCore ? " + LP" : "")),
        infoRow("Max clock", d.maxMhz ? d.maxMhz + " MHz" : null),
        infoRow("Current", hasMp && mp.freq ? fmtFreq(mp.freq) : null),
      ])
    );
    grid.appendChild(
      infoGroup("Memory", [
        infoRow("SRAM", d.sramKb ? d.sramKb + " KB" : null),
        infoRow("Flash", d.flashMb ? d.flashMb + " MB" : null),
        infoRow("PSRAM", d.psram && d.psram.present ? d.psram.label || "yes" : null),
        infoRow("Free heap", hasMp && mp.memfree ? humanSize(mp.memfree) : null),
      ])
    );
    const m = d.match || {};
    grid.appendChild(
      infoGroup("Build target", [
        infoRow("Board", m.board),
        infoRow("Variant", m.variant || "default"),
        infoRow("Flash size", m.flashSize),
      ])
    );
    grid.appendChild(
      infoGroup("Security", [
        infoRow(
          "Flash encryption",
          d.security && d.security.available ? d.security.flashEncryption : "unavailable"
        ),
        infoRow(
          "Secure boot",
          d.security && d.security.available ? d.security.secureBoot : "unavailable"
        ),
      ])
    );
    card.appendChild(grid);

    const status = hasMp
      ? "MicroPython running (" + (mp.impl || "mpy") + ")"
      : "No MicroPython (bootloader / other firmware)";
    card.appendChild(el("div", "info-status", status));

    if (m.confidence && m.confidence !== "matched") {
      card.appendChild(
        pill(
          m.confidence === "family-only" ? "family match" : "unconfirmed",
          "muted"
        )
      );
    }
    if (m.variantOptions && m.variantOptions.length) {
      card.appendChild(
        el("p", "hint sm", "Variant options: " + m.variantOptions.join(", "))
      );
    }
    for (const n of m.notes || []) {
      card.appendChild(el("p", "hint sm", n));
    }
    return card;
  }

  function fmtFreq(freq) {
    if (Array.isArray(freq)) return freq[0] / 1e6 + " MHz";
    if (typeof freq === "number") return Math.round(freq / 1e6) + " MHz";
    return String(freq);
  }

  // Step 1 — Target
  function renderTarget() {
    const card = el("section", "card step");
    card.appendChild(stepHead("1", "Target", chipText()));

    const search = el("input", "search");
    search.placeholder = "Filter ports and boards…";
    search.value = model.filter;
    search.oninput = (e) => {
      model.filter = e.target.value.toLowerCase();
      renderTree(treeWrap);
    };
    card.appendChild(search);

    const treeWrap = el("div", "tree");
    renderTree(treeWrap);
    card.appendChild(treeWrap);
    return card;
  }

  function chipText() {
    const s = model.selection;
    if (!s.port) return "none selected";
    return [s.port, s.board || null, s.variant || "default"].filter(Boolean).join(" / ");
  }

  function matches(text) {
    return !model.filter || text.toLowerCase().includes(model.filter);
  }

  function renderTree(wrap) {
    wrap.innerHTML = "";
    for (const port of model.tree) {
      const portMatch = matches(port.port);
      const boards = (port.boards || []).filter(
        (b) => portMatch || matches(b.board)
      );
      const variants = (port.variants || []).filter((v) => portMatch || matches(v));
      if (!portMatch && !boards.length && !variants.length) continue;

      const node = el("div", "tnode");
      const head = el("div", "trow port");
      const caret = el("i", "codicon codicon-chevron-right caret");
      head.appendChild(caret);
      head.appendChild(el("span", "tname", port.port));
      if (port.flashable) head.appendChild(pill(port.flasher, "flash"));
      else head.appendChild(pill("build-only", "muted"));

      const kids = el("div", "tkids");
      const expanded =
        model.selection.port === port.port || !!model.filter;
      if (expanded) {
        node.classList.add("open");
      }
      head.onclick = () => node.classList.toggle("open");

      if (port.kind === "boards") {
        for (const b of boards) {
          kids.appendChild(renderBoard(port, b));
        }
      } else if (port.kind === "variants") {
        for (const v of variants) {
          kids.appendChild(renderLeaf(port.port, "", v, v));
        }
        if (portMatch) {
          kids.appendChild(renderLeaf(port.port, "", "", "default"));
        }
      } else {
        // plain port
        head.classList.add("selectable");
        head.onclick = () => selectTriple(port.port, "", "");
      }
      node.appendChild(head);
      node.appendChild(kids);
      wrap.appendChild(node);
    }
    if (!wrap.children.length) {
      wrap.appendChild(el("p", "muted pad", "No matches."));
    }
  }

  function renderBoard(port, b) {
    if (b.variants && b.variants.length) {
      const node = el("div", "tnode");
      const head = el("div", "trow board");
      const caret = el("i", "codicon codicon-chevron-right caret");
      head.appendChild(caret);
      head.appendChild(el("span", "tname", b.board));
      head.appendChild(pill(b.variants.length + " variants", "muted"));
      const open =
        model.selection.port === port.port && model.selection.board === b.board;
      if (open) node.classList.add("open");
      head.onclick = () => node.classList.toggle("open");
      const kids = el("div", "tkids");
      kids.appendChild(renderLeaf(port.port, b.board, "", "default"));
      for (const v of b.variants) {
        kids.appendChild(renderLeaf(port.port, b.board, v, v));
      }
      node.appendChild(head);
      node.appendChild(kids);
      return node;
    }
    return renderLeaf(port.port, b.board, "", b.board);
  }

  function renderLeaf(port, board, variant, label) {
    const s = model.selection;
    const selected =
      s.port === port && s.board === board && s.variant === variant;
    const leaf = el("div", "trow leaf selectable" + (selected ? " selected" : ""));
    leaf.appendChild(el("span", "tdot"));
    leaf.appendChild(el("span", "tname", label));
    if (selected) leaf.appendChild(el("i", "codicon codicon-check"));
    leaf.onclick = () => selectTriple(port, board, variant);
    return leaf;
  }

  function selectTriple(port, board, variant) {
    model.selection = { port, board, variant };
    model.partitions = null;
    splitRequested = false;
    vscode.postMessage({ type: "select", port, board, variant });
    render();
  }

  // Step 2 — Modules
  function renderModules() {
    const card = el("section", "card step");
    card.appendChild(stepHead("2", "Modules", ""));
    const cm = model.cmods || {};
    const list = cm.cmods || [];
    if (!list.length) {
      card.appendChild(
        el(
          "p",
          "muted",
          cm.hasAggregator === false
            ? "No user C modules found in the workspace. Add micropython.cmake / micropython.mk modules beside the MicroPython checkout to include them."
            : "No user C modules discovered."
        )
      );
    } else {
      const chips = el("div", "chips");
      for (const c of list) {
        const chip = el("span", "chip mod");
        chip.appendChild(el("i", "codicon codicon-package"));
        chip.appendChild(el("span", null, c.name));
        chip.title = c.path + (c.hasManifest ? " (with manifest)" : "");
        chips.appendChild(chip);
      }
      card.appendChild(chips);
    }
    const flags = el("div", "flags");
    if (cm.hasAggregator) flags.appendChild(pill("USER_C_MODULES", "ok"));
    if (cm.hasManifest) flags.appendChild(pill("FROZEN_MANIFEST", "ok"));
    if (flags.children.length) card.appendChild(flags);
    return card;
  }

  // Step 3 — Build
  function renderBuild() {
    const card = el("section", "card step");
    card.appendChild(stepHead("3", "Build", ""));

    const statusRow = el("div", "status-row");
    statusRow.appendChild(phasePill());
    card.appendChild(statusRow);

    if (model.artifact && model.artifact.ready) {
      const info = el("div", "artifact");
      info.appendChild(el("i", "codicon codicon-file-binary"));
      const details = el("div", "artifact-details");
      const name = (model.artifact.artifact || "").split("/").pop();
      details.appendChild(el("div", "artifact-name", name));
      details.appendChild(
        el(
          "div",
          "muted sm",
          humanSize(model.artifact.size) +
            " · built " +
            relTime(model.artifact.mtime)
        )
      );
      info.appendChild(details);
      card.appendChild(info);
    } else {
      card.appendChild(el("p", "muted", "Not built yet for this selection."));
    }

    const actions = el("div", "actions");
    const build = el("button", "btn primary", model.busy ? "Working…" : "Build");
    build.disabled = model.busy || !model.selection.port;
    build.onclick = () => vscode.postMessage({ type: "build" });
    actions.appendChild(build);

    const clean = el("button", "btn ghost", "Clean");
    clean.disabled = model.busy || !model.selection.port;
    clean.onclick = () => vscode.postMessage({ type: "build", clean: true });
    clean.title = "Clean then rebuild";
    actions.appendChild(clean);

    card.appendChild(actions);

    const alsoFlash = checkbox(
      "Also flash after build",
      model.prefs.alsoFlashAfterBuild,
      (v) => vscode.postMessage({ type: "setPref", key: "alsoFlashAfterBuild", value: v })
    );
    card.appendChild(alsoFlash);
    return card;
  }

  // Step 4 — Flash
  function renderFlash() {
    const card = el("section", "card step");
    card.appendChild(stepHead("4", "Flash", ""));

    const port = model.selection.port;
    const flasher = model.flashers[port];
    if (!flasher) {
      card.appendChild(
        el(
          "p",
          "muted",
          port
            ? `Flashing is not supported for '${port}' — build only.`
            : "Select a target to flash."
        )
      );
      return card;
    }

    const devRow = el("div", "device-row");
    const devInfo = el("div", "device-info");
    devInfo.appendChild(el("i", "codicon codicon-plug"));
    const dev = model.prefs.device || "No device selected";
    const devSpan = el("span", "device-name", dev);
    if (model.prefs.device && model.prefs.device === model.connectedDevice) {
      devSpan.appendChild(el("span", "conn-badge", " connected"));
    }
    devInfo.appendChild(devSpan);
    devRow.appendChild(devInfo);

    const refresh = el("button", "btn ghost icon", "");
    refresh.appendChild(el("i", "codicon codicon-refresh"));
    refresh.title = "Refresh devices";
    refresh.onclick = () => vscode.postMessage({ type: "refreshDevices" });
    devRow.appendChild(refresh);

    const change = el("button", "btn ghost sm", "Change…");
    change.onclick = () => vscode.postMessage({ type: "changeDevice" });
    devRow.appendChild(change);
    card.appendChild(devRow);

    const ready = model.artifact && model.artifact.ready;
    const needsDevice = port === "esp32" && !model.prefs.device;

    const actions = el("div", "actions");
    const flash = el("button", "btn accent", "Flash");
    flash.disabled = model.busy || !ready || needsDevice;
    if (!ready) flash.title = "Build this board first";
    else if (needsDevice) flash.title = "Select a device";
    flash.onclick = () => vscode.postMessage({ type: "flash" });
    actions.appendChild(flash);

    card.appendChild(actions);

    if (port === "esp32") {
      renderStorageSplit(card);
    }

    card.appendChild(
      checkbox("Reconnect after flash", model.prefs.reconnectAfterFlash, (v) =>
        vscode.postMessage({ type: "setPref", key: "reconnectAfterFlash", value: v })
      )
    );

    if (ready) {
      card.appendChild(
        el(
          "p",
          "hint sm",
          "Tip: swap boards and click Flash again — no rebuild needed."
        )
      );
    }
    return card;
  }

  // Firmware / storage split (esp32)
  const STORAGE_SUBTYPES = ["fat", "spiffs", "littlefs"];
  const STORAGE_NAMES = ["vfs", "storage", "ffat", "user"];

  function parseCsvSize(v) {
    v = (v || "").trim().toLowerCase();
    if (!v) return 0;
    if (v.endsWith("k")) return (parseInt(v, 10) || 0) * 1024;
    if (v.endsWith("m")) return (parseInt(v, 10) || 0) * 1048576;
    return parseInt(v, v.startsWith("0x") ? 16 : 10) || 0;
  }

  function isStorageRow(r) {
    return (
      r.type === "data" &&
      (STORAGE_SUBTYPES.includes(r.subtype) ||
        STORAGE_NAMES.includes((r.name || "").toLowerCase()))
    );
  }

  function activeTable() {
    const p = model.partitions;
    if (!p || !p.candidates || !p.candidates.length) return null;
    return (
      p.candidates.find((x) => x.isOverride) ||
      p.candidates.find((x) => x.isStock) ||
      p.candidates[0]
    );
  }

  function renderStorageSplit(card) {
    const wrap = el("div", "split");
    const title = el("div", "split-title");
    title.appendChild(el("span", null, "Firmware / storage"));
    const adv = el("button", "btn ghost sm", "Advanced…");
    adv.title = "Open the full partition editor";
    adv.onclick = () => vscode.postMessage({ type: "openPartitions" });
    title.appendChild(adv);
    wrap.appendChild(title);

    const p = model.partitions;
    if (!p) {
      if (!splitRequested) {
        splitRequested = true;
        vscode.postMessage({ type: "loadPartitions" });
      }
      wrap.appendChild(el("p", "muted sm", "Loading partition layout…"));
      card.appendChild(wrap);
      return;
    }
    const c = activeTable();
    if (!c) {
      wrap.appendChild(el("p", "muted sm", "No partition table for this board."));
      card.appendChild(wrap);
      return;
    }

    const rows = c.rows || [];
    const s = rows.find(isStorageRow);
    const storageBytes = s ? parseCsvSize(s.size) : 0;
    const total = c.targetSize || 0;
    const fixed = Math.max(0, total - storageBytes); // firmware + system regions
    const flashMb = model.partitionsFlashMb || 0;
    const flashBytes = flashMb * 1048576;
    const mb = (b) => Math.round((b / 1048576) * 100) / 100;

    if (!flashBytes) {
      wrap.appendChild(
        el(
          "p",
          "hint sm",
          "Detect the board to read its flash size before adjusting storage."
        )
      );
      wrap.appendChild(
        el(
          "p",
          "muted sm",
          `Current: firmware+system ${mb(fixed)} MB · storage ${mb(storageBytes)} MB`
        )
      );
      card.appendChild(wrap);
      return;
    }

    const maxStorage = Math.max(0, flashBytes - fixed);
    const readout = el("div", "split-readout");
    const value = el("span", "split-value");
    const fw = el("span", "split-fw");
    const setLabels = (storageMb) => {
      value.textContent = "storage " + storageMb + " MB";
      fw.textContent =
        "firmware + system " + mb(fixed) + " MB · flash " + flashMb + " MB";
    };
    readout.appendChild(value);
    readout.appendChild(fw);
    wrap.appendChild(readout);

    const slider = document.createElement("input");
    slider.type = "range";
    slider.className = "split-slider";
    slider.min = "0";
    slider.max = String(mb(maxStorage));
    slider.step = "0.25";
    slider.value = String(Math.min(mb(storageBytes), mb(maxStorage)));
    setLabels(slider.value);

    const num = document.createElement("input");
    num.type = "number";
    num.className = "split-num";
    num.min = "0";
    num.max = slider.max;
    num.step = "0.25";
    num.value = slider.value;

    slider.oninput = () => {
      num.value = slider.value;
      setLabels(slider.value);
    };
    num.oninput = () => {
      slider.value = num.value;
      setLabels(num.value);
    };

    const sliderRow = el("div", "split-row");
    sliderRow.appendChild(slider);
    const numWrap = el("div", "split-num-wrap");
    numWrap.appendChild(num);
    numWrap.appendChild(el("span", "muted sm", "MB"));
    sliderRow.appendChild(numWrap);
    wrap.appendChild(sliderRow);

    const apply = el("button", "btn primary sm", "Apply split");
    apply.disabled = model.busy;
    apply.onclick = () =>
      vscode.postMessage({ type: "applySplit", storageMb: Number(num.value) || 0 });
    wrap.appendChild(apply);

    if (p.usingOverride) {
      wrap.appendChild(
        el("p", "hint sm", "Using a saved override; Apply overwrites it.")
      );
    }
    card.appendChild(wrap);
  }

  // Log
  function renderLog() {
    const card = el("section", "card log-card");
    const head = el("div", "log-head");
    head.appendChild(el("span", "log-title", "Build & flash log"));
    const cancel = el("button", "btn ghost sm", "Cancel");
    cancel.disabled = !model.busy;
    cancel.onclick = () => vscode.postMessage({ type: "cancel" });
    head.appendChild(cancel);
    card.appendChild(head);
    const pre = el("pre", "log");
    pre.id = "logPre";
    if (logBuffer.length) pre.textContent = logBuffer.join("\n");
    card.appendChild(pre);
    return card;
  }

  function stepHead(n, title, chip) {
    const h = el("div", "step-head");
    h.appendChild(el("span", "step-num", n));
    h.appendChild(el("span", "step-title", title));
    if (chip) h.appendChild(el("span", "sel-chip", chip));
    return h;
  }

  function phasePill() {
    const map = {
      idle: ["Idle", "muted"],
      building: ["Building", "run"],
      flashing: ["Flashing", "run"],
      detecting: ["Detecting", "run"],
      ready: ["Ready", "ok"],
      failed: ["Failed", "err"],
    };
    const [label, kind] = map[model.phase] || ["Idle", "muted"];
    const p = el("span", "phase-pill " + kind);
    if (model.phase === "building" || model.phase === "flashing" || model.phase === "detecting") {
      p.appendChild(el("i", "codicon codicon-loading spin"));
    }
    p.appendChild(el("span", null, model.phaseText || label));
    return p;
  }

  function checkbox(label, checked, onChange) {
    const wrap = el("label", "check");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!checked;
    input.onchange = (e) => onChange(e.target.checked);
    wrap.appendChild(input);
    wrap.appendChild(el("span", null, label));
    return wrap;
  }

  // ---------------------------------------------------------------- log buffer
  let logBuffer = [];
  function appendLog(line) {
    logBuffer.push(line);
    if (logBuffer.length > 5000) logBuffer = logBuffer.slice(-4000);
    const pre = $("#logPre");
    if (pre) {
      pre.textContent = logBuffer.join("\n");
      pre.scrollTop = pre.scrollHeight;
    }
  }

  // ---------------------------------------------------------------- messages
  window.addEventListener("message", (ev) => {
    const msg = ev.data;
    switch (msg.type) {
      case "state":
        Object.assign(model, {
          host: msg.host,
          micropython: msg.micropython,
          workspace: msg.workspace,
          idf: msg.idf,
          emsdk: msg.emsdk,
          tree: msg.tree || [],
          cmods: msg.cmods || {},
          flashers: msg.flashers || {},
          selection: msg.selection || model.selection,
          prefs: msg.prefs || model.prefs,
          connectedDevice: msg.connectedDevice || "",
          detect: msg.detect !== undefined ? msg.detect : model.detect,
          busy: msg.busy,
        });
        render();
        break;
      case "detect":
        model.detect = msg.detect || null;
        splitRequested = false;
        model.partitions = null;
        render();
        break;
      case "partitions":
        model.partitions = msg.partitions || null;
        model.partitionsFlashMb = msg.flashMb || 0;
        render();
        break;
      case "splitApplied":
        appendLog("[mpftp] storage split saved: " + (msg.overridePath || ""));
        for (const w of msg.warnings || []) appendLog("[mpftp] " + w);
        break;
      case "artifact":
        model.artifact = msg.artifact || { ready: false };
        render();
        break;
      case "devices":
        model.devices = msg.devices || [];
        model.prefs.device = msg.device || model.prefs.device;
        model.connectedDevice = msg.connectedDevice || "";
        render();
        break;
      case "phase":
        model.phase = msg.phase;
        model.phaseText = msg.text || "";
        model.busy =
          msg.phase === "building" ||
          msg.phase === "flashing" ||
          msg.phase === "detecting";
        render();
        break;
      case "clearLog":
        logBuffer = [];
        appendLog("");
        break;
      case "log":
        appendLog(msg.line);
        break;
      case "flashed":
        appendLog("[mpftp] ✓ flashed " + (msg.device || ""));
        break;
      default:
        break;
    }
  });

  vscode.postMessage({ type: "ready" });
})();
