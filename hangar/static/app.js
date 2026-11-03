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
  setText("host-disk-note", `${host.disk_pct} % ocupado`);
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
