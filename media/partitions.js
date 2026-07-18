/* global acquireVsCodeApi */
(function () {
  const vscode = acquireVsCodeApi();

  const model = {
    board: "",
    variant: "",
    rows: [],
    source: "",
    usingOverride: false,
    warnings: [],
    flashSize: 4 * 1024 * 1024,
  };

  const PRESETS = {
    "Single app (4MiB)": {
      flash: 4 * 1024 * 1024,
      rows: [
        { name: "nvs", type: "data", subtype: "nvs", offset: "0x9000", size: "0x6000", flags: "" },
        { name: "phy_init", type: "data", subtype: "phy", offset: "0xf000", size: "0x1000", flags: "" },
        { name: "factory", type: "app", subtype: "factory", offset: "0x10000", size: "0x1F0000", flags: "" },
        { name: "vfs", type: "data", subtype: "fat", offset: "0x200000", size: "0x200000", flags: "" },
      ],
    },
    "OTA (4MiB)": {
      flash: 4 * 1024 * 1024,
      rows: [
        { name: "nvs", type: "data", subtype: "nvs", offset: "0x9000", size: "0x5000", flags: "" },
        { name: "otadata", type: "data", subtype: "ota", offset: "0xe000", size: "0x2000", flags: "" },
        { name: "ota_0", type: "app", subtype: "ota_0", offset: "0x10000", size: "0x180000", flags: "" },
        { name: "ota_1", type: "app", subtype: "ota_1", offset: "0x190000", size: "0x180000", flags: "" },
        { name: "vfs", type: "data", subtype: "fat", offset: "0x310000", size: "0xF0000", flags: "" },
      ],
    },
    "Large VFS (8MiB)": {
      flash: 8 * 1024 * 1024,
      rows: [
        { name: "nvs", type: "data", subtype: "nvs", offset: "0x9000", size: "0x6000", flags: "" },
        { name: "phy_init", type: "data", subtype: "phy", offset: "0xf000", size: "0x1000", flags: "" },
        { name: "factory", type: "app", subtype: "factory", offset: "0x10000", size: "0x1F0000", flags: "" },
        { name: "vfs", type: "data", subtype: "fat", offset: "0x200000", size: "0x600000", flags: "" },
      ],
    },
  };

  const COLS = [
    { key: "name", label: "Name" },
    { key: "type", label: "Type" },
    { key: "subtype", label: "SubType" },
    { key: "offset", label: "Offset" },
    { key: "size", label: "Size" },
    { key: "flags", label: "Flags" },
  ];

  const el = (tag, cls, txt) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  };

  function parseSize(v) {
    v = (v || "").trim().toLowerCase();
    if (!v) return null;
    try {
      let mult = 1;
      if (v.endsWith("k")) {
        mult = 1024;
        v = v.slice(0, -1);
      } else if (v.endsWith("m")) {
        mult = 1024 * 1024;
        v = v.slice(0, -1);
      }
      const n = v.startsWith("0x") ? parseInt(v, 16) : parseInt(v, 10);
      return isNaN(n) ? null : n * mult;
    } catch {
      return null;
    }
  }

  function totalSize() {
    let sum = 0;
    for (const r of model.rows) {
      const s = parseSize(r.size);
      if (s) sum += s;
    }
    return sum;
  }

  function render() {
    const app = document.getElementById("app");
    app.innerHTML = "";

    const hdr = el("header", "hdr");
    const left = el("div");
    const brand = el("div", "brand");
    brand.appendChild(el("span", "brand-mark", "mpftp"));
    brand.appendChild(el("span", "brand-title", "Partitions"));
    left.appendChild(brand);
    const sub = model.board
      ? `esp32 / ${model.board}${model.variant ? " / " + model.variant : ""}`
      : "esp32";
    left.appendChild(el("p", "subtitle", sub));
    hdr.appendChild(left);

    const badge = el(
      "span",
      "pill " + (model.usingOverride ? "ok" : "muted"),
      model.usingOverride ? "workspace override" : "stock"
    );
    hdr.appendChild(badge);
    app.appendChild(hdr);

    const srcLine = el("p", "muted sm src");
    srcLine.textContent = model.source ? "Source: " + model.source : "";
    srcLine.title = model.source;
    app.appendChild(srcLine);

    // Table
    const table = el("div", "ptable");
    const head = el("div", "prow phead");
    for (const c of COLS) head.appendChild(el("div", "pcell", c.label));
    head.appendChild(el("div", "pcell pact", ""));
    table.appendChild(head);

    model.rows.forEach((row, i) => {
      const tr = el("div", "prow");
      for (const c of COLS) {
        const cell = el("div", "pcell");
        const input = document.createElement("input");
        input.value = row[c.key] || "";
        input.spellcheck = false;
        input.oninput = (e) => {
          model.rows[i][c.key] = e.target.value;
          updateFooter();
        };
        cell.appendChild(input);
        tr.appendChild(cell);
      }
      const act = el("div", "pcell pact");
      const del = el("button", "btn ghost icon");
      del.appendChild(el("i", "codicon codicon-trash"));
      del.title = "Delete row";
      del.onclick = () => {
        model.rows.splice(i, 1);
        render();
      };
      act.appendChild(del);
      tr.appendChild(act);
      table.appendChild(tr);
    });
    app.appendChild(table);

    const addRow = el("button", "btn ghost sm addrow", "");
    addRow.appendChild(el("i", "codicon codicon-add"));
    addRow.appendChild(el("span", null, "Add row"));
    addRow.onclick = () => {
      model.rows.push({ name: "", type: "data", subtype: "", offset: "", size: "", flags: "" });
      render();
    };
    app.appendChild(addRow);

    // Presets
    const presetRow = el("div", "preset-row");
    presetRow.appendChild(el("span", "muted sm", "Presets:"));
    for (const name of Object.keys(PRESETS)) {
      const b = el("button", "btn ghost sm", name);
      b.onclick = () => {
        const p = PRESETS[name];
        model.flashSize = p.flash;
        model.rows = p.rows.map((r) => ({ ...r }));
        render();
      };
      presetRow.appendChild(b);
    }
    app.appendChild(presetRow);

    // Footer: size bar + warnings + total + actions
    const footer = el("div", "footer");

    const sizeWrap = el("div", "size-wrap");
    const sizeHead = el("div", "size-head");
    sizeHead.appendChild(el("span", "muted sm", "Flash usage"));
    const flashSel = document.createElement("select");
    flashSel.className = "flash-sel";
    for (const mb of [2, 4, 8, 16]) {
      const opt = document.createElement("option");
      opt.value = String(mb * 1024 * 1024);
      opt.textContent = mb + " MiB";
      if (mb * 1024 * 1024 === model.flashSize) opt.selected = true;
      flashSel.appendChild(opt);
    }
    flashSel.onchange = (e) => {
      model.flashSize = parseInt(e.target.value, 10);
      updateFooter();
    };
    sizeHead.appendChild(flashSel);
    sizeWrap.appendChild(sizeHead);
    const bar = el("div", "size-bar");
    const fill = el("div", "size-fill");
    fill.id = "sizeFill";
    bar.appendChild(fill);
    sizeWrap.appendChild(bar);
    footer.appendChild(sizeWrap);

    const warn = el("div", "warnings");
    warn.id = "warnBox";
    footer.appendChild(warn);

    const totalDiv = el("div", "total");
    totalDiv.id = "totalBox";
    footer.appendChild(totalDiv);

    const actions = el("div", "actions");
    const save = el("button", "btn primary", "Save override");
    save.onclick = () => vscode.postMessage({ type: "save", rows: model.rows });
    actions.appendChild(save);

    const reset = el("button", "btn ghost", "Reset to stock");
    reset.disabled = !model.usingOverride;
    reset.onclick = () => vscode.postMessage({ type: "reset" });
    actions.appendChild(reset);
    footer.appendChild(actions);

    app.appendChild(footer);
    updateFooter();
  }

  function updateFooter() {
    const warnBox = document.getElementById("warnBox");
    if (warnBox) {
      warnBox.innerHTML = "";
      const w = localValidate();
      if (w.length) {
        for (const line of w) {
          const item = el("div", "warn-item");
          item.appendChild(el("i", "codicon codicon-warning"));
          item.appendChild(el("span", null, line));
          warnBox.appendChild(item);
        }
      } else {
        const ok = el("div", "warn-item ok");
        ok.appendChild(el("i", "codicon codicon-pass"));
        ok.appendChild(el("span", null, "No overlaps or alignment issues detected."));
        warnBox.appendChild(ok);
      }
    }
    const t = totalSize();
    const totalBox = document.getElementById("totalBox");
    if (totalBox) {
      totalBox.textContent =
        "Used: " +
        (t / 1024 / 1024).toFixed(2) +
        " MiB of " +
        (model.flashSize / 1024 / 1024).toFixed(0) +
        " MiB";
    }
    const fill = document.getElementById("sizeFill");
    if (fill) {
      const pct = Math.min(100, (t / model.flashSize) * 100);
      fill.style.width = pct.toFixed(1) + "%";
      fill.classList.toggle("over", t > model.flashSize);
    }
  }

  function localValidate() {
    const warnings = [];
    let prevEnd = null;
    for (const r of model.rows) {
      const off = parseSize(r.offset);
      const size = parseSize(r.size);
      const name = r.name || "(unnamed)";
      if (size == null) {
        warnings.push(`${name}: missing or invalid size`);
        continue;
      }
      if (off != null) {
        if (prevEnd != null && off < prevEnd) {
          warnings.push(
            `${name}: offset 0x${off.toString(16)} overlaps previous end 0x${prevEnd.toString(16)}`
          );
        }
        if (r.type === "app" && off % 0x10000 !== 0) {
          warnings.push(`${name}: app offset 0x${off.toString(16)} is not 64K-aligned`);
        }
        prevEnd = off + size;
      } else if (prevEnd != null) {
        prevEnd += size;
      }
    }
    return warnings;
  }

  window.addEventListener("message", (ev) => {
    const msg = ev.data;
    if (msg.type === "data") {
      model.board = msg.board || "";
      model.variant = msg.variant || "";
      model.rows = (msg.rows || []).map((r) => ({ ...r }));
      model.source = msg.source || "";
      model.usingOverride = !!msg.usingOverride;
      model.warnings = msg.warnings || [];
      render();
    } else if (msg.type === "saved") {
      render();
    } else if (msg.type === "error") {
      const app = document.getElementById("app");
      const err = el("div", "err-banner", msg.error);
      app.prepend(err);
    }
  });

  vscode.postMessage({ type: "ready" });
})();
