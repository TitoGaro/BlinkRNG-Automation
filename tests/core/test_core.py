import threading

import pytest

from rngauto.core.calibration import AttemptRecord, CalibrationHistory
from rngauto.core.clock import SimulatedClock
from rngauto.core.statemachine import (
    InterventionRequired, Outcome, RetryableError, StateMachine, StateSpec,
)
from rngauto.core.timeline import Timeline, TimelineEvent
from rngauto.core.tracker import AdvanceClock


# --- AdvanceClock / tracker -------------------------------------------------------------------

def test_advance_clock_counts_ticks():
    clock = AdvanceClock(anchor_t=100.0, anchor_advance=500, period_s=1.0, calls_per_tick=2)
    assert clock.advance_at(100.0) == 500
    assert clock.advance_at(100.99) == 500
    assert clock.advance_at(101.0) == 502
    assert clock.advance_at(105.5) == 510
    assert clock.time_of_advance(510) == pytest.approx(105.0)
    assert clock.middle_after_advance(510) == pytest.approx(105.5)
    assert clock.phase_margin_ticks(105.5) == pytest.approx(0.5)


def test_uncertainty_grows_with_distance():
    clock = AdvanceClock(0.0, 0, 1.02, period_sigma_s=0.001)
    near, far = clock.sigma_ticks(10.0), clock.sigma_ticks(1000.0)
    assert far > near
    assert clock.count_confidence(10.0 + 0.51) > clock.count_confidence(1000.0 * 1.02 + 0.51)


# --- Timeline -----------------------------------------------------------------------------------

def test_timeline_roundtrip(tmp_path):
    tl = Timeline()
    seen = []
    tl.subscribe(seen.append)
    tl.add(1.0, "blink", "parpadeo", frame=60, advance=12, confidence=0.98, data={"kind": 0})
    tl.add(2.0, "input", "A")
    path = tmp_path / "t.jsonl"
    tl.save(path)
    loaded = Timeline.load(path)
    assert loaded.events == tl.events
    assert len(seen) == 2 and isinstance(seen[0], TimelineEvent)


# --- Calibración --------------------------------------------------------------------------------

def attempt(i, target, delay, candidates, margin=0.3):
    return AttemptRecord(
        attempt_id=str(i), encounter="dialga", config_version="1", engine="bdsp-xorshift",
        engine_version="1.0.0", started_at="2026-10-03T00:00:00", target_advance=target,
        final_delay_used=delay, press_margin_s=margin, candidates=candidates,
    )


def test_calibration_converges_on_true_delay():
    hist = CalibrationHistory("dialga", prior_final_delay=84)
    assert hist.estimate_final_delay().source == "prior"
    # Delay real 86: con delay usado 84 se obtiene objetivo+2
    for i, target in enumerate((1000, 2000, 3000)):
        assert hist.record(attempt(i, target, 84, [target + 2])) == "confirmed"
    est = hist.estimate_final_delay()
    assert est.value == 86 and est.samples == 3 and est.source == "historial"


def test_calibration_robust_to_outlier():
    hist = CalibrationHistory("dialga", 84)
    for i, d in enumerate((2, 2, 3, 2, 15)):
        hist.record(attempt(i, 1000 * (i + 1), 84, [1000 * (i + 1) + d]))
    assert hist.estimate_final_delay().value == 86


def test_ambiguous_attempt_waits_for_vote():
    hist = CalibrationHistory("dialga", 84)
    # Solo naturaleza observada: compatible con +2 o −5
    assert hist.record(attempt(0, 1000, 84, [1002, 995])) == "uncertain"
    assert hist.estimate_final_delay().source == "prior"
    assert hist.target_bump() == 1
    # Un intento inequívoco con +2 resuelve el incierto
    assert hist.record(attempt(1, 2000, 84, [2002])) == "confirmed"
    assert not hist.uncertain
    assert [a.delta for a in hist.confirmed] == [2, 2]


def test_adjacent_ambiguity_resolved_by_exact_vote():
    """Vecinos indistinguibles (+0/+1): el historial exacto decide, sin esperar a otro intento."""
    hist = CalibrationHistory("dialga", 84)
    hist.record(attempt(0, 1000, 84, [1001]))  # delay real 85
    assert hist.record(attempt(1, 2000, 85, [2000, 2001])) == "confirmed"
    assert hist.confirmed[-1].delta == 0
    assert hist.estimate_final_delay().value == 85


