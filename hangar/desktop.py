"""Hangar como aplicación de escritorio: arranca el servidor como proceso
aparte, en un puerto libre y con una clave aleatoria, y muestra la interfaz
en una ventana propia (Qt WebEngine) en vez de en el navegador. Al cerrar la
ventana se apaga el servidor.

El servidor no va en un hilo de este mismo proceso a propósito: junto a
Chromium (QtWebEngine) las páginas se quedaban colgadas o el proceso caía.

Se lanza con ./hangar-app (que prepara el entorno) o con: python -m hangar.desktop
"""
from __future__ import annotations

import ctypes
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ICON = Path(__file__).resolve().parent / "static" / "icon.svg"
LOG = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hangar" / "server.log"


# Lo crea ./hangar-app si Chromium se cae con la GPU: desde entonces, sin ella
NO_GPU_MARK = LOG.parent / "no-gpu"


def use_gpu() -> bool:
    """Con GPU el scroll va a 60 fps (sin ella, por CPU, a unos 35). Se
    desactiva sin pantalla real, sin tarjeta gráfica, si se pide
    (HANGAR_NO_GPU=1) o si ya se cayó antes en este equipo; HANGAR_GPU=1 la fuerza."""
    if os.environ.get("HANGAR_GPU"):
        return True
    if os.environ.get("HANGAR_NO_GPU") or NO_GPU_MARK.exists():
        return False
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        return False   # sin pantalla, Chromium se caía al intentar usar la GPU
    return any(Path("/dev/dri").glob("renderD*"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _die_with_parent() -> None:
    """En el proceso hijo: que el kernel lo termine si muere la ventana,
    aunque sea de golpe, para no dejar servidores huérfanos."""
    PR_SET_PDEATHSIG = 1
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def _start_server(port: int, token: str) -> subprocess.Popen:
    root = Path(__file__).resolve().parent.parent
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("w") as log:   # el hijo hereda el descriptor; aquí se puede cerrar
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "hangar.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=root, env={**os.environ, "HANGAR_TOKEN": token},
            stdout=log, stderr=subprocess.STDOUT, preexec_fn=_die_with_parent)


def _wait_ready(proc: subprocess.Popen, port: int, timeout: float = 30) -> str | None:
    """Espera a que el servidor responda; devuelve el error si no arranca."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return LOG.read_text(errors="replace").strip()[-1500:] or \
                f"terminó con código {proc.returncode}"
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return None
        except urllib.error.HTTPError:
            return None   # responde (un 403 sin clave es lo esperado): ya está arrancado
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    return "tardó demasiado en responder"


def main() -> int:
    token = secrets.token_urlsafe(32)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    if not use_gpu():
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{flags} --disable-gpu".strip()

    from PySide6.QtCore import QStandardPaths, Qt, QTimer, QUrl
    from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

    server = _start_server(port, token)   # antes de Qt: menos hilos al lanzar el hijo


    app = QApplication(sys.argv)
    app.setApplicationName("Hangar")
    app.setApplicationDisplayName("Hangar")
    app.setDesktopFileName("hangar")      # enlaza la ventana con hangar.desktop (icono del dock)
    app.setWindowIcon(QIcon(str(ICON)))

    # Ctrl+C o «kill» cierran la ventana con orden; el temporizador deja que
    # Python atienda las señales mientras Qt tiene el control
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    ticker = QTimer()
    ticker.timeout.connect(lambda: None)
    ticker.start(300)

    error = _wait_ready(server, port)
    if error:
        server.kill()
        QMessageBox.critical(None, "Hangar",
                             f"No se pudo arrancar el servidor interno de Hangar:\n\n{error}")
        return 1

    # Perfil sin disco: la cookie con la clave muere al cerrar la aplicación
    profile = QWebEngineProfile(app)
    windows: list[QMainWindow] = []

    class Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            # Lo que no es de Hangar (p. ej. un enlace a la documentación) va al navegador
            if is_main_frame and url.scheme() in ("http", "https") \
                    and not url.toString().startswith(base):
                QDesktopServices.openUrl(url)
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)

        def createWindow(self, _type):
            # target="_blank" (la consola) abre otra ventana de la aplicación
            return Window(size=(1000, 640)).view.page()

    class Window(QMainWindow):
        def __init__(self, url: str | None = None, size=(1280, 840)):
            super().__init__()
            self.view = QWebEngineView(self)
            self.view.setPage(Page(profile, self.view))
            # Ctrl+Shift+C/V de la consola: permitir que xterm lea/escriba el portapapeles.
            self.view.settings().setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
            self.view.settings().setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptCanPaste, True)
            self.view.titleChanged.connect(self.setWindowTitle)
            if os.environ.get("HANGAR_DEBUG"):
                self.view.loadFinished.connect(lambda ok: print(
                    f"[hangar] carga {'ok' if ok else 'FALLIDA'}: "
                    f"{self.view.url().path() or '/'} · {self.view.title()}", flush=True))
            self.setCentralWidget(self.view)
            self.resize(*size)
            for keys, action in (("F5", self.view.reload), ("Ctrl+R", self.view.reload),
                                 ("Alt+Left", self.view.back), ("Alt+Right", self.view.forward),
                                 ("Ctrl+W", self.close)):
                QShortcut(QKeySequence(keys), self, activated=action)
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.destroyed.connect(lambda: windows.remove(self) if self in windows else None)
            windows.append(self)
            if url:
                self.view.load(QUrl(url))
            self.show()

    def on_download(request):
        # Exportaciones (.md): directamente a la carpeta de Descargas
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        request.setDownloadDirectory(folder)
        request.accept()
        target = Path(folder) / request.downloadFileName()
        request.isFinishedChanged.connect(
            lambda: main_window.statusBar().showMessage(f"Guardado en {target}", 10000))

    profile.downloadRequested.connect(on_download)

    main_window = Window(f"{base}/?token={token}")
    # Cerrar la ventana principal cierra también las consolas abiertas
    main_window.destroyed.connect(app.quit)
    app.setQuitOnLastWindowClosed(True)

    try:
        return app.exec()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
