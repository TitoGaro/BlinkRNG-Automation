"""Ejecución desde consola (``rngauto run``): mismo motor que la interfaz gráfica.

Abre la capturadora y el mando, crea la carpeta de la sesión (``timeline.jsonl``, ``session.json``) y
ejecuta la máquina de estados con ``ConsoleOperator``. Ctrl+C = parada de emergencia (cancela la cola del
ESP32 y deja el mando en neutro).
"""
from __future__ import annotations

import datetime as dt
import json
import signal
from dataclasses import dataclass
from pathlib import Path

from rngauto.core.encounter import EncounterConfiguration
from rngauto.core.pokemon import TargetCriteria
from rngauto.core.statemachine import Outcome
from rngauto.core.timeline import JsonlSink, Timeline
from rngauto.games.bdsp.encounters import static_template
from rngauto.games.bdsp.static_strategy import Mode
from rngauto.i18n import t

from .console_operator import ConsoleOperator
from .session import HardwareSession, RunRequest, build_static_run


class SetupError(RuntimeError):
    """Algo de la preparación impide empezar (mensaje accionable para el usuario)."""


@dataclass(frozen=True)
class RunOptions:
    """Parámetros de ``rngauto run`` ya interpretados."""

    encounter: EncounterConfiguration
    criteria: TargetCriteria
    mode: Mode
    capture_index: int
    serial: str | None
    assets_dir: Path
    data_dir: Path
    lead_s: float = 0.10
    width: int | None = None  # None = elegir el modo automáticamente
    height: int | None = None
    fps: float | None = None
    tid: int = 0
    sid: int = 0


def run_encounter(opts: RunOptions) -> Outcome:
    """Prepara el hardware, ejecuta un encuentro y devuelve su resultado.

    Lanza ``SetupError`` si algo impide empezar. La sesión de hardware se cierra siempre al salir, lo que
    deja el mando en neutro y libera el puerto y la capturadora.
    """
    enc = opts.encounter
    try:
        session = HardwareSession.open_capture(opts.capture_index, opts.width, opts.height, opts.fps)
    except RuntimeError as exc:
        raise SetupError(str(exc)) from exc
    try:
        print(t("Capturadora: {label}", "Capture card: {label}", label=session.capture_label))
        if opts.mode is Mode.DRY_RUN:
            session.use_recording_controller()
        else:
            if not opts.serial:
                raise SetupError(t("Indica el puerto del ESP32 con --serial (p. ej. COM3), o usa --mode dry_run",
                                   "Set the ESP32 port with --serial (e.g. COM3), or use --mode dry_run"))
            try:
                detail = session.connect_esp32(opts.serial)
            except Exception as exc:
                raise SetupError(str(exc)) from exc
            if not session.controller.status().console_ready:
                raise SetupError(t("El ESP32 responde pero la Switch no está lista: {detail}. "
                                   "Empareja el mando en Mandos > Cambiar agarre/orden.",
                                   "The ESP32 responds but the Switch is not ready: {detail}. "
                                   "Pair the controller in Controllers > Change Grip/Order.", detail=detail))
            print(t("Mando: {label} ({detail})", "Controller: {label} ({detail})",
                    label=session.controller_label, detail=detail))

        session_dir = opts.data_dir / "sessions" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{enc.id}"
        session_dir.mkdir(parents=True, exist_ok=True)
        timeline = Timeline()
        sink = JsonlSink(session_dir / "timeline.jsonl")
        timeline.subscribe(sink)
        # Eco en consola de cada evento, salvo los de progreso de la recuperación (uno por parpadeo).
        timeline.subscribe(lambda e: print(f"[{e.t_s:10.2f}] {e.kind:11} {e.message}")
                           if e.kind != "recovery" or not (e.data or {}).get("progress") else None)
        operator = ConsoleOperator(static_template(enc.template), auto_confirm=opts.mode is Mode.AUTOMATIC)
        req = RunRequest(enc, opts.criteria, opts.mode, opts.lead_s, opts.data_dir, opts.assets_dir, opts.tid, opts.sid)
        try:
            run = build_static_run(session, req, operator, timeline, None, session_dir)
        except RuntimeError as exc:
            raise SetupError(t("{error}. Prepáralo antes con la aplicación (paso «Escena»)",
                               '{error}. Set it up first in the application ("Scene" step)', error=exc)) from exc
        machine = run.build()
        (session_dir / "session.json").write_text(json.dumps({
            "encounter": enc.label, "mode": opts.mode.value, "criteria": opts.criteria.describe(),
            "lead_s": opts.lead_s, "capture": session.capture_label, "started": dt.datetime.now().isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(t("Encuentro {name} | objetivo: {criteria} | modo {mode}\n"
                "Registro de la sesión: {folder}\nCtrl+C = parada de emergencia\n",
                "Encounter {name} | target: {criteria} | mode {mode}\n"
                "Session log: {folder}\nCtrl+C = emergency stop\n",
                name=enc.display_name, criteria=opts.criteria.describe(), mode=opts.mode.value, folder=session_dir))
        import threading

        # El manejador de señales corre en el hilo principal, que puede estar dentro de ``machine.run()`` con
        # un candado tomado: si llamara a ``stop`` directamente podría bloquearse. La parada va en otro hilo.
        previous = signal.signal(signal.SIGINT, lambda *_: threading.Thread(
            target=machine.stop, args=(t("Parada de emergencia (Ctrl+C)", "Emergency stop (Ctrl+C)"),),
            daemon=True).start())
        try:
            result = machine.run()
        finally:
            signal.signal(signal.SIGINT, previous)
            sink.close()
        print(t("\nResultado: {outcome} — {message}", "\nResult: {outcome} — {message}",
                outcome=result.outcome.value, message=result.message))
        print(run.d.calibration.estimate_final_delay().describe())
        return result.outcome
    finally:
        session.close()
