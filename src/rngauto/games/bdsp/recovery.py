"""Recuperación y re-identificación del estado Xorshift a partir de parpadeos del jugador.

Cada parpadeo observado aporta ecuaciones lineales en GF(2) sobre los 128 bits del estado; con suficientes
parpadeos el sistema tiene una única solución. Propiedades:
  * Validación completa: se comprueba cada tirada entre el primer y el último parpadeo (también las
    tiradas sin parpadeo), no solo el orden de tipos.
  * Tipo de parpadeo opcional: si el detector no distingue simple/doble, el parpadeo aporta 3
    ecuaciones en vez de 4.
  * Espacio nulo pequeño (≤ ``max_free_bits``): se enumeran los candidatos y se verifican con las
    tiradas sin parpadeo; exige que sobreviva exactamente uno.
  * Tolerancia a observaciones erróneas: exclusión de observaciones (menor confianza primero) y
    desplazamiento ±1 tirada de las dudosas.
  * Resultado explicable: ecuaciones redundantes, exclusiones, desplazamientos y discrepancias.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Literal

from rngauto.core.gf2 import LinearSystem
from rngauto.i18n import t

from .blink import BlinkKind, LowNibbleStream, player_blink_kind
from .xorshift import Xorshift128, XorshiftState

STATE_BITS = 128
MIN_BLINKS = 24  # por debajo ni siquiera con espacio nulo de 12 bits hay ecuaciones suficientes
DEFAULT_MAX_FREE_BITS = 12  # hasta 2^12 = 4096 candidatos: enumerarlos y validarlos sigue siendo rápido


class RecoveryError(RuntimeError):
    """No se pudo determinar un estado consistente con las observaciones."""


class AmbiguousPattern(RecoveryError):
    """Varias alineaciones explican lo observado: hay que observar más (no es un fallo del detector).

    Es un tipo propio para que el código decida por el tipo y no por el texto (que se traduce)."""


@dataclass(frozen=True, slots=True)
class BlinkObservation:
    """Parpadeo del jugador observado.

    ``roll``: índice de tirada del jugador relativo al primer parpadeo (el primero es 0).
    ``kind``: ``None`` si el detector no pudo distinguir simple/doble.
    """

    roll: int
    kind: BlinkKind | None
    confidence: float = 1.0


MismatchType = Literal["missed", "false", "kind"]


@dataclass(frozen=True, slots=True)
class Mismatch:
    """Tirada en la que lo observado no coincide con lo que predice un estado candidato."""

    roll: int
    type: MismatchType  # missed: predicho y no visto; false: visto y no predicho; kind: tipo distinto
    expected: BlinkKind | None

    @property
    def is_missed_detection(self) -> bool:
        return self.type == "missed"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Estado recuperado y cómo se llegó a él (qué se excluyó o desplazó y qué no cuadra)."""

    state: XorshiftState  # estado justo ANTES de la tirada del primer parpadeo
    npc: int
    observations: tuple[BlinkObservation, ...]
    excluded: tuple[int, ...]  # índices (en la lista de entrada ordenada) descartados
    shifted: tuple[tuple[int, int], ...]  # (índice, desplazamiento) aplicados
    redundant_equations: int
    null_space_bits: int
    mismatches: tuple[Mismatch, ...]

    @property
    def last_roll(self) -> int:
        """Índice de la última tirada observada (relativo al primer parpadeo)."""
        return max(o.roll for o in self.observations)

    def advances_for_roll(self, roll: int) -> int:
        """Llamadas a ``next()`` desde ``state`` hasta incluir la tirada ``roll`` del jugador."""
        return roll * (self.npc + 1) + 1

    def state_after_roll(self, roll: int) -> XorshiftState:
        """Estado del generador justo después de la tirada ``roll`` del jugador."""
        rng = Xorshift128.from_state(self.state)
        rng.advance(self.advances_for_roll(roll))
        return rng.state

    @property
    def hard_mismatches(self) -> tuple[Mismatch, ...]:
        """Discrepancias no explicables por un parpadeo no detectado."""
        return tuple(m for m in self.mismatches if not m.is_missed_detection)

    @property
    def confidence(self) -> float:
        """Heurística en [0, 1]: redundancia penalizada por exclusiones/desplazamientos/fallos."""
        redundancy = min(1.0, self.redundant_equations / 16)
        penalty = (
            0.15 * len(self.excluded)
            + 0.1 * len(self.shifted)
            + 0.05 * len(self.mismatches)
            + 0.02 * self.null_space_bits
        )
        return max(0.0, min(1.0, 0.5 + 0.5 * redundancy - penalty))


def _state_from_int(value: int) -> XorshiftState:
    """Convierte la solución del sistema (vector de 128 bits, ``s0`` en lo alto) en un estado."""
    return XorshiftState(
        (value >> 96) & 0xFFFFFFFF,
        (value >> 64) & 0xFFFFFFFF,
        (value >> 32) & 0xFFFFFFFF,
        value & 0xFFFFFFFF,
    )


