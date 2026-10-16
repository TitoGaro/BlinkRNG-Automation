"""Línea de comandos: ``rngauto <comando>`` (o ``python -m rngauto.app.cli``).

Comandos que NO tocan hardware: ``encounters``, ``targets``, ``simulate`` (Switch simulada), ``recover``
(vídeo grabado).
Comandos que abren hardware real (solo cuando el usuario los lanza): ``diagnose``, ``record``, ``run``.

Cada ``cmd_*`` devuelve el código de salida del proceso (0 = éxito). Los imports pesados (visión, juego,
hardware) se hacen dentro de cada comando para que la CLI arranque rápido.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rngauto.app.setup_store import DEFAULT_DATA_DIR as DATA_DIR
from rngauto.core.encounter import load_encounters
from rngauto.core.pokemon import (
    NATURES_EN, NATURES_ES, IVRange, ShinyFilter, TargetCriteria, nature_index,
)
from rngauto.i18n import t
from rngauto.paths import ASSETS_DIR, ENCOUNTERS_DIR
from rngauto.vision.imageio import imread


def _criteria(args: argparse.Namespace) -> TargetCriteria:
    """Criterios de objetivo a partir de ``--shiny``, ``--nature`` e ``--ivs`` (sin ellos, cualquiera vale)."""
    shiny = ShinyFilter[args.shiny.upper()] if args.shiny else ShinyFilter.ANY
    natures = frozenset(nature_index(n) for n in args.nature.split(",")) if args.nature else None
    ivs = tuple(IVRange() for _ in range(6))
    if args.ivs:
        parts = args.ivs.split("/")
        if len(parts) != 6:
            raise SystemExit(t("--ivs necesita 6 valores separados por '/' (usa 'x' para cualquiera), p. ej. 31/x/31/31/31/31",
                               "--ivs needs 6 values separated by '/' (use 'x' for any), e.g. 31/x/31/31/31/31"))
        # «x» = cualquiera, «a-b» = rango, un número = mínimo (hasta 31).
        ivs = tuple(IVRange() if p.lower() == "x" else IVRange(*map(int, p.split("-"))) if "-" in p else IVRange(int(p), 31)
                    for p in parts)
    return TargetCriteria(shiny=shiny, natures=natures, ivs=ivs)


def cmd_encounters(args: argparse.Namespace) -> int:
    """Lista los encuentros automatizables (TOML) y las plantillas de generación por categoría."""
    from rngauto.games.bdsp.encounters import static_templates

    configs = load_encounters(ENCOUNTERS_DIR)
    print(t("Configuraciones de encuentro (automatizables):", "Encounter configurations (automatable):"))
    for cfg in configs.values():
        print(f"  {cfg.id:12} {cfg.display_name:12} v{cfg.version}  [{', '.join(cfg.games)}]")
    print(t("\nPlantillas de generación disponibles (búsqueda de objetivos):",
            "\nAvailable generation templates (target search):"))
    by_cat: dict[str, list[str]] = {}
    for tpl in static_templates().values():
        by_cat.setdefault(tpl.category, []).append(tpl.key)
    for cat, keys in by_cat.items():
        print(f"  {cat:16} {', '.join(keys)}")
    return 0


def cmd_targets(args: argparse.Namespace) -> int:
    """Busca, desde un estado conocido del RNG, los advances que cumplen los criterios."""
    from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
    from rngauto.games.bdsp.xorshift import XorshiftState

    state = XorshiftState.parse(args.state)
    finder = StaticTargetFinder(static_template(args.pokemon), args.tid, args.sid)
    criteria = _criteria(args)
    print(t("Estado {state} | {pokemon} | criterios: {criteria}", "State {state} | {pokemon} | criteria: {criteria}",
            state=state.to_hex32(), pokemon=args.pokemon, criteria=criteria.describe()))
    targets = finder.search(state, criteria, args.min, args.max, limit=args.limit)
    if not targets:
        print(t("Sin resultados en ese rango.", "No results in that range."))
        return 1
    for target in targets:
        p = target.traits
        names = NATURES_ES if args.es else NATURES_EN
        shiny = {0: "-", 1: "★", 2: "■"}[int(p.shiny)]  # no shiny / estrella / cuadrado
        print(f"  {target.advance:>8}  {shiny}  {names[p.nature]:9} {'/'.join(f'{v:2d}' for v in p.ivs)}  "
              + t("hab {slot}", "ab {slot}", slot=p.ability_slot)
              + f"  {p.gender.name:10} EC {p.ec:08X} PID {p.pid:08X}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """Ejecuta la estrategia completa contra la Switch simulada (``sim_switch``), sin hardware.

    El delay real queda oculto a la estrategia: sirve para comprobar que la calibración lo descubre.
    """
    from rngauto.app.sim_switch import SimTruth, SimulatedSwitch
    from rngauto.core.calibration import CalibrationHistory
    from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
    from rngauto.games.bdsp.static_strategy import Mode, StaticDeps, StaticEncounterRun, StaticRunConfig
    from rngauto.vision.blink_signal import EyeMatcher

    enc = load_encounters(ENCOUNTERS_DIR)[args.pokemon]
    finder = StaticTargetFinder(static_template(enc.template))
    truth = SimTruth(final_delay=args.true_delay)
    switch = SimulatedSwitch(finder, truth, seed=args.seed)
    controller = switch.controller()
    controller.connect()
    deps = StaticDeps(
        clock=switch.clock, frames=switch, controller=controller, finder=finder, encounter=enc,
        matcher=EyeMatcher(switch.renderer.template(), switch.scene.eye_box, reference_height=switch.scene.height),
        calibration=CalibrationHistory(enc.id, enc.final.final_delay),
        in_battle=switch.in_battle, observe=switch.observe, reset_game=switch.reset_game,
    )
    # De los eventos de estado solo se muestran las transiciones (los que empiezan por «→»).
    deps.timeline.subscribe(lambda e: print(f"[{e.t_s:9.2f}s] {e.kind:11} {e.message}") if e.kind != "state" or e.message.startswith("→") else None)
    run = StaticEncounterRun(deps, StaticRunConfig(criteria=_criteria(args), mode=Mode(args.mode), lead_s=0.125))
    print(t("SIMULACIÓN ({mode}) — {name}; delay real oculto = {true_delay}, "
            "prior = {prior}; latencias reales vídeo {video_ms:.0f} ms "
            "+ mando {input_ms:.0f} ms\n",
            "SIMULATION ({mode}) — {name}; hidden true delay = {true_delay}, "
            "prior = {prior}; true latencies: video {video_ms:.0f} ms "
            "+ controller {input_ms:.0f} ms\n",
            mode=args.mode, name=enc.display_name, true_delay=args.true_delay, prior=enc.final.final_delay,
            video_ms=truth.video_latency_s * 1000, input_ms=truth.input_latency_s * 1000))
    result = run.build().run()
    print(t("\nResultado: {outcome} — {message} ({n} intentos)", "\nResult: {outcome} — {message} ({n} attempts)",
            outcome=result.outcome.value, message=result.message, n=len(run.attempts)))
    return 0 if result.outcome.value == "success" else 1


def cmd_recover(args: argparse.Namespace) -> int:
    """Recupera el estado del RNG a partir de un vídeo grabado (p. ej. los ``ojo_NN.avi`` de una sesión).

    Sirve para analizar un fallo de detección sin repetir la prueba con la consola.
    """
    from rngauto.games.bdsp.blink_pipeline import recover_from_events
    from rngauto.games.bdsp.recovery import RecoveryError
    from rngauto.vision.blink_signal import BlinkEventStream, EyeMatcher, EyeWatcher, signal_quality
    from rngauto.vision.frames import NormBox
    from rngauto.vision.replay import VideoFileSource

    template = imread(args.template)
    if template is None:
        raise SystemExit(t("No se puede leer la plantilla {path}", "Cannot read the template {path}", path=args.template))
    box = NormBox(*map(float, args.box.split(",")))
    matcher = EyeMatcher(template, box, reference_height=args.ref_height, upscale=args.upscale)
    watcher = EyeWatcher(matcher)
    stream = BlinkEventStream(min_duration_s=min(180.0, args.min_window), min_plateau_blinks=args.min_plateau)
    with VideoFileSource(args.video) as src:
        info = src.info()
        print(t("Vídeo {name}: {w}x{h} a {fps:.1f} FPS", "Video {name}: {w}x{h} at {fps:.1f} FPS",
                name=info.name, w=info.width, h=info.height, fps=info.fps))
        for frame in src:
            stream.feed(watcher.process(frame))
    events = stream.all_events()
    q = signal_quality(watcher.samples, stream.threshold)
    print(t("Muestras {n} | umbral {threshold} | {quality}", "Samples {n} | threshold {threshold} | {quality}",
            n=len(watcher.samples), threshold=stream.threshold, quality=q.describe()))
    print(t("Parpadeos detectados: {n} ({doubtful} de tipo dudoso, confianza media {confidence:.0%})",
            "Blinks detected: {n} ({doubtful} of uncertain type, mean confidence {confidence:.0%})",
            n=len(events), doubtful=sum(1 for e in events if e.kind is None),
            confidence=sum(e.confidence for e in events) / max(1, len(events))))
    try:
        rec = recover_from_events(events, npc=args.npc)
    except RecoveryError as exc:
        print(t("✗ No se pudo recuperar el estado: {error}", "✗ Could not recover the state: {error}", error=exc))
        return 1
    hi, lo = rec.result.state_after_roll(rec.result.last_roll).to_hex64()
    print(f"✓ {rec.describe()}")
    print(t("  Estado tras el último parpadeo (2 × 64 bits): {hi} {lo}",
            "  State after the last blink (2 × 64 bits): {hi} {lo}", hi=hi, lo=lo))
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Comprueba el ESP32 (handshake y estado, sin pulsar botones) y/o la capturadora (FPS reales).

    Devuelve 0 solo si todo lo comprobado está listo.
    """
    ok = True
    if args.serial:
        from rngauto.control.controller import ControllerError
        from rngauto.control.esp32 import ESP32Controller

        print(t("ESP32 ({port}) — solo handshake y estado, sin pulsar botones",
                "ESP32 ({port}) — handshake and status only, no button presses", port=args.serial))
        try:
            ctrl = ESP32Controller.open_serial(args.serial)
        except ControllerError as exc:
            print(f"  ✗ {exc}")
            ok = False
        else:
            try:
                info = ctrl.client.info
                st = ctrl.refresh_status()
                print(t("  ✓ {name} | firmware {firmware} | protocolo RSC {rsc} / MLC {mlc}",
                        "  ✓ {name} | firmware {firmware} | protocol RSC {rsc} / MLC {mlc}",
                        name=info.name, firmware=info.firmware, rsc=info.rsc_version, mlc=info.mlc_version))
                if not info.firmware_mismatch:
                    print(t("  ✓ Firmware fijado", "  ✓ Firmware matches the pinned version"))
                else:
                    print(t("  ~ Firmware distinto del fijado (2026092800)",
                            "  ~ Firmware differs from the pinned version (2026092800)"))
                print(t("  ✓ Cola de comandos: {capacity} | baudios {baud}", "  ✓ Command queue: {capacity} | baud rate {baud}",
                        capacity=info.queue_capacity, baud=ctrl.transport.baudrate))
                print(f"  {'✓' if st.console_ready else '✗'} Switch: {st.detail}")
                if not st.console_ready:
                    print(t("    → Empareja el mando: Switch > Mandos > Cambiar agarre/orden (aparece en ~5 s).",
                            "    → Pair the controller: Switch > Controllers > Change Grip/Order (it appears in ~5 s)."))
                ok = ok and st.console_ready
            finally:
                ctrl.close()  # libera el puerto siempre: si no, otra ventana o la GUI no podrían abrirlo
    if args.capture is not None:
        from rngauto.vision.capture import probe_device

        print(t("CAPTURADORA (índice {index}, backend {backend})", "CAPTURE CARD (index {index}, backend {backend})",
                index=args.capture, backend=args.backend))
        diag = probe_device(args.capture, seconds=args.seconds, backend=args.backend,
                            width=args.width, height=args.height, fps=args.fps, fourcc=args.fourcc)
        for line in diag.lines():
            print("  " + line)
        ok = ok and diag.ok
    return 0 if ok else 1


