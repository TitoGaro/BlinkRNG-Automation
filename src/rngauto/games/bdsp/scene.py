"""Simulación determinista de cómo avanza el RNG de BDSP en una escena.

Una escena se describe por cuántos actores consumen RNG y cómo:
  * jugador + ``npc`` humanos: una tirada cada ``period_s`` (en bloque, jugador en ``player_phase``);
  * ``pokemon_npc`` modelos de Pokémon: una llamada por parpadeo, siguiente a ``rand_float(3,12)+0.285`` s.

Es la base de: (a) el *timeline* (predicción de qué advance habrá en cada instante), (b) el
planificador de pulsaciones y (c) los datos sintéticos para tests y replay sin Switch.

Contabilidad: ``advance`` = nº exacto de llamadas a ``next()`` desde el estado de origen, incluidas la lectura
en blanco y la programación inicial de los parpadeos de Pokémon. Por eso los «delays» publicados que no cuentan
esas llamadas pueden diferir de los de aquí en una constante pequeña.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from rngauto.i18n import t

from .blink import DEFAULT_ROLL_PERIOD_S, BlinkKind, player_blink_kind, pokemon_blink_interval
from .xorshift import Xorshift128, XorshiftState

PLAYER = "player"


@dataclass(frozen=True, slots=True)
class SceneModel:
    """Quién consume RNG en la escena: ``npc`` humanos además del jugador y ``pokemon_npc`` Pokémon que parpadean."""

    npc: int = 0
    pokemon_npc: int = 0
    player_rolls: bool = True  # False: el jugador no tira; solo cuentan los NPC
    player_phase: int = 0  # posición del jugador dentro del bloque de tiradas humanas
    period_s: float = DEFAULT_ROLL_PERIOD_S

    def __post_init__(self) -> None:
        if self.npc < 0 or self.pokemon_npc < 0:
            raise ValueError(t("npc y pokemon_npc deben ser >= 0", "npc and pokemon_npc must be >= 0"))
        if not 0 <= self.player_phase <= self.npc:
            raise ValueError(t("player_phase debe estar en [0, npc]", "player_phase must be in [0, npc]"))
        if self.period_s <= 0:
            raise ValueError(t("period_s debe ser positivo", "period_s must be positive"))

    @property
    def human_rollers(self) -> int:
        """Llamadas que consume cada bloque de tiradas humanas (NPC más el jugador si tira)."""
        return self.npc + (1 if self.player_rolls else 0)


@dataclass(frozen=True, slots=True)
class RngEvent:
    """Una llamada a ``next()``: cuándo, quién la hizo y qué salió."""

    time_s: float
    advance: int  # 1-based: esta llamada deja el RNG en ``advance`` llamadas desde el origen
    actor: str
    output: int

    @property
    def player_blink(self) -> BlinkKind | None:
        """Tipo de parpadeo del jugador si la llamada es suya y parpadea; si no, ``None``."""
        return player_blink_kind(self.output) if self.actor == PLAYER else None


@dataclass(slots=True)
class SceneSimulator:
    """Simulador por eventos (cola de prioridad): cada evento es un bloque de tiradas humanas o un parpadeo de
    Pokémon, y cada uno consume sus llamadas en orden de tiempo."""

    rng: Xorshift128
    model: SceneModel
    time_s: float = 0.0
    advance: int = 0
    next_roll_s: float | None = None
    _queue: list[tuple[float, int, int, str]] = field(default_factory=list)
    _seq: int = 0

    @classmethod
    def start(
        cls,
        state: XorshiftState,
        model: SceneModel,
        *,
        start_time_s: float = 0.0,
        start_advance: int = 0,
        first_roll_s: float | None = None,
        schedule_pokemon_now: bool = True,
    ) -> "SceneSimulator":
        """Crea el simulador. ``start_advance``: llamadas ya consumidas en ``state`` (contabilidad).
        ``first_roll_s``: instante de la primera tirada humana (por defecto un periodo después del
        inicio). Si ``schedule_pokemon_now``, cada Pokémon NPC consume una llamada al inicio para
        programar su primer parpadeo."""
        sim = cls(Xorshift128.from_state(state), model, time_s=start_time_s, advance=start_advance)
        if model.human_rollers:
            sim._push(first_roll_s if first_roll_s is not None else start_time_s + model.period_s, 0, "humans")
        if schedule_pokemon_now:
            for i in range(model.pokemon_npc):
                value = sim._consume()
                sim._push(start_time_s + pokemon_blink_interval(value), 1, f"pokemon{i}")
        return sim

    def _push(self, when: float, priority: int, tag: str) -> None:
        """Programa un evento. A igual instante va antes la menor ``priority`` (humanos 0, Pokémon 1) y, a igual
        prioridad, el orden de llegada (``_seq``): así la simulación es determinista."""
        heapq.heappush(self._queue, (when, priority, self._seq, tag))
        self._seq += 1

    def _consume(self) -> int:
        """Una llamada a ``next()``, contada en ``advance``."""
        self.advance += 1
        return self.rng.next()

    @property
    def state(self) -> XorshiftState:
        return self.rng.state

    def peek_time(self) -> float | None:
        """Instante del siguiente evento, o ``None`` si no queda ninguno."""
        return self._queue[0][0] if self._queue else None

    def step(self) -> list[RngEvent]:
        """Procesa el siguiente instante con eventos y devuelve las llamadas consumidas."""
        when, priority, _, tag = heapq.heappop(self._queue)
        self.time_s = when
        events: list[RngEvent] = []
        if tag == "humans":
            actors = [f"npc{i}" for i in range(self.model.npc)]
            if self.model.player_rolls:
                actors.insert(self.model.player_phase, PLAYER)
            for actor in actors:
                value = self._consume()
                events.append(RngEvent(when, self.advance, actor, value))
            self._push(when + self.model.period_s, 0, "humans")
        else:
            value = self._consume()
            events.append(RngEvent(when, self.advance, tag, value))
            self._push(when + pokemon_blink_interval(value), 1, tag)
        return events

    def run_until(self, time_s: float) -> list[RngEvent]:
        """Procesa todos los eventos hasta ``time_s`` (incluido) y devuelve las llamadas consumidas."""
        events: list[RngEvent] = []
        while self._queue and self._queue[0][0] <= time_s:
            events.extend(self.step())
        self.time_s = max(self.time_s, time_s)
        return events

    def skip(self, calls: int) -> None:
        """Llamadas fijas no ligadas al tiempo (p. ej. *advance delay* de una transición)."""
        for _ in range(calls):
            self._consume()

    def fork(self) -> "SceneSimulator":
        """Copia independiente para simular hacia delante sin alterar este simulador."""
        return SceneSimulator(
            self.rng.copy(), self.model, self.time_s, self.advance, self.next_roll_s,
            list(self._queue), self._seq,
        )
