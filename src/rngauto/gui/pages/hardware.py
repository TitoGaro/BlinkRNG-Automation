"""Paso 2 — Conexión: todo automático. El programa encuentra y conecta el mando (ESP32) y la capturadora,
y reproduce la imagen y el sonido del juego dentro de la propia aplicación.

Enumerar puertos y cámaras, abrir la capturadora, conectar el ESP32 y cerrar dispositivos puede tardar
segundos o colgarse en el controlador de Windows: todo eso va en segundo plano (``Background`` o un hilo) y
con plazo, para que la interfaz y el vídeo nunca se congelen.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QComboBox, QListWidget, QListWidgetItem, QPushButton

from rngauto.app.discovery import find_capture_card, list_cameras, list_serial_ports, probe_cameras
from rngauto.app.session import HardwareSession
from rngauto.games.bdsp.static_strategy import Mode
from rngauto.i18n import t

from .. import theme
from ..widgets import Background, StatusChip, VideoView, bgr_to_qimage, card, hbox, label
from .base import Page


def _close_quietly(obj, **kwargs) -> None:
    """Cierra ``obj`` ignorando errores: se usa al soltar un dispositivo que puede estar ya desconectado."""
    try:
        obj.close(**kwargs)
    except Exception:
        pass


def _close_later(*objs, **kwargs) -> None:
    """Cierra en otro hilo (cerrar el puerto o la capturadora tarda hasta ~3 s: la interfaz no se congela)."""
    def work() -> None:
        for obj in objs:
            if obj is not None:
                _close_quietly(obj, **kwargs)

    threading.Thread(target=work, daemon=True, name="close-late").start()


class HardwarePage(Page):
    """Paso «Conexión»: mando (ESP32) y capturadora, con vista previa del juego.

    Al entrar se busca el ESP32 cada 2,5 s (para detectarlo en cuanto se enchufa) y se abre la capturadora
    recordada o la que parezca una capturadora. Se completa con imagen y con el mando emparejado (en modo
    Ensayo basta la imagen).
    """

    @property
    def step_title(self) -> str:
        return t("Conexión", "Connection")

    @property
    def step_hint(self) -> str:
        return t("Mando y capturadora", "Controller and capture card")

    def __init__(self, state) -> None:
        super().__init__(state, t("Conecta la Switch", "Connect the Switch"),
                         t("Enchufa el ESP32 y la capturadora: el programa los encuentra y los conecta solo. "
                           "La imagen y el sonido del juego se ven y se oyen aquí, sin otros programas.",
                           "Plug in the ESP32 and the capture card: the program finds and connects them by itself. "
                           "The game's picture and sound are shown and played here, with no other programs."))
        # --- Mando ---
        self.esp_chip = StatusChip(t("Mando", "Controller"))
        self.esp_help = label("", "muted")
        self.port = QComboBox()
        self.port.setToolTip(t("Puerto del ESP32 (se elige solo)", "ESP32 port (selected automatically)"))
        self.connect_btn = QPushButton(t("Reconectar", "Reconnect"))
        self.connect_btn.clicked.connect(lambda: self._connect_esp32(force=True))
        self.body.addWidget(card(self.esp_chip, self.esp_help, hbox(self.port, self.connect_btn, stretch_last=True),
                                 title=t("1 · Mando (ESP32)", "1 · Controller (ESP32)")))

        # --- Capturadora ---
        self.cam_chip = StatusChip(t("Capturadora", "Capture card"))
        self.audio_chip = StatusChip(t("Sonido", "Sound"))
        self.cam_help = label("", "muted")
        self.preview = VideoView(min_height=300)
        self.preview.set_placeholder(t("Aquí verás el juego", "The game will appear here"))
        self.other_btn = QPushButton(t("Elegir otra capturadora", "Choose another capture card"))
        self.other_btn.clicked.connect(self._scan_all)
        self.cams = QListWidget()
        self.cams.setViewMode(QListWidget.ViewMode.IconMode)
        self.cams.setIconSize(QSize(240, 135))
        self.cams.setMinimumHeight(190)
        self.cams.setVisible(False)
        self.cams.itemClicked.connect(self._use_picked)
        self.body.addWidget(card(hbox(self.cam_chip, self.audio_chip, None), self.cam_help, self.preview,
                                 hbox(self.other_btn, None), self.cams,
                                 title=t("2 · Capturadora", "2 · Capture card")))
        self.body.addWidget(label(t("Consejo: en la Switch, desactiva el modo de espera automático (Configuración de la "
                                    "consola → Modo de espera); las esperas hasta un shiny pueden durar horas.",
                                    "Tip: on the Switch, turn off auto-sleep (System Settings → Sleep Mode); "
                                    "waits for a shiny can last hours."), "muted"))
        self.body.addStretch(1)

        self._esp_busy = False
        self._cam_busy = False
        self._esp_failed_ports: set[str] = set()
        self._esp_trying: str | None = None  # puerto de la conexión en curso
        self._known_ports: tuple = ()
        self._bg_ports = Background()
        self._bg_ports.done.connect(self._ports_ready)
        self._bg_esp = Background()
        self._bg_esp.done.connect(self._esp_ok)
        self._bg_esp.failed.connect(self._esp_fail)
        # Conexión que llegó después del plazo: el puerto quedó abierto y hay que liberarlo.
        self._bg_esp.late.connect(lambda r: _close_later(r[0]))
        self._bg_cam = Background()
        self._bg_cam.done.connect(self._cam_ok)
        self._bg_cam.failed.connect(self._cam_fail)
        # Captura que llegó tarde (tras el aviso de que no respondía): liberarla.
        self._bg_cam.late.connect(self._cam_late)
        self._bg_scan = Background()
        self._bg_scan.done.connect(self._scan_done)
        self._bg_scan.failed.connect(self._cam_fail)
        self._likely: set[str] = set()
        self._bg_list = Background()
        self._bg_list.done.connect(self._cameras_listed)
        self._bg_list.failed.connect(self._cam_fail)
        self._bg_names = Background()
        self._bg_names.done.connect(self._names_listed)
        self._bg_names.failed.connect(lambda _msg: self._names_listed([]))
        self._ports_timer = QTimer(self)  # detecta el ESP32 en cuanto se enchufa
        self._ports_timer.timeout.connect(self._refresh_ports)
        self._status_timer = QTimer(self)  # el indicador se pone verde en cuanto la Switch acepta el mando
        self._status_timer.timeout.connect(self._poll_controller)
        self.esp_chip.set_state("idle", t("buscando…", "searching…"))
        self.cam_chip.set_state("idle", t("buscando…", "searching…"))
        self.audio_chip.set_state("idle", "—")

    # --- ciclo de vida ---
    def on_enter(self) -> None:
        self.preview.attach(lambda: self.state.session.latest() if self.state.session is not None else None)
        self._status_timer.start(1500)
        if not self.state.auto_hardware:
            return
        self._ports_timer.start(2500)
        self._refresh_ports()
        if self.state.session is None or self.state.session.demo:
            self._auto_open_camera()
        else:
            self.cam_chip.set_state("ok", f"{self.state.session.capture_label}")
        self._sync_audio_chip()

    def on_leave(self) -> None:
        self.preview.attach(None)
        self._ports_timer.stop()
        self._status_timer.stop()

    # --- mando ---
    def _refresh_ports(self) -> None:
        """Lanza en segundo plano la lista de puertos serie (resultado en ``_ports_ready``)."""
        if self._bg_ports.running:
            return  # la búsqueda anterior aún no ha terminado (driver USB lento): no acumular hilos
        self._bg_ports.run(list_serial_ports, timeout_s=10)

    def _ports_ready(self, ports) -> None:
        """Actualiza el desplegable de puertos y, si procede, conecta solo con el que parece un ESP32.

        Un puerto que ya falló no se reintenta solo (evita reconectar en bucle): hace falta «Reconectar».
        """
        self._likely = {p.device for p in ports if p.likely_esp32}
        key = tuple(p.device for p in ports)
        if key != self._known_ports:
            self._known_ports = key
            current = self.port.currentData()
            self.port.clear()
            for p in ports:
                self.port.addItem(p.label, p.device)
            preferred = current or self.state.prefs.serial_port
            idx = self.port.findData(preferred) if preferred else -1
            if idx >= 0:
                self.port.setCurrentIndex(idx)
        if not ports:
            if self.state.pending_controller is None:
                self.esp_chip.set_state("idle", t("no encontrado", "not found"))
                self.esp_help.setText(t("Enchufa el ESP32 al PC con un cable USB de datos; se detectará solo.",
                                        "Plug the ESP32 into the PC with a USB data cable; it will be detected "
                                        "automatically."))
            return
        ctrl = self.state.pending_controller
        device = self.port.currentData()
        # Autoconexión solo a puertos que parecen un ESP32 (no enviar bytes a otros dispositivos).
        if ctrl is None and not self._esp_busy and device in self._likely and device not in self._esp_failed_ports:
            self._connect_esp32()
        elif ctrl is None and not self._esp_busy and device not in self._likely:
            self.esp_chip.set_state("idle", t("elige el puerto del ESP32 y pulsa «Reconectar»",
                                              "choose the ESP32 port and click \"Reconnect\""))

    def _connect_esp32(self, force: bool = False) -> None:
        """Conecta con el ESP32 del puerto elegido en segundo plano; ``force`` reemplaza el mando actual."""
        device = self.port.currentData()
        if not device or self._esp_busy:
            return
        old = self.state.pending_controller
        if old is not None and not force:
            return
        self._esp_busy = True
        self._esp_trying = device  # el desplegable puede cambiar mientras se conecta
        self.connect_btn.setEnabled(False)
        self.esp_chip.set_state("busy", t("conectando con {device}…", "connecting to {device}…", device=device))
        self.esp_help.setText("")
        if old is not None:
            self.state.detach_controller()
        from rngauto.control.esp32 import ESP32Controller

        def connect():
            if old is not None:  # liberar el puerto antes de reabrirlo (si no: «Acceso denegado»)
                _close_quietly(old)
            ctrl = ESP32Controller.open_serial(device)
            return ctrl, device, ctrl.status()

        self._bg_esp.run(connect, timeout_s=30,
                         timeout_message=t("El ESP32 de {device} no responde. Desenchúfalo, vuelve a enchufarlo y "
                                           "pulsa «Reconectar».",
                                           "The ESP32 on {device} is not responding. Unplug it, plug it back in "
                                           "and click \"Reconnect\".", device=device))

    def _esp_ok(self, result) -> None:
        """Mando conectado: se recuerda el puerto y se instala en el estado de la aplicación."""
        ctrl, device, status = result
        self._esp_busy = False
        self.connect_btn.setEnabled(True)
        self._esp_failed_ports.discard(device)
        self.state.prefs.serial_port = device
        self.state.save_prefs()
        info = ctrl.client.info
        self.state.set_controller(ctrl, f"{info.name} · {device}")
        note = ""
        if info.firmware_mismatch:
            note = t("Aviso: firmware {firmware}, distinto del probado; debería funcionar igual.",
                     "Warning: firmware {firmware} differs from the tested one; it should still work.",
                     firmware=info.firmware)
        self._show_controller_status(status, note)
        self.completion_changed.emit()

    def _esp_fail(self, message: str) -> None:
        """Fallo o plazo agotado al conectar: el puerto queda marcado para no reintentarlo solo."""
        self._esp_busy = False
        self.connect_btn.setEnabled(True)
        device = self._esp_trying
        if device:
            self._esp_failed_ports.add(device)
        self.esp_chip.set_state("error", t("no se pudo conectar", "could not connect"))
        self.esp_help.setText(f'<span style="color:{theme.DANGER}">{message.splitlines()[0]}</span><br>'
                              + t("Pulsa «Reconectar» cuando lo hayas solucionado.",
                                  "Click \"Reconnect\" once you have fixed it."))

    def _show_controller_status(self, st, note: str = "") -> None:
        """Indicador y ayuda del mando según esté conectado al PC y emparejado con la Switch."""
        if st.connected and st.console_ready:
            self.esp_chip.set_state("ok", t("{device} · emparejado con la Switch", "{device} · paired with the Switch",
                                            device=st.device))
            self.esp_help.setText(note or t("Listo.", "Ready."))
        elif st.connected:
            self.esp_chip.set_state("warn", t("conectado al PC, falta emparejarlo con la Switch",
                                              "connected to the PC, not yet paired with the Switch"))
            self.esp_help.setText(t("En la Switch: <b>Mandos → Cambiar agarre/orden</b> y espera unos segundos. "
                                    "Se pondrá en verde solo.",
                                    "On the Switch: <b>Controllers → Change Grip/Order</b> and wait a few seconds. "
                                    "It will turn green by itself.") + (f"<br>{note}" if note else ""))
        else:
            self.esp_chip.set_state("error", t("el ESP32 no responde", "the ESP32 is not responding"))
            self.esp_help.setText(t("Comprueba el cable USB y pulsa «Reconectar».",
                                    "Check the USB cable and click \"Reconnect\"."))

    def _poll_controller(self) -> None:
        """Sondeo periódico del mando: el emparejamiento con la Switch no avisa con ninguna señal."""
        ctrl = self.state.pending_controller
        if ctrl is None or self._esp_busy:
            return
        st = ctrl.status()
        ready_before = self.esp_chip.text()
        self._show_controller_status(st)
        if self.esp_chip.text() != ready_before:
            self.completion_changed.emit()

    # --- capturadora ---
    def _auto_open_camera(self) -> None:
        """Lista las cámaras en segundo plano para abrir la capturadora sin intervención (``_cameras_listed``)."""
        if self._cam_busy:
            return
        self._cam_busy = True
        self._bg_list.run(list_cameras, timeout_s=20,
                          timeout_message=t("Windows no responde al buscar las cámaras.",
                                            "Windows is not responding while searching for cameras."))

    def _cameras_listed(self, cams) -> None:
        """Elige la capturadora recordada (si sigue ahí y no es una webcam) o la detectada, y la abre."""
        self._cam_busy = False
        found = find_capture_card(cams)
        if found is None:
            self.cam_chip.set_state("error", t("no encontrada", "not found"))
            self.cam_help.setText(t("No veo ninguna capturadora. Enchúfala al PC y pulsa «Elegir otra capturadora».",
                                    "No capture card found. Plug it into the PC and click "
                                    "\"Choose another capture card\"."))
            return
        preferred = next((c for c in cams if c.index == self.state.prefs.capture_index and not c.is_webcam), found)
        self._open_camera(preferred.index, preferred.name)

    def _open_camera(self, index: int, name: str) -> None:
        """Abre la cámara ``index`` en segundo plano, cerrando antes la sesión actual, y prepara su audio."""
        self._cam_busy = True
        target = name or t("dispositivo {index}", "device {index}", index=index)
        self.cam_chip.set_state("busy", t("conectando con {target}… (eligiendo el mejor modo)",
                                          "connecting to {target}… (choosing the best mode)", target=target))
        self.cam_help.setText("")
        old = self.state.detach_session()  # el mando sigue conectado

        def open_it():
            if old is not None:  # liberar la captura actual antes de abrir otra (tarda hasta ~3 s)
                _close_quietly(old, close_controller=old.demo)
            session = HardwareSession.open_capture(index)
            session.capture_label = f"{name} · {session.capture_label}" if name else session.capture_label
            audio, audio_error = self.state.prepare_audio(name)  # también lento: aquí, no en la interfaz
            return session, name, audio, audio_error

        self._bg_cam.run(open_it, timeout_s=45,
                         timeout_message=t("La capturadora no responde. Desenchúfala y vuelve a enchufarla.",
                                           "The capture card is not responding. Unplug it and plug it back in."))

    def _cam_late(self, result) -> None:
        """Captura que llegó tarde (tras el aviso de que no respondía): liberarla, con su audio."""
        session, _name, audio, _err = result
        if audio is not None:
            audio.stop()
        _close_later(session, close_controller=False)

    def _cam_ok(self, result) -> None:
        """Capturadora abierta: se recuerda, se instala la sesión con su audio y se oculta la lista."""
        session, name, audio, audio_error = result
        self._cam_busy = False
        self.state.prefs.capture_index = session.device.index
        self.state.save_prefs()
        self.state.selection.demo = False
        self.state.set_session(session)
        self.state.install_audio(audio, audio_error)
        self.cam_chip.set_state("ok", session.capture_label)
        self.cam_help.setText(t("Si abajo ves la imagen de tu Switch, todo está bien.",
                                "If you can see your Switch's picture below, everything is fine."))
        self.cams.setVisible(False)
        self._sync_audio_chip()
        self.completion_changed.emit()

    def _cam_fail(self, message: str) -> None:
        """Fallo al buscar o abrir la capturadora: se muestra el motivo y la causa más habitual."""
        self._cam_busy = False
        self.cam_chip.set_state("error", t("sin imagen", "no picture"))
        self.cam_help.setText(f'<span style="color:{theme.DANGER}">{message.splitlines()[0]}</span><br>'
                              + t("Si otro programa (de grabación o streaming) está usando la capturadora, ciérralo (Windows no deja "
                                  "que dos programas la usen a la vez) y pulsa «Elegir otra capturadora».",
                                  "If another program (for recording or streaming) is using the capture card, close it (Windows does not "
                                  "let two programs use it at the same time) and click "
                                  "\"Choose another capture card\"."))

    def _scan_all(self) -> None:
        """Lista por nombre (sin abrir nada). Si no hay nombres, se sondea con miniaturas. Todo en otro hilo:
        si Windows/MSMF se cuelga al enumerar, la interfaz y el vídeo siguen respondiendo."""
        if self._cam_busy:
            return
        self._cam_busy = True
        self.cam_chip.set_state("busy", t("buscando capturadoras…", "searching for capture cards…"))
        self._bg_names.run(list_cameras, timeout_s=20)

    def _names_listed(self, cams) -> None:
        """Con nombres, se muestran tal cual; sin ellos, se sondean las cámaras para sacar miniaturas."""
        if cams and all(c.name for c in cams):
            self._scan_done(cams)
            return
        self.cam_chip.set_state("busy", t("buscando capturadoras… (unos segundos)",
                                          "searching for capture cards… (a few seconds)"))
        old = self.state.detach_session()

        def probe():
            if old is not None:  # el sondeo abre los dispositivos: liberar antes la captura actual
                _close_quietly(old, close_controller=old.demo)
            return probe_cameras(4)

        self._bg_scan.run(probe, timeout_s=60, timeout_message=t("La búsqueda de capturadoras no termina.",
                                                                 "The capture card search is not finishing."))

    def _scan_done(self, cams) -> None:
        """Rellena la lista para elegir capturadora a mano (con miniaturas si las hay)."""
        self._cam_busy = False
        self.cams.clear()
        self.cams.setVisible(True)
        self.cams.setViewMode(QListWidget.ViewMode.IconMode if any(c.thumbnail is not None for c in cams)
                              else QListWidget.ViewMode.ListMode)
        for c in cams:
            if not c.opened:
                continue
            item = QListWidgetItem(c.label + ("  (webcam)" if c.is_webcam and "webcam" not in c.label else ""))
            item.setData(Qt.ItemDataRole.UserRole, (c.index, c.name))
            if c.thumbnail is not None:
                item.setIcon(QIcon(QPixmap.fromImage(bgr_to_qimage(c.thumbnail))))
            if c.thumbnail is not None and not c.frames:  # se abrió pero no da imagen: no se puede elegir
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.cams.addItem(item)
        self.cam_chip.set_state("idle", t("haz clic en tu capturadora", "click your capture card"))

    def _use_picked(self, item: QListWidgetItem) -> None:
        index, name = item.data(Qt.ItemDataRole.UserRole)
        self._open_camera(index, name)

    def _sync_audio_chip(self) -> None:
        """Indicador del sonido según el audio instalado en el estado."""
        audio = self.state.audio
        if audio is None:
            self.audio_chip.set_state("idle", self.state.audio_error
                                      or t("sin entrada de audio de la capturadora", "no audio input from the capture card"))
        elif audio.running:
            self.audio_chip.set_state("ok", t("silenciado", "muted") if audio.muted else t("activado", "on"))
        else:
            self.audio_chip.set_state("warn", audio.error or t("desactivado", "off"))

    def is_complete(self) -> bool:
        return self.state.capture_ready and self.state.controller_ready

    def missing(self) -> str:
        if not self.state.capture_ready:
            return t("Conecta la capturadora", "Connect the capture card")
        if self.state.selection.mode is Mode.DRY_RUN:
            return ""
        return t("Conecta y empareja el mando (ESP32)", "Connect and pair the controller (ESP32)")
