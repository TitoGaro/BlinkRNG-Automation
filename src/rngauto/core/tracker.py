"""Seguimiento del RNG en el tiempo: ¿en qué advance está el juego ahora y con qué certeza?

``AdvanceClock`` modela un avance por "ticks" periódicos (en BDSP: tiradas de parpadeo cada ~1,02 s,
``calls_per_tick = npc + 1``), con incertidumbre explícita: el error en ticks crece con la distancia al
ancla por el error del periodo estimado.

Convención: ``anchor_t`` es el instante (en tiempo de vídeo) de un tick observado y ``anchor_advance``
las llamadas consumidas justo tras ese tick. Si el jugador no es el último actor del bloque de NPCs,
queda un desfase constante (≤ npc) que absorbe la calibración del ``final_delay``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from rngauto import i18n  # ``i18n.t``: aquí ``t`` es el nombre habitual del instante


@dataclass(frozen=True, slots=True)
class AdvanceClock:
    """Contador de advances extrapolado desde un ancla con periodo constante (inmutable: re-anclar crea otro)."""

    anchor_t: float
    anchor_advance: int
    period_s: float
    calls_per_tick: int = 1
    period_sigma_s: float = 0.0005  # error típico del periodo estimado
    anchor_sigma_s: float = 0.02  # error típico del instante del ancla (≈ 1 frame a 60 fps)

    def __post_init__(self) -> None:
        if self.period_s <= 0 or self.calls_per_tick < 1:
            raise ValueError(i18n.t("Periodo y llamadas por tick deben ser positivos",
                                     "Period and calls per tick must be positive"))

    def ticks_since_anchor(self, t: float) -> float:
        """Ticks (fraccionarios) transcurridos desde el ancla; negativo antes de ella."""
        return (t - self.anchor_t) / self.period_s

    def advance_at(self, t: float) -> int:
        """Llamadas consumidas en el instante ``t`` (un tick se cuenta en cuanto ocurre)."""
        # El épsilon evita que el error de coma flotante deje un tick exacto justo por debajo del entero.
        ticks = math.floor(self.ticks_since_anchor(t) + 1e-9)
        return self.anchor_advance + ticks * self.calls_per_tick

    def time_of_tick(self, tick: int) -> float:
        """Instante del tick ``tick`` relativo al ancla (0 = ancla)."""
        return self.anchor_t + tick * self.period_s

    def time_of_advance(self, advance: int) -> float:
        """Instante del tick a partir del cual ya se han consumido ``advance`` llamadas."""
        ticks = math.ceil((advance - self.anchor_advance) / self.calls_per_tick)
        return self.time_of_tick(ticks)

    def middle_after_advance(self, advance: int) -> float:
        """Centro del intervalo durante el cual el contador vale ``advance`` (máximo margen)."""
        return self.time_of_advance(advance) + self.period_s / 2

    def sigma_ticks(self, t: float) -> float:
        """Error típico de la posición en ticks en ``t``.

        Combina el error del ancla (fijo) y el del periodo (crece con cada tick extrapolado) como
        independientes; por eso conviene re-anclar con frecuencia."""
        n = abs(self.ticks_since_anchor(t))
        return math.hypot(self.anchor_sigma_s, n * self.period_sigma_s) / self.period_s

    def phase_margin_ticks(self, t: float) -> float:
        """Distancia (en ticks) al borde de tick más cercano."""
        frac = self.ticks_since_anchor(t) % 1.0
        return min(frac, 1.0 - frac)

    def count_confidence(self, t: float) -> float:
        """Probabilidad aproximada de que ``advance_at(t)`` sea exacto (modelo gaussiano)."""
        sigma = self.sigma_ticks(t)
        if sigma <= 0:
            return 1.0
        return math.erf(self.phase_margin_ticks(t) / (math.sqrt(2) * sigma))

    def reanchored(self, t: float, advance: int, *, period_s: float | None = None) -> "AdvanceClock":
        """Copia anclada en un tick observado ahora, opcionalmente con un periodo mejor estimado."""
        return replace(self, anchor_t=t, anchor_advance=advance, period_s=period_s or self.period_s)