def cmd_record(args: argparse.Namespace) -> int:
    """Graba ``--seconds`` de la capturadora a un vídeo para usarlo después en replay."""
    import time

    from rngauto.vision.capture import CaptureDevice
    from rngauto.vision.replay import FrameRecorder

    with CaptureDevice(args.capture, args.width, args.height, args.fps, args.fourcc, args.backend) as dev:
        info = dev.info()
        print(t("Grabando {seconds:.0f} s de {w}x{h}@{fps:.0f} en {out}", "Recording {seconds:.0f} s of {w}x{h}@{fps:.0f} to {out}",
                seconds=args.seconds, w=info.width, h=info.height, fps=info.fps, out=args.out))
        with FrameRecorder(args.out, info.fps or args.fps, (info.width, info.height), codec=args.codec) as rec:
            end = time.perf_counter() + args.seconds
            while time.perf_counter() < end:
                frame = dev.read()
                if frame is not None:
                    rec.write(frame)
        print(t("{frames} frames; FPS medidos {fps:.1f}; repetidos {dup}; huecos {gaps}",
                "{frames} frames; measured FPS {fps:.1f}; duplicates {dup}; gaps {gaps}",
                frames=rec.frames, fps=dev.stats.measured_fps, dup=dev.stats.duplicates, gaps=dev.stats.gaps))
    return 0


