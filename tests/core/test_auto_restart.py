"""Ante un imprevisto antes de la A final se reinicia el juego y se sigue, en vez de parar."""
from types import SimpleNamespace

from rngauto.core.clock import SimulatedClock
from rngauto.core.statemachine import InterventionRequired, Outcome, StateMachine, StateSpec
from rngauto.core.timeline import Timeline
from rngauto.games.bdsp.static_strategy import StaticEncounterRun


def test_on_failure_hook_redirects_instead_of_stopping():
    calls = []

    def flaky(ctx):
        calls.append("collect")
        if len(calls) == 1:
            raise InterventionRequired("no aparece el mensaje")
        return None

    sm = StateMachine([StateSpec("COLLECT", flaky), StateSpec("RESET", lambda ctx: "COLLECT")], "COLLECT",
                      SimulatedClock(), on_failure=lambda state, exc: "RESET")
    result = sm.run()
    assert result.outcome is Outcome.SUCCESS
    assert [t.state for t in result.transitions] == ["COLLECT", "RESET", "COLLECT"]


def test_without_hook_or_when_hook_declines_it_stops():
    def boom(ctx):
        raise InterventionRequired("x")

    assert StateMachine([StateSpec("A", boom)], "A", SimulatedClock()).run().outcome is Outcome.INTERVENTION
    sm = StateMachine([StateSpec("A", boom)], "A", SimulatedClock(), on_failure=lambda s, e: None)
    assert sm.run().outcome is Outcome.INTERVENTION


def test_unexpected_errors_also_go_through_the_hook():
    def bug(ctx):
        raise ValueError("fallo raro")

    seen = []
    sm = StateMachine([StateSpec("A", bug), StateSpec("B", lambda ctx: None)], "A", SimulatedClock(),
                      on_failure=lambda s, e: seen.append((s, type(e))) or "B")
    assert sm.run().outcome is Outcome.SUCCESS
    assert seen == [("A", ValueError)]


def _run(**over):
    """Lo justo de ``StaticEncounterRun`` para probar su política de recuperación."""
    run = SimpleNamespace(
        _battle_possible=False, _auto_restarts=0, _can_reset_from_home=True,
        cfg=SimpleNamespace(max_auto_restarts=3),
        d=SimpleNamespace(timeline=Timeline(), clock=SimulatedClock()),
    )
    for k, v in over.items():
        setattr(run, k, v)
    return run


def decide(run, state, exc):
    return StaticEncounterRun._on_failure(run, state, exc)


def test_restarts_the_game_before_the_final_press():
    run = _run()
    for state in ("COLLECT", "PLAN", "FAST_ADVANCE", "REIDENTIFY", "WAIT_TRIGGER", "TIMELINE", "RESET"):
        run._auto_restarts = 0
        assert decide(run, state, InterventionRequired("imprevisto")) == "RESET"
    run._auto_restarts = 0
    assert decide(run, "COLLECT", RuntimeError("error inesperado")) == "RESET"


def test_never_restarts_when_a_shiny_could_be_on_screen():
    for state in ("ENCOUNTER", "CATCH", "VERIFY"):
        assert decide(_run(), state, InterventionRequired("pantalla inesperada")) is None
    # tras la A final, aunque el fallo llegue en otro estado
    assert decide(_run(_battle_possible=True), "TIMELINE", InterventionRequired("x")) is None


def test_never_restarts_when_restarting_cannot_help():
    assert decide(_run(), "COLLECT", InterventionRequired("sin mando", recoverable=False)) is None
    assert decide(_run(_can_reset_from_home=False), "COLLECT", InterventionRequired("x")) is None  # ensayo, etc.


def test_gives_up_after_too_many_restarts_in_a_row():
    run = _run()
    results = [decide(run, "COLLECT", InterventionRequired("x")) for _ in range(5)]
    assert results == ["RESET", "RESET", "RESET", None, None]
