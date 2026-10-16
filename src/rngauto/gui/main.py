"""Aplicación de escritorio guiada: ``rngauto-gui`` (o doble clic en «RNG Automation.bat»).

``MainWindow`` guía por pasos: barra lateral con los pasos, barra superior con el estado del mando,
la capturadora y el modo (más sonido, «Ver juego» y PARADA), la página actual en el centro y la navegación
«Atrás»/«Siguiente» abajo. ``main`` crea la aplicación, el estado compartido y la ventana.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from rngauto import __author__, __version__, i18n
from rngauto.games.bdsp.static_strategy import Mode
from rngauto.i18n import t

from . import theme
from .about import SUPPORT_URL, AboutDialog
from .game_window import GameWindow
from .icons import icon
from .pages.base import Page
from .pages.detection import DetectionPage
from .pages.hardware import HardwarePage
from .pages.history import HistoryPage
from .pages.objective import ObjectivePage
from .pages.run import RunPage
from .pages.scene import ScenePage
from .state import AppState
from .widgets import ROLE_HINT, ROLE_STATE, StatusChip, StepDelegate

APP_NAME = "RNG Automation"
GAME_NAME = "Brilliant Diamond · Shining Pearl"
ICON_PATH = Path(__file__).with_name("icon.png")


def _separator(vertical: bool = False) -> QFrame:
    line = QFrame()
    line.setObjectName("vseparator" if vertical else "separator")
    return line


def _mode_name(mode: Mode) -> str:
    return {Mode.ASSISTED: t("Asistido", "Assisted"), Mode.AUTOMATIC: t("Automático", "Automatic"),
            Mode.DRY_RUN: t("Ensayo", "Dry run")}[mode]


class MainWindow(QMainWindow):
    """Ventana principal: pasos, barras de estado y navegación alrededor de las páginas.

    Un paso solo es accesible si los anteriores están completos (el historial, siempre). Mientras hay una
    ejecución en marcha no se puede cambiar de paso ni de idioma, y el botón de PARADA (y F12) queda visible.
    """

    def __init__(self, state: AppState | None = None, on_language=None) -> None:
        super().__init__()
        self.state = state or AppState()
        self._on_language = on_language  # reconstruye la ventana en el nuevo idioma (lo da main())
        self._closing_for_rebuild = False  # cierre por cambio de idioma: no se suelta el hardware
        self._game: GameWindow | None = None
        self.setWindowTitle(f"{APP_NAME} — BDSP")
        self.resize(1320, 860)
        self.setMinimumSize(1100, 720)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        main.addWidget(self._build_topbar())
        self.stack = QStackedWidget()
        self.pages: list[Page] = [
            ObjectivePage(self.state), HardwarePage(self.state), ScenePage(self.state),
            DetectionPage(self.state), RunPage(self.state), HistoryPage(self.state),
        ]
        for i, page in enumerate(self.pages):
            self.stack.addWidget(page)
            item = QListWidgetItem(page.step_title)
            item.setData(ROLE_HINT, page.step_hint)
            self.steps.addItem(item)
            page.set_step(i, len(self.pages))
            page.completion_changed.connect(self._refresh)
        main.addWidget(self.stack, 1)
        main.addWidget(self._build_bottombar())
        outer.addLayout(main, 1)

        self.back_btn.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        self.next_btn.clicked.connect(lambda: self.go(self.stack.currentIndex() + 1))
        self.steps.currentRowChanged.connect(self._step_clicked)
        self.emergency.clicked.connect(self.state.request_stop)
        self.state.stop_requested.connect(self.run_page.stop)
        stop_key = QShortcut(QKeySequence("F12"), self)  # PARADA también con el teclado
        # En toda la aplicación: también desde «Ver juego» o con un diálogo de la ejecución en primer plano.
        stop_key.setContext(Qt.ShortcutContext.ApplicationShortcut)
        stop_key.activated.connect(lambda: self.state.request_stop() if self.emergency.isVisible() else None)
        self.run_page.running_changed.connect(self._running_changed)
        self.state.changed.connect(self._refresh)
        # El emparejamiento del mando con la Switch cambia sin emitir ninguna señal: se refresca cada segundo.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(1000)
        self.go(0)

    # --- construcción ---
    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(248)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        brand = QWidget()
        bl = QHBoxLayout(brand)
        bl.setContentsMargins(18, 16, 18, 16)
        bl.setSpacing(10)
        logo = QLabel()
        if ICON_PATH.exists():
            logo.setPixmap(QPixmap(str(ICON_PATH)).scaled(28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                                                         Qt.TransformationMode.SmoothTransformation))
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel(APP_NAME)
        name.setObjectName("brand")
        game = QLabel(GAME_NAME)
        game.setObjectName("brandSub")
        author = QLabel(t("por {author}", "by {author}", author=__author__))
        author.setObjectName("brandAuthor")
        names.addWidget(name)
        names.addWidget(game)
        names.addWidget(author)
        bl.addWidget(logo)
        bl.addLayout(names, 1)
        lay.addWidget(brand)
        lay.addWidget(_separator())

        steps_title = QLabel(t("PASOS", "STEPS"))
        steps_title.setObjectName("caption")
        steps_title.setContentsMargins(20, 16, 0, 6)
        lay.addWidget(steps_title)
        self.steps = QListWidget()
        self.steps.setObjectName("steps")
        self.steps.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.steps.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.steps.setItemDelegate(StepDelegate(self.steps))
        self.steps.setMouseTracking(True)
        self.steps.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay.addWidget(self.steps, 1)

        lay.addWidget(_separator())
        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 10, 16, 12)
        fl.setSpacing(8)
        globe = QLabel()
        globe.setPixmap(icon("globe", theme.MUTED).pixmap(QSize(16, 16)))
        self.language = QComboBox()
        for code, label in i18n.LANGUAGES.items():
            self.language.addItem(label, code)
        self.language.setCurrentIndex(max(0, self.language.findData(i18n.language())))
        self.language.setToolTip(t("Idioma de la aplicación", "Application language"))
        self.language.currentIndexChanged.connect(self._language_changed)
        # Iconos sin texto (con descripción al pasar el ratón): así cabe el selector de idioma.
        about = QPushButton()
        about.setObjectName("ghost")
        about.setIcon(icon("info", theme.MUTED))
        about.setCursor(Qt.CursorShape.PointingHandCursor)
        about.setToolTip(t("Créditos · versión {version}: autor, agradecimientos y licencia",
                           "Credits · version {version}: author, acknowledgements and license", version=__version__))
        about.setAccessibleName(t("Créditos", "Credits"))
        about.clicked.connect(lambda: AboutDialog(self).exec())
        coffee = QPushButton()
        coffee.setObjectName("ghost")
        coffee.setIcon(icon("coffee", theme.GOLD))
        coffee.setCursor(Qt.CursorShape.PointingHandCursor)
        coffee.setToolTip(t("Invítame a un café (abre el navegador)", "Buy me a coffee (opens the browser)"))
        coffee.setAccessibleName(t("Invítame a un café", "Buy me a coffee"))
        coffee.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SUPPORT_URL)))
        fl.addWidget(globe)
        fl.addWidget(self.language, 1)
        fl.addWidget(about)
        fl.addWidget(coffee)
        lay.addWidget(foot)
        return side

    def _build_topbar(self) -> QFrame:
        top = QFrame()
        top.setObjectName("topbar")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(24, 10, 16, 10)
        tl.setSpacing(18)
        self.chip_ctrl = StatusChip(t("Mando", "Controller"))
        self.chip_cap = StatusChip(t("Capturadora", "Capture card"))
        self.chip_mode = StatusChip(t("Modo", "Mode"))
        for chip in (self.chip_ctrl, self.chip_cap, self.chip_mode):
            tl.addWidget(chip)
        tl.addStretch(1)

        self.game_btn = QPushButton(t("Ver juego", "Game view"))
        self.game_btn.setObjectName("ghost")
        self.game_btn.setIcon(icon("monitor", theme.MUTED))
        self.game_btn.setToolTip(t("Abre la imagen del juego en una ventana aparte (doble clic: pantalla completa)",
                                   "Open the game video in a separate window (double-click: full screen)"))
        self.game_btn.clicked.connect(self._show_game)
        self.sound_btn = QPushButton()
        self.sound_btn.setObjectName("ghost")
        self.sound_btn.setCheckable(True)
        self.sound_btn.setToolTip(t("Sonido del juego (entrada de audio de la capturadora)",
                                    "Game audio (from the capture card)"))
        self.sound_btn.clicked.connect(lambda on: self.state.set_audio(enabled=on))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(96)
        self.volume.setToolTip(t("Volumen", "Volume"))
        self.volume.setValue(int(self.state.audio_volume * 100))
        self.volume.valueChanged.connect(lambda v: self.state.set_audio(volume=v / 100))
        self.emergency = QPushButton(t("Detener", "Stop"))
        self.emergency.setObjectName("danger")
        self.emergency.setIcon(icon("stop", "#ffffff"))
        self.emergency.setVisible(False)
        self.emergency.setToolTip(t("Detiene al instante lo que esté en marcha y deja el mando en neutro (F12)",
                                    "Stops everything immediately and releases the controller (F12)"))
        tl.addWidget(self.game_btn)
        tl.addWidget(_separator(vertical=True))
        tl.addWidget(self.sound_btn)
        tl.addWidget(self.volume)
        tl.addWidget(self.emergency)
        return top

    def _build_bottombar(self) -> QFrame:
        nav = QFrame()
        nav.setObjectName("bottombar")
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(24, 10, 24, 10)
        self.back_btn = QPushButton(t("Atrás", "Back"))
        self.back_btn.setIcon(icon("arrow_left", theme.TEXT, disabled=theme.FAINT))
        self.next_btn = QPushButton(t("Siguiente", "Next"))
        self.next_btn.setObjectName("primary")
        self.next_btn.setIcon(icon("arrow_right", "#ffffff", disabled="#7d8aa0"))
        self.next_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.hint = QLabel("")
        self.hint.setObjectName("muted")
        nl.addWidget(self.back_btn)
        nl.addStretch(1)
        nl.addWidget(self.hint)
        nl.addSpacing(12)
        nl.addWidget(self.next_btn)
        return nav

    # --- comportamiento ---
    def _language_changed(self) -> None:
        """Aplica el idioma elegido reconstruyendo la ventana entera.

        Todos los textos se generan con ``t()`` al construir los widgets, así que crear una ventana nueva es más
        simple y seguro que retraducir cada uno. Se difiere con ``singleShot(0)`` porque esta llamada sale de
        una señal de un widget de la ventana que se va a destruir.
        """
        code = self.language.currentData()
        if code == i18n.language():
            return
        busy = self.state.running or any(getattr(p, "busy", lambda: False)() for p in self.pages)
        if busy:
            # Reconstruir la ventana a mitad de una conexión dejaría el mando o la capturadora abiertos sin dueño.
            QMessageBox.information(self, t("En marcha", "Running"),
                                    t("Espera a que termine la ejecución o la conexión antes de cambiar de idioma.",
                                      "Wait for the run or the connection to finish before changing the language."))
            self.language.blockSignals(True)  # devolver el desplegable al idioma actual sin volver a entrar aquí
            self.language.setCurrentIndex(self.language.findData(i18n.language()))
            self.language.blockSignals(False)
            return
        self.state.set_language(code)
        if self._on_language is not None:
            QTimer.singleShot(0, lambda: self._on_language(self))

    def _show_game(self) -> None:
        """Abre (o trae al frente) la ventana «Ver juego»; se crea una sola vez y se reutiliza."""
        if self._game is None:
            self._game = GameWindow(self.state)
        self._game.show()
        self._game.raise_()
        self._game.activateWindow()

    @property
    def run_page(self) -> RunPage:
        return self.pages[4]

    def _max_reachable(self) -> int:
        """Índice del primer paso incompleto (hasta ahí se puede avanzar)."""
        for i, page in enumerate(self.pages):
            if not page.is_complete():
                return i
        return len(self.pages) - 1

    def go(self, index: int) -> None:
        """Cambia al paso ``index`` si está permitido y avisa a las páginas con ``on_leave``/``on_enter``.

        El último paso (historial) se puede abrir siempre. Con una ejecución en marcha no se cambia de paso.
        """
        index = max(0, min(index, len(self.pages) - 1))
        if index > self._max_reachable() and index != len(self.pages) - 1:
            return
        if self.state.running and index != self.stack.currentIndex():
            QMessageBox.information(self, t("En marcha", "Running"),
                                    t("Detén la ejecución antes de cambiar de paso.",
                                      "Stop the run before changing steps."))
            self.steps.blockSignals(True)
            self.steps.setCurrentRow(self.stack.currentIndex())
            self.steps.blockSignals(False)
            return
        current = self.pages[self.stack.currentIndex()]
        if current is not self.pages[index]:
            current.on_leave()
        self.stack.setCurrentIndex(index)
        self.steps.blockSignals(True)
        self.steps.setCurrentRow(index)
        self.steps.blockSignals(False)
        self.pages[index].on_enter()
        self._refresh()

    def _step_clicked(self, row: int) -> None:
        if row != self.stack.currentIndex():
            self.go(row)
            if self.stack.currentIndex() != row:  # no alcanzable todavía
                self.steps.blockSignals(True)
                self.steps.setCurrentRow(self.stack.currentIndex())
                self.steps.blockSignals(False)

    def _refresh(self) -> None:
        """Sincroniza la barra lateral, los botones de navegación y los indicadores con el estado actual."""
        idx = self.stack.currentIndex()
        page = self.pages[idx]
        reach = self._max_reachable()
        for i, p in enumerate(self.pages):
            item = self.steps.item(i)
            done = p.is_complete() and i < reach
            enabled = i <= reach or i == len(self.pages) - 1
            item.setData(ROLE_STATE, "current" if i == idx else "done" if done else "todo" if enabled else "locked")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable if enabled else Qt.ItemFlag.NoItemFlags)
        last = idx >= len(self.pages) - 2  # «Ejecutar» e «Historial» no tienen «Siguiente»
        self.next_btn.setVisible(not last)
        busy = self.state.running
        self.emergency.setVisible(busy)
        self.back_btn.setEnabled(idx > 0 and not busy)
        complete = page.is_complete()
        self.next_btn.setEnabled(complete and not busy)
        self.hint.setText("" if complete else page.missing())
        s = self.state.session
        if self.state.selection.mode is Mode.DRY_RUN:
            self.chip_ctrl.set_state("idle", t("no hace falta", "not needed"))
        elif self.state.controller_ready:
            self.chip_ctrl.set_state("ok", t("emparejado", "paired"))
        elif self.state.pending_controller is not None:
            self.chip_ctrl.set_state("warn", t("sin emparejar", "not paired"))
        else:
            self.chip_ctrl.set_state("idle", t("sin conectar", "not connected"))
        if s is not None:
            self.chip_cap.set_state("ok", s.capture_label)
        else:
            self.chip_cap.set_state("idle", t("sin elegir", "not selected"))
        audio = self.state.audio
        self.sound_btn.blockSignals(True)
        self.sound_btn.setChecked(self.state.audio_enabled)
        self.sound_btn.blockSignals(False)
        self.sound_btn.setIcon(icon("volume" if self.state.audio_enabled else "mute", theme.MUTED, disabled=theme.FAINT))
        self.sound_btn.setEnabled(audio is not None)
        self.volume.setEnabled(audio is not None and self.state.audio_enabled)
        self.chip_mode.set_state("busy" if self.state.running else "ok", _mode_name(self.state.selection.mode))

    def _running_changed(self, on: bool) -> None:
        self.steps.setEnabled(not on)
        self.language.setEnabled(not on)
        self._refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        """Al salir se detiene lo que esté en marcha y se suelta el hardware; en una reconstrucción, no."""
        if self._closing_for_rebuild:  # cambio de idioma: el estado (mando, captura) sigue vivo
            for p in self.pages:
                p.on_leave()
            if self._game is not None:
                self._game.close()
            self._refresh_timer.stop()
            event.accept()
            return
        if self.state.running:
            r = QMessageBox.question(self, t("Salir", "Quit"), t("Hay una ejecución en marcha. ¿Detenerla y salir?",
                                                                 "A run is in progress. Stop it and quit?"))
            if r != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.state.request_stop()
        for p in self.pages:
            p.on_leave()
        if self._game is not None:
            self._game.close()
        self.state.close()
        event.accept()


def _install_error_handlers(log_dir: Path) -> None:
    """Sin consola (ejecutable o pythonw), los errores se perderían: van a ``errores.log`` (también los avisos de
    ``logging`` de cualquier módulo) y los de la interfaz se muestran en un diálogo, uno a la vez."""
    import logging
    import threading
    import time
    import traceback
    from logging.handlers import RotatingFileHandler

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "errores.log"
    showing = {"now": False, "last": 0.0}
    try:  # 1 MB y dos copias anteriores como mucho: el registro no crece sin límite
        handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("\n[%(asctime)s] %(levelname)s %(name)s\n%(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.WARNING)
    except OSError:  # carpeta sin permisos: la aplicación funciona igual, sin registro
        pass
    logger = logging.getLogger("rngauto")

    last = {"text": "", "repeats": 0}

    def write(text: str) -> None:
        # Un error que se repite (p. ej. en un temporizador) se registra una vez y se cuenta: si no, en minutos
        # la rotación del registro borraría el primero, que es el que explica el fallo.
        if text == last["text"]:
            last["repeats"] += 1
            return
        if last["repeats"]:
            logger.error("(el error anterior se repitió %d veces más)", last["repeats"])
        last["text"], last["repeats"] = text, 0
        logger.error(text.rstrip())

    def hook(exc_type, exc, tb) -> None:
        """Excepción no capturada en el hilo de la interfaz: se registra y se muestra en un diálogo, con la opción
        de informar del problema."""
        write("".join(traceback.format_exception(exc_type, exc, tb)))
        # Un error repetido (p. ej. en un temporizador) no debe apilar diálogos: uno a la vez y como mucho cada 10 s.
        if QApplication.instance() is None or showing["now"] or time.monotonic() - showing["last"] < 10:
            return
        showing["now"] = True
        try:
            from .report import offer_report

            offer_report(QApplication.activeWindow(), t("Error inesperado", "Unexpected error"),
                         t("{exc}\n\nSe ha guardado el detalle en:\n{path}",
                           "{exc}\n\nDetails were saved to:\n{path}", exc=exc, path=log_file), critical=True,
                         modal=False)  # con una ejecución en marcha, Detener y F12 deben seguir funcionando
        finally:
            showing["now"] = False
            showing["last"] = time.monotonic()

    sys.excepthook = hook
    # En otros hilos solo se registra: un diálogo de Qt no se puede abrir fuera del hilo de la interfaz.
    threading.excepthook = lambda args: write("".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


def self_test(report: str | None = None) -> int:
    """Comprobación sin hardware (``--self-test``): carga encuentros y recursos, las librerías del mando, la
    capturadora y el sonido (sin abrir ningún dispositivo) y construye la ventana sin mostrarla. Sirve para
    verificar un ejecutable recién compilado. Devuelve 0 si todo está bien; con ``report``, escribe además el
    resultado en ese fichero (un ejecutable sin consola no muestra nada)."""
    import os
    import tempfile

    from rngauto.games.bdsp.encounters import static_templates
    from rngauto.games.bdsp.fast_advance import load_methods
    from rngauto.games.bdsp.screens import _MASTER_BALL_TEMPLATE
    from rngauto.vision.ocr import SCRIPT

    os.environ["QT_QPA_PLATFORM"] = "offscreen"  # sin ventana: vale en un servidor sin pantalla
    app = QApplication.instance() or QApplication([])
    problems: list[str] = []
    for path in (ICON_PATH, SCRIPT, _MASTER_BALL_TEMPLATE, Path(theme._ASSETS) / "chevron_down.svg"):
        if not path.exists():
            problems.append(f"falta {path}")
    try:  # librerías que solo se cargan al conectar el hardware: que estén dentro del ejecutable
        import importlib

        import cv2
        import serial.tools.list_ports
        import sounddevice

        for module in ("cv2_enumerate_cameras", "rngauto.control.esp32"):
            importlib.import_module(module)
        serial.tools.list_ports.comports()  # solo lista los puertos, no abre ninguno
        sounddevice.get_portaudio_version()  # la DLL de PortAudio carga
        if not cv2.videoio_registry.hasBackend(cv2.CAP_MSMF):
            problems.append("OpenCV sin Media Foundation (MSMF)")
    except Exception as exc:
        problems.append(f"hardware: {type(exc).__name__}: {exc}")
    try:
        if not static_templates() or not load_methods():
            problems.append("datos del juego vacíos")
        with tempfile.TemporaryDirectory() as tmp:
            state = AppState(data_dir=Path(tmp))
            state.auto_hardware = False  # nunca tocar el mando ni la capturadora
            if len(state.encounters) < 10:
                problems.append(f"solo {len(state.encounters)} encuentros")
            window = MainWindow(state)
            window.show()
            app.processEvents()
            window.close()
            state.close()
    except Exception as exc:  # cualquier fallo de carga es un ejecutable roto
        problems.append(f"{type(exc).__name__}: {exc}")
    result = "self-test: " + ("OK" if not problems else "; ".join(problems))
    print(result)
    if report:
        Path(report).write_text(result + "\n", encoding="utf-8")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada de la aplicación de escritorio; devuelve el código de salida de Qt."""
    from rngauto.app.setup_store import DEFAULT_DATA_DIR

    args = argv if argv is not None else sys.argv
    if "--self-test" in args or any(a.startswith("--self-test=") for a in args):
        # «--self-test=<fichero>» deja además el resultado en ese fichero (para la integración continua).
        report = next((a.split("=", 1)[1] for a in args if a.startswith("--self-test=")), None)
        return self_test(report)
    _install_error_handlers(DEFAULT_DATA_DIR / "logs")
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyleSheet(theme.STYLE)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    # Una sola copia abierta: dos se pelearían por el puerto del ESP32, la capturadora y los ficheros de datos.
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(DEFAULT_DATA_DIR / "rngauto.lock"))
    lock.setStaleLockTime(0)  # si la anterior se cerró de golpe, su bloqueo no cuenta (se comprueba el proceso)
    if not lock.tryLock(200):
        QMessageBox.information(None, APP_NAME, t("RNG Automation ya está abierto.", "RNG Automation is already open."))
        return 0
    state = AppState()
    windows: dict[str, MainWindow] = {}  # referencia a la ventana actual para que no la recoja el recolector

    def rebuild(old: MainWindow) -> None:
        """Nueva ventana en el idioma elegido, con el mismo estado (mando y captura siguen conectados)."""
        geometry, page = old.saveGeometry(), old.stack.currentIndex()
        new = MainWindow(state, on_language=rebuild)
        new.restoreGeometry(geometry)
        new.show()
        new.go(page)
        old._closing_for_rebuild = True
        old.close()
        old.deleteLater()
        windows["main"] = new

    windows["main"] = MainWindow(state, on_language=rebuild)
    windows["main"].show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
