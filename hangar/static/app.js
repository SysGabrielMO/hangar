// Hangar · comportamiento de la interfaz (sin dependencias).

// base.html deja en HANGAR_ICONS la URL del sprite con su versión
const ICONS = window.HANGAR_ICONS || "/static/icons.svg";
const icon = (name) =>
  `<svg class="icon" aria-hidden="true"><use href="${ICONS}#${name}"/></svg>`;

const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

// Igual que human_bytes() del servidor
const fmtBytes = (n) => {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${i <= 1 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
};

function toast(text, isError = false) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const item = document.createElement("div");
  item.className = "toast" + (isError ? " error" : "");
  item.textContent = text;
  box.appendChild(item);
  setTimeout(() => item.remove(), 4500);
}

// --- Tema claro / oscuro --------------------------------------------------

document.getElementById("theme-toggle")?.addEventListener("click", () => {
  const root = document.documentElement;
  const current = root.dataset.theme
    || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  try {
    localStorage.setItem("hangar.theme", next);
  } catch (error) {
    // sin almacenamiento local: el tema dura hasta recargar
  }
  window.dispatchEvent(new Event("resize"));   // las gráficas se repintan con los colores nuevos
});

// --- Confirmaciones -------------------------------------------------------
// Diálogo propio en vez de window.confirm(), que algunos navegadores
// integrados bloquean y devuelven "cancelar" sin mostrar nada.

function askConfirm({ title, text, label, danger }) {
  const dialog = document.getElementById("confirm-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    return Promise.resolve(window.confirm(`${title}\n\n${text || ""}`));
  }
  if (dialog.open) return Promise.resolve(false);
  const ok = document.getElementById("confirm-ok");
  document.getElementById("confirm-title").textContent = title || "¿Seguro?";
  const body = document.getElementById("confirm-text");
  body.textContent = text || "";
  body.hidden = !text;
  ok.textContent = label || "Confirmar";
  ok.className = `btn ${danger ? "danger-solid" : "primary"}`;
  return new Promise((resolve) => {
    dialog.returnValue = "";
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
    // En lo peligroso el foco empieza en «Cancelar»: un Enter de más no rompe nada
    (danger ? document.getElementById("confirm-cancel") : ok).focus();
  });
}

document.getElementById("confirm-dialog")?.addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close("cancel");   // clic fuera
});

// Evita enviar dos veces el mismo formulario (doble clic en «Crear snapshot»…)
function markSent(form) {
  form.dataset.sent = "1";
  form.querySelectorAll("button").forEach((button) => {
    if (button.disabled) return;
    button.setAttribute("aria-busy", "true");
    setTimeout(() => { button.disabled = true; }, 0);
  });
}

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.method === "dialog") return;
  if (form.dataset.sent === "1") {
    event.preventDefault();
    return;
  }
  if (form.dataset.confirm === undefined) {
    if (form.method === "post") markSent(form);
    return;
  }
  event.preventDefault();
  const button = event.submitter || form.querySelector("button");
  const confirmed = await askConfirm({
    title: form.dataset.confirmTitle,
    text: form.dataset.confirm,
    label: form.dataset.confirmLabel || button?.textContent.trim(),
    danger: form.dataset.confirmDanger !== undefined,
  });
  if (!confirmed) return;
  markSent(form);
  form.submit();
});

// Al volver atrás el navegador restaura la página tal cual: se reactivan los botones
window.addEventListener("pageshow", () => {
  document.querySelectorAll("form[data-sent]").forEach((form) => {
    delete form.dataset.sent;
    form.querySelectorAll("button[aria-busy]").forEach((button) => {
      button.removeAttribute("aria-busy");
      button.disabled = false;
    });
  });
});

// --- Pequeñas ayudas ------------------------------------------------------

// El mensaje de la última acción viaja en la URL (?msg=…). Una vez pintado se
// quita, para que al recargar o volver atrás no reaparezca un «Apagando…» viejo.
(() => {
  const url = new URL(location.href);
  if (!url.searchParams.has("msg") && !url.searchParams.has("error")) return;
  url.searchParams.delete("msg");
  url.searchParams.delete("error");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
})();

