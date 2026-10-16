"""Tipos de observación compartidos entre visión y modelos de juego (sin dependencias)."""
from __future__ import annotations

from enum import IntEnum


class BlinkKind(IntEnum):
    """Parpadeo simple o doble del jugador: el tipo depende de la salida del RNG en esa tirada."""

    SINGLE = 0
    DOUBLE = 1