def test_tied_doubt_is_not_resolved_arbitrarily():
    """«85 u 89» y luego un 87 confirmado: los dos quedan a 2, así que el dudoso sigue dudoso (antes se
    resolvía a la fuerza como 85, el primero, y la estimación bajaba a 86)."""
    hist = CalibrationHistory("dialga", 84)
    assert hist.record(attempt(0, 1000, 84, [1001, 1005])) == "uncertain"  # delays 85 u 89
    assert hist.record(attempt(1, 2000, 84, [2003])) == "confirmed"  # delay 87
    assert len(hist.uncertain) == 1 and len(hist.confirmed) == 1
    assert hist.estimate_final_delay().value == 87


def test_no_match_is_not_recorded():
    hist = CalibrationHistory("dialga", 84)
    a = attempt(0, 1000, 84, [])
    assert hist.record(a) == "no_match" and a.outcome == "no_match"
    assert not hist.confirmed


def test_calibration_persistence(tmp_path):
    hist = CalibrationHistory("dialga", 84)
    hist.record(attempt(0, 1000, 84, [1003]))
    path = tmp_path / "cal" / "dialga.json"
    hist.save(path)
    loaded = CalibrationHistory.load(path, "dialga", 84)
    assert loaded.estimate_final_delay().value == 87
    assert CalibrationHistory.load(tmp_path / "nope.json", "x", 10).estimate_final_delay().value == 10


# --- Máquina de estados -------------------------------------------------------------------------

def test_state_machine_success_and_retries():
    clock = SimulatedClock()
    calls = {"b": 0, "recovered": 0}

    def a(ctx):
        ctx.sleep_until(ctx.now() + 1.0)
        return "b"

    def b(ctx):
        calls["b"] += 1
        if calls["b"] < 3:
            raise RetryableError("pantalla inesperada")
        return None

    def recover(ctx, exc):
        calls["recovered"] += 1

    sm = StateMachine([StateSpec("a", a), StateSpec("b", b, max_retries=2, recover=recover)], "a", clock)
    result = sm.run()
    assert result.outcome is Outcome.SUCCESS
    assert calls == {"b": 3, "recovered": 2}
    assert clock.now() == pytest.approx(1.0)


def test_state_machine_timeout_and_intervention():
    clock = SimulatedClock()

    def slow(ctx):
        ctx.sleep_until(ctx.now() + 100)
        return None

    sm = StateMachine([StateSpec("slow", slow, timeout_s=5)], "slow", clock)
    assert sm.run().outcome is Outcome.FAILED

    def unsure(ctx):
        raise InterventionRequired("posición desconocida")

    sm2 = StateMachine([StateSpec("u", unsure)], "u", clock)
    res = sm2.run()
    assert res.outcome is Outcome.INTERVENTION and "posición" in res.message


def test_emergency_stop_interrupts_long_action():
    stopped = []
    started = threading.Event()

    class BlockingClock(SimulatedClock):
        def sleep_until(self, t, cancel=None):
            started.set()
            cancel.wait(5)
            return not cancel.is_set()

    bclock = BlockingClock()

    def long_action(ctx):
        ctx.sleep_until(ctx.now() + 1000)
        return None

    sm = StateMachine([StateSpec("long", long_action)], "long", bclock, on_stop=[lambda: stopped.append(1)])
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", sm.run()))
    th.start()
    assert started.wait(2)
    sm.stop("emergencia")
    th.join(2)
    assert out["r"].outcome is Outcome.STOPPED
    assert stopped == [1]


def test_doubtful_neighbours_count_in_estimate():
    """Caso real (Regice, Parque Hansa): 89 exacto y luego «87 u 88» (la ficha cuadra con dos advances
    vecinos). El delay que queda a 1 de ambos es 88; 89 quedaría a 2 si el dudoso fuera 87."""
    hist = CalibrationHistory("hansa_regice", 85)
    assert hist.record(attempt(0, 352, 85, [356])) == "confirmed"
    assert hist.estimate_final_delay().value == 89
    assert hist.record(attempt(1, 422, 89, [421, 420])) == "uncertain"
    est = hist.estimate_final_delay()
    assert est.value == 88 and est.samples == 2 and est.doubtful == 1
    assert "2 intentos, 1 dudoso" in est.describe()


def test_far_apart_doubts_wait_for_vote():
    hist = CalibrationHistory("dialga", 84)
    hist.record(attempt(0, 1000, 84, [1002, 995]))  # +2 o −5: no se usa hasta que haya votación
    assert hist.estimate_final_delay().source == "prior"


def test_singular_plural_in_description():
    hist = CalibrationHistory("dialga", 84)
    hist.record(attempt(0, 1000, 84, [1002]))
    assert "(1 intento;" in hist.estimate_final_delay().describe()
