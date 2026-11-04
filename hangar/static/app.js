// Confirmación en dos clics para formularios con data-confirm.
// No se usa window.confirm() porque algunos navegadores integrados lo
// bloquean y devuelven "cancelar" sin mostrar nada.
document.querySelectorAll("form[data-confirm]").forEach((form) => {
  const button = form.querySelector("button");
  const original = button.textContent;

  form.addEventListener("submit", (event) => {
    if (form.dataset.armed === "1") return;   // segundo clic: se envía
    event.preventDefault();
    form.dataset.armed = "1";
    button.textContent = form.dataset.confirm;
    button.classList.add("armed");
    setTimeout(() => {                        // si no confirma, se desarma
      form.dataset.armed = "";
      button.textContent = original;
      button.classList.remove("armed");
    }, 4000);
  });
});

// Selects que guardan al cambiar (p. ej. el módulo en la portada)
document.querySelectorAll("select[data-autosubmit]").forEach((select) => {
  select.addEventListener("change", () => select.form.submit());
});

// Filas de tabla clicables, salvo cuando se pulsa un enlace, botón o select
document.querySelectorAll("tr[data-href]").forEach((row) => {
  row.addEventListener("click", (event) => {
    if (event.target.closest("a, button, select, input, form")) return;
    location.href = row.dataset.href;
  });
});

// Formulario de cambiar IP: en modo DHCP no hacen falta IP, gateway ni DNS
document.querySelectorAll("select[data-ip-mode]").forEach((select) => {
  const update = () => select.form.querySelectorAll("[data-static]")
    .forEach((el) => { el.hidden = select.value === "dhcp"; });
  select.addEventListener("change", update);
  update();
});

// --- Portada: acciones rápidas y estado en vivo ---------------------------

const fmtBytes = (n) => {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${i <= 1 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
};

const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

function toast(text, isError = false) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const item = document.createElement("div");
  item.className = "toast" + (isError ? " error" : "");
  item.textContent = text;
  box.appendChild(item);
  setTimeout(() => item.remove(), 4500);
}

function powerButton(m) {
  const running = m.state === "running";
  const label = running ? "Parar" : (m.state === "paused" ? "Reanudar" : "Arrancar");
  return `<button class="btn small power" data-power="${running ? "stop" : "start"}"
    data-kind="${escapeHtml(m.kind)}" data-id="${escapeHtml(m.id)}"
    data-name="${escapeHtml(m.name)}" title="${label}"
    aria-label="${label} ${escapeHtml(m.name)}">${running ? "■" : "▶"}</button>`;
}

