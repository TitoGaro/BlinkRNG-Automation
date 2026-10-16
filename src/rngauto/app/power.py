"""Evita que Windows suspenda el equipo o apague la pantalla durante una ejecución (esperas de horas).

En otros sistemas no hace nada.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


@contextmanager
def prevent_sleep():
    """Mientras dura el bloque, el sistema no entra en suspensión ni apaga la pantalla.

    ``SetThreadExecutionState`` actúa por hilo: hay que usarlo en el hilo que ejecuta la espera. Al salir
    se restaura el comportamiento normal aunque haya una excepción.
    """
    if sys.platform != "win32":
        yield
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED)
    try:
        yield
    finally:
        kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
