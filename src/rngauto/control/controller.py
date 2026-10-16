"""Interfaz abstracta de mando: el RNG y el planificador no saben cómo viaja una pulsación.

Modelo (el mismo que usa el firmware PABotBase2): una secuencia de *comandos*, cada uno = «mantener
este estado completo del mando durante N ms». El dispositivo los reproduce con su propio reloj, de
modo que la precisión relativa dentro de una ráfaga encolada no depende del PC.

Implementaciones: ``ESP32Controller`` (hardware real, ``esp32.py``) y ``RecordingController`` (sin
hardware: tests, DRY RUN y replay).
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, IntFlag

from rngauto.i18n import t

MAX_COMMAND_MS = 65535  # la duración viaja como u16 en el mensaje del protocolo


class Button(IntFlag):
    """Botones del Pro Controller como máscara de bits; se combinan con ``|``."""

    NONE = 0
    Y = 1 << 0
    X = 1 << 1
    B = 1 << 2
    A = 1 << 3
    R = 1 << 4
    ZR = 1 << 5
    MINUS = 1 << 6
    PLUS = 1 << 7
    RSTICK = 1 << 8
    LSTICK = 1 << 9
    HOME = 1 << 10
    CAPTURE = 1 << 11
    L = 1 << 12
    ZL = 1 << 13


class DPad(Enum):
    """Posición de la cruceta (las diagonales son posiciones propias, no combinaciones)."""

    NONE = "none"
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    UP_LEFT = "up_left"
    UP_RIGHT = "up_right"
    DOWN_LEFT = "down_left"
    DOWN_RIGHT = "down_right"


@dataclass(frozen=True, slots=True)
class ControllerState:
    """Estado completo del mando. Sticks en [-1, 1]; +y = arriba."""

    buttons: Button = Button.NONE
    dpad: DPad = DPad.NONE
    left: tuple[float, float] = (0.0, 0.0)
    right: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        for x, y in (self.left, self.right):
            if not (-1.0 <= x <= 1.0 and -1.0 <= y <= 1.0):
                raise ValueError(t("Los sticks deben estar en [-1, 1]", "Sticks must be within [-1, 1]"))

    @property
    def is_neutral(self) -> bool:
        return self == NEUTRAL


NEUTRAL = ControllerState()


@dataclass(frozen=True, slots=True)
class Command:
    """Mantener ``state`` durante ``duration_ms``. ``label`` solo sirve para registros y tests."""

    state: ControllerState
    duration_ms: int
    label: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.duration_ms <= MAX_COMMAND_MS:
            raise ValueError(t("Duración fuera de rango (0..{max_ms} ms): {ms}", "Duration out of range (0..{max_ms} ms): {ms}",
                               max_ms=MAX_COMMAND_MS, ms=self.duration_ms))


def split_long(state: ControllerState, duration_ms: int, label: str = "") -> list[Command]:
    """Divide esperas/pulsaciones más largas que ``MAX_COMMAND_MS`` en varios comandos consecutivos.

    Siempre devuelve al menos un comando (aunque ``duration_ms`` sea 0).
    """
    out: list[Command] = []
    remaining = duration_ms
    while remaining > MAX_COMMAND_MS:
        out.append(Command(state, MAX_COMMAND_MS, label))
        remaining -= MAX_COMMAND_MS
    out.append(Command(state, remaining, label))
    return out


class ControllerError(RuntimeError):
    """Fallo del mando (no responde, dispositivo incompatible…)."""


class ControllerNotReady(ControllerError):
    """No hay mando utilizable: sin conexión, enlace roto o detenido por una PARADA."""


@dataclass(frozen=True, slots=True)
class ControllerStatus:
    """Instantánea del estado del mando para la interfaz y las comprobaciones previas a una ejecución."""

    connected: bool  # enlace PC ↔ dispositivo
    console_ready: bool  # el dispositivo está emparejado/conectado a la Switch
    device: str = ""
    firmware: str = ""
    detail: str = ""
    last_ack_age_s: float | None = None


class Controller(ABC):
    """Contrato común de los mandos.

    Las implementaciones deben aceptar ``emergency_stop`` desde otro hilo (el botón PARADA de la GUI) mientras
    el hilo de trabajo está en ``submit`` o ``wait_idle``.
    """

    @abstractmethod
    def connect(self) -> ControllerStatus: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def status(self) -> ControllerStatus: ...

    @abstractmethod
    def submit(self, commands: list[Command]) -> None:
        """Encola comandos (no bloquea salvo si la cola del dispositivo está llena)."""

    @abstractmethod
    def wait_idle(self, timeout_s: float | None = None) -> None:
        """Bloquea hasta que el dispositivo ha terminado todo lo encolado."""

    @abstractmethod
    def emergency_stop(self) -> None:
        """Vacía la cola y deja el mando en neutro inmediatamente. No debe lanzar excepciones."""

    def resume(self) -> None:
        """Vuelve a aceptar pulsaciones tras una PARADA (``emergency_stop``). Por defecto no hace nada."""

    def take_warnings(self) -> list[str]:
        """Incidencias del dispositivo desde la última llamada (comandos descartados, la consola se
        desconectó…), ya redactadas para el registro. Por defecto, ninguna."""
        return []

    def clear_queue(self, timeout_s: float = 1.0) -> bool:
        """Como ``emergency_stop`` pero **síncrono**: al volver, lo que estaba encolado ya no se ejecutará y
        lo que se envíe después no se cancelará. Para empezar una secuencia temporizada (p. ej. reiniciar
        desde HOME) sin que se mezcle con restos de la anterior. Devuelve si consta que se vació."""
        self.emergency_stop()
        return True

    # --- Atajos sobre ``submit`` -----------------------------------------------------------------
    # Todos terminan en neutro (salvo ``hold``): así ningún botón queda pulsado al acabar la secuencia.
    def press(self, buttons: Button, hold_ms: int = 80, release_ms: int = 80, label: str = "") -> None:
        self.submit([Command(ControllerState(buttons), hold_ms, label or buttons.name or "press"),
                     *split_long(NEUTRAL, release_ms, label)])

    def hold(self, buttons: Button, duration_ms: int, label: str = "") -> None:
        self.submit(split_long(ControllerState(buttons), duration_ms, label or f"hold {buttons.name}"))

    def move(self, direction: DPad, duration_ms: int, release_ms: int = 0, label: str = "") -> None:
        self.submit([*split_long(ControllerState(dpad=direction), duration_ms, label or f"move {direction.value}"),
                     *split_long(NEUTRAL, release_ms, label)])

    def release(self) -> None:
        self.submit([Command(NEUTRAL, 0, "release")])

    def wait(self, duration_ms: int, label: str = "wait") -> None:
        self.submit(split_long(NEUTRAL, duration_ms, label))


@dataclass(slots=True)
class SubmittedCommand:
    """Entrada del registro de ``RecordingController``."""

    command: Command
    submitted_at: float  # reloj monótono del host
    scheduled_start: float  # inicio previsto si el dispositivo encadena sin huecos


@dataclass
class RecordingController(Controller):
    """Mando simulado para DRY RUN, replay y tests: registra lo que se enviaría y simula los tiempos.

    No envía nada a ningún hardware. ``clock`` permite inyectar un reloj simulado. Calcula el inicio previsto
    de cada comando como lo haría el dispositivo: encadenados sin huecos tras lo que ya estaba encolado.
    """

    clock: object = time.monotonic
    realtime: bool = False  # si True, ``wait_idle`` duerme hasta que "terminaría" la cola
    log: list[SubmittedCommand] = field(default_factory=list)
    _busy_until: float = 0.0
    _connected: bool = False
    _stopped: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def connect(self) -> ControllerStatus:
        self._connected = True
        return self.status()

    def close(self) -> None:
        self._connected = False

    def status(self) -> ControllerStatus:
        return ControllerStatus(self._connected, self._connected, device="recording", firmware="sim")

    def submit(self, commands: list[Command]) -> None:
        if not self._connected:
            raise ControllerNotReady(t("Mando no conectado", "Controller not connected"))
        with self._lock:
            now = self.clock()
            start = max(now, self._busy_until)
            for cmd in commands:
                self.log.append(SubmittedCommand(cmd, now, start))
                start += cmd.duration_ms / 1000.0
            self._busy_until = start

    def wait_idle(self, timeout_s: float | None = None) -> None:
        if not self.realtime:
            return
        remaining = self._busy_until - self.clock()
        if timeout_s is not None and remaining > timeout_s:
            raise TimeoutError(t("La cola no terminó a tiempo", "The command queue did not finish in time"))
        if remaining > 0:
            time.sleep(remaining)

    def emergency_stop(self) -> None:
        with self._lock:
            self._busy_until = self.clock()
            self._stopped += 1

    @property
    def stops(self) -> int:
        return self._stopped

    def pressed_labels(self) -> list[str]:
        """Etiquetas de los comandos que no son neutro, en orden (para aserciones de tests)."""
        return [c.command.label for c in self.log if not c.command.state.is_neutral]
