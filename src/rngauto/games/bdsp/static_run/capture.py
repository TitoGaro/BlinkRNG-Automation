"""Combate y captura: ENCOUNTER (¿es shiny?), CATCH (Master Ball, textos y ficha) y VERIFY (identificar el
advance obtenido y calibrar). Si es shiny, se para sin tocar el mando."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from rngauto.control.controller import Button, Command, ControllerState, DPad, NEUTRAL
from rngauto.core.calibration import AttemptRecord
from rngauto.core.pokemon import Shiny, ShinyFilter
from rngauto.core.statemachine import InterventionRequired, StateContext
from rngauto.core.target import Target
from rngauto.i18n import t

from ..battle import BattleError, finish_capture, open_menu_from_anywhere, open_stats_page, throw_master_ball
from ..battle_clip import EncounterClip, learn_from_shiny, new_clip_dir, prune_normal_clips
from ..blink_pipeline import BlinkRecovery
from ..encounters import encounter_species, target_label
from ..identify import Observation, candidate_advances
from ..planner import Plan
from ..shiny import BEFORE, ShinyEncounterWatcher
from ..shiny_sound import detect as detect_sound
from ..summary import read_summary_any
from .config import Mode

if TYPE_CHECKING:
    from ..static_strategy import StaticEncounterRun


ENGINE_VERSION = "1.0.0"  # se guarda en cada intento del historial de calibración
WIDE_IDENTIFY_RADIUS = 500  # búsqueda ampliada si no hay nada cerca del objetivo
ENGINE = "bdsp-xorshift128"


class _BattleIO:
    """Adaptador de la estrategia para ``battle.py``: pulsaciones cortas y frames posteriores a ellas."""

    DPAD_MS = 100
    DPAD_GAP_MS = 220

    def __init__(self, run: StaticEncounterRun, ctx: StateContext) -> None:
        self.run, self.ctx = run, ctx
        self._last = ctx.now()

    def now(self) -> float:
        return self.ctx.now()

    def press(self, button: Button, label: str) -> None:
        self.run._press(self.ctx, button, label)
        self._last = self.ctx.now() + 0.16  # fin de la pulsación: 80 ms pulsado + 80 ms en neutro

    def dpad(self, direction: str, label: str) -> None:
        self.run.d.controller.submit([Command(ControllerState(dpad=DPad[direction]), self.DPAD_MS, label),
                                      Command(NEUTRAL, self.DPAD_GAP_MS, label)])
        self.ctx.log("input", f"{direction}: {label}")
        self._last = self.ctx.now() + (self.DPAD_MS + self.DPAD_GAP_MS) / 1000
        self.fresh(0.0)

    def fresh(self, delay_s: float = 0.25) -> np.ndarray:
        """Primera imagen posterior a la última pulsación más ``delay_s``: los frames que aún quedan en la cola
        no reflejan la pulsación y harían decidir sobre una pantalla vieja."""
        target = self._last + delay_s
        while True:
            frame = self.run._next_frame(self.ctx)
            if frame.t_s >= target:
                return frame.image

    def frame(self) -> np.ndarray:
        return self.run._next_frame(self.ctx).image

    def log(self, kind: str, message: str) -> None:
        self.ctx.log(kind, message)


class CaptureMixin:
    """Combate, captura, lectura de la ficha y verificación del resultado."""

    def encounter(self, ctx: StateContext) -> str | None:
        """ENCOUNTER: comprueba que empieza el combate tras la A final.

        Según la configuración: captura a cargo del usuario (``_user_capture``), combate automático con detección
        de shiny (``_auto_encounter``), detector ``in_battle`` o confirmación del operador.
        """
        if self.d.encounter.capture == "user" and self.cfg.mode is not Mode.DRY_RUN:
            return self._user_capture(ctx)
        if self.d.in_battle is None and self.cfg.auto_battle and self.cfg.mode is not Mode.DRY_RUN:
            return self._auto_encounter(ctx)
        self._emit(phase="ENCOUNTER", message=t("Esperando el combate", "Waiting for the battle"), next_action="",
                   next_action_t=None)
        if self.d.in_battle is None:
            ok = self.d.operator.confirm(t("¿Ha empezado el combate con el Pokémon objetivo?",
                                           "Has the battle with the target Pokémon started?"))
            ctx.check()
            if not ok:
                raise InterventionRequired(t("El combate no empezó: pantalla inesperada",
                                             "The battle did not start: unexpected screen"))
            ctx.log("detection", t("Combate confirmado por el usuario", "Battle confirmed by the user"))
            return "VERIFY"
        deadline = ctx.now() + self.cfg.battle_timeout_s
        sent = ctx.data.get("final_sent_t", float("-inf"))
        while ctx.now() < deadline:
            frame = self._next_frame(ctx)
            if frame.t_s < sent:
                continue  # frames anteriores a la pulsación que aún estaban en el búfer
            if self.d.in_battle(frame):
                ctx.log("detection", t("Combate detectado", "Battle detected"))
                return "VERIFY"
        raise InterventionRequired(t("No se detectó el inicio del combate: pantalla inesperada",
                                     "The start of the battle was not detected: unexpected screen"))

    def verify(self, ctx: StateContext) -> str | None:
        """VERIFY: identifica el advance obtenido a partir de lo observado y registra el intento para calibrar.

        La observación sale de la ficha leída en CATCH o, si no, del lector externo o del usuario. Termina
        (``None``) si se consiguió el objetivo; para si puede haber un shiny; si no, RESET.
        """
        rec: BlinkRecovery = ctx.data["recovery"]
        target: Target = ctx.data["target"]
        plan: Plan = ctx.data["plan"]
        fields = list(self.cfg.observe_fields)
        self._emit(phase="VERIFY", message=t("Leyendo la ficha del Pokémon", "Reading the Pokémon's Summary"))
        obs = None
        summary = ctx.data.pop("summary_image", None)
        notes = ctx.data.pop("notes_image", None)
        if summary is not None:
            options = [(sp.name, sp.base_stats, sp.level) for sp in encounter_species(self.d.encounter)]
            try:
                obs, why, _species = read_summary_any(summary, options, notes)
            except Exception as exc:  # OCR no disponible, etc.
                obs, why = None, str(exc)
            ctx.log("detection", t("Ficha: {why}", "Summary: {why}", why=why))
            if obs is None:
                ctx.log("warning", t("No se pudo leer la ficha: se pide al usuario",
                                     "Could not read the Summary: asking the user"))
        if obs is None:
            obs = self.d.observe(fields)
        if obs is None:  # sin lector automático: lo observa el usuario (AutoOperator devuelve None)
            obs = self.d.operator.ask_observation(fields)
            ctx.check()
        if obs is None:
            ctx.log("warning", t("Intento sin verificar: no se puede calibrar con él",
                                 "Unverified attempt: it cannot be used for calibration"))
            return "RESET"
        cands = candidate_advances(self.d.finder, rec.result.state, target.advance, obs, self.cfg.identify_radius)
        if not cands:
            # Encuentro nuevo con el delay inicial muy lejos del real: IVs + naturaleza son casi únicos, así que
            # una sola zona de coincidencias en un radio grande también vale para calibrar.
            wide = candidate_advances(self.d.finder, rec.result.state, target.advance, obs, WIDE_IDENTIFY_RADIUS)
            ctx.log("calibration", t("Nada en ±{radius}; búsqueda ampliada a ±{wide_radius}: {found}",
                                     "Nothing within ±{radius}; search widened to ±{wide_radius}: {found}",
                                     radius=self.cfg.identify_radius, wide_radius=WIDE_IDENTIFY_RADIUS,
                                     found=wide[:5] if wide else t("nada", "nothing")))
            if wide and max(wide) - min(wide) <= 4:  # una sola zona (vecinos indistinguibles): la calibración decide
                cands = wide
        attempt = AttemptRecord(
            attempt_id=f"{next(self._ids)}", encounter=self.d.encounter.id,
            config_version=self.d.encounter.version, engine=ENGINE, engine_version=ENGINE_VERSION,
            started_at=dt.datetime.now().isoformat(timespec="seconds"),
            target_advance=target.advance, final_delay_used=ctx.data["delay"], lead_s_used=self.cfg.lead_s,
            press_margin_s=plan.final.margin_s, plan={"presses": [p.__repr__() for p in plan.presses]},
            observation={k: str(getattr(obs, k)) for k in obs.fields()}, candidates=cands,
            notes=f"estado {rec.result.state}",  # para rehacer la búsqueda a mano si hiciera falta
        )
        if ctx.data.get("early_start"):
            status = t("no se calibra (el combate empezó antes de la A final)",
                       "not calibrated (the battle started before the final A)")
            self._early_starts += 1
            if self._early_starts >= 2:
                raise InterventionRequired(t(
                    "En 2 intentos seguidos el combate empezó antes de la A final: la ruta de este encuentro deja en "
                    "pantalla el último texto (sobra una A). Revisa la aproximación de su configuración",
                    "In 2 attempts in a row the battle started before the final A: this encounter's route leaves the "
                    "last text on screen (one A too many). Check the approach in its configuration"),
                    recoverable=False)
        else:
            self._early_starts = 0
            status = self.d.calibration.record(attempt)
        self.attempts.append(attempt)
        if self.d.on_attempt:
            self.d.on_attempt(attempt)
        hit = self._is_success(obs, cands, rec)
        if hit is not True and any(self.d.finder.generate(rec.result.state, c).shiny is not Shiny.NONE for c in cands):
            # La ficha no dice si es shiny, pero el advance sí: si lo obtenido puede ser un shiny (el detector del
            # combate falló o el usuario contestó «no» por error), nunca se reinicia el juego: se para para guardar.
            raise InterventionRequired(t("Por el advance obtenido puede ser el SHINY: guarda la partida (no se reinicia)",
                                         "Judging by the advance obtained it may be the SHINY: save the game "
                                         "(no restart)"), recoverable=False)
        ctx.log("calibration", t(
            "Esperado {expected}, candidatos {cands} → {status}; delta {delta}; {estimate}",
            "Expected {expected}, candidates {cands} → {status}; delta {delta}; {estimate}",
            expected=target.advance, cands=cands[:5], status=status, delta=attempt.delta,
            estimate=self.d.calibration.estimate_final_delay().describe(),
        ), advance=attempt.resolved)
        self._emit(delay_desc=self.d.calibration.estimate_final_delay().describe())
        if hit is None:
            raise InterventionRequired(t("Ha salido shiny, pero no puedo confirmar el resto de criterios con lo "
                                         "observado: revísalo tú (no se reinicia el juego)",
                                         "It is shiny, but the other criteria cannot be confirmed from what was "
                                         "observed: check it yourself (the game is not restarted)"))
        if hit:
            ctx.log("state", t("¡Objetivo conseguido! {traits}", "Target obtained: {traits}",
                               traits=target.traits.describe()))
            self._emit(phase="DONE",
                       message=t("¡Objetivo conseguido! {label}", "Target obtained: {label}",
                                 label=target_label(self.d.encounter, target.traits)))
            return None
        return "RESET"

    def _auto_encounter(self, ctx: StateContext) -> str | None:
        """Mira la entrada en combate (destellos, duración de la animación y sonido) hasta el menú de combate.
        Shiny → PARA sin tocar nada más (lo captura el usuario). Si no → CATCH."""
        self._emit(phase="ENCOUNTER", message=t("Entrando en combate: mirando si es shiny…",
                                                "Entering battle: checking whether it is shiny…"), next_action="",
                   next_action_t=None)
        sent = ctx.data.get("final_sent_t", ctx.now())
        watcher = ShinyEncounterWatcher(sparkle_every_s=0.03)
        clip = EncounterClip()
        start_deadline = ctx.now() + self.cfg.battle_start_timeout_s
        end_deadline: float | None = None
        while True:
            frame = self._next_frame(ctx)
            if frame.t_s < sent:
                continue
            image = frame.image
            if image.shape[0] > 720:  # los umbrales del detector están pensados para 720p; así además va más rápido
                image = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
            clip.add(frame.t_s, image)
            if watcher.feed(image, frame.t_s):
                break
            if watcher.state == BEFORE and ctx.now() > start_deadline:
                ctx.log("error", t("El combate no ha empezado tras la A final: se reinicia el juego",
                                   "The battle did not start after the final A: restarting the game"))
                self._save_clip(ctx, clip, watcher, sent, video=True)
                return "RESET"
            if watcher.state != BEFORE and end_deadline is None:
                end_deadline = ctx.now() + 60.0
                self._emit(message=t("Combate en marcha: mirando si es shiny…",
                                     "Battle in progress: checking whether it is shiny…"))
            if end_deadline is not None and ctx.now() > end_deadline:
                self._save_clip(ctx, clip, watcher, sent, video=True)
                raise InterventionRequired(t("El combate empezó pero no llega el menú de combate: revisa la consola "
                                             "(no se ha tocado nada)",
                                             "The battle started but the battle menu does not appear: check the "
                                             "console (nothing has been touched)"))
        audio = rate = audio_t0 = None
        if self.d.audio is not None:
            try:
                audio, audio_t0 = self.d.audio.since(sent - 1.0)
                rate = self.d.audio.samplerate
            except Exception as exc:  # sin sonido se decide solo con la imagen
                ctx.log("warning", t("No se pudo leer el sonido del combate: {exc}",
                                     "Could not read the battle audio: {exc}", exc=exc))
                audio = None
        if audio is not None and self.d.shiny_sound is not None:
            match = detect_sound(audio, rate, self.d.shiny_sound)
            watcher.add_sound(match.detected, match.best_error)
        verdict = watcher.verdict()
        ctx.log("detection", t("Entrada en combate: {result} · {desc}", "Battle entry: {result} · {desc}",
                               result=t("¡SHINY!", "SHINY!") if verdict.is_shiny else t("no es shiny", "not shiny"),
                               desc=verdict.describe()))
        suspicious = verdict.is_shiny or verdict.sparkle_alpha >= 2 or verdict.dialog_alpha > 0 or (
            verdict.sound_error is not None and verdict.sound_error < 0.85)
        folder = self._save_clip(ctx, clip, watcher, sent, audio=audio, rate=rate, audio_t0=audio_t0,
                                 video=suspicious)
        if not verdict.is_shiny:
            return "CATCH"
        if (self.d.shiny_sound is None and audio is not None and rate and audio_t0 is not None
                and verdict.sparkle_t is not None and self.d.shiny_sound_path is not None and self.d.clips_dir):
            try:
                tpl = learn_from_shiny(audio, rate, verdict.sparkle_t - audio_t0, self.d.clips_dir,
                                       self.d.shiny_sound_path)
                self.d.shiny_sound = tpl
                ctx.log("calibration", t("Sonido shiny aprendido: {note}", "Shiny sound learned: {note}",
                                         note=tpl.note))
            except Exception as exc:
                ctx.log("warning", t("No se pudo aprender el sonido shiny: {exc}",
                                     "Could not learn the shiny sound: {exc}", exc=exc))
        # Nombre del encuentro, no de la plantilla: en el Parque Hansa sale cualquiera de los tres Regis.
        self._record_shiny(ctx)
        name = self.d.encounter.display_name or self.d.finder.template.name
        where = t(" Grabación: {folder}", " Recording: {folder}", folder=folder) if folder is not None else ""
        ctx.log("state", t("¡SHINY! {name}: el programa se detiene; captúralo tú y guarda la partida.{where}",
                           "SHINY! {name}: the program stops; catch it yourself and save the game.{where}",
                           name=name, where=where))
        self._emit(phase="SHINY", message=t("¡SHINY! {name}", "SHINY! {name}", name=name))
        return None

    def _record_shiny(self, ctx: StateContext) -> None:
        """Un shiny también calibra: si solo el objetivo (y no sus vecinos) es shiny, se acertó el advance."""
        if ctx.data.get("early_start"):
            return  # el combate empezó antes de la A final: no dice nada del delay
        try:
            rec: BlinkRecovery = ctx.data["recovery"]
            target: Target = ctx.data["target"]
            plan: Plan = ctx.data["plan"]
            cands = candidate_advances(self.d.finder, rec.result.state, target.advance, Observation(is_shiny=True),
                                       self.cfg.identify_radius)
            attempt = AttemptRecord(
                attempt_id=f"{next(self._ids)}", encounter=self.d.encounter.id,
                config_version=self.d.encounter.version, engine=ENGINE, engine_version=ENGINE_VERSION,
                started_at=dt.datetime.now().isoformat(timespec="seconds"), target_advance=target.advance,
                final_delay_used=ctx.data["delay"], lead_s_used=self.cfg.lead_s, press_margin_s=plan.final.margin_s,
                plan={"presses": [p.__repr__() for p in plan.presses]}, observation={"is_shiny": "True"},
                candidates=cands, notes="shiny visto en el combate")
            status = self.d.calibration.record(attempt)
            self.attempts.append(attempt)
            if self.d.on_attempt:
                self.d.on_attempt(attempt)
            ctx.log("calibration", t("Shiny en {cands} (objetivo {target}) → {status}; {estimate}",
                                     "Shiny at {cands} (target {target}) → {status}; {estimate}",
                                     cands=cands[:5], target=target.advance, status=status,
                                     estimate=self.d.calibration.estimate_final_delay().describe()))
        except Exception as exc:  # nunca debe impedir avisar del shiny
            ctx.log("warning", t("No se pudo registrar el shiny en la calibración: {exc}",
                                 "Could not record the shiny in the calibration: {exc}", exc=exc))

    def _save_clip(self, ctx: StateContext, clip: EncounterClip, watcher: ShinyEncounterWatcher, t0: float, *,
                   audio=None, rate=None, audio_t0=None, video: bool = False) -> Path | None:
        """Guarda la grabación de la entrada en combate (sonido y, con ``video``, imágenes) para ajustar los
        detectores y poda las de combates normales. Devuelve la carpeta o ``None``; un fallo nunca para el intento."""
        if self.d.clips_dir is None:
            return None
        try:
            verdict = watcher.verdict()
            folder = clip.save(new_clip_dir(self.d.clips_dir, verdict.is_shiny), verdict=verdict, events=watcher.log,
                               t0=t0, audio=audio, rate=rate, audio_t0=audio_t0, video=video)
            prune_normal_clips(self.d.clips_dir, self.cfg.clips_keep)
            return folder
        except Exception as exc:  # disco lleno, etc.: no debe parar el intento
            ctx.log("warning", t("No se pudo guardar la grabación del combate: {exc}",
                                 "Could not save the battle recording: {exc}", exc=exc))
            return None

    def _user_capture(self, ctx: StateContext) -> str:
        """Encuentros que no entran en combate al generarse (Cresselia: se genera al hablarle, huye y queda
        errante). Lo captura el usuario; al pulsar «Listo» se lee su ficha (6.º hueco del equipo) para calibrar."""
        self._battle_possible = True  # el usuario va a tener el Pokémon en la mano: nunca reiniciar sin su aviso
        name = self.d.encounter.display_name or self.d.finder.template.name
        self._emit(phase="ENCOUNTER", message=t("Tu turno: captura a {name} y pulsa «Listo»",
                                                'Your turn: catch {name} and press "Ready"', name=name),
                   next_action="", next_action_t=None)
        ctx.log("state", t("{name} generado: espera a que el usuario lo capture",
                           "{name} generated: waiting for the user to catch it", name=name))
        ready = self.d.operator.wait_ready(t(
            "{name} ya está generado.\n\nVe a capturarlo tú (sin guardar la partida). Con el equipo de 5, entrará en el "
            "6.º hueco. Cuando lo tengas, suelta el mando y pulsa «Listo». Si es shiny, guarda primero la partida.",
            "{name} has been generated.\n\nGo and catch it yourself (without saving the game). With a party of 5, it "
            'will go into the 6th slot. Once you have it, let go of the controller and press "Ready". If it is shiny, '
            "save the game first.", name=name))
        ctx.check()
        if not ready:  # cerrado sin pulsar «Listo»: no se toca el mando (puede haber un shiny en la mano)
            raise InterventionRequired(t("Sin «Listo»: no se toca el mando", "No “Ready”: the controller "
                                         "is left alone"), recoverable=False)
        # Antes de tocar nada: si es el shiny, no se abre la ficha (si no se reconociera, se reiniciaría el juego y se
        # perdería). Sin respuesta, tampoco: nunca se arriesga un shiny.
        shiny = self.d.operator.confirm(t("¿{name} ha salido SHINY?\n\nSí: el programa se para sin tocar el mando (guarda la "
                                          "partida si no lo has hecho). No: va a su ficha para calibrar y sigue.",
                                          "Is {name} SHINY?\n\nYes: the program stops without touching the controller "
                                          "(save the game if you have not already). No: it goes to its Summary to "
                                          "calibrate and continues.", name=name))
        ctx.check()
        if shiny is not False:
            if shiny:
                self._record_shiny(ctx)
                ctx.log("state", t("¡SHINY! {name}: el programa se detiene sin tocar el mando; guarda la partida.",
                                   "SHINY! {name}: the program stops without touching the controller; save the game.",
                                   name=name))
                self._emit(phase="SHINY", message=t("¡SHINY! {name}", "SHINY! {name}", name=name))
                return None
            raise InterventionRequired(t("Sin respuesta a «¿ha salido shiny?»: no se toca el mando",
                                         'No answer to "is it shiny?": the controller is not touched'),
                                       recoverable=False)
        self._emit(phase="CATCH", message=t("Abriendo la ficha para leer naturaleza y estadísticas",
                                            "Opening the Summary to read nature and stats"))
        io = _BattleIO(self, ctx)
        try:
            open_menu_from_anywhere(io)  # el usuario puede haberlo dejado en cualquier pantalla
            ctx.data["summary_image"], ctx.data["notes_image"] = open_stats_page(io, self.cfg.party_slot)
        except BattleError as exc:
            raise InterventionRequired(t("No consigo abrir su ficha ({exc}): vuelve al mapa y reanuda",
                                         "Cannot open its Summary ({exc}): go back to the map and resume",
                                         exc=exc)) from exc
        return "VERIFY"

    def catch(self, ctx: StateContext) -> str:
        """No es shiny: Master Ball, textos hasta el mapa y ficha en la página de estadísticas (rápido y
        comprobando cada pantalla)."""
        self._emit(phase="CATCH", message=t("No es shiny: capturándolo con la Master Ball para calibrar",
                                            "Not shiny: catching it with the Master Ball to calibrate"))
        io = _BattleIO(self, ctx)
        try:
            throw_master_ball(io)
            self._emit(message=t("Capturado: pasando los textos", "Caught: skipping through the text"))
            finish_capture(io)
            self._emit(message=t("Abriendo la ficha para leer naturaleza y estadísticas",
                                 "Opening the Summary to read nature and stats"))
            ctx.data["summary_image"], ctx.data["notes_image"] = open_stats_page(io, self.cfg.party_slot)
        except BattleError as exc:
            if exc.fatal:
                raise InterventionRequired(str(exc)) from exc
            ctx.log("error", t("{exc}: se reinicia el juego (el intento no sirve para calibrar)",
                               "{exc}: restarting the game (the attempt cannot be used for calibration)", exc=exc))
            return "RESET"
        return "VERIFY"

    def _is_success(self, obs: Observation, cands: list[int], rec: BlinkRecovery) -> bool | None:
        """¿Cumple el Pokémon obtenido los criterios? Se decide por lo OBSERVADO (no por el advance previsto):
        advances vecinos pueden dar los mismos stats. ``None`` = shiny observado pero el resto no se puede
        confirmar (no se reinicia nunca con un shiny en la mano)."""
        crit = self.cfg.criteria
        wants_shiny = crit.shiny not in (ShinyFilter.ANY, ShinyFilter.NOT_SHINY)
        seen_shiny = obs.is_shiny if obs.shiny is None else obs.shiny is not Shiny.NONE
        if wants_shiny and seen_shiny is not True:
            return False
        only_shiny = (crit.natures is None and crit.gender is None and crit.ability_slot is None
                      and all((r.lo, r.hi) == (0, 31) for r in crit.ivs))
        if wants_shiny and only_shiny:
            return True
        if not cands:
            return None if seen_shiny else False
        ok = [crit.matches(self.d.finder.generate(rec.result.state, c)) for c in cands]
        if all(ok):
            return True
        if any(ok) and seen_shiny:
            return None
        return False
