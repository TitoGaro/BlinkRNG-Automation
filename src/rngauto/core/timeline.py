"""Timeline: registro ordenado de todo lo relevante (parpadeos, re-anclajes, pulsaciones, decisiones).

Sirve para depurar, reproducir y explicar ("¿por qué pulsó aquí?"). Se serializa a JSONL.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """Un evento del registro; ``data`` admite cualquier detalle serializable a JSON."""

    t_s: float  # reloj monótono de la sesión
    kind: str  # blink, pokemon_blink, recovery, reanchor, plan, input, state, detection, warning, error...
    message: str = ""
    frame: int | None = None
    advance: int | None = None
    confidence: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, line: str) -> "TimelineEvent":
        return cls(**json.loads(line))


Listener = Callable[[TimelineEvent], None]


class Timeline:
    """Registro en memoria, seguro entre hilos, con oyentes (UI, log en disco)."""

    def __init__(self, events: Iterable[TimelineEvent] = ()) -> None:
        self._events: list[TimelineEvent] = list(events)
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        """Registra un oyente; se llama en el hilo que añade cada evento."""
        self._listeners.append(listener)

    def add(self, t_s: float, kind: str, message: str = "", **kwargs: Any) -> TimelineEvent:
        """Añade un evento y avisa a los oyentes (fuera del cerrojo, para que un oyente lento no bloquee a
        los demás hilos que registran)."""
        event = TimelineEvent(t_s, kind, message, **kwargs)
        with self._lock:
            self._events.append(event)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:  # un oyente defectuoso no debe parar la automatización
                pass
        return event

    @property
    def events(self) -> list[TimelineEvent]:
        """Copia de los eventos (se puede recorrer mientras otros hilos siguen añadiendo)."""
        with self._lock:
            return list(self._events)

    def of_kind(self, *kinds: str) -> list[TimelineEvent]:
        return [e for e in self.events if e.kind in kinds]

    def last(self, kind: str) -> TimelineEvent | None:
        with self._lock:
            for e in reversed(self._events):
                if e.kind == kind:
                    return e
        return None

    def save(self, path: Path) -> None:
        """Escribe el registro completo en JSONL (un evento por línea)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for e in self.events:
                f.write(e.to_json() + "\n")

    @classmethod
    def load(cls, path: Path) -> "Timeline":
        with path.open(encoding="utf-8") as f:
            return cls(TimelineEvent.from_json(line) for line in f if line.strip())


class JsonlSink:
    """Oyente que escribe cada evento en disco según llega (no se pierde nada si el proceso cae)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def __call__(self, event: TimelineEvent) -> None:
        with self._lock:
            if self._file.closed:  # una ejecución abandonada tras la PARADA puede seguir emitiendo un poco
                return
            self._file.write(event.to_json() + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()  # cerrar dos veces no hace nada
