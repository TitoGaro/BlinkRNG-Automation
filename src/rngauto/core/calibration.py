"""Calibración: aprender de cada intento (esperado vs obtenido) sin asumir que el delta es fijo.

Funcionamiento:
  * Historial **incierto** (el resultado admite varios advances) y **confirmado**.
  * Votación por moda con radio; ante empate **no** se compromete nada.
  * Estimación robusta (mediana ponderada, no media simple) del delay implícito.
  * Los intentos dudosos entre delays cercanos (p. ej. 87 u 88: la ficha cuadra con dos advances vecinos)
    también cuentan, como «uno de estos». Se elige el delay que menos se aleja de todos los intentos
    (mediana generalizada); a igualdad, el que deja el peor intento más cerca y, después, el más próximo al
    valor inicial. Ej.: 89 exacto + «87 u 88» → 88 u 89 (empatan), según cuál esté más cerca del valor inicial.
  * Un dudoso solo se da por resuelto si un único candidato es el más cercano al valor votado: con «85 u 89»
    y un 87 confirmado sigue dudoso (los dos están a 2) en vez de elegir uno al azar.

Para un intento: el juego generó en ``hit = cuenta_al_pulsar + delay_real`` y planificamos
``target = cuenta_al_pulsar + delay_usado`` ⇒ ``delay_real = delay_usado + (hit − target)``.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from rngauto.i18n import t

# Desplazamientos sucesivos del objetivo mientras haya dudas: apuntar a advances distintos separa hipótesis.
BUMP_SEQUENCE = (0, 1, -1, 2, -2)
MAX_UNCERTAIN = 40  # intentos dudosos que se conservan (los más recientes)


@dataclass
class AttemptRecord:
    """Un intento completo: qué se planificó, qué se observó y cómo encaja con la calibración."""

    attempt_id: str
    encounter: str
    config_version: str
    engine: str
    engine_version: str
    started_at: str  # hora de pared ISO-8601
    target_advance: int
    final_delay_used: int
    lead_s_used: float = 0.0
    press_margin_s: float | None = None  # margen del hueco en el que se pulsó
    plan: dict[str, Any] = field(default_factory=dict)
    observation: dict[str, Any] = field(default_factory=dict)
    candidates: list[int] = field(default_factory=list)  # advances compatibles con lo observado
    resolved: int | None = None
    outcome: str = "pending"  # hit | miss | no_match | ambiguous | error | aborted
    notes: str = ""

    @property
    def delta(self) -> int | None:
        """Advance obtenido menos el objetivo; ``None`` mientras no esté resuelto."""
        return None if self.resolved is None else self.resolved - self.target_advance

    def implied_delay(self, candidate: int) -> int:
        """Delay real que explicaría haber obtenido ``candidate`` (ver la fórmula del módulo)."""
        return self.final_delay_used + (candidate - self.target_advance)

    @property
    def weight(self) -> float:
        """Intentos pulsados con poco margen pesan menos (el delta puede deberse a la latencia).

        Peso lineal hasta 0,15 s de margen, con un mínimo de 0,2 para no descartarlos del todo."""
        if self.press_margin_s is None:
            return 1.0
        return max(0.2, min(1.0, self.press_margin_s / 0.15))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AttemptRecord":
        known = {f.name for f in fields(cls)}  # un campo de una versión más nueva no invalida el historial
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True, slots=True)
class DelayEstimate:
    """Delay final recomendado y lo fiable que es."""

    value: int
    samples: int
    spread: float  # distancia media ponderada de los intentos al delay elegido (advances)
    source: str  # "prior" | "historial"
    doubtful: int = 0  # de ``samples``, cuántos admitían varios delays vecinos

    def describe(self) -> str:
        """Resumen legible para la interfaz."""
        if self.source == "prior":
            return t("delay {value} (valor inicial, sin calibrar)", "delay {value} (initial value, not calibrated)",
                     value=self.value)
        if self.samples == 1:
            n = t("{n} intento", "{n} attempt", n=self.samples)
        else:
            n = t("{n} intentos", "{n} attempts", n=self.samples)
        if self.doubtful == 1:
            n += t(", {n} dudoso", ", {n} uncertain", n=self.doubtful)
        elif self.doubtful:
            n += t(", {n} dudosos", ", {n} uncertain", n=self.doubtful)
        return t("delay {value} ({n}; dispersión ±{spread:.1f})", "delay {value} ({n}; spread ±{spread:.1f})",
                 value=self.value, n=n, spread=self.spread)


@dataclass
class CalibrationHistory:
    """Historial de calibración de un encuentro: intentos confirmados (acotados) y dudosos pendientes."""

    encounter: str
    prior_final_delay: int
    max_confirmed: int = 10
    vote_radius: int = 2
    confirmed: deque[AttemptRecord] = field(default_factory=deque)
    uncertain: list[AttemptRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.confirmed = deque(self.confirmed, maxlen=self.max_confirmed)

    # --- Registro ------------------------------------------------------------------------------
    def record(self, attempt: AttemptRecord) -> str:
        """Incorpora un intento. Devuelve ``"confirmed"``, ``"uncertain"`` o ``"no_match"``."""
        if not attempt.candidates:
            attempt.outcome = "no_match"
            return "no_match"
        implied = {attempt.implied_delay(c) for c in attempt.candidates}
        if len(implied) == 1:
            attempt.resolved = attempt.candidates[0] if len(attempt.candidates) == 1 else min(
                attempt.candidates, key=lambda c: abs(c - attempt.target_advance)
            )
            self._confirm(attempt)
            self._resolve_uncertain(next(iter(implied)))
            return "confirmed"
        attempt.outcome = "ambiguous"
        self.uncertain.append(attempt)
        # Con tope: votar es cuadrático y, tras decenas de dudas sin resolver, las más antiguas ya no aportan.
        del self.uncertain[:-MAX_UNCERTAIN]
        mode = self._vote()
        if mode is not None:
            self._resolve_uncertain(mode)
            return "confirmed" if attempt.resolved is not None else "uncertain"
        return "uncertain"

    def _confirm(self, attempt: AttemptRecord) -> None:
        """Marca el intento como acierto o fallo y lo pasa al historial confirmado."""
        attempt.outcome = "hit" if attempt.delta == 0 else "miss"
        self.confirmed.append(attempt)

    def _vote(self) -> int | None:
        """Moda de delays implícitos sobre intentos inciertos + confirmados; ``None`` si hay empate."""
        pool_candidates = {d for a in self.uncertain for d in (a.implied_delay(c) for c in a.candidates)}
        if not pool_candidates:
            return None
        anchors = [a.final_delay_used + (a.delta or 0) for a in self.confirmed]
        # Primero coincidencia exacta (desempata vecinos adyacentes indistinguibles); después, con radio
        # ``vote_radius`` para tolerar el jitter de la pulsación entre intentos.
        for radius in (0, self.vote_radius):
            votes: dict[int, float] = {}
            for value in pool_candidates:
                v = sum(
                    1 for a in self.uncertain
                    if any(abs(a.implied_delay(c) - value) <= radius for c in a.candidates)
                )
                v += sum(1 for d in anchors if abs(d - value) <= radius)
                votes[value] = v
            best = max(votes.values())
            winners = [k for k, v in votes.items() if v == best]
            if len(winners) == 1 and best >= 2:
                return winners[0]
        return None

    def _resolve_uncertain(self, mode: int) -> None:
        """Confirma los dudosos con un único candidato más cercano a ``mode`` (dentro del radio)."""
        still: list[AttemptRecord] = []
        for a in self.uncertain:
            best = min(a.candidates, key=lambda c: abs(a.implied_delay(c) - mode))
            best_dist = abs(a.implied_delay(best) - mode)
            tied = {a.implied_delay(c) for c in a.candidates if abs(a.implied_delay(c) - mode) == best_dist}
            if best_dist <= self.vote_radius and len(tied) == 1:
                a.resolved = best
                self._confirm(a)
            else:
                still.append(a)
        self.uncertain = still

    # --- Estimación ----------------------------------------------------------------------------
    def estimate_final_delay(self) -> DelayEstimate:
        """Delay que minimiza la distancia ponderada a todos los intentos; el inicial si no hay ninguno.

        Cada intento aporta su conjunto de delays posibles (uno si está confirmado) y la distancia a él es la del
        más cercano. Los dudosos con candidatos muy separados se dejan para la votación."""
        obs: list[tuple[list[int], float]] = [([a.implied_delay(a.resolved)], a.weight) for a in self.confirmed]
        doubtful = 0
        for a in self.uncertain:
            implied = sorted({a.implied_delay(c) for c in a.candidates})
            if implied and implied[-1] - implied[0] <= self.vote_radius:  # dudas lejanas esperan a la votación
                obs.append((implied, a.weight))
                doubtful += 1
        if not obs:
            return DelayEstimate(self.prior_final_delay, 0, 0.0, "prior")

        def dist(d: int, values: list[int]) -> int:
            return min(abs(d - v) for v in values)

        lo = min(v for values, _ in obs for v in values)
        hi = max(v for values, _ in obs for v in values)
        best = min(range(lo, hi + 1), key=lambda d: (
            # Redondeo: evita que el error de coma flotante de los pesos rompa empates reales.
            round(sum(w * dist(d, values) for values, w in obs), 9),  # mediana generalizada
            max(dist(d, values) for values, _ in obs),  # el peor intento, lo más cerca posible
            abs(d - self.prior_final_delay),
        ))
        total = sum(w for _, w in obs)
        spread = sum(w * dist(best, values) for values, w in obs) / total  # distancia media a los intentos
        return DelayEstimate(best, len(obs), spread, "historial", doubtful)

    def target_bump(self) -> int:
        """Desplazamiento sugerido del objetivo para desempatar hipótesis.

        Mientras haya dudosos, cada intento apunta a un advance algo distinto: así las hipótesis vecinas
        predicen resultados diferentes y el siguiente intento las separa."""
        return BUMP_SEQUENCE[len(self.uncertain) % len(BUMP_SEQUENCE)]

    # --- Persistencia --------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Formato JSON persistido (``schema_version`` 1)."""
        return {
            "schema_version": 1,
            "encounter": self.encounter,
            "prior_final_delay": self.prior_final_delay,
            "max_confirmed": self.max_confirmed,
            "vote_radius": self.vote_radius,
            "confirmed": [a.to_dict() for a in self.confirmed],
            "uncertain": [a.to_dict() for a in self.uncertain],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationHistory":
        return cls(
            encounter=data["encounter"],
            prior_final_delay=data["prior_final_delay"],
            max_confirmed=data.get("max_confirmed", 10),
            vote_radius=data.get("vote_radius", 2),
            confirmed=deque(AttemptRecord.from_dict(a) for a in data.get("confirmed", [])),
            uncertain=[AttemptRecord.from_dict(a) for a in data.get("uncertain", [])],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # escritura atómica: no perder la calibración si se corta

    @classmethod
    def load(cls, path: Path, encounter: str, prior_final_delay: int) -> "CalibrationHistory":
        """Carga el historial o empieza uno vacío si no existe o está dañado."""
        if not path.exists():
            return cls(encounter, prior_final_delay)
        try:
            history = cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError, AttributeError):
            # Dañado (p. ej. disco lleno al guardar): se aparta para poder revisarlo y se empieza de cero.
            try:
                path.replace(path.with_suffix(path.suffix + ".bad"))
            except OSError:  # bloqueado (antivirus, OneDrive…): se ignora y se empieza de cero igualmente
                pass
            return cls(encounter, prior_final_delay)
        # El delay inicial lo manda la configuración: el guardado puede venir de una versión anterior del TOML
        # con otro valor, y conservarlo arrastraría un delay ajeno al encuentro.
        history.prior_final_delay = prior_final_delay
        return history