def recover_state(
    observations: list[BlinkObservation],
    npc: int = 0,
    *,
    max_exclusions: int = 2,
    ambiguous: tuple[int, ...] = (),
    max_shift_candidates: int = 3,
    max_free_bits: int = DEFAULT_MAX_FREE_BITS,
) -> RecoveryResult:
    """Recupera el estado a partir de parpadeos del jugador con ``npc`` personajes adicionales.

    ``ambiguous``: índices (de la lista ordenada por ``roll``) cuyo ``roll`` es dudoso (ver
    ``RollAssignment.ambiguous``); se probarán desplazamientos de ±1 tirada.

    Prueba primero sin exclusiones y luego excluyendo hasta ``max_exclusions`` observaciones, empezando por las de
    menor confianza. Devuelve el primer candidato sin discrepancias graves; si no hay ninguno, ``RecoveryError``.
    """
    if npc < 0:
        raise ValueError(t("npc no puede ser negativo", "npc cannot be negative"))
    if len(observations) < MIN_BLINKS:
        raise RecoveryError(t("Se necesitan al menos {min} parpadeos (hay {n})", "At least {min} blinks are needed ({n} so far)",
                              min=MIN_BLINKS, n=len(observations)))
    obs = sorted(observations, key=lambda o: o.roll)
    base = obs[0].roll
    obs = [BlinkObservation(o.roll - base, o.kind, o.confidence) for o in obs]
    stream = LowNibbleStream()
    stride = npc + 1

    def solve_candidate(rolls: list[int], skip: set[int], allow_enumeration: bool):
        """Devuelve (estado, redundantes, bits_libres, observaciones_usadas) o None."""
        system = LinearSystem(STATE_BITS)
        for i, roll in enumerate(rolls):
            if i in skip:
                continue
            # Un parpadeo implica bits 1-3 a cero (ecuaciones lineales) y, si se sabe el tipo, el bit 0. Las tiradas
            # sin parpadeo no dan ecuaciones («no todos cero» no es lineal): solo sirven para validar.
            b0, b1, b2, b3 = stream.at(roll * stride)
            rows = [(b1, 0), (b2, 0), (b3, 0)]
            if obs[i].kind is not None:
                rows.append((b0, int(obs[i].kind)))
            for mask, rhs in rows:
                if system.add(mask, rhs) == "conflict":
                    return None
        kept = [
            BlinkObservation(r, obs[i].kind, obs[i].confidence)
            for i, r in enumerate(rolls)
            if i not in skip
        ]
        free = len(system.free_variables)
        if free == 0:
            return _state_from_int(system.solve()), system.redundant, 0, kept
        # Faltan ecuaciones: se prueban las 2^free soluciones y se valida cada una con las tiradas sin parpadeo.
        if not allow_enumeration or free > max_free_bits:
            return None
        survivors = []
        for value in system.solutions(max_free=max_free_bits):
            state = _state_from_int(value)
            if not [m for m in validate(state, kept, npc) if not m.is_missed_detection]:
                survivors.append(state)
                if len(survivors) > 1:
                    return None  # ambiguo
        if len(survivors) != 1:
            return None
        return survivors[0], system.redundant, free, kept

    # Alineaciones a probar: la asignada y, para cada observación dudosa, la misma con esa tirada movida ±1 (siempre
    # que el orden se mantenga y la primera siga en 0).
    rolls0 = [o.roll for o in obs]
    candidates: list[tuple[list[int], tuple[tuple[int, int], ...]]] = [(rolls0, ())]
    for idx in list(ambiguous)[:max_shift_candidates]:
        for delta in (-1, 1):
            rolls = rolls0.copy()
            rolls[idx] += delta
            if rolls[0] != 0 or rolls[idx] < 0:
                continue
            if (idx > 0 and rolls[idx] <= rolls[idx - 1]) or (
                idx + 1 < len(rolls) and rolls[idx] >= rolls[idx + 1]
            ):
                continue
            candidates.append((rolls, ((idx, delta),)))

    by_confidence = sorted(range(len(obs)), key=lambda i: obs[i].confidence)
    best_failure = ""
    for n_excl in range(0, max_exclusions + 1):
        if len(obs) - n_excl < MIN_BLINKS:
            break
        for rolls, shifted in candidates:
            for skip in combinations(by_confidence, n_excl):
                skip_set = set(skip)
                # Enumerar el espacio nulo es caro: solo en el caso base, no en cada combinación de exclusiones.
                solved = solve_candidate(rolls, skip_set, allow_enumeration=(n_excl == 0 and not shifted))
                if solved is None:
                    continue
                state, redundant, free, kept = solved
                mismatches = validate(state, kept, npc)
                result = RecoveryResult(
                    state=state,
                    npc=npc,
                    observations=tuple(kept),
                    excluded=tuple(sorted(skip_set)),
                    shifted=shifted,
                    redundant_equations=redundant,
                    null_space_bits=free,
                    mismatches=tuple(mismatches),
                )
                if not result.hard_mismatches:
                    return result
                best_failure = t(" (un candidato dejó {n} discrepancias)", " (one candidate left {n} mismatches)",
                                 n=len(result.hard_mismatches))
    raise RecoveryError(t(
        "No hay ningún estado consistente con los parpadeos observados; probablemente falta o "
        "sobra algún parpadeo o hay que observar más",
        "No state is consistent with the observed blinks; a blink is probably missing or "
        "extra, or more need to be observed",
    ) + best_failure)


