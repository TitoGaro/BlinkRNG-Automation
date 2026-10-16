"""Estado compartido de la aplicación (selección, objetivo, modo, hardware).

Una sola instancia de ``AppState`` vive durante toda la ejecución y la comparten todas las páginas. Sobrevive a la
reconstrucción de la ventana al cambiar de idioma, así que el mando y la capturadora siguen conectados.
Las preferencias se guardan en disco en cuanto cambian.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from rngauto import i18n
from rngauto.i18n import t

from rngauto.app.session import HardwareSession
from rngauto.app.setup_store import DEFAULT_DATA_DIR, Preferences, load_eye_setup
from rngauto.control.controller import Controller, RecordingController
from rngauto.core.encounter import EncounterConfiguration, load_encounters
from rngauto.core.pokemon import TargetCriteria
from rngauto.games.bdsp.static_strategy import Mode
from rngauto.paths import ASSETS_DIR, ENCOUNTERS_DIR  # noqa: F401 (también se importan desde aquí)

log = logging.getLogger(__name__)


@dataclass
class Selection:
    """Lo que el usuario ha elegido en el paso «Objetivo»: encuentro, criterios y modo de trabajo."""

    encounter: EncounterConfiguration | None = None
    criteria: TargetCriteria = TargetCriteria()
    mode: Mode = Mode.AUTOMATIC
    demo: bool = False


class AppState(QObject):
    """Estado de la aplicación: preferencias, encuentros, selección, sesión de captura, mando y sonido.

    El mando (``pending_controller``) pertenece a la aplicación y no a la sesión de captura: cambiar de
    capturadora no lo desconecta. Todos los métodos se llaman desde el hilo de la interfaz salvo
    ``prepare_audio``, que es lento y se ejecuta en segundo plano.
    """

    changed = Signal()  # cualquier cambio relevante (para refrescar indicadores y pasos)
    stop_requested = Signal()  # PARADA: cada página detiene lo que tenga en marcha

    def __init__(self, data_dir: Path = DEFAULT_DATA_DIR) -> None:
        super().__init__()
        self.data_dir = data_dir
        self.assets_dir = ASSETS_DIR
        self.prefs = Preferences.load(data_dir)
        i18n.set_language(str(self.prefs.extra.get("language") or i18n.system_language()))
        self.encounters = load_encounters(ENCOUNTERS_DIR)
        self.selection = Selection()
        self.session: HardwareSession | None = None
        self.pending_controller: Controller | None = None  # mando conectado, con o sin sesión de captura
        self.running = False  # hay una ejecución en marcha (bloquea la navegación y el cambio de idioma)
        self.auto_hardware = True  # buscar/conectar ESP32 y capturadora solo (desactivado en tests)
        self.audio = None  # AudioPassthrough del sonido de la capturadora
        self.audio_error = ""

    # --- idioma ---
    def set_language(self, code: str) -> None:
        """Cambia el idioma y lo guarda. Los encuentros se recargan (sus nombres e instrucciones van por idioma);
        la ventana se reconstruye fuera de aquí."""
        i18n.set_language(code)
        self.prefs.extra["language"] = i18n.language()
        self.save_prefs()
        selected = self.selection.encounter.id if self.selection.encounter is not None else None
        self.encounters = load_encounters(ENCOUNTERS_DIR)
        if selected in self.encounters:
            self.selection.encounter = self.encounters[selected]

    # --- avance rápido (Pokédex) ---
    @property
    def fast_advance_enabled(self) -> bool:
        return bool(self.prefs.extra.get("fast_advance", True))

    def set_fast_advance(self, enabled: bool) -> None:
        self.prefs.extra["fast_advance"] = enabled
        self.save_prefs()
        self.changed.emit()

    # --- objeto registrado (caña) ---
    @property
    def ITEM_DIRECTIONS(self) -> dict[str, str]:  # noqa: N802 — se usa como tabla fija; es propiedad por el idioma
        return {"": t("— elige —", "— choose —"), "NONE": t("Es el único objeto registrado", "It is the only registered item"),
                "UP": t("Arriba", "Up"), "DOWN": t("Abajo", "Down"), "LEFT": t("Izquierda", "Left"),
                "RIGHT": t("Derecha", "Right")}

    @property
    def item_direction(self) -> str:
        return str(self.prefs.extra.get("item_direction", ""))

    def set_item_direction(self, value: str) -> None:
        self.prefs.extra["item_direction"] = value
        self.save_prefs()
        self.changed.emit()

    # --- sonido del juego ---
    @property
    def audio_volume(self) -> float:
        return float(self.prefs.extra.get("audio_volume", 0.8))

    @property
    def audio_enabled(self) -> bool:
        return bool(self.prefs.extra.get("audio_enabled", True))

    def prepare_audio(self, video_name: str = ""):
        """Busca la entrada de audio de la capturadora y la arranca. Puede tardar segundos (Windows prueba
        cada dispositivo): se llama FUERA del hilo de la interfaz. Devuelve ``(audio | None, error)``."""
        from rngauto.app.audio import AudioPassthrough
        from rngauto.app.discovery import find_capture_audio_candidates

        try:
            source = find_capture_audio_candidates(video_name)
        except Exception as exc:  # PortAudio no disponible, etc.: sin sonido, pero la app sigue
            return None, t("sonido no disponible ({exc})", "audio unavailable ({exc})", exc=exc)
        if not source:
            return None, t("la capturadora no tiene entrada de audio", "the capture card has no audio input")
        audio = AudioPassthrough(source, self.audio_volume)
        audio.muted = not self.audio_enabled
        try:
            audio.start()
        except Exception as exc:
            return audio, str(exc)
        return audio, ""

    def install_audio(self, audio, error: str) -> None:
        """Instala el audio preparado con ``prepare_audio`` (en el hilo de la interfaz)."""
        self.stop_audio()
        self.audio, self.audio_error = audio, error
        self.changed.emit()

    def stop_audio(self) -> None:
        if self.audio is not None:
            self.audio.stop()
            self.audio = None

    def set_audio(self, enabled: bool | None = None, volume: float | None = None) -> None:
        """Cambia y guarda el estado del sonido y el volumen, y los aplica al audio activo si lo hay."""
        if enabled is not None:
            self.prefs.extra["audio_enabled"] = enabled
        if volume is not None:
            self.prefs.extra["audio_volume"] = volume
        if self.audio is not None:
            self.audio.muted = not self.audio_enabled
            self.audio.set_volume(self.audio_volume)
        self.save_prefs()
        self.changed.emit()

    # --- hardware ---
    def detach_session(self) -> HardwareSession | None:
        """Quita la captura actual SIN cerrarla y la devuelve.

        Cerrarla tarda hasta ~3 s, así que la cierra quien la recibe, en otro hilo. El mando real se separa
        antes de la sesión para que ese cierre no lo desconecte.
        """
        old = self.session
        if old is not None:
            self.stop_audio()
            if old.controller is not None and old.controller is self.pending_controller and not old.demo:
                old.controller = None  # el mando pertenece a la aplicación, no a la captura
            self.session = None
            self.changed.emit()
        return old

    def set_session(self, session: HardwareSession | None) -> None:
        """Instala una sesión de captura (cerrando la anterior) y le asigna el mando conectado si no trae uno."""
        if self.session is not None and self.session is not session:
            # El mando real pertenece a la aplicación, no a la captura: solo se cierra el mando propio de una
            # sesión simulada (``demo``, solo en tests).
            self.session.close(close_controller=self.session.demo)
            self.stop_audio()
        self.session = session
        if session is not None and session.controller is None and self.pending_controller is not None:
            session.controller = self.pending_controller
        self.changed.emit()

    def set_controller(self, controller: Controller, label: str) -> None:
        """Registra el mando conectado y lo asigna a la sesión de captura real, si la hay."""
        self.pending_controller = controller
        if self.session is not None and not self.session.demo:
            self.session.controller = controller
            self.session.controller_label = label
        self.changed.emit()

    def save_prefs(self) -> None:
        """Guarda las preferencias; un fallo de disco se registra pero no interrumpe la aplicación."""
        try:
            self.prefs.save(self.data_dir)
        except OSError:  # disco lleno, carpeta sin permisos…: se sigue con lo que hay en memoria
            log.exception("No se pudieron guardar las preferencias")

    def detach_controller(self) -> Controller | None:
        """Quita el mando actual SIN cerrarlo (``close`` cancela la cola del ESP32 y puede tardar ~1 s: lo
        hace quien lo recibe, fuera del hilo de la interfaz)."""
        ctrl = self.pending_controller
        self.pending_controller = None
        if self.session is not None and self.session.controller is ctrl:
            self.session.controller = None
        self.changed.emit()
        return ctrl

    @property
    def capture_ready(self) -> bool:
        return self.session is not None

    @property
    def controller_ready(self) -> bool:
        """Hay un mando real emparejado con la Switch (en modo Ensayo no hace falta)."""
        if self.selection.mode is Mode.DRY_RUN:
            return True
        if self.session is not None and self.session.demo:  # sesión simulada (tests): su mando siempre vale
            return True
        ctrl = self.session.controller if self.session is not None else self.pending_controller
        if ctrl is None or isinstance(ctrl, RecordingController):
            return False  # un mando de ensayo no pulsa nada: nunca vale fuera del modo Ensayo
        return ctrl.status().console_ready

    @property
    def eye_ready(self) -> bool:
        """Hay ojo para cada posición del encuentro: los extra, marcados; el principal, marcado o el incluido."""
        enc = self.selection.encounter
        if enc is None:
            return False
        if self.session is not None and self.session.demo:  # la escena simulada trae su propio ojo
            return True
        if any(load_eye_setup(self.data_dir, eye) is None for eye in enc.extra_eyes):
            return False  # el ojo de delante del legendario no tiene plantilla por defecto: hay que marcarlo
        if load_eye_setup(self.data_dir, enc.id) is not None:
            return True
        return (self.assets_dir / enc.blink_scene.eye_template).exists()  # plantilla incluida con la aplicación

    def request_stop(self) -> None:
        """PARADA de emergencia: avisa a las páginas y deja el mando en neutro al instante."""
        self.stop_requested.emit()
        ctrl = self.session.controller if self.session is not None else self.pending_controller
        if ctrl is not None:
            try:
                ctrl.emergency_stop()
            except Exception:  # no debe lanzar; si lo hace, las páginas ya han recibido la PARADA
                pass

    def close(self) -> None:
        """Guarda y suelta todo; cada paso va por separado: que falle uno no deja el mando o la capturadora
        abiertos."""
        steps = [lambda: self.prefs.save(self.data_dir), self.stop_audio]
        if self.session is not None:
            session = self.session
            steps.append(lambda: session.close(close_controller=session.demo))
        if self.pending_controller is not None:
            steps.append(self.pending_controller.close)  # cancela la cola del ESP32 (síncrono, con tope) y cierra
        for step in steps:
            try:
                step()
            except Exception:
                log.exception("Error al cerrar")
