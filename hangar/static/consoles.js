// Varias consolas xterm.js en pestañas, dentro de la misma ventana.
(() => {
  const catalog = JSON.parse(document.getElementById("console-catalog").textContent || "[]");
  const byKey = new Map(catalog.map((m) => [`${m.kind}/${m.id}`, m]));
  const tabs = new Map();
  const tabList = document.getElementById("console-tab-list");
  const panes = document.getElementById("console-panes");
  const empty = document.getElementById("console-empty");
  const add = document.getElementById("console-add");
  const detail = document.getElementById("active-detail");
  let activeKey = null;
  const STORAGE_KEY = "hangar.consoles.v1";

  const keyOf = (m) => `${m.kind}/${m.id}`;

  function loadState() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (saved && Array.isArray(saved.tabs)) {
        return {
          tabs: saved.tabs.filter((key) => typeof key === "string"),
          active: typeof saved.active === "string" ? saved.active : null,
        };
      }
    } catch (error) {
      // localStorage puede estar deshabilitado; se sigue usando la URL.
    }
    return { tabs: [], active: null };
  }

  function saveState() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        tabs: [...tabs.keys()],
        active: activeKey,
      }));
    } catch (error) {
      // Sin persistencia local: la URL de la página sigue sirviendo.
    }
  }

  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  const wsUrl = (m) => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/m/${encodeURIComponent(m.kind)}`
      + `/${encodeURIComponent(m.id)}/console/ws`;
  };

  function makeTab(m) {
    const key = keyOf(m);
    if (tabs.has(key)) {
      activate(key);
      return;
    }

    const tab = document.createElement("div");
    tab.className = "console-tab";
    tab.dataset.key = key;
    tab.setAttribute("role", "tab");
    tab.tabIndex = 0;
    tab.innerHTML = `<span class="badge ${escapeHtml(m.kind)}">${escapeHtml(m.label)}</span>`
      + `<span class="tab-name">${escapeHtml(m.name)}</span>`
      + `<button class="tab-close" type="button" aria-label="Cerrar ${escapeHtml(m.name)}">×</button>`;
    tabList.appendChild(tab);

    const pane = document.createElement("div");
    pane.className = "console-pane";
    pane.dataset.key = key;
    panes.appendChild(pane);

    const state = { m, tab, pane, term: null, fit: null, ws: null };
    tabs.set(key, state);
    tab.addEventListener("click", (event) => {
      if (event.target.closest(".tab-close")) {
        closeTab(key);
        return;
      }
      activate(key);
    });
    tab.addEventListener("keydown", (event) => {
      if (event.target.closest(".tab-close")) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate(key);
      }
    });
    activate(key);
  }

  function activate(key) {
    const state = tabs.get(key);
    if (!state) return;
    activeKey = key;
    tabs.forEach((item, itemKey) => {
      item.tab.classList.toggle("active", itemKey === key);
      item.tab.setAttribute("aria-selected", itemKey === key ? "true" : "false");
      item.pane.classList.toggle("active", itemKey === key);
    });
    if (!state.term) {
      createTerminal(state);
    } else {
      requestAnimationFrame(() => {
        state.fit.fit();
        state.term.focus();
        sendResize(state);
      });
    }
    detail.hidden = false;
    detail.href = `/m/${encodeURIComponent(state.m.kind)}/${encodeURIComponent(state.m.id)}`;
    empty.hidden = true;
    updateUrl();
  }

  function createTerminal(state) {
    const term = new Terminal({
      cursorBlink: true, fontSize: 14, scrollback: 5000,
      fontFamily: 'ui-monospace, "JetBrains Mono", Menlo, monospace',
      theme: { background: "#0b0e12", foreground: "#e6e8eb", cursor: "#e6e8eb" },
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(state.pane);
    state.term = term;
    state.fit = fit;

    const ws = new WebSocket(wsUrl(state.m));
    ws.binaryType = "arraybuffer";
    state.ws = ws;
    ws.onopen = () => {
      state.tab.classList.remove("offline");
      requestAnimationFrame(() => {
        fit.fit();
        sendResize(state);
        term.focus();
      });
    };
    ws.onmessage = (event) => term.write(
      typeof event.data === "string" ? event.data : new Uint8Array(event.data),
    );
    ws.onclose = () => {
      if (state.ws !== ws) return;
      state.tab.classList.add("offline");
      if (state.term) state.term.options.cursorBlink = false;
    };
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "input", data }));
      }
    });
    requestAnimationFrame(() => {
      fit.fit();
      sendResize(state);
    });
  }

  function sendResize(state) {
    if (state.term && state.ws?.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({
        type: "resize", rows: state.term.rows, cols: state.term.cols,
      }));
    }
  }

  function closeTab(key) {
    const state = tabs.get(key);
    if (!state) return;
    const ws = state.ws;
    state.ws = null;
    if (ws) ws.close();
    if (state.term) state.term.dispose();
    state.tab.remove();
    state.pane.remove();
    tabs.delete(key);

    if (activeKey === key) {
      const next = [...tabs.keys()].pop();
      if (next) {
        activate(next);
      } else {
        activeKey = null;
        detail.hidden = true;
        empty.hidden = false;
      }
    }
    updateAdd();
    updateUrl();
  }

  function updateAdd() {
    add.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = catalog.length ? "Añadir consola…" : "No hay máquinas encendidas";
    add.appendChild(placeholder);

    let available = 0;
    catalog.forEach((m) => {
      if (tabs.has(keyOf(m))) return;
      const option = document.createElement("option");
      option.value = keyOf(m);
      option.textContent = `${m.name} · ${m.label}`;
      add.appendChild(option);
      available += 1;
    });
    if (catalog.length && !available) placeholder.textContent = "Todas abiertas";
    add.disabled = available === 0;
  }

  function updateUrl() {
    const params = new URLSearchParams();
    tabs.forEach((state) => params.append("m", keyOf(state.m)));
    const query = params.toString();
    history.replaceState(null, "", `/consoles${query ? `?${query}` : ""}`);
    saveState();
  }

  add.addEventListener("change", () => {
    const machine = byKey.get(add.value);
    if (machine) makeTab(machine);
    add.value = "";
    updateAdd();
  });

  window.addEventListener("resize", () => {
    const state = tabs.get(activeKey);
    if (state?.fit) {
      state.fit.fit();
      sendResize(state);
    }
  });

  // Primero las pestañas guardadas y luego las que pida la URL. Así, al volver
  // desde «Máquinas» o «Ficha», se recuperan las consolas que estaban abiertas.
  const requested = new URLSearchParams(location.search).getAll("m");
  const saved = loadState();
  const keys = [];
  saved.tabs.forEach((key) => {
    if (byKey.has(key) && !keys.includes(key)) keys.push(key);
  });
  requested.forEach((key) => {
    if (byKey.has(key) && !keys.includes(key)) keys.push(key);
  });
  keys.forEach((key) => makeTab(byKey.get(key)));

  let target = null;
  if (requested.length) {
    target = [...requested].reverse().find((key) => tabs.has(key)) || null;
  } else if (saved.active && tabs.has(saved.active)) {
    target = saved.active;
  }
  if (target) activate(target);

  updateAdd();
  empty.hidden = tabs.size > 0;
  if (!tabs.size) detail.hidden = true;
  if (tabs.size) saveState();
})();
