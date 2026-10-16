"""Une visión y RNG: eventos de parpadeo → índices de tirada → estado recuperado → reloj de advances.

Convención del ancla (ver ``core/tracker.py``): el estado base es el previo a la tirada del primer
parpadeo; tras la tirada ``r`` del jugador, el bloque de ``npc+1`` llamadas termina en
``(r + 1)·(npc + 1)`` (suponiendo que el jugador abre el bloque; si no, la constante la absorbe la
calibración del delay final).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from rngauto.core.observation import BlinkKind
from rngauto.core.tracker import AdvanceClock
from rngauto.i18n import t

from .blink import DEFAULT_ROLL_PERIOD_S, RollAssignment, assign_rolls
from .recovery import BlinkObservation, RecoveryError, RecoveryResult, recover_state

MIN_EVENT_CONFIDENCE = 0.2  # por debajo, ni siquiera se intenta usar el evento


class EventLike(Protocol):
    """Lo mínimo que se necesita de un evento (``vision.blink_signal.BlinkEvent`` lo cumple)."""

    t_s: float
    kind: BlinkKind | None
    confidence: float


@dataclass(frozen=True)
class BlinkRecovery:
    """Estado recuperado junto con la asignación de tiradas y el reloj de advances que se deduce de ella."""

    result: RecoveryResult
    assignment: RollAssignment
    clock: AdvanceClock
    events_used: int
    events_discarded: int
    # (periodo, peso) ya medidos antes (p. ej. antes de un avance rápido): los menús no cambian el periodo y
    # conservarlo evita que una espera larga dependa solo de los pocos parpadeos posteriores.
    period_prior: tuple[float, float] | None = None

    def describe(self) -> str:
        """Resumen de una línea para el registro."""
        r = self.result
        return t(
            "Estado {state} | {used} parpadeos "
            "({discarded} descartados por baja confianza, {excluded} excluidos por "
            "inconsistentes) | periodo {period:.4f} s | "
            "redundancia {redundant} ecuaciones | confianza {confidence:.0%}",
            "State {state} | {used} blinks "
            "({discarded} discarded for low confidence, {excluded} excluded as "
            "inconsistent) | period {period:.4f} s | "
            "redundancy {redundant} equations | confidence {confidence:.0%}",
            state=r.state.to_hex32(), used=self.events_used, discarded=self.events_discarded,
            excluded=len(r.excluded), period=self.assignment.period_s, redundant=r.redundant_equations,
            confidence=r.confidence,
        )


class BlinkPredictor:
    """Qué debería hacer el ojo del jugador en cada tirada según el estado recuperado (sync check)."""

    def __init__(self, recovery: BlinkRecovery) -> None:
        from .xorshift import Xorshift128

        self._stride = recovery.result.npc + 1
        self._rng = Xorshift128.from_state(recovery.result.state)
        self._kinds: list[BlinkKind | None] = []
        self.recovery = recovery
        self._clock: AdvanceClock | None = None

    def use_clock(self, clock: AdvanceClock) -> None:
        """Usa el reloj re-anclado (periodo afinado): en esperas de horas, el periodo inicial acumularía error."""
        self._clock = clock

    def roll_at(self, t_s: float) -> int:
        """Índice de tirada del jugador que corresponde al instante ``t_s`` (con el reloj re-anclado si lo hay)."""
        c = self._clock
        if c is not None:
            anchor_roll = c.anchor_advance // c.calls_per_tick - 1
            return anchor_roll + round((t_s - c.anchor_t) / c.period_s)
        a = self.recovery.assignment
        return round((t_s - a.phase_s) / a.period_s)

    def kind_at_roll(self, roll: int) -> BlinkKind | None:
        """Tipo de parpadeo esperado en la tirada ``roll`` (``None`` = no parpadea).

        La secuencia se genera bajo demanda y se guarda, así que sirve para esperas de horas sin recalcular.
        Entre dos tiradas del jugador se saltan las ``npc`` llamadas de los NPC."""
        from .blink import player_blink_kind

        if roll < 0:
            return None
        while len(self._kinds) <= roll:
            self._kinds.append(player_blink_kind(self._rng.next()))
            self._rng.advance(self._stride - 1)
        return self._kinds[roll]

    def check(self, t_s: float, kind: BlinkKind | None) -> bool:
        """¿Es coherente un parpadeo observado en ``t_s`` con el estado? (tipo ``None`` = no comprobar)."""
        expected = self.kind_at_roll(self.roll_at(t_s))
        if expected is None:
            return False
        # Un doble visto como simple (segundo cierre perdido) es un fallo típico del detector, no una
        # desincronización; lo contrario sí cuenta como incoherente.
        return kind is None or kind == expected or (kind is BlinkKind.SINGLE and expected is BlinkKind.DOUBLE)


def recover_from_events(
    events: Sequence[EventLike],
    npc: int = 0,
    *,
    period_prior_s: float = DEFAULT_ROLL_PERIOD_S,
    min_confidence: float = MIN_EVENT_CONFIDENCE,
    max_exclusions: int = 2,
) -> BlinkRecovery:
    """Recupera el estado del RNG a partir de los parpadeos detectados.

    Descarta los eventos con confianza menor que ``min_confidence``, asigna una tirada a cada uno, resuelve el
    estado (con hasta ``max_exclusions`` observaciones descartables) y construye el reloj anclado en la última
    tirada usada. Lanza ``RecoveryError`` si no hay una solución fiable.
    """
    usable = [e for e in sorted(events, key=lambda e: e.t_s) if e.confidence >= min_confidence]
    assignment = assign_rolls([e.t_s for e in usable], period_prior_s=period_prior_s)
    observations = [
        BlinkObservation(roll, e.kind, e.confidence) for roll, e in zip(assignment.rolls, usable)
    ]
    try:
        result = recover_state(
            observations, npc, ambiguous=assignment.ambiguous(), max_exclusions=max_exclusions
        )
    except RecoveryError:
        # El detector a veces no ve el segundo cierre de un doble y lo da por simple (lo contrario es raro):
        # se reintenta sin exigir los «simples» (tipo desconocido) antes de pedir más parpadeos. Con menos
        # ecuaciones el sistema puede quedar indeterminado (se pide observar más) y la solución se valida igual.
        relaxed = [o if o.kind is not BlinkKind.SINGLE else BlinkObservation(o.roll, None, o.confidence)
                   for o in observations]
        if relaxed == observations:
            raise
        result = recover_state(relaxed, npc, ambiguous=assignment.ambiguous(), max_exclusions=max_exclusions)
    stride = npc + 1
    last_roll = result.last_roll
    clock = AdvanceClock(
        anchor_t=assignment.phase_s + assignment.period_s * last_roll,
        anchor_advance=(last_roll + 1) * stride,
        period_s=assignment.period_s,
        calls_per_tick=stride,
        # Incertidumbre del periodo: el peor residuo repartido entre las tiradas observadas, con un suelo.
        period_sigma_s=max(0.0002, assignment.max_abs_residual * assignment.period_s / max(1, last_roll)),
    )
    return BlinkRecovery(result, assignment, clock, len(usable), len(events) - len(usable))


def refine_clock(recovery: BlinkRecovery, extra: list[tuple[int, float]]) -> AdvanceClock:
    """Reajusta periodo y fase con los parpadeos de la recuperación más ``extra`` (tirada, instante)
    observados después (re-anclaje continuo por mínimos cuadrados: cuantos más parpadeos, más fino el periodo).
    """
    points = _points(recovery, extra)
    n = len(points)
    mk = sum(r for r, _ in points) / n
    mt = sum(t for _, t in points) / n
    sxx = sum((r - mk) ** 2 for r, _ in points)
    sxy = sum((r - mk) * (t - mt) for r, t in points)
    p0, w = recovery.period_prior or (recovery.assignment.period_s, 0.0)
    # Mínimos cuadrados con un término a priori sobre el periodo: min Σ(t-fase-periodo·k)² + w·(periodo-p0)²
    period = (sxy + w * p0) / (sxx + w) if (sxx + w) > 0 else p0
    phase = mt - period * mk
    residuals = [t - (phase + period * r) for r, t in points]
    last_roll = max(r for r, _ in points)
    stride = recovery.result.npc + 1
    spread = max(abs(x) for x in residuals) if residuals else 0.0
    return AdvanceClock(
        anchor_t=phase + period * last_roll,
        anchor_advance=(last_roll + 1) * stride,
        period_s=period,
        calls_per_tick=stride,
        period_sigma_s=max(0.0001, spread / max(1, last_roll)),
    )


def _points(recovery: BlinkRecovery, extra: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Pares ``(tirada, instante)`` de la recuperación (instantes reconstruidos con sus residuos) más ``extra``."""
    a = recovery.assignment
    return [(r, a.phase_s + a.period_s * (r + res)) for r, res in zip(a.rolls, a.residuals)] + list(extra)


def period_information(recovery: BlinkRecovery, extra: list[tuple[int, float]]) -> float:
    """Peso (Σ(k-k̄)²) que respalda el periodo estimado: se arrastra a la siguiente reidentificación."""
    points = _points(recovery, extra)
    mk = sum(r for r, _ in points) / len(points)
    prior = recovery.period_prior[1] if recovery.period_prior else 0.0
    return prior + sum((r - mk) ** 2 for r, _ in points)
