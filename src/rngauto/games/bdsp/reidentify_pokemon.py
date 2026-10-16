"""Reidentificación con un Pokémon parpadeando en escena (Regigigas, legendarios a la vista).

Desde un estado conocido (la semilla se saca donde no hay Pokémon) se buscan los parpadeos del jugador admitiendo
llamadas intercaladas del Pokémon. Contar solo los huecos que dejan esas llamadas no basta para distinguir bien
las alineaciones, y después habría que ignorar al Pokémon. Aquí se usa además el tiempo: cada parpadeo del
Pokémon programa el siguiente a ``rand_float(3, 12) + 0,285`` s con su propia salida, así que la alineación
correcta explica a la vez el patrón del jugador y los instantes de los huecos. Como resultado se sabe también
cuándo será su siguiente parpadeo, y la espera delante del legendario se simula entera (jugador + Pokémon) en
vez de suponer que solo parpadea el jugador.

Contabilidad: todos los ``advance`` son llamadas a ``next()`` desde ``base`` (el estado de la recuperación).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rngauto.core.observation import BlinkKind
from rngauto.i18n import t

from .blink import is_player_blink, player_blink_kind, pokemon_blink_interval
from .recovery import AmbiguousPattern, RecoveryError
from .scene import PLAYER, SceneModel, SceneSimulator
from .xorshift import Xorshift128, XorshiftState

POKEMON_FIRST_MAX_S = 12.3  # el siguiente parpadeo del Pokémon cae como mucho a 12 s + 0,285 de cualquier instante
PHASE_STEP_S = 0.05  # resolución con que se prueba el instante de ese primer parpadeo


@dataclass(frozen=True, slots=True)
class TickObservation:
    """Tirada del jugador ``roll`` (relativa a la primera observada) y si parpadeó (y de qué tipo)."""

    roll: int
    blink: bool
    kind: BlinkKind | None = None  # solo se exige si es doble (el detector a veces ve simple un doble)


@dataclass(frozen=True)
class PokemonReidentified:
    """Alineación encontrada por :func:`reidentify_with_pokemon`: dónde está el juego y cuándo parpadeará el
    Pokémon la próxima vez."""

    first_advance: int  # llamadas desde ``base`` hasta la tirada 0 del jugador (incluida)
    end_advance: int  # llamadas consumidas tras la última tirada observada
    state: XorshiftState  # estado en ``end_advance``
    pokemon_next_s: float  # siguiente parpadeo del Pokémon, relativo a la tirada 0
    pokemon_window_s: tuple[float, float]  # margen de ese instante (todas las alineaciones válidas)
    pokemon_blinks: int  # parpadeos del Pokémon dentro de lo observado
    ticks: int
    mismatches: int = 0  # tiradas que el detector vio distinto (parpadeo perdido o falso)

    def simulator(self, tick0_t: float, period_s: float) -> SceneSimulator:
        """Escena (jugador + Pokémon) en tiempo de vídeo, justo tras la última tirada observada."""
        model = SceneModel(npc=0, pokemon_npc=1, period_s=period_s)
        last_t = tick0_t + (self.ticks - 1) * period_s
        sim = SceneSimulator.start(self.state, model, start_time_s=last_t, start_advance=self.end_advance,
                                   first_roll_s=last_t + period_s, schedule_pokemon_now=False)
        # El parpadeo del Pokémon ya está calculado: se programa a mano en vez de tirar uno nuevo al empezar.
        sim._push(tick0_t + self.pokemon_next_s, 1, "pokemon0")
        return sim


def _simulate(values: Sequence[int], start: int, first_pk_s: float, period_s: float,
              observed: dict[int, TickObservation], ticks: int, max_bad: int) -> tuple[int, float, int, int] | None:
    """Tiradas 0..ticks-1 desde ``start`` con el Pokémon parpadeando por primera vez en ``first_pk_s``.
    Devuelve (llamadas consumidas, siguiente parpadeo del Pokémon, parpadeos del Pokémon, discrepancias) o ``None``
    si hay más de ``max_bad`` tiradas que contradicen lo observado (parpadeo perdido o falso del detector)."""
    pos, pk_t, pk_blinks, bad = start, first_pk_s, 0, 0
    for roll in range(ticks):
        tick_t = roll * period_s
        # Los parpadeos del Pokémon anteriores a esta tirada consumen cada uno una salida, que además fija cuándo
        # será el siguiente.
        while pk_t < tick_t:
            pk_t += pokemon_blink_interval(values[pos])
            pos += 1
            pk_blinks += 1
        value = values[pos]
        pos += 1
        obs = observed.get(roll)
        if obs is None:
            continue
        blink = is_player_blink(value)
        # El tipo solo se exige si se vio doble: un doble visto como simple es un fallo típico del detector.
        wrong = obs.blink != blink or (
            blink and obs.kind is not None and int(obs.kind) == 1 and player_blink_kind(value) != obs.kind)
        if wrong:
            bad += 1
            if bad > max_bad or roll == 0:  # la tirada 0 es el ancla: tiene que cuadrar siempre
                return None
    return pos - start, pk_t, pk_blinks, bad


def reidentify_with_pokemon(base: XorshiftState, observations: Sequence[TickObservation], period_s: float,
                            search_min: int, search_max: int, max_bad: int = 2) -> PokemonReidentified:
    """Busca en ``[search_min, search_max)`` la tirada 0 del jugador y el instante del primer parpadeo del Pokémon
    que mejor explican las tiradas observadas (``observations`` debe cubrir cada tirada: parpadee o no).

    Se admiten hasta ``max_bad`` discrepancias (el detector pierde o inventa algún parpadeo en minutos de
    observación), pero la mejor alineación tiene que destacar: si otra que lleva a un estado distinto tiene las
    mismas discrepancias, ``RecoveryError`` (observar más). También si no hay ninguna.
    """
    obs = {o.roll: o for o in observations}
    if not obs or min(obs) != 0 or not obs[0].blink:
        raise ValueError(t("La tirada 0 debe ser un parpadeo observado", "Roll 0 must be an observed blink"))
    ticks = max(obs) + 1
    if sum(1 for o in obs.values() if o.blink) < 4:
        raise RecoveryError(t("Muy pocos parpadeos para reidentificar con el Pokémon en escena",
                              "Too few blinks to reidentify with the Pokémon in the scene"))
    search_min = max(0, int(search_min))
    search_max = max(search_min + 1, int(search_max))
    rng = Xorshift128.from_state(base)
    rng.skip(search_min)
    # Cada tirada consume 1 y cada parpadeo del Pokémon otro (como mucho uno cada 3,285 s).
    values = rng.outputs(search_max - search_min + ticks * 2 + 16)
    phases = [i * PHASE_STEP_S for i in range(1, int(POKEMON_FIRST_MAX_S / PHASE_STEP_S) + 1)]
    first_kind = obs[0].kind
    found: dict[tuple[int, int], list[tuple[int, float, float, int]]] = {}  # clave → [(malas, 1.º, sig., pk)]
    # Fuerza bruta: cada posible tirada 0 (filtrada por el primer parpadeo) × cada instante del primer parpadeo del
    # Pokémon. Las alineaciones se agrupan por (inicio, llamadas consumidas), que determina el estado final.
    for offset in range(search_max - search_min):
        v = values[offset]
        if not is_player_blink(v) or (first_kind is not None and int(first_kind) == 1 and player_blink_kind(v) != first_kind):
            continue
        for first_pk in phases:
            res = _simulate(values, offset, first_pk, period_s, obs, ticks, max_bad)
            if res is not None:
                used, pk_next, pk_blinks, bad = res
                found.setdefault((offset, used), []).append((bad, first_pk, pk_next, pk_blinks))
    if not found:
        raise RecoveryError(t("No se encontró el patrón de parpadeos (con el Pokémon) en el rango de búsqueda",
                              "Blink pattern (with the Pokémon) not found in the search range"))
    best_bad = {key: min(b for b, _, _, _ in valid) for key, valid in found.items()}
    ranked = sorted(best_bad.items(), key=lambda kv: kv[1])
    (key, bad), rest = ranked[0], ranked[1:]
    if rest and rest[0][1] <= bad:
        raise AmbiguousPattern(t("Patrón ambiguo con el Pokémon en escena ({n} alineaciones con {bad} "
                                 "discrepancias o menos): observa más parpadeos",
                                 "Ambiguous pattern with the Pokémon in the scene ({n} alignments with {bad} "
                                 "mismatches or fewer): observe more blinks", n=len(ranked), bad=bad))
    offset, used = key
    valid = [x for x in found[key] if x[0] == bad]
    # Siguiente parpadeo del Pokémon: el de la alineación válida central (todas explican lo observado).
    _, _, pk_next, pk_blinks = valid[len(valid) // 2]
    nexts = [n for _, _, n, _ in valid]
    first = search_min + offset
    end = Xorshift128.from_state(base)
    end.advance(first + used)
    return PokemonReidentified(first_advance=first + 1, end_advance=first + used, state=end.state,
                               pokemon_next_s=pk_next, pokemon_window_s=(min(nexts), max(nexts)),
                               pokemon_blinks=pk_blinks, ticks=ticks, mismatches=bad)


def certain_until(sim: SceneSimulator, tick0_t: float, res: PokemonReidentified, until_s: float,
                  margin_s: float = 0.08) -> float:
    """Hasta qué instante se puede fiar de la escena predicha: el primer parpadeo del Pokémon que cae a menos de
    la incertidumbre de su instante (+ ``margin_s``) de una tirada del jugador podría ir antes o después y
    cambiaría todo lo siguiente. ``sim`` se consume (pasar una copia)."""
    lo, hi = res.pokemon_window_s
    slack = (hi - lo) / 2 + margin_s
    period = sim.model.period_s
    while (t := sim.peek_time()) is not None and t <= until_s:
        for e in sim.step():
            if e.actor != PLAYER:
                phase = (e.time_s - tick0_t) % period
                if min(phase, period - phase) < slack:
                    return e.time_s - slack
    return until_s


def ticks_from_blinks(blink_times: Sequence[float], kinds: Sequence[BlinkKind | None], tick0_t: float,
                      period_s: float, until_t: float) -> list[TickObservation]:
    """Todas las tiradas desde la del primer parpadeo hasta ``until_t``: parpadeos donde se vieron y el resto
    «sin parpadeo». Cada parpadeo se asigna a la tirada más cercana según ``tick0_t`` y ``period_s``."""
    if not blink_times:
        raise RecoveryError(t("Sin parpadeos para reidentificar", "No blinks to reidentify"))
    marks: dict[int, BlinkKind | None] = {}
    for t_blink, k in zip(blink_times, kinds):
        marks[round((t_blink - tick0_t) / period_s)] = k
    last = int((until_t - tick0_t) / period_s)
    return [TickObservation(r, r in marks, marks.get(r)) for r in range(0, max(last, max(marks)) + 1)]


__all__ = ["PokemonReidentified", "TickObservation", "certain_until", "fit_period", "reidentify_with_pokemon",
           "ticks_from_blinks"]


def fit_period(blink_times: Sequence[float], period_guess_s: float) -> tuple[float, float]:
    """Periodo y fase de las tiradas ajustados a los parpadeos observados (mínimos cuadrados sobre la tirada más
    cercana, tres pasadas). Devuelve ``(periodo, instante de la tirada 0)``.

    El periodo medido en la recuperación inicial puede diferir unas milésimas y, en minutos de observación, eso
    basta para ordenar mal los parpadeos del Pokémon respecto a los del jugador."""
    if not blink_times:
        raise RecoveryError(t("Sin parpadeos para ajustar el periodo", "No blinks to fit the period"))
    t0 = blink_times[0]
    period = period_guess_s
    for _ in range(3):
        rolls = [round((t - t0) / period) for t in blink_times]
        n = len(rolls)
        mr, mt = sum(rolls) / n, sum(blink_times) / n
        var = sum((r - mr) ** 2 for r in rolls)
        if var <= 0:
            return period, t0
        period = sum((r - mr) * (t - mt) for r, t in zip(rolls, blink_times)) / var
        t0 = mt - period * mr
    return period, t0
