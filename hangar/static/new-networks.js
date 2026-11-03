// Editor de redes del alta de KVM: filas con desplegables y creación inline.
(() => {
  const rows = document.getElementById("network-rows");
  const hidden = document.getElementById("networks-json");
  const form = document.getElementById("new-machine-form");
  if (!rows || !hidden || !form) return;

  const optionsNode = document.getElementById("network-options");
  const initialNode = document.getElementById("network-initial");
  const defaultNode = document.getElementById("network-default");
  const choices = JSON.parse(optionsNode?.textContent || "[]");
  const initial = JSON.parse(initialNode?.textContent || "[]");
  const defaultSource = JSON.parse(defaultNode?.textContent || '""');
  const models = [
    ["virtio", "virtio (Linux)"],
    ["e1000e", "e1000e (Windows)"],
    ["rtl8139", "rtl8139 (antiguas)"],
  ];

  const addOption = (select, value, label) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  };

  const sync = () => {
    const data = [...rows.querySelectorAll(".network-row")].map((row) => ({
      source: row.querySelector(".network-source").value,
      model: row.querySelector(".network-model").value,
    })).filter((item) => item.source);
    hidden.value = JSON.stringify(data);
  };

  const addRow = (source = "", model = "virtio") => {
    const row = document.createElement("div");
    row.className = "network-row";

    const sourceSelect = document.createElement("select");
    sourceSelect.className = "network-source";
    sourceSelect.setAttribute("aria-label", "Red");
    choices.forEach((choice) => addOption(sourceSelect, choice.value, choice.label));
    if (source) sourceSelect.value = source;

    const modelSelect = document.createElement("select");
    modelSelect.className = "network-model";
    modelSelect.setAttribute("aria-label", "Modelo de tarjeta");
    models.forEach(([value, label]) => addOption(modelSelect, value, label));
    modelSelect.value = model;

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn small network-remove";
    remove.textContent = "×";
    remove.title = "Quitar esta red";
    remove.addEventListener("click", () => { row.remove(); sync(); });

    row.append(sourceSelect, modelSelect, remove);
    rows.appendChild(row);
    sourceSelect.addEventListener("change", sync);
    modelSelect.addEventListener("change", sync);
    sync();
    return row;
  };

  const selected = initial.length ? initial : (defaultSource ? [{ source: defaultSource, model: "virtio" }] : []);
  selected.forEach((item) => addRow(item.source, item.model || "virtio"));
  sync();

  document.getElementById("add-network").addEventListener("click", () => {
    addRow(choices[0]?.value || "", "virtio");
  });

  form.addEventListener("submit", sync);

  // --- Crear una red nueva sin salir del alta ---
  const panel = document.getElementById("network-create");
  const error = document.getElementById("net-create-error");
  document.getElementById("new-network").addEventListener("click", () => {
    panel.hidden = false;
    error.hidden = true;
    document.getElementById("net-name").focus();
  });
  document.getElementById("net-create-cancel").addEventListener("click", () => {
    panel.hidden = true;
  });

  document.getElementById("net-create-submit").addEventListener("click", async () => {
    const name = document.getElementById("net-name").value.trim();
    const address = document.getElementById("net-address").value.trim();
    if (!name || !address) {
      error.textContent = "Indica el nombre y la dirección del host (por ejemplo 10.30.0.1/24).";
      error.hidden = false;
      return;
    }
    const button = document.getElementById("net-create-submit");
    button.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch("/networks/libvirt/create", {
        method: "POST",
        headers: { Accept: "application/json" },
        body: new URLSearchParams({
          name,
          mode: document.getElementById("net-mode").value,
          address,
          dhcp: document.getElementById("net-dhcp").checked ? "1" : "",
          autostart: document.getElementById("net-autostart").checked ? "1" : "",
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "No se pudo crear la red.");

      const value = `network:${data.name || name}`;
      if (!choices.some((choice) => choice.value === value)) {
        const choice = { value, label: `Red ${data.name || name}` };
        choices.push(choice);
        rows.querySelectorAll(".network-source").forEach((select) => {
          addOption(select, choice.value, choice.label);
        });
      }
      addRow(value, "virtio");
      panel.hidden = true;
      document.getElementById("net-name").value = "";
      document.getElementById("net-address").value = "";
    } catch (e) {
      error.textContent = e.message || "No se pudo crear la red.";
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });
})();
