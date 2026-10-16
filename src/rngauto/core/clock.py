"""Relojes inyectables: el mismo código corre en tiempo real, simulado (tests) o en replay."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

from rngauto import i18n  # ``i18n.t``: aquí ``t`` es el nombre habitual del instante

# Últimos milisegundos en espera activa: ``time.sleep`` en Windows tiene ~1-15 ms de granularidad.
_SPIN_S = 0.002
# Trozo máximo de ``time.sleep``: acota cuánto tarda en notarse una cancelación.
_MAX_SLEEP_SLICE_S = 0.05


class Clock(Protocol):
    """Interfaz común de los relojes."""

    def now(self) -> float:
        """Segundos monótonos (misma base que los timestamps de frames y comandos)."""

    def sleep_until(self, t: float, cancel: threading.Event | None = None) -> bool:
        """Espera hasta ``t``. Devuelve ``False`` si se canceló antes."""


class MonotonicClock:
    """Reloj real basado en ``time.perf_counter`` (monótono, alta resolución)."""

    def now(self) -> float:
        return time.perf_counter()

    def sleep_until(self, t: float, cancel: threading.Event | None = None) -> bool:
        while True:
            if cancel is not None and cancel.is_set():
                return False
            remaining = t - time.perf_counter()
            if remaining <= 0:
                return True
            if remaining > _SPIN_S:
                time.sleep(min(remaining - _SPIN_S, _MAX_SLEEP_SLICE_S))
            # Si queda menos de ``_SPIN_S``, el bucle sigue sin dormir: espera activa hasta ``t``.


@dataclass
class SimulatedClock:
    """Reloj manual para tests y DRY RUN acelerado."""

    t: float = 0.0

    def now(self) -> float:
        return self.t

    def sleep_until(self, t: float, cancel: threading.Event | None = None) -> bool:
        """Salta directamente a ``t`` (nunca hacia atrás)."""
        if cancel is not None and cancel.is_set():
            return False
        self.t = max(self.t, t)
        return True

    def advance(self, dt: float) -> None:
        """Adelanta el reloj ``dt`` segundos."""
        if dt < 0:
            raise ValueError(i18n.t("El reloj simulado no retrocede", "The simulated clock cannot go backwards"))
        self.t += dt
