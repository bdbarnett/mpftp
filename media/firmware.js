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
    artifact: { ready: false },
    phase: "idle",
    phaseText: "Idle",
    filter: "",
    busy: false,
  };

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

    if (port === "esp32") {
      const part = el("button", "btn ghost", "Partitions…");
      part.onclick = () => vscode.postMessage({ type: "openPartitions" });
      actions.appendChild(part);
    }
    card.appendChild(actions);

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
      ready: ["Ready", "ok"],
      failed: ["Failed", "err"],
    };
    const [label, kind] = map[model.phase] || ["Idle", "muted"];
    const p = el("span", "phase-pill " + kind);
    if (model.phase === "building" || model.phase === "flashing") {
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
          busy: msg.busy,
        });
        render();
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
        model.busy = msg.phase === "building" || msg.phase === "flashing";
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