def validate(state: XorshiftState, observations: list[BlinkObservation], npc: int = 0) -> list[Mismatch]:
    """Compara cada tirada del jugador entre la primera y la última observación con el modelo.

    Devuelve las discrepancias: parpadeos predichos y no vistos, vistos y no predichos, y de tipo distinto.
    """
    if not observations:
        return []
    observed = {o.roll: o.kind for o in observations}
    last = max(observed)
    rng = Xorshift128.from_state(state)
    mismatches: list[Mismatch] = []
    for roll in range(last + 1):
        value = rng.next()
        rng.advance(npc)
        expected = player_blink_kind(value)
        seen = roll in observed
        if expected is None and seen:
            mismatches.append(Mismatch(roll, "false", None))
        elif expected is not None and not seen:
            mismatches.append(Mismatch(roll, "missed", expected))
        elif expected is not None and observed[roll] is not None and observed[roll] != expected:
            mismatches.append(Mismatch(roll, "kind", expected))
    return mismatches


# --- Re-identificación ------------------------------------------------------------------------

def reidentify_kinds(base: XorshiftState, start: int, count: int) -> list[BlinkKind | None]:
    """Tipo de parpadeo del jugador de las ``count`` salidas a partir de ``start`` llamadas desde ``base``."""
    rng = Xorshift128.from_state(base)
    rng.skip(start)
    return [player_blink_kind(rng.next()) for _ in range(count)]


@dataclass(frozen=True, slots=True)
class ReidentifyResult:
    """Dónde está el juego respecto a un estado base conocido, según :func:`reidentify`."""

    advance: int  # llamadas a next() desde el estado base hasta incluir la última tirada observada
    state: XorshiftState  # estado tras esa tirada
    phase: int  # posición (mod npc+1) del jugador respecto al estado base
    missed: int  # parpadeos predichos y no observados tolerados


def reidentify(
    base: XorshiftState,
    observations: list[BlinkObservation],
    npc: int = 0,
    *,
    search_min: int = 0,
    search_max: int = 1_000_000,
    tolerate_missed: int = 0,
    kinds: list[BlinkKind | None] | None = None,
) -> ReidentifyResult:
    """Busca, a partir de un estado conocido, en qué advance está ahora el juego.

    Usa el patrón completo (posiciones y tipos de los parpadeos, y ausencia de parpadeo en el resto
    de tiradas), por lo que suele bastar con ~7–10 parpadeos. Falla si hay ambigüedad.
    ``tolerate_missed``: nº de parpadeos predichos y no observados (falsos negativos) admitidos.
    ``kinds``: tipos de parpadeo ya calculados desde ``search_min`` (``reidentify_kinds``), para no
    regenerar decenas de miles de salidas en cada intento.
    """
    if not observations:
        raise ValueError(t("Se necesita al menos una observación", "At least one observation is needed"))
    obs = sorted(observations, key=lambda o: o.roll)
    base_roll = obs[0].roll
    pattern: dict[int, BlinkKind | None] = {o.roll - base_roll: o.kind for o in obs}
    span = max(pattern)
    stride = npc + 1
    window = span * stride + 1

    if kinds is None or len(kinds) < search_max - search_min + window:
        kinds = reidentify_kinds(base, search_min, search_max - search_min + window)

    def matches_at(start: int) -> int | None:
        """Parpadeos predichos y no vistos si el patrón encaja empezando en ``start``; ``None`` si no encaja."""
        missed = 0
        for roll in range(span + 1):
            expected = kinds[start + roll * stride]
            if roll in pattern:
                got = pattern[roll]
                if expected is None or (got is not None and got != expected):
                    return None
            elif expected is not None:
                missed += 1
                if missed > tolerate_missed:
                    return None
        return missed

    found: list[tuple[int, int]] = []
    first = pattern[0]
    for start in range(0, search_max - search_min):
        k = kinds[start]
        # Filtro rápido: la primera observación tiene que ser un parpadeo del tipo visto.
        if k is None or (first is not None and k != first):
            continue
        missed = matches_at(start)
        if missed is not None:
            found.append((start, missed))
            if len(found) > 1:
                break  # con dos coincidencias ya es ambiguo: no hace falta seguir
    if not found:
        raise RecoveryError(t("No se encontró el patrón de parpadeos en el rango de búsqueda",
                              "Blink pattern not found in the search range"))
    if len(found) > 1:
        raise AmbiguousPattern(t("Patrón ambiguo: observa más parpadeos o reduce el rango de búsqueda",
                                 "Ambiguous pattern: observe more blinks or narrow the search range"))
    start, missed = found[0]
    advance = search_min + start + span * stride + 1
    final = Xorshift128.from_state(base)
    final.skip(advance)
    return ReidentifyResult(
        advance=advance, state=final.state, phase=(search_min + start) % stride, missed=missed
    )