// Los enlaces de navegación responden al pulsar el botón del ratón, sin esperar
// a soltarlo: son unos 100 ms menos en cada cambio de página.
document.addEventListener("mousedown", (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest("a[href]");
  if (!link || link.target || link.hasAttribute("download") || link.origin !== location.origin) return;
  if (!link.closest(".sidebar, .tabs, .seg, .crumbs, .m-name, .members, .kind-tabs")) return;
  if (link.pathname === location.pathname && link.search === location.search) return;
  event.preventDefault();
  location.href = link.href;
});

function showNotices(notices) {
  (notices || []).forEach((text) => toast(text, true));
}

// Selects que guardan al cambiar (p. ej. el módulo)
document.querySelectorAll("select[data-autosubmit]").forEach((select) => {
  select.addEventListener("change", () => select.form.submit());
});

// Filas de tabla clicables, salvo cuando se pulsa un enlace, botón o select
document.querySelectorAll("tr[data-href]").forEach((row) => {
  row.addEventListener("click", (event) => {
    if (event.target.closest("a, button, select, input, form")) return;
    if (window.getSelection()?.toString()) return;   // está seleccionando texto (una IP)
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

// Botones que copian un texto (las IPs)
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  event.preventDefault();
  const text = button.dataset.copy;
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch (e) { /* sin permiso */ }
    area.remove();
    if (!copied) {
      toast("No se pudo copiar al portapapeles.", true);
      return;
    }
  }
  toast(`Copiado: ${text}`);
});

// Alta de KVM: solo se enseñan los campos que aplican al origen elegido
(() => {
  const form = document.getElementById("new-machine-form");
  const origin = form?.querySelector("#f-origin");
  if (!form || form.dataset.kind !== "kvm" || !origin) return;
  const show = (key, visible) => {
    const field = form.querySelector(`[data-field="${key}"]`);
    if (field) field.hidden = !visible;
  };
  const update = () => {
    const kind = origin.value.split(":")[0];
    show("cloud_init", kind === "image");
    ["osinfo", "disks", "networks"].forEach((key) => show(key, kind !== "clone"));
  };
  origin.addEventListener("change", update);
  update();
})();

// --- Estado del host (barra lateral) y portada en vivo -------------------

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
  setText("host-ram-text", `${fmtBytes(host.mem_used)} / ${fmtBytes(host.mem_total)}`);
  setBar("host-ram-bar", host.mem_pct);
  setText("host-load-text", `${host.load.toFixed(1)} / ${host.cpus} núcleos`);
  setBar("host-load-bar", host.load_pct);
  setText("host-disk-text", `${fmtBytes(host.disk_used)} / ${fmtBytes(host.disk_total)}`);
  setBar("host-disk-bar", host.disk_pct);
}

const STATE_ES = { running: "Encendida", stopped: "Apagada", paused: "Pausada" };

// Mismo marcado que la macro power_buttons de index.html
function powerButtons(m) {
  const running = m.state === "running";
  const label = running ? "Apagar" : (m.state === "paused" ? "Reanudar" : "Arrancar");
  const name = escapeHtml(m.name);
  const base = `/m/${encodeURIComponent(m.kind)}/${encodeURIComponent(m.id)}`;
  const consoleLink = running
    ? `<a class="btn small icon-only ghost" href="${base}?console=1" title="Abrir consola"
         aria-label="Consola de ${name}">${icon("terminal")}</a>`
    : "";
  return `${consoleLink}<button class="btn small icon-only power" type="button"
    data-power="${running ? "stop" : "start"}" data-kind="${escapeHtml(m.kind)}"
    data-id="${escapeHtml(m.id)}" data-name="${name}" title="${label}"
    aria-label="${label} ${name}">${icon(running ? "stop" : "play")}</button>`;
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
      if (dot) {
        dot.className = `dot ${m.state} js-dot`;
        dot.title = STATE_ES[m.state] || "Estado desconocido";
      }
      const ips = row.querySelector(".js-ips");
      if (ips) ips.textContent = m.ips.length ? m.ips.join(", ") : "—";
      const cell = row.querySelector(".js-power");
      if (cell && row.dataset.busy !== "1" && cell.dataset.state !== m.state) {
        cell.dataset.state = m.state;
        cell.innerHTML = powerButtons(m);
      }
    });

    document.querySelectorAll("[data-stat]").forEach((el) => {
      const key = el.dataset.stat;
      if (key === "total") el.textContent = data.total;
      else if (key === "running") el.textContent = data.running;
      else if (key.startsWith("kind-")) el.textContent = data.counts[key.slice(5)] ?? 0;
    });
    updateHost(data.host);
    showNotices(data.notices);
  } catch (error) {
    // Un fallo puntual no molesta: el siguiente intento lo corrige.
  } finally {
    statusBusy = false;
  }
}

