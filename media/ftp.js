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

  function parentLocal(p) {
    if (!p) return p;
    const norm = p.replace(/[\\/]+$/, "");
    const idx = Math.max(norm.lastIndexOf("/"), norm.lastIndexOf("\\"));
    if (idx <= 0) return p;
    return norm.slice(0, idx) || "/";
  }

  function parentRemote(p) {
    if (!p || p === "/") return "/";
    const trimmed = p.replace(/\/+$/, "");
    const idx = trimmed.lastIndexOf("/");
    return idx <= 0 ? "/" : trimmed.slice(0, idx);
  }

  function renderList(which) {
    const listing = $(which === "local" ? "localListing" : "remoteListing");
    const entries = which === "local" ? state.localEntries : state.remoteEntries;
    const selected = which === "local" ? state.localSelected : state.remoteSelected;
    const pathEl = $(which === "local" ? "localPath" : "remotePath");
    pathEl.value = which === "local" ? state.localPath : state.remotePath;

    listing.innerHTML = "";

    const up = document.createElement("div");
    up.className = "row";
    up.innerHTML = `<span class="icon">⬆</span><span class="name">..</span><span class="size"></span>`;
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
      row.innerHTML = `<span class="icon">${e.isDir ? "📁" : "📄"}</span><span class="name"></span><span class="size"></span>`;
      row.querySelector(".name").textContent = e.name + (e.isDir ? "/" : "");
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
            type: "download",
            remotePaths: [joinRemote(state.remotePath, e.name)],
          });
        } else {
          vscode.postMessage({
            type: "upload",
            localPaths: [joinLocal(state.localPath, e.name)],
          });
        }
      });

      listing.appendChild(row);
    }
  }

  function setStatus(msg) {
    $("footer").textContent = msg || "";
  }

  function updateChrome() {
    $("status").textContent = state.connected
      ? `Connected: ${state.device}`
      : "Not connected";
    $("btnConnect").textContent = state.connected ? "Disconnect" : "Connect";
    $("btnUpload").disabled = !state.connected;
    $("btnDownload").disabled = !state.connected;
    $("btnXferUp").disabled = !state.connected;
    $("btnXferDown").disabled = !state.connected;
    $("btnMkdir").disabled = !state.connected;
    $("btnRm").disabled = !state.connected;
    $("btnNewFile").disabled = !state.connected;
    $("btnRefreshRemote").disabled = !state.connected;
    $("btnRepl").disabled = !state.connected;
  }

  $("btnConnect").addEventListener("click", () => {
    vscode.postMessage({ type: state.connected ? "disconnect" : "connect" });
  });
  $("btnRefreshLocal").addEventListener("click", () => {
    vscode.postMessage({ type: "refreshLocal" });
  });
  $("btnRefreshRemote").addEventListener("click", () => {
    vscode.postMessage({ type: "refreshRemote" });
  });
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
  $("btnUpload").addEventListener("click", doUpload);
  $("btnXferUp").addEventListener("click", doUpload);
  $("btnDownload").addEventListener("click", doDownload);
  $("btnXferDown").addEventListener("click", doDownload);
  $("btnMkdir").addEventListener("click", () => {
    vscode.postMessage({ type: "mkdir" });
  });
  $("btnNewFile").addEventListener("click", () => {
    vscode.postMessage({ type: "newFile" });
  });
  $("btnRm").addEventListener("click", () => {
    const paths = [...state.remoteSelected].map((n) => joinRemote(state.remotePath, n));
    if (!paths.length) {
      setStatus("Select remote items to delete");
      return;
    }
    vscode.postMessage({ type: "rm", remotePaths: paths });
  });
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
        if (msg.remotePath != null) state.remotePath = msg.remotePath;
        if (msg.localEntries) {
          state.localEntries = msg.localEntries;
          state.localSelected.clear();
        }
        if (msg.remoteEntries) {
          state.remoteEntries = msg.remoteEntries;
          state.remoteSelected.clear();
        }
        updateChrome();
        renderList("local");
        renderList("remote");
        break;
      case "status":
        setStatus(msg.text);
        break;
      default:
        break;
    }
  });

  vscode.postMessage({ type: "ready" });
})();
