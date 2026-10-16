"""Transporte de bytes entre el PC y el ESP32: puerto serie real con pyserial.

El resto de la capa de control (``RscEndpoint``, ``ESP32Controller``) solo depende del protocolo ``Transport``;
los tests le inyectan tuberías en memoria (``tests/control/fakes.py``) en lugar de ``SerialTransport``.
"""
from __future__ import annotations

import threading
import time
from typing import Protocol


class Transport(Protocol):
    """Canal de bytes bidireccional. ``read`` devuelve ``b""`` si vence el plazo sin datos."""

    def write(self, data: bytes) -> None: ...
    def read(self, max_bytes: int, timeout_s: float) -> bytes: ...
    def close(self) -> None: ...


class SerialTransport:
    """Puerto serie 8N1 sin control de flujo, el formato que espera el firmware PABotBase2.

    DTR=1 y RTS=0 se fijan **antes** de abrir: en placas ESP32 con auto-reset, DTR=0/RTS=1 reinicia la
    placa (y un reinicio pierde el emparejamiento con la Switch).
    """

    def __init__(self, port: str, baudrate: int = 921600) -> None:
        # Import diferido: pyserial solo hace falta con hardware real (los tests y la simulación no lo cargan).
        import serial  # pyserial

        ser = serial.Serial()
        ser.port = port
        ser.baudrate = baudrate
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        # Lectura no bloqueante (``read`` hace su propio sondeo con plazo) y escritura con plazo corto para
        # que un puerto atascado no cuelgue el hilo que envía.
        ser.timeout = 0
        ser.write_timeout = 0.1
        ser.dtr = True
        ser.rts = False
        ser.open()
        self._ser = ser
        # Escriben varios hilos (el que envía comandos, el de E/S de RSC con acks y retransmisiones, la
        # cancelación de emergencia): cada ``write`` debe salir entero, sin intercalarse con otro.
        self._lock = threading.Lock()

    @property
    def baudrate(self) -> int:
        return self._ser.baudrate

    def set_baudrate(self, baudrate: int) -> None:
        """Cambia la velocidad sin cerrar el puerto (``ESP32Controller.open_serial`` alterna baudios así)."""
        self._ser.baudrate = baudrate

    def write(self, data: bytes) -> None:
        with self._lock:
            self._ser.write(data)

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        """Devuelve lo que haya en el búfer (hasta ``max_bytes``) o ``b""`` si pasa ``timeout_s`` sin datos.

        Sondea cada milisegundo en vez de usar el ``timeout`` de pyserial para poder devolver en cuanto llega
        algo, sin esperar a completar ``max_bytes``.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            waiting = self._ser.in_waiting
            if waiting:
                return self._ser.read(min(waiting, max_bytes))
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.001)

    def close(self) -> None:
        # RTS=0 antes de cerrar: con RTS=1 las placas con auto-reset se reinician y pierden el emparejamiento.
        try:
            self._ser.rts = False
        finally:
            self._ser.close()