async function refreshHost() {
  if (document.hidden) return;
  try {
    const response = await fetch("/api/host", {
      headers: { Accept: "application/json" }, cache: "no-store",
    });
    if (response.ok) updateHost(await response.json());
  } catch (error) {
    // se reintenta en la siguiente vuelta
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-power]");
  if (!button) return;
  event.preventDefault();
  const row = button.closest("tr");
  if (row && row.dataset.busy === "1") return;

  const name = button.dataset.name || "";
  const action = button.dataset.power;
  // Apagar pide confirmación, como en la ficha; arrancar es inmediato.
  if (action === "stop") {
    const confirmed = await askConfirm({
      title: `Apagar ${name}`,
      text: "Se le pide al sistema que se apague de forma ordenada.",
      label: "Apagar", danger: true,
    });
    if (!confirmed) return;
  }

  const url = `/m/${encodeURIComponent(button.dataset.kind)}`
    + `/${encodeURIComponent(button.dataset.id)}/${action}`;
  button.disabled = true;
  if (row) { row.dataset.busy = "1"; row.classList.add("busy"); }
  try {
    const response = await fetch(url, {
      method: "POST", headers: { Accept: "application/json" },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "No se pudo completar la acción.");
    toast(`${data.msg || "Hecho"}${name ? ` · ${name}` : ""}`);
    // Arrancar o apagar tarda unos segundos: se mira más a menudo un rato
    [700, 2000, 4000, 8000, 12000, 20000, 30000].forEach((ms) => setTimeout(refreshStatus, ms));
  } catch (error) {
    toast(error.message || "No se pudo completar la acción.", true);
  } finally {
    if (row) { row.dataset.busy = ""; row.classList.remove("busy"); }
    if (button.isConnected) button.disabled = false;
  }
});

if (document.querySelector("tr[data-key]")) {
  setTimeout(refreshStatus, 1200);           // la página puede salir de una lista de hace segundos
  setInterval(refreshStatus, 6000);          // la portada: máquinas y host
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshStatus();
  });
} else if (document.getElementById("host-ram-bar")) {
  setInterval(refreshHost, 10000);           // el resto de páginas: solo el host
}

// Ficha de una máquina: si su estado cambia (termina de apagarse, la arrancan
// desde otro sitio…) la página se recarga sola para enseñar los botones buenos.
(() => {
  const head = document.getElementById("machine-head");
  if (!head) return;
  let dirty = false;            // el usuario está escribiendo en algún formulario
  document.addEventListener("input", (event) => {
    if (!event.target.closest("[data-autosubmit]")) dirty = true;
  });
  let stopped = false;
  let changes = 0;

  const check = async () => {
    if (document.hidden || stopped) return;
    let data;
    try {
      const response = await fetch(head.dataset.stateUrl, {
        headers: { Accept: "application/json" }, cache: "no-store",
      });
      if (!response.ok) return;
      data = await response.json();
    } catch (error) {
      return;                   // Hangar no contesta: se reintenta en la siguiente vuelta
    }
    showNotices(data.notices);
    if (data.state === head.dataset.state) {
      changes = 0;
      return;
    }
    // Dos lecturas seguidas distintas: una sola podría venir de una lista de hace segundos
    changes += 1;
    if (changes < 2) return;
    stopped = true;
    if (data.state === "gone") {
      location.href = "/";      // la han borrado
      return;
    }
    const dialogOpen = document.getElementById("confirm-dialog")?.open;
    if (dirty || dialogOpen) {
      const notice = document.getElementById("state-changed");
      if (notice) notice.hidden = false;
      return;
    }
    // Si la consola estaba abierta y la máquina sigue encendida, se reabre
    const url = new URL(location.href);
    const drawerOpen = document.body.classList.contains("console-open");
    if (drawerOpen && data.state === "running") url.searchParams.set("console", "1");
    else url.searchParams.delete("console");
    location.replace(url.pathname + url.search);
  };
  setInterval(check, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });
})();

