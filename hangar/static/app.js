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
