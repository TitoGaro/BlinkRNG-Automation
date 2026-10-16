"""Paso 5 — Ejecutar: el programa trabaja; el usuario ve en todo momento qué hace y por qué.

Diseño: cabecera con el encuentro y un solo botón (Empezar / Detener), fases del intento, vídeo grande y un
panel de estado con UNA cifra principal según la fase (cuenta atrás, parpadeos, ¡SHINY!), su barra de progreso
y cuatro indicadores. Lo técnico (estado RNG y registro) queda plegado abajo.

La máquina de estados corre en un hilo propio («rng-run») y habla con la interfaz solo mediante señales Qt
(``_Bridge``) y el ``GuiOperator``. La PARADA corta la ejecución, deja el mando en neutro y, si el hilo no
responde a tiempo, un vigilante libera la interfaz igualmente.
"""
from __future__ import annotations

import datetime as dt
import logging
import shutil
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QToolButton,
    QVBoxLayout, QWidget,
)

from rngauto.app.power import prevent_sleep
from rngauto.app.session import RunRequest, build_static_run
from rngauto.core.statemachine import Outcome, RunResult, StateMachine
from rngauto.core.timeline import JsonlSink, Timeline, TimelineEvent
from rngauto.games.bdsp.encounters import encounter_species
from rngauto.games.bdsp.static_strategy import Mode, RunStatus
from rngauto.i18n import fmt_int, t

from .. import theme
from ..icons import icon
from ..operator import GuiOperator
from ..report import offer_report
from ..widgets import PhaseStepper, Tile, VideoView, label, panel
from .base import Page

logger = logging.getLogger(__name__)

# Fases del intento en el orden del stepper (claves internas: no se traducen).
PHASES = ("COLLECT", "PLAN", "FAST_ADVANCE", "WAIT_TRIGGER", "TIMELINE", "ENCOUNTER", "VERIFY")


def phase_labels() -> list[str]:
    """Etiquetas visibles de ``PHASES`` en el idioma actual (se evalúa al construir la página, no al importar)."""
    return [t("Semilla", "Seed"), t("Objetivo", "Target"), t("Pokédex", "Pokédex"), t("Espera", "Wait"),
            t("Timeline", "Timeline"), t("Combate", "Battle"), t("Ficha y calibración", "Summary and calibration")]


# Fases de la estrategia que no tienen paso propio en el stepper: se muestran como la fase a la que pertenecen.
PHASE_OF = {"REIDENTIFY": "FAST_ADVANCE", "CATCH": "ENCOUNTER", "RESET": "COLLECT", "PRECHECK": "COLLECT"}


def fmt_clock(seconds: float) -> str:
    """«m:ss», o «h:mm:ss» a partir de una hora."""
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def fmt_countdown(seconds: float) -> str:
    """«8.4 s» en el último minuto (para ver que se mueve); «3:45» o «1:02:10» antes."""
    return f"{max(0.0, seconds):.1f} s" if seconds < 60 else fmt_clock(seconds)


# Color de cada tipo de evento en el registro técnico (el resto, en el color normal del texto).
KIND_COLORS = {"error": theme.DANGER, "warning": theme.WARNING, "plan": theme.ACCENT, "input": theme.SUCCESS,
               "calibration": theme.WARNING, "recovery": theme.ACCENT}


STOP_WATCHDOG_MS = 10_000  # tras la PARADA, tiempo máximo hasta liberar la interfaz


class _Bridge(QObject):
    """Señales que el hilo de ejecución emite y Qt entrega en cola en el hilo de la interfaz."""

    status = Signal(object)
    event = Signal(object)
    finished = Signal(object)