function setBar(id, pct) {
  const bar = document.getElementById(id);
  if (!bar) return;
  bar.style.width = `${Math.min(Math.max(pct, 0), 100)}%`;
  bar.classList.toggle("hot", pct >= 90);
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function updateHost(host) {
  if (!host || !host.mem_total) return;
  setText("host-ram-text", fmtBytes(host.mem_used));
  setText("host-ram-note", `${host.mem_pct} % en uso · ${fmtBytes(host.mem_available)} disponible`);
  setBar("host-ram-bar", host.mem_pct);
  setText("host-load-text", host.load.toFixed(1));
  setBar("host-load-bar", host.load_pct);
  setText("host-disk-text", fmtBytes(host.disk_used));
  setText("host-disk-note", `${host.disk_pct} % de ${host.disk_path}`);
  setBar("host-disk-bar", host.disk_pct);
}

let statusBusy = false;

async function refreshStatus() {
  if (document.hidden || statusBusy) return;
  statusBusy = true;
  try {
    const response = await fetch("/api/status", {
      headers: { Accept: "application/json" }, cache: "no-store",
    });
    if (!response.ok) return;
    const data = await response.json();
    const machines = new Map(data.machines.map((m) => [`${m.kind}/${m.id}`, m]));

    document.querySelectorAll("tr[data-key]").forEach((row) => {
      const m = machines.get(row.dataset.key);
      if (!m) return;
      const dot = row.querySelector(".js-dot");
      if (dot) { dot.className = `dot ${m.state}`; dot.title = m.state; }
      const ips = row.querySelector(".js-ips");
      if (ips) ips.textContent = m.ips.length ? m.ips.join(", ") : "—";
      const cell = row.querySelector(".js-power");
      if (cell && row.dataset.busy !== "1" && cell.dataset.state !== m.state) {
        cell.dataset.state = m.state;
        cell.innerHTML = powerButton(m);
      }
    });

    document.querySelectorAll("[data-stat]").forEach((el) => {
      const key = el.dataset.stat;
      if (key === "total") el.textContent = data.total;
      else if (key === "running") el.textContent = data.running;
      else if (key.startsWith("kind-")) el.textContent = data.counts[key.slice(5)] ?? 0;
    });
    updateHost(data.host);
  } catch (error) {
    // Un fallo puntual no molesta: el siguiente intento lo corrige.
  } finally {
    statusBusy = false;
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-power]");
  if (!button) return;
  event.preventDefault();
  const row = button.closest("tr");
  if (row && row.dataset.busy === "1") return;

  // Parar pide un segundo clic, como en la ficha; arrancar es inmediato.
  if (button.dataset.power === "stop" && button.dataset.armed !== "1") {
    button.dataset.armed = "1";
    button.dataset.original = button.textContent;
    button.textContent = "¿Parar?";
    button.classList.add("armed");
    setTimeout(() => {
      if (!button.isConnected) return;
      button.dataset.armed = "";
      button.textContent = button.dataset.original || "■";
      button.classList.remove("armed");
    }, 4000);
    return;
  }

  const name = button.dataset.name || "";
  const url = `/m/${encodeURIComponent(button.dataset.kind)}`
    + `/${encodeURIComponent(button.dataset.id)}/${button.dataset.power}`;
  const icon = button.dataset.power === "stop" ? "■" : "▶";
  button.dataset.armed = "";
  button.classList.remove("armed");
  button.disabled = true;
  if (row) { row.dataset.busy = "1"; row.classList.add("busy"); }
  try {
    const response = await fetch(url, {
      method: "POST", headers: { Accept: "application/json" },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "No se pudo completar la acción.");
    toast(`${data.msg || "Hecho"}${name ? ` · ${name}` : ""}`);
    setTimeout(refreshStatus, 900);
    setTimeout(refreshStatus, 3500);
  } catch (error) {
    toast(error.message || "No se pudo completar la acción.", true);
  } finally {
    if (row) { row.dataset.busy = ""; row.classList.remove("busy"); }
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = icon;
    }
  }
});

if (document.querySelector("tr[data-key]")) {
  setInterval(refreshStatus, 6000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshStatus();
  });
}

// --- Pestaña Rendimiento --------------------------------------------------

function cssVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function drawSpark(canvas, values, maxValue, color) {
  if (!canvas) return;
  const width = Math.max(canvas.clientWidth, 1);
  const height = Math.max(canvas.clientHeight, 1);
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  ctx.strokeStyle = cssVar("--border", "#e3e6ea");
  ctx.lineWidth = 1;
  for (const fraction of [0.25, 0.5, 0.75]) {
    const y = Math.round(height * fraction) + 0.5;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
  if (!values.length) return;

  const max = Math.max(maxValue, 0.001);
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const yOf = (value) => height - Math.min(Math.max(value / max, 0), 1) * (height - 4) - 2;

  ctx.beginPath();
  values.forEach((value, index) => {
    const x = values.length > 1 ? index * step : 0;
    const y = yOf(value);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();

  if (values.length < 2) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(1, yOf(values[0]), 2.5, 0, Math.PI * 2);
    ctx.fill();
  }
}

function initMetrics() {
  const card = document.querySelector("[data-metrics-url]");
  if (!card) return;

  const cpuChart = document.getElementById("metric-cpu-chart");
  const memChart = document.getElementById("metric-mem-chart");
  const cpuValue = document.getElementById("metric-cpu");
  const memValue = document.getElementById("metric-mem");
  const memNote = document.getElementById("metric-mem-note");
  const updated = document.getElementById("metrics-updated");
  const cpuValues = [];
  const memValues = [];
  let memPercent = card.dataset.memLimit === "1";
  let stopped = false;
  let timer = null;

  // La primera muestra viene ya renderizada desde el servidor: así la pestaña
  // no aparece vacía mientras llega el primer fetch.
  try {
    const initial = JSON.parse(document.getElementById("metric-history")?.textContent || "[]");
    cpuValues.push(...initial.map((point) => point.cpu).filter((value) => value !== null));
    memValues.push(...initial.map((point) => (
      memPercent ? (point.mem ?? 0) : point.used / 2 ** 30
    )));
  } catch (error) {
    // Si el JSON no está, las gráficas empiezan vacías.
  }

  const draw = () => {
    drawSpark(cpuChart, cpuValues, 100, cssVar("--accent", "#2563eb"));
    const max = memPercent ? 100 : Math.max(1, ...memValues) * 1.2;
    drawSpark(memChart, memValues, max, cssVar("--lxc", "#0891b2"));
  };

  const tick = async () => {
    if (stopped) return;
    try {
      const response = await fetch(card.dataset.metricsUrl, {
        headers: { Accept: "application/json" }, cache: "no-store",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "No se pudo leer el consumo.");

      if (data.state !== "running") {
        stopped = true;
        if (timer) clearInterval(timer);
        cpuValue.textContent = "—";
        memValue.textContent = "—";
        updated.textContent = "La máquina ya no está encendida";
        return;
      }

      const history = data.history || [];
      cpuValues.length = 0;
      cpuValues.push(...history.map((point) => point.cpu).filter((value) => value !== null));
      memPercent = Boolean(data.mem_limit);
      memValues.length = 0;
      memValues.push(...history.map((point) => (
        memPercent ? (point.mem ?? 0) : point.used / 2 ** 30
      )));

      cpuValue.textContent = data.cpu_pct === null
        ? "Midiendo…"
        : `${data.cpu_pct.toFixed(1)} %`;
      memValue.textContent = data.mem_limit
        ? `${fmtBytes(data.mem_used)} / ${fmtBytes(data.mem_limit)}`
        : fmtBytes(data.mem_used);
      memNote.textContent = data.mem_limit
        ? `${data.mem_pct.toFixed(1)} % del límite`
        : "sin límite configurado";
      updated.textContent = `Actualizado ${new Date().toLocaleTimeString()}`;
      draw();
    } catch (error) {
      updated.textContent = error.message || "No se pudo leer el consumo";
    }
  };

  timer = setInterval(() => {
    if (!document.hidden) tick();
  }, 3000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !stopped) tick();
  });
  window.addEventListener("resize", draw);
  draw();
  tick();
}

initMetrics();

// --- Cajón de consola de la ficha -----------------------------------------

function initConsoleDrawer() {
  const toggle = document.getElementById("console-toggle");
  const drawer = document.getElementById("console-drawer");
  const host = document.getElementById("console-drawer-term");
  const status = document.getElementById("console-drawer-status");
  const close = document.getElementById("console-drawer-close");
  const resizeHandle = document.getElementById("console-drawer-resize");
  if (!toggle || !drawer || !host) return;
  if (typeof Terminal === "undefined" || typeof FitAddon === "undefined") return;

  let term = null;
  let fit = null;
  let ws = null;

  const sendResize = () => {
    if (term && ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
    }
  };

  const ensureTerminal = () => {
    if (term) return;
    term = new Terminal({
      cursorBlink: true, fontSize: 14, scrollback: 5000,
      fontFamily: 'ui-monospace, "JetBrains Mono", Menlo, monospace',
      theme: { background: "#0b0e12", foreground: "#e6e8eb", cursor: "#e6e8eb" },
    });
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    attachTerminalClipboard(term);
    term.open(host);

    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}${toggle.dataset.consoleUrl}`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      status.textContent = "Conectado";
      requestAnimationFrame(() => {
        fit.fit();
        sendResize();
        term.focus();
      });
    };
    ws.onmessage = (event) => term.write(
      typeof event.data === "string" ? event.data : new Uint8Array(event.data),
    );
    ws.onclose = () => {
      status.textContent = "Desconectado";
      term.options.cursorBlink = false;
    };
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "input", data }));
      }
    });
  };

  const fitTerminal = () => {
    if (!term || drawer.hidden) return;
    fit.fit();
    sendResize();
  };

  const HEIGHT_KEY = "hangar.console.height";
  const MIN_HEIGHT = 180;

  const maxHeight = () => Math.max(MIN_HEIGHT, window.innerHeight - 100);

  const saveHeight = () => {
    try {
      localStorage.setItem(HEIGHT_KEY, String(Math.round(drawer.getBoundingClientRect().height)));
    } catch (error) {
      // sin almacenamiento local
    }
  };

  const applyHeight = (value, persist = false) => {
    const height = Math.round(Math.max(MIN_HEIGHT, Math.min(value, maxHeight())));
    document.documentElement.style.setProperty("--console-drawer-height", `${height}px`);
    resizeHandle?.setAttribute("aria-valuenow", String(height));
    resizeHandle?.setAttribute("aria-valuemax", String(maxHeight()));
    if (persist) saveHeight();
    requestAnimationFrame(fitTerminal);
  };

  let storedHeight = 0;
  try {
    storedHeight = parseInt(localStorage.getItem(HEIGHT_KEY) || "0", 10) || 0;
  } catch (error) {
    storedHeight = 0;
  }

  const open = () => {
    drawer.hidden = false;
    document.body.classList.add("console-open");
    toggle.setAttribute("aria-expanded", "true");
    toggle.textContent = "Ocultar consola";
    ensureTerminal();
    applyHeight(storedHeight || drawer.getBoundingClientRect().height);
    requestAnimationFrame(() => {
      fitTerminal();
      term.focus();
    });
  };

  const hide = () => {
    drawer.hidden = true;
    document.body.classList.remove("console-open");
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "Consola";
  };

  toggle.addEventListener("click", () => {
    if (drawer.hidden) open();
    else hide();
  });
  close.addEventListener("click", hide);

  if (resizeHandle) {
    resizeHandle.addEventListener("pointerdown", (event) => {
      if (drawer.hidden || event.button !== 0) return;
      event.preventDefault();
      const startY = event.clientY;
      const startHeight = drawer.getBoundingClientRect().height;
      try {
        resizeHandle.setPointerCapture(event.pointerId);
      } catch (error) {
        // sin captura de puntero, se sigue pudiendo arrastrar
      }
      document.body.classList.add("console-resizing");

      const move = (moveEvent) => {
        applyHeight(startHeight + startY - moveEvent.clientY);
      };
      const finish = () => {
        resizeHandle.removeEventListener("pointermove", move);
        resizeHandle.removeEventListener("pointerup", finish);
        resizeHandle.removeEventListener("pointercancel", finish);
        document.body.classList.remove("console-resizing");
        saveHeight();
      };
      resizeHandle.addEventListener("pointermove", move);
      resizeHandle.addEventListener("pointerup", finish);
      resizeHandle.addEventListener("pointercancel", finish);
    });

    resizeHandle.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      const delta = event.key === "ArrowUp" ? 30 : -30;
      applyHeight(drawer.getBoundingClientRect().height + delta, true);
    });

    resizeHandle.addEventListener("dblclick", () => {
      applyHeight(Math.min(window.innerHeight * 0.45, 480), true);
    });
  }

  window.addEventListener("resize", () => {
    if (drawer.hidden) return;
    applyHeight(drawer.getBoundingClientRect().height);
    fitTerminal();
  });

  if (new URLSearchParams(location.search).get("console") === "1") open();
}

initConsoleDrawer();
