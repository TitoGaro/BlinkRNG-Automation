"""Sesión de hardware y construcción de la ejecución, compartidas por la interfaz gráfica y la CLI.

Una ``HardwareSession`` ofrece siempre lo mismo (reloj, frames, último frame para mostrar, mando),
venga de hardware real o de la Switch simulada. La variante simulada (``demo_session``, ``PacedClock``,
``PacedFrames``) existe para los tests: la interfaz gráfica ya no ofrece modo demo.

``build_static_run`` monta las dependencias de la estrategia (detector del ojo, calibración, avance rápido,
grabación del ojo y del sonido) a partir de la sesión y de lo que se pide en ``RunRequest``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rngauto.control.controller import Controller, RecordingController
from rngauto.core.calibration import AttemptRecord, CalibrationHistory
from rngauto.core.clock import Clock, MonotonicClock, SimulatedClock
from rngauto.core.encounter import EncounterConfiguration
from rngauto.core.pokemon import TargetCriteria
from rngauto.core.timeline import Timeline
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.fast_advance import AdvanceStats, load_methods
from rngauto.games.bdsp.shiny_sound import SoundTemplate
from rngauto.games.bdsp.static_strategy import Mode, Operator, RunStatus, StaticDeps, StaticEncounterRun, StaticRunConfig
from rngauto.i18n import t
from rngauto.vision.blink_signal import EyeMatcher
from rngauto.vision.frames import Frame
from rngauto.vision.screen import ScreenSignatures

from .audio import AudioRing
from .setup_store import load_eye_setup, resolve_eye

log = logging.getLogger(__name__)


# (ancho, alto, fps, fourcc) por orden de preferencia. MJPEG primero: por USB 2.0, 1080p sin comprimir no
# llega a una velocidad de fotogramas útil.
CAPTURE_MODES = [
    (1920, 1080, 60, "MJPG"),
    (1920, 1080, 30, "MJPG"),
    (1280, 720, 60, "MJPG"),
    (1920, 1080, 60, None),
    (1280, 720, 60, None),
]


EYE_VIDEO_SESSIONS = 5  # ejecuciones cuyo vídeo del ojo se conserva: cada una ocupa cientos de MB


def prune_eye_videos(sessions_dir: Path, keep: int = EYE_VIDEO_SESSIONS) -> int:
    """Borra el vídeo del ojo (``ojo_*.avi`` y sus tiempos) de las ejecuciones más antiguas y conserva el de las
    ``keep`` más recientes. Los registros de texto (``timeline.jsonl``, ``attempts.jsonl``) no se tocan: ocupan
    poco y sirven para el historial y para depurar. Devuelve los bytes liberados."""
    if not sessions_dir.is_dir():
        return 0
    folders = sorted((p for p in sessions_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
    freed = 0
    for folder in folders[:-keep] if keep > 0 else folders:
        for video in [*folder.glob("ojo_*.avi"), *folder.glob("ojo_*.frames.jsonl")]:
            try:
                size = video.stat().st_size
                video.unlink()
                freed += size
            except OSError:  # abierto por otro programa: se intentará la próxima vez
                continue
    return freed


class RoiTap:
    """Pasa los frames y, mientras ``recording``, guarda el recorte del ojo (vídeo + tiempos) para
    poder analizar después un fallo de detección sin repetir la prueba (``rngauto recover``).

    Cada tramo de grabación va a un fichero ``ojo_NN.avi`` nuevo. Se graba el recorte ampliado (``margin``)
    y no el frame completo para que los vídeos ocupen poco.
    """

    def __init__(self, frames, box, folder: Path, margin: float = 2.0) -> None:
        self.frames = frames
        self.box = box.expanded(margin)
        self.folder = folder
        self.recording = False
        self._recorder = None
        self._count = 0
        self._disabled = False  # sin espacio o sin permiso: se deja de grabar, la ejecución sigue

    def read(self):
        frame = self.frames.read()
        if frame is None:
            return None
        if self.recording and not self._disabled:
            from rngauto.vision.replay import FrameRecorder

            crop = self.box.crop(frame.image)
            try:
                if self._recorder is None and crop.size:
                    self._count += 1
                    self._recorder = FrameRecorder(self.folder / f"ojo_{self._count:02d}.avi", 60.0,
                                                   (crop.shape[1], crop.shape[0]))
                    self._shape = crop.shape
                # Un recorte de otro tamaño (cambio de resolución) no cabe en el vídeo abierto: se descarta.
                if self._recorder is not None and crop.shape == self._shape:
                    self._recorder.write(Frame(crop, frame.t_s, frame.index))
            except (OSError, RuntimeError):  # disco lleno, sin permiso…: la grabación es solo para diagnóstico
                log.warning("Se deja de grabar el vídeo del ojo", exc_info=True)
                self._disabled = True
                self.close()
        elif self._recorder is not None:
            self._recorder.close()
            self._recorder = None
        return frame

    def latest(self):
        return self.frames.latest()

    def close(self) -> None:
        if self._recorder is not None:
            self._recorder.close()
            self._recorder = None


class PacedClock:
    """Reloj simulado que avanza a ``speed``× tiempo real (solo para ``demo_session``, usada en tests).

    Avanza a pasos cortos para que la cancelación se atienda enseguida.
    """

    def __init__(self, sim_clock: SimulatedClock, speed: float = 10.0) -> None:
        self.sim = sim_clock
        self.speed = speed

    def now(self) -> float:
        return self.sim.now()

    def sleep_until(self, t: float, cancel: threading.Event | None = None) -> bool:
        while self.sim.now() < t:
            if cancel is not None and cancel.is_set():
                return False
            step = min(t - self.sim.now(), 0.05 * self.speed)
            time.sleep(step / self.speed)
            self.sim.advance(step)
        return not (cancel is not None and cancel.is_set())


class PacedFrames:
    """Frames de la Switch simulada entregados a ritmo ``speed``× tiempo real (solo para ``demo_session``).

    ``latest`` lo consulta otro hilo (la vista previa) sin avanzar la simulación.
    """

    def __init__(self, switch, speed: float = 10.0) -> None:
        self.switch = switch
        self.speed = speed
        self._latest: Frame | None = None
        self._lock = threading.Lock()

    def read(self) -> Frame | None:
        time.sleep(1.0 / self.switch.scene.fps / self.speed)
        with self._lock:
            frame = self.switch.read()
        self._latest = frame
        return frame

    def latest(self) -> Frame | None:
        return self._latest


@dataclass
class HardwareSession:
    """Capturadora + mando abiertos, reales o simulados, con la misma interfaz para la estrategia."""

    demo: bool
    clock: Clock
    frames: object  # read() / latest()
    controller: Controller | None = None
    device: object | None = None  # CaptureDevice en real
    sim: object | None = None  # SimulatedSwitch en la sesión simulada
    capture_label: str = ""
    controller_label: str = ""
    extra: dict = field(default_factory=dict)

    # --- construcción ---
    @classmethod
    def open_capture(cls, index: int, width: int | None = None, height: int | None = None,
                     fps: float | None = None) -> "HardwareSession":
        """Abre la capturadora. Sin parámetros prueba modos (MJPEG primero: una USB 2.0 no puede dar 1080p
        sin comprimir a buena velocidad), cerrando cada prueba, y abre al final solo el mejor: 1080p con
        ≥ 25 FPS si existe; si no, el más alto y fluido disponible."""
        from rngauto.vision.capture import CaptureDevice

        modes = [(width, height, fps, None)] if width else CAPTURE_MODES
        best = None  # (puntuación, modo, fps medidos, info)
        # Cada modo se prueba abriendo, midiendo 1 s y cerrando: nunca quedan dos aperturas a la vez.
        errors = []
        for i, mode in enumerate(modes):
            if i:
                time.sleep(0.3)  # Windows tarda en liberar el dispositivo antes de reabrirlo
            dev = CaptureDevice(index, *mode)
            try:
                info = dev.open()
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            try:
                if dev.read(timeout_s=3.0) is None:
                    errors.append(t("{w}x{h}: sin imagen", "{w}x{h}: no image", w=mode[0], h=mode[1]))
                    continue
                t_end = time.perf_counter() + 1.0
                while time.perf_counter() < t_end:
                    dev.read(timeout_s=0.5)
                measured = dev.stats.measured_fps
            finally:
                dev.close()
            # Primero que sea fluido (≥ 25 FPS); entre los fluidos, la mayor altura; luego, más FPS.
            score = (measured >= 25, info.height if measured >= 25 else 0, measured)
            if best is None or score > best[0]:
                best = (score, mode, measured, info)
            if measured >= 25 and info.height >= 1080:
                break  # 1080p fluido: no hace falta probar más
        if best is None:
            raise RuntimeError(t("La capturadora no entrega imagen (¿la usa otro programa? ¿hay señal HDMI?). ",
                                 "The capture card is not delivering video (is another program using it? "
                                 "Is there an HDMI signal?). ")
                               + "; ".join(errors[:2]))
        (ok, _, measured), mode, _, info = best
        if not ok:
            raise RuntimeError(t("La capturadora solo da {fps:.0f} FPS; se necesitan ≥ 25 (prueba otro cable/puerto USB)",
                                 "The capture card only delivers {fps:.0f} FPS; at least 25 are needed "
                                 "(try another cable or USB port)", fps=measured))
        time.sleep(0.3)  # igual que entre pruebas: dar tiempo a Windows a liberar el dispositivo
        dev = CaptureDevice(index, *mode)
        info = dev.open()
        if dev.read(timeout_s=3.0) is None:
            dev.close()
            raise RuntimeError(t("La capturadora dejó de dar imagen al reabrirla; vuelve a intentarlo",
                                 "The capture card stopped delivering video after reopening it; try again"))
        # Algunos controladores devuelven un FOURCC vacío o con bytes no imprimibles: no se muestra.
        fourcc = info.fourcc if info.fourcc and info.fourcc.isprintable() and info.fourcc.strip() else ""
        label = f"{info.width}x{info.height} · {measured:.0f} FPS" + (f" · {fourcc}" if fourcc else "")
        return cls(False, MonotonicClock(), dev, device=dev, capture_label=label)

    @classmethod
    def demo_session(cls, encounter: EncounterConfiguration, speed: float = 10.0, seed: int = 7,
                     true_delay: int | None = None) -> "HardwareSession":
        """Sesión con la Switch simulada a ``speed``× tiempo real. Solo la usan los tests (la interfaz ya no
        ofrece modo demo). Por defecto el delay real difiere en +2 del de la configuración, para que la
        calibración tenga algo que corregir."""
        from .sim_switch import SimTruth, SimulatedSwitch

        finder = StaticTargetFinder(static_template(encounter.template))
        sim_clock = SimulatedClock(10.0)
        truth = SimTruth(final_delay=true_delay if true_delay is not None else encounter.final.final_delay + 2)
        switch = SimulatedSwitch(finder, truth, seed=seed, clock=sim_clock)
        ctrl = switch.controller()
        ctrl.connect()
        return cls(True, PacedClock(sim_clock, speed), PacedFrames(switch, speed), controller=ctrl, sim=switch,
                   capture_label=t("Switch simulada · {w}x{h} · ×{speed:g}", "Simulated Switch · {w}x{h} · ×{speed:g}",
                                   w=switch.scene.width, h=switch.scene.height, speed=speed),
                   controller_label=t("Mando simulado", "Simulated controller"))

    def connect_esp32(self, port: str) -> str:
        """Conecta el ESP32 en ``port`` y lo deja como mando de la sesión. Devuelve el detalle de su estado."""
        from rngauto.control.esp32 import ESP32Controller

        ctrl = ESP32Controller.open_serial(port)
        self.controller = ctrl
        st = ctrl.refresh_status()
        self.controller_label = f"{ctrl.client.info.name} · {port}"
        return st.detail

    def use_recording_controller(self) -> None:
        """Usa un mando que solo registra (ensayo sin pulsar nada)."""
        self.controller = RecordingController(clock=time.perf_counter)
        self.controller.connect()
        self.controller_label = t("Sin mando (ensayo)", "No controller (dry run)")

    def latest(self) -> Frame | None:
        return self.frames.latest()

    def close(self, close_controller: bool = True) -> None:
        """Libera la capturadora y, si ``close_controller``, deja el mando en neutro y lo cierra.

        Con ``close_controller=False`` el mando real sigue conectado para la siguiente sesión (la interfaz
        reabre la capturadora sin perder el emparejamiento).
        """
        if self.controller is not None and close_controller:
            try:
                self.controller.emergency_stop()
                self.controller.close()
            except Exception:
                pass
        if self.device is not None:
            self.device.close()


@dataclass
class RunRequest:
    """Lo que se pide para una ejecución (encuentro, objetivo, modo y opciones), venga de la GUI o de la CLI."""

    encounter: EncounterConfiguration
    criteria: TargetCriteria
    mode: Mode
    lead_s: float
    data_dir: Path
    assets_dir: Path
    tid: int = 0
    sid: int = 0
    fast_advance: bool = True  # usar la Pokédex para adelantar el RNG cuando falte mucho
    item_direction: str = "NONE"  # caña en la rueda de «+» (NONE = único objeto registrado)
    audio: object | None = None  # AudioPassthrough del sonido de la capturadora (detección del sonido shiny)
    auto_battle: bool = True  # consola real: combate, captura y ficha sin ayuda (False: lo confirma el usuario)


def _record_while_collecting(tap: RoiTap, forward: Callable[[RunStatus], None] | None) -> Callable[[RunStatus], None]:
    """Graba el ojo solo mientras se recogen parpadeos y reenvía el estado a la interfaz."""

    def callback(st: RunStatus) -> None:
        tap.recording = st.phase == "COLLECT"
        if forward is not None:
            forward(st)

    return callback


def build_static_run(
    session: HardwareSession,
    req: RunRequest,
    operator: Operator,
    timeline: Timeline,
    on_status: Callable[[RunStatus], None] | None = None,
    session_dir: Path | None = None,
) -> StaticEncounterRun:
    """Monta la ejecución de un encuentro estático sobre ``session``.

    Con consola real (ni simulada ni ensayo) activa además la automatización del combate, la grabación de
    clips y la detección del sonido shiny. Con ``session_dir`` guarda los intentos y el recorte del ojo
    mientras se recogen parpadeos. Lanza ``RuntimeError`` si falta preparar algo.
    """
    enc = req.encounter
    finder = StaticTargetFinder(static_template(enc.template), req.tid, req.sid)
    # La calibración de la sesión simulada va aparte para no contaminar la de la consola real.
    cal_path = req.data_dir / "calibration" / f"{enc.id}{'-demo' if session.demo else ''}.json"
    calibration = CalibrationHistory.load(cal_path, enc.id, enc.final.final_delay)

    def on_attempt(a: AttemptRecord) -> None:
        # Un fallo al guardar (disco lleno, fichero bloqueado) no puede tapar el resultado del intento.
        try:
            if session_dir is not None:
                with (session_dir / "attempts.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(a.to_dict(), ensure_ascii=False) + "\n")
            calibration.save(cal_path)
        except OSError:
            log.warning("No se pudo guardar el intento ni la calibración", exc_info=True)

    if session.demo:
        sim = session.sim
        matcher = EyeMatcher(sim.renderer.template(), sim.scene.eye_box, reference_height=sim.scene.height)
        in_battle, observe, reset_game = sim.in_battle, sim.observe, sim.reset_game
        lead = 0.125  # latencia total del simulador (vídeo 80 ms + mando 45 ms)
    else:
        eye = resolve_eye(enc, req.data_dir, req.assets_dir)
        if eye is None:
            raise RuntimeError(t("Falta preparar el ojo para este encuentro (paso «Escena»)",
                                 'The eye has not been set up for this encounter yet ("Scene" step)'))
        template, box, ref_h = eye
        matcher = EyeMatcher(template, box, reference_height=ref_h)
        in_battle, observe, reset_game = None, (lambda fields: None), None
        lead = req.lead_s
    controller = session.controller
    if req.mode is Mode.DRY_RUN and not session.demo:
        # El ensayo no pulsa nada aunque el ESP32 esté conectado: un mando que solo apunta (no se guarda en la sesión).
        controller = RecordingController(clock=time.perf_counter)
        controller.connect()
    if controller is None:
        raise RuntimeError(t("No hay mando conectado", "No controller connected"))
    frames = session.frames
    status_cb = on_status
    if not session.demo and session_dir is not None:
        prune_eye_videos(session_dir.parent)  # incluye la actual: quedan las EYE_VIDEO_SESSIONS más recientes
        tap = RoiTap(session.frames, matcher.box, session_dir)
        frames = tap
        status_cb = _record_while_collecting(tap, on_status)
    suffix = "-demo" if session.demo else ""  # igual que la calibración: datos simulados aparte
    deps = StaticDeps(
        clock=session.clock, frames=frames, controller=controller, finder=finder, encounter=enc,
        matcher=matcher, calibration=calibration, in_battle=in_battle, observe=observe, reset_game=reset_game,
        operator=operator, timeline=timeline, on_attempt=on_attempt, on_status=status_cb,
        fast_methods=tuple(load_methods().values()),
        fast_stats=AdvanceStats.load(req.data_dir / "fast_advance" / f"stats{suffix}.json"),
        screens=ScreenSignatures(req.data_dir / f"screens{suffix}"),
    )
    if not session.demo:
        # Detector para otra preparación del ojo guardada (p. ej. la que se vigila delante del legendario).
        def eye_matcher_for(setup_id: str) -> EyeMatcher | None:
            setup = load_eye_setup(req.data_dir, setup_id)
            template = setup.load_template() if setup is not None else None
            if template is None:
                return None
            return EyeMatcher(template, setup.norm_box, reference_height=setup.reference_height)

        deps.eye_matcher_for = eye_matcher_for
    if enc.blink_scene.message and req.item_direction not in ("NONE", "UP", "DOWN", "LEFT", "RIGHT"):
        raise RuntimeError(t("Indica en el paso «Escena» en qué dirección de la rueda de «+» tienes la caña",
                              'In the "Scene" step, set which direction of the "+" wheel holds the fishing rod'))
    real = not session.demo and req.mode is not Mode.DRY_RUN
    cfg = StaticRunConfig(criteria=req.criteria, mode=req.mode, lead_s=lead, fast_advance=req.fast_advance,
                          registered_item_direction=req.item_direction or "NONE",
                          # consola real: combate, captura, ficha y reinicio sin ayuda
                          auto_battle=real and req.auto_battle,
                          reset_from_home=real and req.auto_battle and req.mode is Mode.AUTOMATIC)
    run = StaticEncounterRun(deps, cfg)
    if real:
        deps.clips_dir = req.data_dir / "combates"
        deps.shiny_sound_path = req.data_dir / "sonidos" / "shiny_bdsp.npz"
        deps.shiny_sound = SoundTemplate.load(deps.shiny_sound_path) if deps.shiny_sound_path.exists() else None
        audio = req.audio
        if audio is not None and getattr(audio, "running", False):
            ring = AudioRing(int(audio.source.samplerate))
            audio.taps.append(ring)
            deps.audio = ring
            # Al terminar, se desengancha el anillo para que no siga acumulando sonido.
            run.closers.append(lambda: audio.taps.remove(ring) if ring in audio.taps else None)
    return run
