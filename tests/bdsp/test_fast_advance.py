"""Avance rápido (Pokédex) + reidentificación: decisión, aprendizaje, reidentificación y flujo completo."""
from dataclasses import dataclass
from pathlib import Path

import pytest

import cv2
import numpy as np

from rngauto.app.sim_switch import MENU_OPEN, POKEDEX, SimTruth, SimulatedSwitch
from rngauto.core.calibration import CalibrationHistory
from rngauto.core.encounter import load_encounter
from rngauto.core.observation import BlinkKind
from rngauto.core.pokemon import ShinyFilter, TargetCriteria
from rngauto.core.statemachine import Outcome
from rngauto.games.bdsp.encounters import StaticTargetFinder, static_template
from rngauto.games.bdsp.fast_advance import AdvanceStats, choose_method, load_methods, reidentify_events
from rngauto.games.bdsp.recovery import RecoveryError
from rngauto.games.bdsp.scene import SceneModel, SceneSimulator
from rngauto.games.bdsp.screens import menu_visible
from rngauto.games.bdsp.static_strategy import Mode, StaticDeps, StaticEncounterRun, StaticRunConfig
from rngauto.games.bdsp.xorshift import Xorshift128, XorshiftState
from rngauto.vision.blink_signal import EyeMatcher
from rngauto.vision.screen import ScreenSignatures, thumbnail

ROOT = Path(__file__).resolve().parents[2]
METHODS = load_methods()


@dataclass(frozen=True)
class Ev:
    t_s: float
    kind: BlinkKind | None
    confidence: float = 1.0


def test_methods_are_valid_data():
    assert {"pokedex_peek", "pokedex_scroll"} <= set(METHODS)
    for m in METHODS.values():
        assert m.duration_s() > 0 and "pokedex" in m.screens
        assert m.steps[0].button == "X"  # siempre empieza abriendo el menú
        # Nunca se usa la cruceta sin haber comprobado antes el menú o la Pokédex (fuera de los menús, la
        # cruceta movería al personaje y estropearía la escena).
        last_check = None
        for st in m.steps:
            if st.kind == "check":
                last_check = st.screen
            elif st.kind == "hold":
                assert last_check in ("menu", "pokedex"), (m.id, st)
            elif st.kind == "press" and st.button in ("PLUS", "B"):
                last_check = None if st.button == "B" and last_check == "pokedex" else last_check
                if st.button == "PLUS":
                    last_check = None  # tras abrir el mapa hay que volver a comprobar el menú


@pytest.mark.parametrize("cursor", range(8))
def test_reaches_the_pokedex_wherever_the_cursor_is(cursor):
    """X → + (mapa) → B (cursor en «Mapa») → ARRIBA → A: siempre la Pokédex."""
    sw = SimulatedSwitch(StaticTargetFinder(static_template("dialga")), SimTruth(menu_cursor=cursor), seed=3)
    ctrl = sw.controller()
    ctrl.connect()
    steps = METHODS["pokedex_peek"].steps
    upto = next(i for i, st in enumerate(steps) if st.kind == "check" and st.screen == "pokedex")
    for st in steps[:upto]:
        ctrl.submit(st.commands("test"))
    for _ in range(int(sw.scene.fps * 12)):
        sw.read()
    assert sw.game.menu == POKEDEX and not sw.player_moved


def test_menu_detector_on_simulated_menu_and_scene():
    sw = SimulatedSwitch(StaticTargetFinder(static_template("dialga")), SimTruth(), seed=4)
    assert not menu_visible(sw.read().image)
    sw.game.menu = MENU_OPEN
    assert menu_visible(sw.read().image)


PA_IMAGES = (Path(__file__).resolve().parents[3] / "Pokemon Automation" / "pokemonautomation.github.io-main" / "docs"
             / "Programs" / "PokemonBDSP" / "images")