def _encounter(enc_id: str):
    """Configuración del encuentro ``enc_id``; termina con un mensaje si no existe."""
    configs = load_encounters(ENCOUNTERS_DIR)
    if enc_id not in configs:
        raise SystemExit(t("Encuentro desconocido: {id}. Disponibles: {available}", "Unknown encounter: {id}. Available: {available}",
                           id=enc_id, available=", ".join(configs)))
    return configs[enc_id]


def _capture_mode(args: argparse.Namespace) -> dict:
    """Sin parámetros explícitos, ``run`` elige el modo de captura automáticamente (MJPEG, ≥ 25 FPS).

    Los valores por defecto de ``add_capture`` (1920x1080 a 60) se interpretan como «no indicado».
    """
    if (args.width, args.height, args.fps) == (1920, 1080, 60.0):
        return {"width": None, "height": None, "fps": None}
    return {"width": args.width, "height": args.height, "fps": args.fps}


def cmd_run(args: argparse.Namespace) -> int:
    """Ejecuta un encuentro con hardware real. Código 2 si la preparación falla; 1 si no se consigue."""
    from rngauto.app.run import RunOptions, SetupError, run_encounter
    from rngauto.games.bdsp.static_strategy import Mode

    opts = RunOptions(
        encounter=_encounter(args.encounter), criteria=_criteria(args), mode=Mode(args.mode),
        capture_index=args.capture, serial=args.serial, assets_dir=ASSETS_DIR, data_dir=args.data_dir,
        lead_s=args.lead, **_capture_mode(args), tid=args.tid, sid=args.sid,
    )
    try:
        outcome = run_encounter(opts)
    except SetupError as exc:
        print(f"✗ {exc}")
        return 2
    return 0 if outcome.value == "success" else 1


