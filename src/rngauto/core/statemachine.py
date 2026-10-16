"""Máquina de estados de automatización con timeout, reintentos, recuperación y parada inmediata.

Cada estado es una función ``run(ctx) -> nombre_siguiente | None`` (``None`` = éxito final). Los
estados deben llamar a ``ctx.check()`` / ``ctx.sleep_until()`` en sus bucles: así una parada de
emergencia o un timeout cortan la ejecución sin esperar a que termine una acción larga.
"""
from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rngauto.i18n import t

from .clock import Clock
from .timeline import Timeline


class Stopped(Exception):
    """Parada solicitada (usuario o emergencia)."""


class StateTimeout(Exception):
    """El estado ha superado su ``timeout_s``."""

    pass


class RetryableError(Exception):
    """Fallo transitorio: el estado puede reintentarse tras su recuperación."""


class InterventionRequired(Exception):
    """Incertidumbre significativa: parar y pedir intervención en vez de actuar a ciegas.

    ``recoverable=False``: reiniciar no lo arregla o sería peligroso (sin mando, sin imagen, ha salido shiny…), así
    que nunca se intenta recuperar solo."""

    def __init__(self, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message)
        self.recoverable = recoverable


class Outcome(Enum):
    """Resultado final de ``StateMachine.run``."""

    SUCCESS = "success"
    STOPPED = "stopped"
    FAILED = "failed"
    INTERVENTION = "intervention"


@dataclass
class StateContext:
    """Lo que recibe cada estado: reloj, registro, datos compartidos y el plazo del intento actual."""

    clock: Clock
    timeline: Timeline
    stop_event: threading.Event
    data: dict[str, Any]
    state: str = ""
    attempt: int = 0
    deadline: float | None = None

    def now(self) -> float:
        return self.clock.now()

    def check(self) -> None:
        """Lanza ``Stopped`` si se pidió parar o ``StateTimeout`` si se pasó el plazo."""
        if self.stop_event.is_set():
            raise Stopped()
        if self.deadline is not None and self.clock.now() > self.deadline:
            raise self._timeout()

    def _timeout(self) -> StateTimeout:
        return StateTimeout(t("Timeout en el estado {state}", "Timeout in state {state}", state=self.state))

    def sleep_until(self, t: float) -> None:  # aquí ``t`` es el instante, no la función de traducción
        """Espera interrumpible hasta ``t``.

        Si ``t`` cae después del plazo, solo se espera hasta el plazo y se lanza el timeout: no tiene sentido
        dormir más allá para fallar igualmente."""
        if self.deadline is not None and t > self.deadline:
            t_end = self.deadline
            if not self.clock.sleep_until(t_end, self.stop_event):
                raise Stopped()
            raise self._timeout()
        if not self.clock.sleep_until(t, self.stop_event):
            raise Stopped()
        self.check()

    def log(self, kind: str, message: str, **kwargs: Any) -> None:
        """Añade un evento al timeline etiquetado con el estado actual."""
        self.timeline.add(self.clock.now(), kind, message, data={"state": self.state, **kwargs.pop("data", {})}, **kwargs)


@dataclass(frozen=True)
class StateSpec:
    """Definición de un estado: función, plazo, reintentos y cómo recuperarse antes de reintentar."""

    name: str
    run: Callable[[StateContext], str | None]
    timeout_s: float | None = None
    max_retries: int = 0
    recover: Callable[[StateContext, Exception], None] | None = None
    on_timeout: str | None = None  # estado al que saltar si se agota el tiempo (si no, fallo)


@dataclass(frozen=True)
class Transition:
    """Paso por un estado: cuándo entró, cuándo salió y con qué resultado."""

    state: str
    t_enter: float
    t_exit: float
    result: str


@dataclass
class RunResult:
    """Resultado de una ejecución completa con el historial de transiciones."""

    outcome: Outcome
    last_state: str
    message: str = ""
    transitions: list[Transition] = field(default_factory=list)


