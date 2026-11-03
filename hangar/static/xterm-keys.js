// Atajos de portapapeles tipo Terminator: Ctrl+Shift+C / Ctrl+Shift+V.
// Se usa en la consola individual, en las pestañas y en el cajón de la ficha.
function attachTerminalClipboard(term) {
  const copyText = async (text) => {
    if (!text) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        return;
      }
    } catch (error) {
      // Se prueba el método clásico.
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } catch (error) { /* sin permiso */ }
    area.remove();
  };

  const pasteText = async () => {
    try {
      if (navigator.clipboard?.readText) {
        const text = await navigator.clipboard.readText();
        if (text) term.paste(text);
        return;
      }
    } catch (error) {
      // Se prueba el método clásico.
    }
    const area = document.createElement("textarea");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.focus();
    let text = "";
    try {
      if (document.execCommand("paste")) text = area.value;
    } catch (error) { /* sin permiso */ }
    area.remove();
    if (text) term.paste(text);
  };

  term.attachCustomKeyEventHandler((event) => {
    if (event.type !== "keydown" || !event.ctrlKey || !event.shiftKey) return true;
    const key = (event.key || "").toLowerCase();
    if (key === "c") {
      const selection = term.getSelection();
      if (selection) {
        event.preventDefault();
        event.stopPropagation();
        copyText(selection);
        return false;
      }
    }
    if (key === "v") {
      event.preventDefault();
      event.stopPropagation();
      pasteText();
      return false;
    }
    return true;
  });
}