@pytest.mark.local
@pytest.mark.skipif(not PA_IMAGES.exists(), reason="sin las capturas reales de la documentación de PA")
@pytest.mark.parametrize("name,expected", [
    ("CloneItemsBoxCopy2-1.jpg", True), ("CloneItemsBoxCopy2-0.jpg", True),
    ("CloneItemsMenuOverlap-1.jpg", False), ("CloneItemsBoxCopy-1.jpg", False), ("StarterReset-0.jpg", False),
])
def test_menu_detector_on_real_game_screenshots(name, expected):
    img = cv2.imdecode(np.fromfile(str(PA_IMAGES / name), np.uint8), cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = cv2.morphologyEx((np.abs(gray.astype(int) - 240) > 12).astype(np.uint8), cv2.MORPH_OPEN,
                            np.ones((9, 9), np.uint8))
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    x, y, w, _h = stats[max(range(1, n), key=lambda i: stats[i, cv2.CC_STAT_AREA]), :4]
    game = cv2.resize(img[y:y + int(round(w * 9 / 16)), x:x + w], (1920, 1080))
    assert menu_visible(game) is expected


def test_choice_depends_on_what_is_left():
    stats = AdvanceStats()  # sin nada aprendido: valores orientativos, muy inciertos
    ms = list(METHODS.values())
    assert choose_method(150, 0.98, ms, stats) is None  # 2,5 min de espera: no merece la pena
    assert choose_method(300, 0.98, ms, stats) is None  # no cabe con la reidentificación y la ruta
    far = choose_method(20_000, 0.98, ms, stats)
    assert far.method.id == "pokedex_scroll" and far.hold_s > 20  # recorrido largo
    mid = choose_method(7_000, 0.98, ms, stats)
    assert mid.method.id == "pokedex_scroll" and 5 < mid.hold_s < mid.method.max_hold_s  # recorrido a medida
    assert mid.expected + 2 * mid.sd < 7_000
    holds = [st for st in mid.steps if st.scalable]
    assert abs(sum(st.ms for st in holds) / 1000 - mid.hold_s) < 0.01
    # Con lo aprendido (entrar y salir ≈ 180, casi sin variación) sí compensa para unos cientos
    for x in (181, 180, 178, 181, 180):
        stats.add("pokedex_peek", x)
    small = choose_method(600, 0.98, ms, stats)
    assert small is not None and small.expected >= 150 and small.expected + 2 * small.sd < 600
    assert choose_method(330, 0.98, ms, stats) is None  # 180 + lo que avanza solo + la ruta: no cabe


def test_stats_replace_the_prior_with_measurements(tmp_path):
    stats = AdvanceStats.load(tmp_path / "s.json")
    peek = METHODS["pokedex_peek"]
    assert stats.estimate(peek) == (150, 80)
    for x in (131, 140, 128, 135):
        stats.add(peek.id, x)
    mean, sd = stats.estimate(peek)
    assert 128 <= mean <= 140 and sd < 30
    scroll = METHODS["pokedex_scroll"]
    assert stats.estimate(scroll) == (scroll.prior_mean, scroll.prior_sd)
    # En esta partida: ~180 al abrir + 370/s a velocidad de crucero, con 1,2 s de arranque en cada sentido
    for h in (4.8, 9.0, 14.9, 24.7, 30.0):
        stats.add(scroll.id, round(180 + 370 * scroll.cruise_s(h, 1.2)), h)
    m15, sd15 = stats.estimate(scroll, 14.9)
    m4, _ = stats.estimate(scroll, 4.8)
    real15 = 180 + 370 * scroll.cruise_s(14.9, 1.2)
    real4 = 180 + 370 * scroll.cruise_s(4.8, 1.2)
    assert abs(m15 - real15) < 0.05 * real15 and abs(m4 - real4) < 0.08 * real4 and sd15 < 300
    again = AdvanceStats.load(tmp_path / "s.json")  # persistente
    assert again.estimate(scroll, 14.9) == (m15, sd15) and again.estimate(peek) == (mean, sd)


def _blinks_after_jump(seed: int, jump: int, n: int, npc: int = 0):
    base = XorshiftState(*Xorshift128(seed, seed * 3 + 1, seed * 7 + 2, seed * 11 + 3).outputs(4))
    rng = Xorshift128.from_state(base)
    rng.advance(jump)
    sim = SceneSimulator.start(rng.state, SceneModel(npc=npc), schedule_pokemon_now=False, start_time_s=100.0)
    out = []
    while len(out) < n:
        for ev in sim.step():
            if ev.player_blink is not None:
                out.append((ev.time_s + 0.03, ev.player_blink, ev.advance))
    return base, out


@pytest.mark.parametrize("npc", [0, 1])
def test_reidentify_finds_exact_advance_after_unknown_jump(npc):
    base, blinks = _blinks_after_jump(5, 12_345, 7, npc)
    events = [Ev(t, k) for t, k, _ in blinks]
    found = reidentify_events(base, events, npc, 1.0197, 5_000, 25_000)
    assert found.start_advance == 12_345 + blinks[0][2] - 1
    check = Xorshift128.from_state(base)
    check.advance(found.start_advance)
    assert found.recovery.result.state == check.state
    assert found.recovery.clock.period_s == pytest.approx(1.0197)


def test_reidentify_tolerates_a_missed_and_a_false_blink():
    base, blinks = _blinks_after_jump(9, 3_000, 10)
    events = [Ev(t, k) for t, k, _ in blinks]
    missed = events[:4] + events[5:]
    found = reidentify_events(base, missed, 0, 1.0197, 0, 10_000)
    assert found.start_advance == 3_000 + blinks[0][2] - 1 and found.note
    fake = Ev(events[2].t_s + 3 * 1.0197, BlinkKind.SINGLE, confidence=0.4)
    noisy = sorted(events + [fake], key=lambda e: e.t_s)
    found = reidentify_events(base, noisy, 0, 1.0197, 0, 10_000)
    assert found.start_advance == 3_000 + blinks[0][2] - 1


def test_reidentify_refuses_to_guess():
    base, blinks = _blinks_after_jump(3, 50_000, 3)
    with pytest.raises(RecoveryError):
        reidentify_events(base, [Ev(t, None) for t, _, _ in blinks], 0, 1.0197, 0, 400_000)


# --- Flujo completo con la Switch simulada ----------------------------------------------------------

def build(truth=SimTruth(), seed=21, criteria=None, screens=None):
    enc = load_encounter(ROOT / "encounters" / "bdsp" / "dialga.toml")
    finder = StaticTargetFinder(static_template(enc.template))
    switch = SimulatedSwitch(finder, truth, seed=seed)
    controller = switch.controller()
    controller.connect()
    matcher = EyeMatcher(switch.renderer.template(), switch.scene.eye_box, reference_height=switch.scene.height)
    deps = StaticDeps(
        clock=switch.clock, frames=switch, controller=controller, finder=finder, encounter=enc,
        matcher=matcher, calibration=CalibrationHistory(enc.id, 86),
        in_battle=switch.in_battle, observe=lambda fields: switch.observe(fields), reset_game=switch.reset_game,
        fast_methods=tuple(METHODS.values()), fast_stats=AdvanceStats(), screens=screens or ScreenSignatures(None),
    )
    cfg = StaticRunConfig(criteria=criteria or TargetCriteria(shiny=ShinyFilter.SHINY), mode=Mode.AUTOMATIC,
                          lead_s=0.125, max_attempts=2, min_plateau_blinks=12, blink_threshold_window_s=60)
    return StaticEncounterRun(deps, cfg), switch


def _messages(run, *kinds):
    return [e.message for e in run.d.timeline.of_kind(*kinds)]


@pytest.mark.slow
def test_shiny_far_away_uses_the_pokedex_and_hits():
    run, switch = build()
    result = run.build().run()
    assert result.outcome is Outcome.SUCCESS, (result.message, _messages(run, "error", "warning")[-6:])
    fast = [m for m in _messages(run, "recovery") if m.startswith("Reidentificado:")]
    assert fast, "debería haber usado el avance rápido"
    assert run.d.fast_stats.samples  # aprendió cuánto avanza cada acción
    assert switch.game.generated is not None and switch.game.generated.shiny.value != 0
    assert switch.game.menu == 0  # el menú quedó cerrado


@pytest.mark.slow
def test_never_uses_the_dpad_if_the_menu_is_not_on_screen():
    """Si al salir del mapa el juego volviera al mundo abierto, ARRIBA movería al personaje: no se pulsa."""
    screens = ScreenSignatures(None)
    sw = SimulatedSwitch(StaticTargetFinder(static_template("dialga")), SimTruth(), seed=1)
    screens.put("pokedex", thumbnail(sw._menu_screen(POKEDEX)))  # firma ya aprendida
    run, switch = build(SimTruth(map_exit_to_menu=False), seed=6, screens=screens)
    result = run.build().run()
    msgs = _messages(run, "warning")
    assert any("no se ve «menú»" in m for m in msgs)
    assert switch.game.menu == 0 and not switch.player_moved
    assert not [c for _, c in run.d.controller.log if c.state.dpad.value != "none"]
    assert result.outcome is Outcome.SUCCESS, (result.message, msgs[-6:])
