/* global acquireVsCodeApi */
(function () {
  const vscode = acquireVsCodeApi();

  const state = {
    connected: false,
    device: "",
    localPath: "",
    remotePath: "/",
    localEntries: [],
    remoteEntries: [],
    localSelected: new Set(),
    remoteSelected: new Set(),
    focus: "local",
  };

  const $ = (id) => document.getElementById(id);
  const ctxMenu = $("ctxMenu");

  function fmtSize(n, isDir) {
    if (isDir) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  function joinLocal(base, name) {
    if (!base) return name;
    const sep = base.includes("\\") ? "\\" : "/";
    return base.replace(/[\\/]+$/, "") + sep + name;
  }

  function joinRemote(base, name) {
    if (!base || base === "/") return "/" + name.replace(/^\/+/, "");
    return base.replace(/\/+$/, "") + "/" + name;
  }

  function parentRemote(p) {
    if (!p || p === "/") return "/";
    const trimmed = p.replace(/\/+$/, "");
    const idx = trimmed.lastIndexOf("/");
    return idx <= 0 ? "/" : trimmed.slice(0, idx);
  }

  /** Codicon + Seti-like color kind for Cursor-style explorer icons. */
  function entryIcon(entry) {
    if (!entry) return { icon: "codicon-file", kind: "file" };
    if (entry.isDir) return { icon: "codicon-folder", kind: "folder" };
    const name = (entry.name || "").toLowerCase();
    if (name.endsWith(".py") || name.endsWith(".pyi")) return { icon: "codicon-file-code", kind: "python" };
    if (name.endsWith(".ts") || name.endsWith(".tsx")) return { icon: "codicon-file-code", kind: "ts" };
    if (name.endsWith(".js") || name.endsWith(".jsx") || name.endsWith(".mjs") || name.endsWith(".cjs")) {
      return { icon: "codicon-file-code", kind: "js" };
    }
    if (name.endsWith(".json") || name.endsWith(".jsonc")) return { icon: "codicon-json", kind: "json" };
    if (name.endsWith(".md") || name.endsWith(".markdown")) return { icon: "codicon-markdown", kind: "md" };
    if (name.endsWith(".html") || name.endsWith(".htm")) return { icon: "codicon-file-code", kind: "html" };
    if (name.endsWith(".css") || name.endsWith(".scss") || name.endsWith(".less")) {
      return { icon: "codicon-file-code", kind: "css" };
    }
    if (
      name.endsWith(".png") ||
      name.endsWith(".jpg") ||
      name.endsWith(".jpeg") ||
      name.endsWith(".gif") ||
      name.endsWith(".svg") ||
      name.endsWith(".webp") ||
      name.endsWith(".bmp") ||
      name.endsWith(".ico")
    ) {
      return { icon: "codicon-file-media", kind: "media" };
    }
    if (name.endsWith(".zip") || name.endsWith(".gz") || name.endsWith(".tar") || name.endsWith(".tgz")) {
      return { icon: "codicon-file-zip", kind: "zip" };
    }
    if (
      name.endsWith(".toml") ||
      name.endsWith(".yaml") ||
      name.endsWith(".yml") ||
      name.endsWith(".ini") ||
      name.endsWith(".cfg") ||
      name === "makefile" ||
      name.startsWith("dockerfile")
    ) {
      return { icon: "codicon-settings-gear", kind: "config" };
    }
    if (name.endsWith(".txt") || name.endsWith(".log") || name.endsWith(".csv")) {
      return { icon: "codicon-file-text", kind: "text" };
    }
    return { icon: "codicon-file", kind: "file" };
  }

  function iconHtml(spec) {
    const icon = typeof spec === "string" ? spec : spec.icon;
    const kind = typeof spec === "string" ? "file" : spec.kind;
    return `<span class="icon icon-kind-${kind}"><i class="codicon ${icon}"></i></span>`;
  }

  function hideContextMenu() {
    ctxMenu.hidden = true;
    ctxMenu.innerHTML = "";
  }

  function showContextMenu(x, y, items) {
    ctxMenu.innerHTML = "";
    for (const item of items) {
      if (item === "---") {
        const sep = document.createElement("div");
        sep.className = "ctx-sep";
        ctxMenu.appendChild(sep);
        continue;
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ctx-item" + (item.danger ? " danger" : "");
      btn.textContent = item.label;
      btn.disabled = !!item.disabled;
      btn.addEventListener("click", () => {
        hideContextMenu();
        if (!item.disabled && item.action) item.action();
      });
      ctxMenu.appendChild(btn);
    }
    ctxMenu.hidden = false;
    const pad = 4;
    const rect = ctxMenu.getBoundingClientRect();
    const left = Math.min(x, window.innerWidth - rect.width - pad);
    const top = Math.min(y, window.innerHeight - rect.height - pad);
    ctxMenu.style.left = Math.max(pad, left) + "px";
    ctxMenu.style.top = Math.max(pad, top) + "px";
  }

  function selectOnContext(which, name) {
    const selected = which === "local" ? state.localSelected : state.remoteSelected;
    state.focus = which;
    if (!selected.has(name)) {
      selected.clear();
      selected.add(name);
      renderList(which);
    }
  }

  function doUpload() {
    const paths = [...state.localSelected].map((n) => joinLocal(state.localPath, n));
    if (!paths.length) {
      setStatus("Select local files/folders to upload");
      return;
    }
    vscode.postMessage({ type: "upload", localPaths: paths });
  }

  function doDownload() {
    const paths = [...state.remoteSelected].map((n) => joinRemote(state.remotePath, n));
    if (!paths.length) {
      setStatus("Select remote files/folders to download");
      return;
    }
    vscode.postMessage({ type: "download", remotePaths: paths });
  }

  function doRemoteDelete() {
    const paths = [...state.remoteSelected].map((n) => joinRemote(state.remotePath, n));
    if (!paths.length) {
      setStatus("Select remote items to delete");
      return;
    }
    vscode.postMessage({ type: "rm", remotePaths: paths });
  }

  function doLocalDelete() {
    const paths = [...state.localSelected].map((n) => joinLocal(state.localPath, n));
    if (!paths.length) {
      setStatus("Select local items to delete");
      return;
    }
    vscode.postMessage({ type: "localRm", localPaths: paths });
  }

  function openLocalContext(ev, entry) {
    ev.preventDefault();
    ev.stopPropagation();
    const items = [];
    if (entry) {
      selectOnContext("local", entry.name);
      if (entry.isDir) {
        items.push({
          label: "Open",
          action: () =>
            vscode.postMessage({ type: "localCd", path: joinLocal(state.localPath, entry.name) }),
        });
      }
      items.push({
        label: "Upload",
        disabled: !state.connected,
        action: doUpload,
      });
      items.push({
        label: "Rename",
        action: () =>
          vscode.postMessage({
            type: "localRename",
            path: joinLocal(state.localPath, entry.name),
          }),
      });
      items.push({
        label: "Delete",
        danger: true,
        action: doLocalDelete,
      });
      items.push("---");
    }
    items.push({
      label: "New folder",
      action: () => vscode.postMessage({ type: "localMkdir" }),
    });
    items.push({
      label: "Refresh",
      action: () => vscode.postMessage({ type: "refreshLocal" }),
    });
    items.push({
      label: "Browse…",
      action: () => vscode.postMessage({ type: "pickLocal" }),
    });
    showContextMenu(ev.clientX, ev.clientY, items);
  }

  function openRemoteContext(ev, entry) {
    ev.preventDefault();
    ev.stopPropagation();
    const items = [];
    if (entry) {
      selectOnContext("remote", entry.name);
      if (entry.isDir) {
        items.push({
          label: "Open",
          action: () =>
            vscode.postMessage({
              type: "remoteCd",
              path: joinRemote(state.remotePath, entry.name),
            }),
        });
      }
      if (!entry.isDir) {
        items.push({
          label: "Open in Editor",
          disabled: !state.connected,
          action: () =>
            vscode.postMessage({
              type: "openRemote",
              path: joinRemote(state.remotePath, entry.name),
            }),
        });
        items.push({
          label: "SHA-256",
          disabled: !state.connected,
          action: () =>
            vscode.postMessage({
              type: "hashRemote",
              path: joinRemote(state.remotePath, entry.name),
            }),
        });
      }
      items.push({
        label: "Download",
        disabled: !state.connected,
        action: doDownload,
      });
      items.push({
        label: "Rename",
        disabled: !state.connected,
        action: () =>
          vscode.postMessage({
            type: "remoteRename",
            path: joinRemote(state.remotePath, entry.name),
          }),
      });
      items.push({
        label: "Delete",
        danger: true,
        disabled: !state.connected,
        action: doRemoteDelete,
      });
      items.push("---");
    }
    items.push({
      label: "New folder",
      disabled: !state.connected,
      action: () => vscode.postMessage({ type: "mkdir" }),
    });
    items.push({
      label: "New file",
      disabled: !state.connected,
      action: () => vscode.postMessage({ type: "newFile" }),
    });
    items.push({
      label: "Show tree",
      disabled: !state.connected,
      action: () => vscode.postMessage({ type: "showTree" }),
    });
    items.push({
      label: "Refresh",
      disabled: !state.connected,
      action: () => vscode.postMessage({ type: "refreshRemote" }),
    });
    showContextMenu(ev.clientX, ev.clientY, items);
  }

  function renderList(which) {
    const listing = $(which === "local" ? "localListing" : "remoteListing");
    const entries = which === "local" ? state.localEntries : state.remoteEntries;
    const selected = which === "local" ? state.localSelected : state.remoteSelected;
    const pathEl = $(which === "local" ? "localPath" : "remotePath");
    pathEl.value = which === "local" ? state.localPath : state.remotePath;

    listing.innerHTML = "";

    if (which === "remote" && !state.connected) {
      listing.oncontextmenu = (ev) => {
        ev.preventDefault();
        openRemoteContext(ev, null);
      };
      return;
    }

    const up = document.createElement("div");
    up.className = "row";
    up.innerHTML = `${iconHtml({ icon: "codicon-arrow-up", kind: "up" })}<span class="name">..</span><span class="size"></span>`;
    up.addEventListener("dblclick", () => {
      if (which === "local") {
        vscode.postMessage({ type: "localUp" });
      } else {
        vscode.postMessage({ type: "remoteCd", path: parentRemote(state.remotePath) });
      }
    });
    listing.appendChild(up);

    for (const e of entries) {
      const row = document.createElement("div");
      row.className = "row" + (selected.has(e.name) ? " selected" : "");
      row.innerHTML = `${iconHtml(entryIcon(e))}<span class="name"></span><span class="size"></span>`;
      // Match Cursor explorer: show basename without forcing trailing slash in mono
      row.querySelector(".name").textContent = e.name;
      row.querySelector(".size").textContent = fmtSize(e.size || 0, e.isDir);

      row.addEventListener("click", (ev) => {
        state.focus = which;
        if (!ev.ctrlKey && !ev.metaKey) {
          selected.clear();
        }
        if (selected.has(e.name)) {
          selected.delete(e.name);
        } else {
          selected.add(e.name);
        }
        renderList(which);
      });

      row.addEventListener("dblclick", () => {
        if (e.isDir) {
          if (which === "local") {
            vscode.postMessage({ type: "localCd", path: joinLocal(state.localPath, e.name) });
          } else {
            vscode.postMessage({ type: "remoteCd", path: joinRemote(state.remotePath, e.name) });
          }
        } else if (which === "remote") {
          vscode.postMessage({
            type: "openRemote",
            path: joinRemote(state.remotePath, e.name),
          });
        } else {
          vscode.postMessage({
            type: "upload",
            localPaths: [joinLocal(state.localPath, e.name)],
          });
        }
      });

      row.draggable = true;
      row.addEventListener("dragstart", (ev) => {
        state.focus = which;
        if (!selected.has(e.name)) {
          selected.clear();
          selected.add(e.name);
          renderList(which);
        }
        const paths =
          which === "local"
            ? [...state.localSelected].map((n) => joinLocal(state.localPath, n))
            : [...state.remoteSelected].map((n) => joinRemote(state.remotePath, n));
        ev.dataTransfer.setData(
          "application/mpftp",
          JSON.stringify({ side: which, paths })
        );
        ev.dataTransfer.effectAllowed = "copy";
      });

      row.addEventListener("contextmenu", (ev) => {
        if (which === "local") openLocalContext(ev, e);
        else openRemoteContext(ev, e);
      });

      listing.appendChild(row);
    }

    listing.ondragover = (ev) => {
      ev.preventDefault();
      if (!state.connected && which === "remote") return;
      listing.classList.add("drag-over");
      ev.dataTransfer.dropEffect = "copy";
    };
    listing.ondragleave = () => listing.classList.remove("drag-over");
    listing.ondrop = (ev) => {
      ev.preventDefault();
      listing.classList.remove("drag-over");
      let payload = null;
      try {
        payload = JSON.parse(ev.dataTransfer.getData("application/mpftp") || "null");
      } catch {
        payload = null;
      }
      if (!payload || !payload.paths || !payload.paths.length) return;
      if (payload.side === "local" && which === "remote") {
        vscode.postMessage({ type: "upload", localPaths: payload.paths });
      } else if (payload.side === "remote" && which === "local") {
        vscode.postMessage({ type: "download", remotePaths: payload.paths });
      }
    };

    listing.oncontextmenu = (ev) => {
      if (ev.target.closest(".row")) return;
      if (which === "local") openLocalContext(ev, null);
      else openRemoteContext(ev, null);
    };
  }

  function setStatus(msg, phase) {
    const footer = $("footer");
    footer.textContent = msg || "";
    footer.classList.remove("xfer-active", "xfer-stalled", "xfer-done");
    if (phase === "active") footer.classList.add("xfer-active");
    else if (phase === "stalled") footer.classList.add("xfer-stalled");
    else if (phase === "done") footer.classList.add("xfer-done");
  }

  function updateChrome() {
    const on = state.connected;
    $("status").textContent = on ? `Connected: ${state.device}` : "Not connected";
    $("btnConnect").textContent = on ? "Disconnect" : "Connect";
    $("btnXferUp").disabled = !on;
    $("btnXferDown").disabled = !on;
    $("btnRefreshRemote").disabled = !on;
    $("btnRepl").disabled = !on;
    $("remotePath").disabled = !on;
    $("remotePath").placeholder = on ? "" : "Not connected";
    $("remotePane").classList.toggle("disconnected", !on);
    $("btnXferUp").classList.toggle("xfer-disabled", !on);
    $("btnXferDown").classList.toggle("xfer-disabled", !on);
  }

  /** Same set as Command Palette entries titled "mpftp: …". */
  const MPFTP_COMMANDS = [
    { command: "mpftp.connect", title: "Connect to Board" },
    { command: "mpftp.disconnect", title: "Disconnect", needsConnected: true },
    { command: "mpftp.resume", title: "Resume Last Device" },
    { command: "mpftp.openFtp", title: "Open File Browser" },
    { command: "mpftp.openFtpEditor", title: "Open File Browser in Editor" },
    { command: "mpftp.openRepl", title: "Open REPL" },
    { command: "mpftp.editRemote", title: "Edit Board File", needsConnected: true },
    { command: "mpftp.softReset", title: "Soft Reset", needsConnected: true },
    { command: "mpftp.hardReset", title: "Hard Reset", needsConnected: true },
    { command: "mpftp.bootloader", title: "Enter Bootloader", needsConnected: true },
    { command: "mpftp.runFile", title: "Run Current File on Board", needsConnected: true },
    { command: "mpftp.eval", title: "Eval Expression", needsConnected: true },
    { command: "mpftp.exec", title: "Exec Code", needsConnected: true },
    { command: "mpftp.rtcGet", title: "Get RTC", needsConnected: true },
    { command: "mpftp.rtcSet", title: "Set RTC from Host", needsConnected: true },
    { command: "mpftp.mipInstall", title: "mip Install Package", needsConnected: true },
    { command: "mpftp.df", title: "Disk Free (df)", needsConnected: true },
    { command: "mpftp.mount", title: "Mount Local Folder (/remote)", needsConnected: true },
    { command: "mpftp.umount", title: "Unmount /remote", needsConnected: true },
    { command: "mpftp.romfsQuery", title: "ROMFS Query", needsConnected: true },
    { command: "mpftp.hashRemote", title: "Hash Board File", needsConnected: true },
    { command: "mpftp.refreshPorts", title: "Refresh Serial Ports" },
    { command: "mpftp.agentStatus", title: "Agent Status (RPC / logs)" },
  ];

  function openCommandsMenu(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    const rect = $("btnMore").getBoundingClientRect();
    const items = [];
    for (const c of MPFTP_COMMANDS) {
      // Mirror palette: Disconnect only when connected (commandPalette when clause).
      if (c.command === "mpftp.disconnect" && !state.connected) {
        continue;
      }
      items.push({
        label: c.title,
        disabled: !!(c.needsConnected && !state.connected),
        action: () => vscode.postMessage({ type: "command", command: c.command }),
      });
    }
    showContextMenu(rect.left, rect.bottom + 4, items);
  }

  document.addEventListener("click", hideContextMenu);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") hideContextMenu();
  });
  window.addEventListener("blur", hideContextMenu);
  window.addEventListener("resize", hideContextMenu);

  $("btnMore").addEventListener("click", openCommandsMenu);
  $("btnConnect").addEventListener("click", () => {
    vscode.postMessage({ type: state.connected ? "disconnect" : "connect" });
  });
  $("btnRefreshLocal").addEventListener("click", () => {
    vscode.postMessage({ type: "refreshLocal" });
  });
  $("btnRefreshRemote").addEventListener("click", () => {
    vscode.postMessage({ type: "refreshRemote" });
  });
  $("btnXferUp").addEventListener("click", doUpload);
  $("btnXferDown").addEventListener("click", doDownload);
  $("btnRepl").addEventListener("click", () => {
    vscode.postMessage({ type: "openRepl" });
  });
  $("btnPickLocal").addEventListener("click", () => {
    vscode.postMessage({ type: "pickLocal" });
  });

  $("localPath").addEventListener("change", (e) => {
    vscode.postMessage({ type: "localCd", path: e.target.value });
  });
  $("remotePath").addEventListener("change", (e) => {
    vscode.postMessage({ type: "remoteCd", path: e.target.value || "/" });
  });

  window.addEventListener("message", (event) => {
    const msg = event.data;
    switch (msg.type) {
      case "state":
        state.connected = !!msg.connected;
        state.device = msg.device || "";
        if (msg.localPath != null) state.localPath = msg.localPath;
        state.remotePath = msg.remotePath != null ? msg.remotePath : state.remotePath;
        if (msg.localEntries) {
          state.localEntries = msg.localEntries;
          state.localSelected.clear();
        }
        state.remoteEntries = Array.isArray(msg.remoteEntries) ? msg.remoteEntries : [];
        state.remoteSelected.clear();
        updateChrome();
        renderList("local");
        renderList("remote");
        break;
      case "status":
        setStatus(msg.text, msg.phase);
        break;
      default:
        break;
    }
  });

  vscode.postMessage({ type: "ready" });
})();