// Grupos de la portada plegables; se recuerda cuáles están cerrados
(() => {
  const groups = document.querySelectorAll(".group[data-group]");
  if (!groups.length) return;
  const KEY = "hangar.groups.collapsed";
  let collapsed = [];
  try {
    collapsed = JSON.parse(localStorage.getItem(KEY) || "[]");
    if (!Array.isArray(collapsed)) collapsed = [];
  } catch (error) {
    collapsed = [];
  }
  // Con un módulo filtrado no tiene sentido enseñarlo plegado
  const filtered = new URLSearchParams(location.search).has("module");
  const apply = (group, closed) => {
    group.classList.toggle("collapsed", closed);
    group.querySelector(".group-toggle")?.setAttribute("aria-expanded", String(!closed));
  };
  groups.forEach((group) => {
    const key = group.dataset.group;
    if (!filtered && collapsed.includes(key)) apply(group, true);
    group.querySelector(".group-toggle")?.addEventListener("click", () => {
      const closed = !group.classList.contains("collapsed");
      apply(group, closed);
      collapsed = collapsed.filter((item) => item !== key);
      if (closed) collapsed.push(key);
      try {
        localStorage.setItem(KEY, JSON.stringify(collapsed));
      } catch (error) {
        // sin almacenamiento local: no se recuerda
      }
    });
  });
})();

// --- Página de una tarea: registro en vivo -------------------------------

(() => {
  const box = document.getElementById("job");
  const log = document.getElementById("log");
  if (!box || !log) return;
  log.scrollTop = log.scrollHeight;
  if (box.dataset.status !== "running") return;

  let failures = 0;
  const finish = (data) => {
    document.getElementById("job-running").hidden = true;
    if (data.status === "done") {
      if (data.url) document.getElementById("job-link").href = data.url;
      document.getElementById("job-done").hidden = false;
    } else {
      document.getElementById("job-error-text").textContent = data.error || "La tarea falló.";
      document.getElementById("job-error").hidden = false;
    }
  };

  const tick = async () => {
    let data;
    try {
      const response = await fetch(box.dataset.jobUrl, {
        headers: { Accept: "application/json" }, cache: "no-store",
      });
      if (response.status === 404) {
        finish({ status: "error", error: "Hangar se reinició y ya no recuerda esta tarea. "
          + "Lo que estuviera creando puede haberse quedado a medias." });
        return;
      }
      if (!response.ok) throw new Error("respuesta no válida");
      data = await response.json();
      failures = 0;
    } catch (error) {
      failures += 1;
      if (failures >= 5) {
        finish({ status: "error", error: "Se perdió la conexión con Hangar." });
        return;
      }
      setTimeout(tick, 2000);
      return;
    }
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
    const text = data.lines.join("\n") || "…";
    if (log.textContent !== text) log.textContent = text;
    if (atBottom) log.scrollTop = log.scrollHeight;
    if (data.status === "running") setTimeout(tick, 1000);
    else finish(data);
  };
  setTimeout(tick, 700);
})();

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
// Lo llama machine.html después de cargar xterm.js.

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

  // El botón lleva un icono: solo se cambia el texto
  const setToggleLabel = (text) => {
    const label = toggle.querySelector("span");
    if (label) label.textContent = text;
    else toggle.textContent = text;
  };

  const open = () => {
    drawer.hidden = false;
    document.body.classList.add("console-open");
    toggle.setAttribute("aria-expanded", "true");
    setToggleLabel("Ocultar consola");
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
    setToggleLabel("Consola");
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