class StateMachine:
    """Ejecuta estados en secuencia hasta el éxito, una parada o un fallo sin recuperación."""

    def __init__(
        self,
        states: list[StateSpec],
        initial: str,
        clock: Clock,
        timeline: Timeline | None = None,
        data: dict[str, Any] | None = None,
        on_stop: list[Callable[[], None]] | None = None,
        on_failure: Callable[[str, Exception], str | None] | None = None,
    ) -> None:
        """``on_failure(estado, error)``: ante un fallo que pararía la ejecución (intervención, error inesperado o
        reintentos agotados) puede devolver el estado con el que seguir (p. ej. reiniciar el juego) o ``None``
        (parar). ``on_stop``: ganchos que ``stop`` ejecuta al instante, desde el hilo que pide la parada."""
        self.states = {s.name: s for s in states}
        if initial not in self.states:
            raise ValueError(t("Estado inicial desconocido: {initial}", "Unknown initial state: {initial}", initial=initial))
        self.initial = initial
        self.clock = clock
        self.timeline = timeline or Timeline()
        self.data = data if data is not None else {}
        self.stop_event = threading.Event()
        self._on_stop = list(on_stop or [])
        self._on_failure = on_failure
        self.current: str | None = None

    def stop(self, reason: str | None = None) -> None:
        """Parada inmediata (segura desde otro hilo): activa el evento y ejecuta los ganchos
        (p. ej. ``controller.emergency_stop``)."""
        if reason is None:
            reason = t("Parada solicitada", "Stop requested")
        self.stop_event.set()
        self.timeline.add(self.clock.now(), "state", reason, data={"state": self.current})
        for hook in self._on_stop:
            # Un gancho que falla no debe impedir que se ejecuten los demás (p. ej. soltar los botones).
            try:
                hook()
            except Exception:
                pass

    def run(self, max_transitions: int = 100_000) -> RunResult:
        """Ejecuta la máquina desde el estado inicial.

        Por cada estado: ``Stopped`` termina en el acto; ``StateTimeout`` salta a ``on_timeout`` o reintenta;
        ``RetryableError`` reintenta tras ``recover``; el resto (y los reintentos agotados) pasa por
        ``on_failure`` antes de darse por fallido. ``max_transitions`` corta bucles infinitos."""
        transitions: list[Transition] = []
        name = self.initial
        for _ in range(max_transitions):
            spec = self.states.get(name)
            if spec is None:
                return RunResult(Outcome.FAILED, name, t("Transición a estado desconocido: {name}", "Transition to unknown state: {name}", name=name), transitions)
            self.current = name
            attempt = 0
            while True:
                ctx = StateContext(self.clock, self.timeline, self.stop_event, self.data, name, attempt)
                t_enter = self.clock.now()
                if spec.timeout_s is not None:
                    ctx.deadline = t_enter + spec.timeout_s
                self.timeline.add(t_enter, "state", f"→ {name}" + (t(" (reintento {attempt})", " (retry {attempt})", attempt=attempt) if attempt else ""))
                try:
                    ctx.check()
                    nxt = spec.run(ctx)
                    transitions.append(Transition(name, t_enter, self.clock.now(), nxt or "SUCCESS"))
                    break
                except Stopped:
                    transitions.append(Transition(name, t_enter, self.clock.now(), "STOPPED"))
                    return RunResult(Outcome.STOPPED, name, t("Detenido", "Stopped"), transitions)
                except InterventionRequired as exc:
                    transitions.append(Transition(name, t_enter, self.clock.now(), "INTERVENTION"))
                    if (nxt := self._fallback(name, exc)) is not None:
                        break
                    self.timeline.add(self.clock.now(), "error", t("Necesito intervención: {exc}", "Intervention required: {exc}", exc=exc), data={"state": name})
                    return RunResult(Outcome.INTERVENTION, name, str(exc), transitions)
                except StateTimeout as exc:
                    transitions.append(Transition(name, t_enter, self.clock.now(), "TIMEOUT"))
                    if spec.on_timeout:
                        nxt = spec.on_timeout
                        self.timeline.add(self.clock.now(), "warning", f"{exc} → {nxt}")
                        break
                    if attempt < spec.max_retries:
                        attempt += 1
                        self._recover(spec, ctx, exc)
                        continue
                    if (nxt := self._fallback(name, exc)) is not None:
                        break
                    return RunResult(Outcome.FAILED, name, str(exc), transitions)
                except RetryableError as exc:
                    transitions.append(Transition(name, t_enter, self.clock.now(), "RETRY"))
                    if attempt < spec.max_retries:
                        attempt += 1
                        self._recover(spec, ctx, exc)
                        continue
                    if (nxt := self._fallback(name, exc)) is not None:
                        break
                    return RunResult(Outcome.FAILED, name, t("Agotados los reintentos: {exc}", "Retries exhausted: {exc}", exc=exc), transitions)
                except Exception as exc:  # error inesperado: fallo seguro
                    if self.stop_event.is_set():  # p. ej. «Mando detenido»: es la PARADA, no un fallo
                        transitions.append(Transition(name, t_enter, self.clock.now(), "STOPPED"))
                        return RunResult(Outcome.STOPPED, name, t("Detenido", "Stopped"), transitions)
                    transitions.append(Transition(name, t_enter, self.clock.now(), "ERROR"))
                    self.timeline.add(self.clock.now(), "error", t("Error en {name}: {exc}", "Error in {name}: {exc}", name=name, exc=exc),
                                      data={"trace": traceback.format_exc(limit=5)})
                    if (nxt := self._fallback(name, exc)) is not None:
                        break
                    return RunResult(Outcome.FAILED, name, str(exc), transitions)
            if nxt is None:
                return RunResult(Outcome.SUCCESS, name, t("Completado", "Completed"), transitions)
            name = nxt
        return RunResult(Outcome.FAILED, name, t("Demasiadas transiciones (¿bucle?)", "Too many transitions (loop?)"), transitions)

    def _fallback(self, name: str, exc: Exception) -> str | None:
        """Estado de recuperación que propone ``on_failure``; ``None`` si no hay, se está parando o no existe."""
        if self._on_failure is None or self.stop_event.is_set():
            return None
        try:
            nxt = self._on_failure(name, exc)
        except Exception as hook_exc:  # el gancho nunca debe ocultar el fallo original
            self.timeline.add(self.clock.now(), "error", t("Fallo al intentar recuperarse: {exc}", "Recovery attempt failed: {exc}", exc=hook_exc))
            return None
        if nxt is not None and nxt not in self.states:
            return None
        return nxt

    def _recover(self, spec: StateSpec, ctx: StateContext, exc: Exception) -> None:
        """Registra el reintento y ejecuta la recuperación propia del estado, si la tiene."""
        self.timeline.add(self.clock.now(), "warning", t("Reintentando {name}: {exc}", "Retrying {name}: {exc}", name=spec.name, exc=exc))
        if spec.recover is not None:
            spec.recover(ctx, exc)