def build_parser() -> argparse.ArgumentParser:
    """Parser con un subcomando por ``cmd_*``; cada uno deja su función en ``func``."""
    p = argparse.ArgumentParser(prog="rngauto", description=t("Automatización de RNG (BDSP Blink RNG)",
                                                              "RNG automation (BDSP Blink RNG)"))
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("encounters", help=t("Lista encuentros configurados y plantillas",
                                                "List configured encounters and templates")).set_defaults(func=cmd_encounters)

    def add_criteria(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--shiny", choices=["any", "shiny", "star", "square", "not_shiny"])
        sp.add_argument("--nature", help=t("Naturalezas separadas por comas (ES o EN)",
                                           "Comma-separated natures (ES or EN)"))
        sp.add_argument("--ivs", help=t("HP/Atk/Def/SpA/SpD/Spe: número = mínimo, 'a-b' = rango, 'x' = cualquiera",
                                        "HP/Atk/Def/SpA/SpD/Spe: number = minimum, 'a-b' = range, 'x' = any"))

    tg = sub.add_parser("targets", help=t("Busca advances objetivo desde un estado",
                                          "Search target advances from a state"))
    tg.add_argument("--state", required=True, help=t("4 palabras de 32 bits o 2 de 64 (hex)",
                                                     "4 32-bit words or 2 64-bit words (hex)"))
    tg.add_argument("--pokemon", required=True)
    tg.add_argument("--min", type=int, default=0)
    tg.add_argument("--max", type=int, default=100_000)
    tg.add_argument("--limit", type=int, default=20)
    tg.add_argument("--tid", type=int, default=0)
    tg.add_argument("--sid", type=int, default=0)
    tg.add_argument("--es", action="store_true", help=t("Naturalezas en español", "Show natures in Spanish"))
    add_criteria(tg)
    tg.set_defaults(func=cmd_targets)

    s = sub.add_parser("simulate", help=t("Vertical slice completo contra una Switch simulada",
                                                  "Full vertical slice against a simulated Switch"))
    s.add_argument("--pokemon", default="dialga")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--true-delay", type=int, default=86)
    s.add_argument("--mode", choices=["automatic", "dry_run"], default="automatic")
    add_criteria(s)
    s.set_defaults(func=cmd_simulate)

    r = sub.add_parser("recover", help=t("Recupera el estado desde un vídeo grabado (replay)",
                                                 "Recover the state from a recorded video (replay)"))
    r.add_argument("--video", type=Path, required=True)
    r.add_argument("--template", type=Path, required=True, help=t("Imagen del ojo abierto", "Image of the open eye"))
    r.add_argument("--box", required=True, help=t("Caja normalizada x,y,w,h (0..1)", "Normalized box x,y,w,h (0..1)"))
    r.add_argument("--ref-height", type=int, default=1080, help=t("Altura del frame donde se recortó la plantilla",
                                                                           "Height of the frame the template was cropped from"))
    r.add_argument("--upscale", type=float, default=1.0, help=t("Zoom digital (recorte + reescalado)",
                                                                           "Digital zoom (crop + rescale)"))
    r.add_argument("--npc", type=int, default=0)
    r.add_argument("--min-window", type=float, default=180.0)
    r.add_argument("--min-plateau", type=int, default=20)
    r.set_defaults(func=cmd_recover)

    def add_capture(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--capture", type=int, default=1)
        sp.add_argument("--backend", choices=["msmf", "dshow", "any"], default="msmf")
        sp.add_argument("--width", type=int, default=1920)
        sp.add_argument("--height", type=int, default=1080)
        sp.add_argument("--fps", type=float, default=60.0)
        sp.add_argument("--fourcc", default=None)

    d = sub.add_parser("diagnose", help=t("Diagnóstico de capturadora y/o ESP32 (abre el hardware real)",
                                                  "Diagnose the capture card and/or ESP32 (opens the real hardware)"))
    add_capture(d)
    d.set_defaults(capture=None)  # sin --capture, ``diagnose`` no abre la capturadora
    d.add_argument("--serial", help=t("Puerto del ESP32, p. ej. COM3 (handshake sin pulsar botones)",
                                           "ESP32 port, e.g. COM3 (handshake without pressing buttons)"))
    d.add_argument("--seconds", type=float, default=3.0)
    d.set_defaults(func=cmd_diagnose)

    run = sub.add_parser("run", help=t("Ejecuta un encuentro con hardware real (o dry_run sin pulsar)",
                                                "Run an encounter on real hardware (or dry_run without pressing)"))
    run.add_argument("--encounter", required=True)
    run.add_argument("--serial", help=t("Puerto del ESP32 (p. ej. COM3)", "ESP32 port (e.g. COM3)"))
    run.add_argument("--mode", choices=["assisted", "automatic", "dry_run"], default="assisted")
    run.add_argument("--lead", type=float, default=0.10, help=t("Adelanto de envío en s (la calibración absorbe el resto)",
                                                                        "Send lead time in s (calibration absorbs the rest)"))
    run.add_argument("--data-dir", type=Path, default=DATA_DIR)
    run.add_argument("--tid", type=int, default=0)
    run.add_argument("--sid", type=int, default=0)
    add_capture(run)
    add_criteria(run)
    run.set_defaults(func=cmd_run)

    rec = sub.add_parser("record", help=t("Graba una sesión para replay (abre el dispositivo real)",
                                                "Record a session for replay (opens the real device)"))
    add_capture(rec)
    rec.add_argument("--seconds", type=float, default=600.0)
    rec.add_argument("--out", type=Path, required=True)
    rec.add_argument("--codec", default="MJPG")
    rec.set_defaults(func=cmd_record)
    return p


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada de ``rngauto``."""
    from rngauto.app.setup_store import apply_saved_language

    # La consola de Windows no siempre usa UTF-8 y los mensajes llevan tildes y símbolos (✓, ★).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    apply_saved_language()  # antes de construir el parser: la ayuda sale ya en el idioma del usuario
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