class RunPage(Page):
    """Paso «Ejecutar»: lanza la ejecución en segundo plano y muestra su progreso.

    ``running_changed`` avisa a la ventana para bloquear la navegación y mostrar la PARADA mientras dura.
    """

    running_changed = Signal(bool)

    @property
    def step_title(self) -> str:
        return t("Ejecutar", "Run")

    @property
    def step_hint(self) -> str:
        return t("Busca el shiny", "Hunt the shiny")

    def __init__(self, state) -> None:
        super().__init__(state, t("Ejecución", "Run"),
                         t("Pulsa Empezar y no toques el mando: el programa hace el resto y te "
                           "avisa si necesita algo (o si sale el shiny).",
                           "Press Start and leave the controller alone: the program does the rest and lets you "
                           "know if it needs anything (or if the shiny appears)."))
        # --- cabecera: encuentro, botón Empezar/Detener y fases
        head, hl = panel()
        row = QHBoxLayout()
        info = QVBoxLayout()
        info.setSpacing(4)
        self.encounter_name = QLabel("—")
        self.encounter_name.setObjectName("heading")
        self.summary = label("", "muted")
        info.addWidget(self.encounter_name)
        info.addWidget(self.summary)
        row.addLayout(info, 1)
        self.start_btn = QPushButton(t("Empezar", "Start"))
        self.start_btn.setIcon(icon("play", "#08200f", disabled="#5f7a66"))
        self.start_btn.setObjectName("start")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton(t("Detener", "Stop"))
        self.stop_btn.setIcon(icon("stop", "#ffffff"))
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self.stop)
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        hl.addLayout(row)
        self.stepper = PhaseStepper(phase_labels())
        hl.addWidget(self.stepper)
        self.body.addWidget(head)

        # --- vídeo y panel de estado
        main = QHBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(16)
        vid, vl = panel()
        self.video = VideoView(min_height=340)
        self.video.set_placeholder(t("Sin imagen de la capturadora", "No video from the capture card"))
        vl.addWidget(self.video, 1)
        main.addWidget(vid, 3)

        stat, sl = panel()
        stat.setMinimumWidth(400)
        self.message = label(t("Listo para empezar.", "Ready to start."), "message")
        self.hero = QLabel("—")
        self.hero.setObjectName("hero")
        self.hero_caption = label("", "heroCaption")
        self.progress = QProgressBar()
        self.progress.setObjectName("slim")
        self.progress.setTextVisible(False)
        self.progress_text = label("", "muted")
        self.progress_text.setVisible(False)
        sl.addWidget(self.message)
        sl.addSpacing(4)
        sl.addWidget(self.hero)
        sl.addWidget(self.hero_caption)
        sl.addWidget(self.progress)
        sl.addWidget(self.progress_text)
        sl.addSpacing(6)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.tiles = {key: Tile(title) for key, title in (
            ("advance", t("Avance actual", "Current advance")), ("remaining", t("Faltan", "Remaining")),
            ("target", t("Objetivo", "Target")), ("attempt", t("Intento", "Attempt")))}
        for i, key in enumerate(("advance", "remaining", "target", "attempt")):
            grid.addWidget(self.tiles[key], i // 2, i % 2)
        sl.addLayout(grid)
        self.target_desc = label("", "pill")
        self.target_desc.setVisible(False)
        sl.addWidget(self.target_desc)
        facts = QGridLayout()
        facts.setHorizontalSpacing(12)
        facts.setVerticalSpacing(6)
        self.facts: dict[str, QLabel] = {}
        for i, (key, title) in enumerate((("calibration", t("Calibración", "Calibration")),
                                          ("seed", t("Semilla", "Seed")))):
            cap = QLabel(title)
            cap.setObjectName("muted")
            val = label("—")
            facts.addWidget(cap, i, 0, Qt.AlignmentFlag.AlignTop)
            facts.addWidget(val, i, 1)
            self.facts[key] = val
        facts.setColumnStretch(1, 1)
        sl.addLayout(facts)
        sl.addStretch(1)
        main.addWidget(stat, 2)
        wrap = QWidget()
        wrap.setLayout(main)
        self.body.addWidget(wrap, 1)

        # --- detalles técnicos (plegados)
        tech, tl = panel()
        toggle = QToolButton()
        toggle.setObjectName("link")
        toggle.setText(t("▸  Detalles técnicos", "▸  Technical details"))
        toggle.setCheckable(True)
        self.state_hex = label("", "muted")
        self.state_hex.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.state_hex.setVisible(False)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)  # en esperas de horas el registro no debe crecer sin límite
        self.log.setVisible(False)
        self.log.setMinimumHeight(240)

        def show_details(on: bool) -> None:
            self.log.setVisible(on)
            self.state_hex.setVisible(on and bool(self.state_hex.text()))
            toggle.setText(t("▾  Detalles técnicos", "▾  Technical details") if on
                           else t("▸  Detalles técnicos", "▸  Technical details"))

        toggle.toggled.connect(show_details)
        tl.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        tl.addWidget(self.state_hex)
        tl.addWidget(self.log)
        self.body.addWidget(tech)

        self.bridge = _Bridge()
        self.bridge.status.connect(self._on_status)
        self.bridge.event.connect(self._on_event)
        self.bridge.finished.connect(self._on_finished)
        self.machine: StateMachine | None = None
        self.operator: GuiOperator | None = None
        self.status: RunStatus | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._sink: JsonlSink | None = None
        self._thread: threading.Thread | None = None
        self._wait_span: tuple[float, float] | None = None  # (desde, hasta) de la espera actual

    # --- ciclo de vida ---
    def on_enter(self) -> None:
        """Rellena la cabecera con el encuentro, el objetivo y el modo, y arranca el vídeo y el refresco."""
        sel = self.state.selection
        if sel.encounter is None:
            return
        mode_text = {Mode.ASSISTED: t("Asistido", "Assisted"), Mode.AUTOMATIC: t("Automático", "Automatic"),
                     Mode.DRY_RUN: t("Ensayo", "Dry run")}[sel.mode]
        demo = " · DEMO" if self.state.session is not None and self.state.session.demo else ""  # sesión simulada
        self.encounter_name.setText(sel.encounter.display_name)
        self.summary.setText(t("Objetivo: <b>{target}</b> · modo {mode}{demo}", "Target: <b>{target}</b> · {mode} mode{demo}",
                               target=sel.criteria.describe(), mode=mode_text.lower(), demo=demo))
        self._timer.start(100)
        self.video.attach(lambda: self.state.session.latest() if self.state.session is not None else None)

    def on_leave(self) -> None:
        # Con una ejecución en marcha el refresco sigue (la ventana no deja cambiar de paso, pero sí cerrarse).
        if not self.state.running:
            self._timer.stop()

    def start(self) -> None:
        """Prepara la sesión de datos, construye la ejecución y la lanza en el hilo «rng-run».

        Si algo falla al construirla (falta el ojo, la capturadora no responde…), se explica y no se empieza.
        """
        sel, session = self.state.selection, self.state.session
        if sel.encounter is None or session is None:
            return
        if self._thread is not None and self._thread.is_alive():
            # La anterior no respondió a la PARADA y sigue viva: dos ejecuciones a la vez se pisarían el mando.
            QMessageBox.warning(self, t("No se puede empezar", "Cannot start"),
                                t("La ejecución anterior aún se está deteniendo. Espera unos segundos; si no "
                                  "termina, cierra y vuelve a abrir la aplicación.",
                                  "The previous run is still stopping. Wait a few seconds; if it does not "
                                  "finish, close and reopen the application."))
            return
        self.on_enter()  # refresca el resumen con el modo/objetivo actuales
        enc = sel.encounter
        session_dir = self.state.data_dir / "sessions" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{enc.id}"
        session_dir.mkdir(parents=True, exist_ok=True)
        timeline = Timeline()
        self._sink = JsonlSink(session_dir / "timeline.jsonl")
        timeline.subscribe(self._sink)
        timeline.subscribe(self.bridge.event.emit)
        self.operator = GuiOperator(self.window(), encounter_species(enc))
        self.operator.message.connect(lambda m: self._append_log("plan", m))
        self.operator.stop_requested.connect(self.state.request_stop)
        req = RunRequest(enc, sel.criteria, sel.mode, self.state.prefs.lead_s, self.state.data_dir,
                         self.state.assets_dir, self.state.prefs.tid, self.state.prefs.sid,
                         fast_advance=self.state.fast_advance_enabled, item_direction=self.state.item_direction,
                         audio=self.state.audio)
        try:
            run = build_static_run(session, req, self.operator, timeline, self.bridge.status.emit, session_dir)
        except Exception as exc:
            self._sink.close()
            # Sin ejecución no queda nada que guardar: una carpeta vacía confundiría al historial y al informe.
            shutil.rmtree(session_dir, ignore_errors=True)
            QMessageBox.warning(self, t("No se puede empezar", "Cannot start"), str(exc))
            return
        self.machine = run.build()
        self.log.clear()
        self._set_running(True)
        self.message.setText(t("Empezando…", "Starting…"))
        machine, frames = self.machine, run.d.frames

        controller = run.d.controller

        def work() -> None:
            """Cuerpo del hilo: ejecuta la máquina y, pase lo que pase, suelta recursos y avisa a la interfaz."""
            result = RunResult(Outcome.FAILED, "", t("Error inesperado", "Unexpected error"))
            try:
                with prevent_sleep():  # esperas de horas: que Windows no se suspenda ni apague la pantalla
                    result = machine.run()
            except Exception as exc:  # el motivo, al usuario (y el detalle completo, a errores.log)
                logger.exception("La ejecución terminó con una excepción")
                result = RunResult(Outcome.FAILED, "", f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    run.close()  # suelta el búfer circular del sonido
                except Exception:  # pase lo que pase, la interfaz tiene que enterarse de que ha terminado
                    logger.exception("Error al cerrar la ejecución")
                if result.outcome is not Outcome.SUCCESS:
                    try:
                        controller.emergency_stop()  # nunca dejar un botón pulsado
                    except Exception:
                        pass
                try:
                    if hasattr(frames, "close") and frames is not session.frames:
                        frames.close()  # grabación del ojo
                finally:
                    self.bridge.finished.emit((machine, result))

        self._thread = threading.Thread(target=work, daemon=True, name="rng-run")
        self._thread.start()

    def stop(self) -> None:
        """PARADA: pide a la máquina que pare, cierra los diálogos pendientes y arma el vigilante.

        Es asíncrona: el resultado llega por ``_on_finished`` cuando el hilo termina.
        """
        if not self.state.running:
            return  # la PARADA llega a todas las páginas; sin ejecución en marcha no hay nada que parar aquí
        if self.machine is not None:
            self.machine.stop(t("Parada solicitada por el usuario", "Stop requested by the user"))
        if self.operator is not None:
            self.operator.cancel_pending()
        self.message.setText(t("Deteniendo… (mando en neutro)", "Stopping… (controller in neutral)"))
        machine = self.machine
        QTimer.singleShot(STOP_WATCHDOG_MS, self, lambda: self._stop_watchdog(machine))

    def _stop_watchdog(self, machine) -> None:
        """La ejecución no ha terminado tras la PARADA: no dejar la interfaz bloqueada en «Deteniendo…».
        Se repite la parada del mando y se libera la página; si el hilo acaba más tarde, se ignora."""
        if not self.state.running or machine is None or self.machine is not machine:
            return
        session = self.state.session
        ctrl = session.controller if session is not None else self.state.pending_controller
        if ctrl is not None:
            try:
                ctrl.emergency_stop()
            except Exception:
                pass
        self.machine = None
        self._set_running(False)
        self.start_btn.setEnabled(False)  # hasta que el hilo termine de verdad (``_on_finished``)
        if self._sink is not None:
            self._sink.close()
        self.message.setText(t("La ejecución no respondió a la PARADA. El mando está en neutro; si algo no va "
                               "bien, cierra y vuelve a abrir la aplicación.",
                               "The run did not respond to STOP. The controller is in neutral; if anything looks "
                               "wrong, close and reopen the application."))

    def _set_running(self, on: bool) -> None:
        """Cambia entre Empezar y Detener y avisa a la ventana."""
        self.state.running = on
        self.start_btn.setEnabled(not on)
        self.start_btn.setVisible(not on)
        self.stop_btn.setEnabled(on)
        self.stop_btn.setVisible(on)
        self.running_changed.emit(on)

    # --- actualizaciones ---
    def _set_progress(self, value: float | None, text: str = "") -> None:
        """``value`` en 0..1; ``None`` = indeterminado (barra animada)."""
        if value is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(max(0.0, min(1.0, value)) * 1000))
        self.progress_text.setText(text)
        self.progress_text.setVisible(bool(text))

    def _on_status(self, st: RunStatus) -> None:
        """Nuevo estado de la ejecución: mensaje, fase del stepper, tramo de la espera e indicadores."""
        prev = self.status
        self.status = st
        self.message.setText(st.message)
        order = list(PHASES)
        phase = PHASE_OF.get(st.phase, st.phase)
        # DONE y SHINY marcan todas las fases como hechas; una fase desconocida no marca ninguna.
        current = order.index(phase) if phase in order else (len(order) if phase in ("DONE", "SHINY") else -1)
        self.stepper.set_current(current, theme.GOLD if st.phase == "SHINY" else theme.ACCENT)
        if st.next_action_t is None:
            self._wait_span = None
        elif st.next_action_t != (prev.next_action_t if prev else None):
            # Un ajuste pequeño del instante objetivo (> 5 s es otra espera) no reinicia la barra de progreso.
            now = self.state.session.clock.now() if self.state.session is not None else st.next_action_t
            if self._wait_span is None or abs(self._wait_span[1] - st.next_action_t) > 5:
                self._wait_span = (now, st.next_action_t)  # espera nueva: la barra empieza de cero
            else:
                self._wait_span = (self._wait_span[0], st.next_action_t)  # re-anclaje: la misma espera
        tiles = self.tiles
        if st.target_advance is not None:
            tiles["target"].set(fmt_int(st.target_advance + st.advance_offset))
            self.target_desc.setText(st.target_desc)
            self.target_desc.setVisible(bool(st.target_desc))
        else:
            tiles["target"].set("—")
            self.target_desc.setVisible(False)
        tiles["attempt"].set(str(st.attempt))
        self.facts["calibration"].setText(st.delay_desc or "—")
        self.facts["seed"].setText(st.seed_desc or "—")
        self.state_hex.setText(t("Estado RNG: {state}", "RNG state: {state}", state=st.state_hex)
                               if st.state_hex else "")
        self._render_hero()

    def _render_hero(self) -> None:
        """Cifra principal, su explicación y la barra según la fase."""
        st, session = self.status, self.state.session
        if st is None:
            return
        now = session.clock.now() if session is not None else 0.0
        color = theme.TEXT
        if st.phase == "COLLECT":
            approx = "≈" if st.calibrating else ""
            self.hero.setText(f"{approx}{st.blinks} / {st.blinks_needed}")
            caption = (t("parpadeos para confirmar la semilla", "blinks to confirm the seed") if st.provisional
                       else t("parpadeos observados", "blinks observed"))
            self.hero_caption.setText(t("{caption} · {clock} observando", "{caption} · {clock} watching",
                                        caption=caption, clock=fmt_clock(st.phase_elapsed_s)))
            self._set_progress(st.blinks / max(1, st.blinks_needed),
                               t("El detector aún se está ajustando a tu personaje",
                                 "The detector is still adjusting to your character") if st.calibrating else "")
        elif st.phase == "REIDENTIFY":
            self.hero.setText(f"{st.blinks} / {st.blinks_needed}")
            self.hero_caption.setText(t("parpadeos para reidentificar tras la Pokédex",
                                        "blinks to re-identify after the Pokédex"))
            self._set_progress(st.blinks / max(1, st.blinks_needed))
        elif st.phase == "FAST_ADVANCE":
            self.hero.setText("Pokédex")
            self.hero_caption.setText(t("adelantando el RNG para no esperar tanto",
                                        "advancing the RNG to cut the wait"))
            self._set_progress(None)
        elif st.phase in ("PLAN", "WAIT_TRIGGER", "TIMELINE") and st.next_action_t is not None:
            left = st.next_action_t - now
            self.hero.setText(fmt_countdown(left))
            at = dt.datetime.now() + dt.timedelta(seconds=max(0.0, left))
            self.hero_caption.setText(t("hasta la {action} · a las {at:%H:%M:%S}", "until {action} · at {at:%H:%M:%S}",
                                        action=st.next_action, at=at))
            if self._wait_span is not None and self._wait_span[1] > self._wait_span[0]:
                a, b = self._wait_span
                self._set_progress((now - a) / (b - a))
            else:
                self._set_progress(None)
        elif st.phase in ("ENCOUNTER", "CATCH", "VERIFY"):
            self.hero.setText({"ENCOUNTER": t("Combate", "Battle"), "CATCH": t("Captura", "Catch"),
                               "VERIFY": t("Ficha", "Summary")}[st.phase])
            self.hero_caption.setText({
                "ENCOUNTER": t("mirando si es shiny (destellos, animación y sonido)",
                               "checking for shiny (sparkles, animation and sound)"),
                "CATCH": t("no es shiny: Master Ball y a por la ficha",
                           "not shiny: Master Ball, then on to the Summary"),
                "VERIFY": t("leyendo naturaleza y estadísticas para calibrar",
                            "reading nature and stats to calibrate")}[st.phase])
            self._set_progress(None)
        elif st.phase == "SHINY":
            self.hero.setText(t("¡SHINY!", "SHINY!"))
            self.hero_caption.setText(t("Captúralo tú y guarda la partida: el programa no tocará el mando",
                                        "Catch it yourself and save the game: the program will not touch the controller"))
            color = theme.GOLD
            self._set_progress(1.0)
        elif st.phase == "RESET":
            self.hero.setText(t("Reinicio", "Reset"))
            self.hero_caption.setText(t("cerrando y abriendo el juego para otro intento",
                                        "closing and reopening the game for another attempt"))
            self._set_progress(None)
        else:
            self.hero.setText("—")
            self.hero_caption.setText("")
            self._set_progress(0.0)
        self.hero.setStyleSheet(f"color: {color};")  # dorado solo con shiny
        self.progress.setStyleSheet(f"QProgressBar::chunk {{ background: {theme.GOLD}; }}" if st.phase == "SHINY" else "")
        # El advance actual solo se conoce mientras hay reloj de advances (antes de la timeline).
        if st.clock is not None and st.phase in ("PLAN", "WAIT_TRIGGER"):
            adv = st.clock.advance_at(now)
            self.tiles["advance"].set(fmt_int(adv + st.advance_offset))
            left = st.target_advance - adv if st.target_advance is not None else None
            self.tiles["remaining"].set(fmt_int(left) if left is not None else "—", t("avances", "advances"))
        elif st.phase == "TIMELINE":
            self.tiles["advance"].set("timeline")
            self.tiles["remaining"].set("—")
        else:
            self.tiles["advance"].set("—")
            self.tiles["remaining"].set("—")

    def _tick(self) -> None:
        """Refresco a 10 Hz de la cuenta atrás y la barra; el vídeo lo pinta ``VideoView`` por su cuenta."""
        if self.status is not None and self.state.session is not None:
            self._render_hero()

    def _on_event(self, e: TimelineEvent) -> None:
        # Los avisos de progreso de una recuperación («aún sin solución») solo añadirían ruido al registro.
        if e.kind == "recovery" and (e.data or {}).get("progress"):
            return
        self._append_log(e.kind, f"[{self._clock_text(e.t_s)}] {e.message}")

    def _clock_text(self, t: float) -> str:
        """Hora de pared de un instante del reloj de la sesión, p. ej. «20:41:03.2»."""
        session = self.state.session
        offset = time.time() - session.clock.now() if session is not None else 0.0
        return dt.datetime.fromtimestamp(t + offset).strftime("%H:%M:%S.%f")[:-5]

    def _append_log(self, kind: str, text: str) -> None:
        """Añade una línea al registro técnico, coloreada por tipo y con el HTML escapado."""
        color = KIND_COLORS.get(kind, theme.TEXT)
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
        self.log.appendHtml(f'<span style="color:{color}">{safe}</span>')

    def _on_finished(self, payload) -> None:
        """Fin del hilo de ejecución: congela el panel en la fase final e informa del resultado."""
        machine, result = payload
        if machine is not self.machine:
            # Ejecución abandonada por el vigilante de la PARADA que ha acabado más tarde: ya se puede empezar otra.
            self.start_btn.setEnabled(not self.state.running)
            return
        self._set_running(False)
        shiny = self.status is not None and self.status.phase == "SHINY"  # antes de cambiar la fase
        if self.status is not None:  # sin reloj ni próxima acción, la cuenta atrás y el advance se detienen
            from dataclasses import replace as _replace

            final = "SHINY" if shiny else ("DONE" if result.outcome is Outcome.SUCCESS else "STOPPED")
            self.status = _replace(self.status, clock=None, next_action_t=None, phase=final)
            self._render_hero()
        if self._sink is not None:
            self._sink.close()
        if result.outcome is Outcome.SUCCESS:
            done = self.status.message if self.status else t("Completado", "Completed")
            self.message.setText(done)
            if shiny:
                from PySide6.QtWidgets import QApplication

                QApplication.beep()
                QMessageBox.information(self, t("¡SHINY!", "SHINY!"),
                                        t("{done}\n\nEl juego está en el combate, con el menú abierto. El programa "
                                          "no tocará el mando: captúralo tú.",
                                          "{done}\n\nThe game is in battle with the menu open. The program will "
                                          "not touch the controller: catch it yourself.", done=done))
            else:
                QMessageBox.information(self, t("Resultado", "Result"), done)
        elif result.outcome is Outcome.STOPPED:
            self.message.setText(t("Detenido. El mando está en neutro.", "Stopped. The controller is in neutral."))
        elif result.outcome is Outcome.INTERVENTION:
            self.message.setText(t("Necesito tu ayuda: {msg}", "I need your help: {msg}", msg=result.message))
            offer_report(self, t("Necesito tu ayuda", "I need your help"),
                         t("{msg}\n\nNo se ha hecho nada más para no arriesgar la partida. "
                           "Revisa la situación y vuelve a pulsar Empezar cuando quieras.",
                           "{msg}\n\nNothing else was done so as not to risk the save. "
                           "Check the situation and press Start again whenever you are ready.",
                           msg=result.message))
        else:
            self.message.setText(t("Error: {msg}", "Error: {msg}", msg=result.message))
            offer_report(self, t("Error", "Error"),
                         t("{msg}\n\nEl detalle está en «Detalles técnicos».",
                           "{msg}\n\nSee Technical details for more information.", msg=result.message),
                         critical=True)
        self.completion_changed.emit()
