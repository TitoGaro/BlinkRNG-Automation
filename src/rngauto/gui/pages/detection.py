"""Paso 4 — Detección: comprobar en vivo que se detectan los parpadeos.

No hace falta medir la latencia del montaje: la pulsación cae en el centro de un hueco de ±0,5 s y lo que
quede de desfase lo absorbe la calibración del delay con cada intento.

El análisis corre en un hilo propio (``DetectionMonitor``) y la página solo lee una instantánea ~30 veces por
segundo, así que la interfaz no se frena aunque el análisis vaya justo. Todo se para al salir de la página.
"""
from __future__ import annotations

import threading
from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QLabel

from rngauto.app.setup_store import resolve_eye
from rngauto.i18n import t
from rngauto.vision.blink_signal import BlinkEventStream, EyeMatcher, EyeWatcher

from .. import theme
from ..widgets import EyeZoom, ScorePlot, VideoView, card, hbox, label
from .base import Page


class DetectionMonitor:
    """Lee frames en un hilo y mantiene la señal del ojo y los parpadeos detectados.

    Los datos compartidos con la interfaz (``recent``, ``events``, ``error``, ``started_t``) se protegen con un
    cerrojo; la página los lee con ``snapshot`` y ``last_error``.
    """

    def __init__(self, frames, matcher: EyeMatcher, min_window_s: float = 45.0) -> None:
        self.frames = frames
        self.watcher = EyeWatcher(matcher)
        self.stream = BlinkEventStream(min_duration_s=min_window_s, min_plateau_blinks=4, retention_s=300)
        self.recent: deque = deque(maxlen=900)  # últimas muestras: gráfica y nivel de reposo
        self.events: list = []
        self.started_t: float | None = None
        self.error: str | None = None  # el análisis falló (se muestra en la página en lugar de quedarse mudo)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="detection-monitor")
        self._thread.start()

    def stop(self) -> None:
        """No bloquea más de ~1,5 s: ``read()`` vuelve como mucho en 1 s aunque no haya señal."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)

    def _loop(self) -> None:
        """Bucle del hilo: lee, analiza y alimenta el detector de parpadeos hasta que se pida parar."""
        n = 0
        while not self._stop.is_set():
            try:
                frame = self.frames.read()
            except Exception:  # la capturadora se ha cerrado o desconectado: no matar el hilo en bucle
                self._stop.wait(0.2)
                continue
            if frame is None:
                continue
            try:
                sample = self.watcher.process(frame)
                self.watcher.samples.clear()  # no acumular: el stream ya guarda lo necesario
                with self._lock:
                    if self.started_t is None:
                        self.started_t = sample.t_s
                    self.recent.append(sample)
                    self.stream.feed(sample)
                    n += 1
                    if n % 15 == 0:  # buscar parpadeos nuevos no hace falta en cada frame
                        self.events.extend(self.stream.poll())
                    self.error = None
            except Exception as exc:  # p. ej. un frame de otro tamaño al cambiar de capturadora: seguir con el siguiente
                with self._lock:
                    self.error = f"{type(exc).__name__}: {exc}"
                self._stop.wait(0.2)

    def snapshot(self):
        """Copia coherente de ``(muestras, parpadeos, umbral, segundos observados)`` para pintar en la interfaz."""
        with self._lock:
            samples = list(self.recent)
            events = list(self.events)
            threshold = self.stream.threshold
            elapsed = (samples[-1].t_s - self.started_t) if samples and self.started_t is not None else 0.0
        return samples, events, threshold, elapsed

    def last_error(self) -> str | None:
        with self._lock:
            return self.error


class DetectionPage(Page):
    """Paso «Detección»: lupa del ojo, gráfica de la señal y recuento de parpadeos en directo.

    Se completa con 3 parpadeos detectados o marcando la casilla de continuar igualmente.
    """

    @property
    def step_title(self) -> str:
        return t("Detección", "Detection")

    @property
    def step_hint(self) -> str:
        return t("Comprueba los parpadeos", "Check the blinks")

    def __init__(self, state) -> None:
        super().__init__(state, t("Comprueba que se detectan los parpadeos", "Check that blinks are detected"),
                         t("Quédate quieto en la escena. En la lupa ves el ojo en directo: el marco se pone rojo cuando "
                           "tu personaje parpadea (cada ~8 s de media). Tras ~1 minuto el programa ajusta su sensibilidad.",
                           "Stand still in the scene. The magnifier shows the eye live: the frame turns red when your "
                           "character blinks (every ~8 s on average). After ~1 minute the program tunes its sensitivity."))
        self.video = VideoView(min_height=320)
        self.zoom = EyeZoom(220)
        self.zoom.caption = t("ahora (en directo)", "now (live)")
        self.template_view = EyeZoom(220)
        self.template_view.caption = t("ojo marcado (abierto)", "marked eye (open)")
        self.score = QLabel("")
        self.score.setObjectName("muted")
        zoom_box = hbox(self.zoom, self.template_view, None)
        self.plot = ScorePlot()
        self.counter = label("", "message")
        self.detail = label("", "muted")
        self.body.addWidget(card(zoom_box, self.score, self.plot, self.counter, self.detail,
                                 title=t("Lupa del ojo", "Eye magnifier")))
        self.body.addWidget(card(self.video, title=t("Imagen completa", "Full image")))

        self.override = QCheckBox(t("Lo he comprobado y quiero continuar igualmente", "I have checked it and want to continue anyway"))
        self.override.stateChanged.connect(lambda _s: self.completion_changed.emit())
        self.body.addWidget(self.override)
        self.body.addStretch(1)

        self.monitor: DetectionMonitor | None = None
        self.matcher: EyeMatcher | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._ok = False

    def _make_matcher(self) -> tuple[EyeMatcher, object] | None:
        """Comparador del ojo principal del encuentro y su plantilla; ``None`` si aún no hay ojo."""
        s, enc = self.state.session, self.state.selection.encounter
        if s is None or enc is None:
            return None
        if s.demo:  # sesión simulada (tests): la plantilla sale de la escena sintética
            tpl = s.sim.renderer.template()
            return EyeMatcher(tpl, s.sim.scene.eye_box, reference_height=s.sim.scene.height), tpl
        eye = resolve_eye(enc, self.state.data_dir, self.state.assets_dir)
        if eye is None:
            return None
        template, box, ref_h = eye
        return EyeMatcher(template, box, reference_height=ref_h), template

    def _start_monitor(self) -> None:
        self._stop_monitor()
        made = self._make_matcher()
        if made is None:
            self.counter.setText(t("Falta marcar el ojo en el paso anterior.", "Mark the eye in the previous step first."))
            return
        self.matcher, template = made
        self.template_view.set_crop(template)
        self.video.set_box(self.matcher.box)
        self.monitor = DetectionMonitor(self.state.session.frames, self.matcher)
        self.monitor.start()
        self._timer.start(33)

    def _stop_monitor(self) -> None:
        self._timer.stop()
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None

    def on_enter(self) -> None:
        s = self.state.session
        if s is None:
            return
        self.video.attach(lambda: self.state.session.latest() if self.state.session is not None else None)
        self._start_monitor()

    def on_leave(self) -> None:
        self.video.attach(None)
        self._stop_monitor()

    def _tick(self) -> None:
        """Refresco de la página (~30 Hz): gráfica, lupa, recuadro y textos a partir de la instantánea."""
        s = self.state.session
        if s is None or self.matcher is None:
            return
        if self.monitor is None:
            return
        samples, events, threshold, elapsed = self.monitor.snapshot()
        error = self.monitor.last_error()
        if error is not None:
            self.counter.setText(t("No se puede analizar la imagen del ojo", "Cannot analyse the eye image"))
            self.detail.setText(t("{error}. Si cambiaste de capturadora o de resolución, vuelve a marcar el ojo.",
                                  "{error}. If you changed capture card or resolution, mark the eye again.",
                                  error=error))
            return
        closed = False
        if samples:
            # El umbral del detector es relativo: caída respecto al nivel en reposo (la mediana de la ventana).
            scores = sorted(x.score for x in samples)
            resting = scores[len(scores) // 2]
            th_score = resting * (1 - threshold) if threshold is not None else None
            last = samples[-1]
            closed = th_score is not None and last.score < th_score
            self.plot.set_data([x.t_s for x in samples], [x.score for x in samples], th_score, [e.t_s for e in events])
            quality = (t("bien", "good") if last.score >= 0.6 else
                       (t("regular", "fair") if last.score >= 0.4 else t("no encuentro el ojo", "eye not found")))
            self.score.setText(t("Parecido con el ojo marcado: {score:.2f} ({quality})",
                                 "Match with the marked eye: {score:.2f} ({quality})", score=last.score, quality=quality))
        frame = s.latest()
        if frame is not None:
            self.zoom.set_crop(self.matcher.box.crop(frame.image), theme.DANGER if closed else theme.SUCCESS)
            self.video.set_box(self.matcher.box, theme.DANGER if closed else theme.SUCCESS)
        if threshold is None:
            self.counter.setText(t("Ajustando la sensibilidad… {s:.0f} s", "Tuning sensitivity… {s:.0f} s", s=elapsed))
            self.detail.setText(t("Los parpadeos aparecerán como caídas en la gráfica azul.",
                                  "Blinks will show up as dips in the blue graph."))
        else:
            kinds = {None: t("dudoso", "uncertain"), 0: t("simple", "single"), 1: t("doble", "double")}
            last_e = events[-1] if events else None
            self.counter.setText(t("Parpadeos detectados: {n}", "Blinks detected: {n}", n=len(events)))
            self.detail.setText(
                t("Aún ninguno: espera unos segundos más.", "None yet: wait a few more seconds.") if last_e is None else
                t("Último: {kind}, confianza {conf:.0%}. Confianza media: {mean:.0%}.",
                  "Last: {kind}, confidence {conf:.0%}. Average confidence: {mean:.0%}.",
                  kind=kinds[None if last_e.kind is None else int(last_e.kind)], conf=last_e.confidence,
                  mean=sum(e.confidence for e in events) / len(events)))
        ok = len(events) >= 3
        if ok != self._ok:
            self._ok = ok
            self.completion_changed.emit()

    def is_complete(self) -> bool:
        return self._ok or self.override.isChecked() or (self.state.session is not None and self.state.session.demo)

    def missing(self) -> str:
        return t("Espera a que se detecten al menos 3 parpadeos (o marca la casilla para continuar)",
                 "Wait until at least 3 blinks are detected (or tick the box to continue)")
