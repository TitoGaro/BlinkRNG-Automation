"""Comparación de pantallas completas: ¿ha cambiado la imagen? ¿es la pantalla que esperamos?

Se trabaja con miniaturas en gris (160×90, suavizadas), robustas al ruido de la capturadora:

* ``changed_fraction``: fracción de píxeles que han cambiado de verdad. Un parpadeo cambia muy pocos;
  abrir un menú, muchos. Sirve para medir latencia y para comprobar que el juego volvió a su estado.
* ``similarity``: correlación normalizada entre dos miniaturas (1 = idénticas). Sirve para reconocer una
  pantalla conocida (p. ej. la Pokédex) a partir de una «firma» guardada.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .frames import Frame, NormBox
from .imageio import imread

THUMB_SIZE = (160, 90)
PIXEL_LEVEL = 24.0  # diferencia de gris para considerar que un píxel ha cambiado


def thumbnail(frame: Frame | np.ndarray, box: NormBox | None = None) -> np.ndarray:
    """Miniatura gris ``THUMB_SIZE`` en ``float32`` de la imagen completa o de ``box``.

    ``INTER_AREA`` promedia al reducir y el desenfoque posterior quita el ruido de compresión de la capturadora."""
    image = frame.image if isinstance(frame, Frame) else frame
    if box is not None:
        image = box.crop(image)
    small = cv2.resize(image, THUMB_SIZE, interpolation=cv2.INTER_AREA)
    gray = small if small.ndim == 2 else cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray.astype(np.float32), (3, 3), 0)


def changed_fraction(a: np.ndarray, b: np.ndarray) -> float:
    """Fracción de píxeles de dos miniaturas cuya diferencia supera ``PIXEL_LEVEL``."""
    return float((np.abs(a - b) > PIXEL_LEVEL).mean())


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Correlación normalizada en [-1, 1] (se toleran brillo y contraste distintos)."""
    a0, b0 = a - a.mean(), b - b.mean()
    den = float(np.sqrt((a0 * a0).sum() * (b0 * b0).sum()))
    if den < 1e-6:
        return 1.0 if float(np.abs(a - b).mean()) < 4 else 0.0  # pantallas lisas (p. ej. negro)
    return float((a0 * b0).sum() / den)


class ScreenSignatures:
    """Firmas de pantallas conocidas, guardadas como PNG en ``directory`` (se aprenden la primera vez con
    confirmación del usuario y después se reconocen solas)."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self._cache: dict[str, np.ndarray] = {}

    def _path(self, name: str) -> Path | None:
        return None if self.directory is None else self.directory / f"{name}.png"

    def get(self, name: str) -> np.ndarray | None:
        """Firma ``name``; ``None`` si no existe o se guardó con otro tamaño de miniatura (hay que reaprenderla)."""
        if name in self._cache:
            return self._cache[name]
        path = self._path(name)
        if path is None or not path.exists():
            return None
        img = imread(path, cv2.IMREAD_GRAYSCALE)  # None si no existe, está dañada o se borró al leerla
        if img is None or img.shape[::-1] != THUMB_SIZE:
            return None
        self._cache[name] = img.astype(np.float32)
        return self._cache[name]

    def put(self, name: str, thumb: np.ndarray) -> None:
        """Guarda una firma en memoria y, si se puede, en disco (un fallo de disco no impide usarla ahora)."""
        self._cache[name] = thumb.astype(np.float32)
        path = self._path(name)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            ok, buf = cv2.imencode(".png", np.clip(thumb, 0, 255).astype(np.uint8))
            if ok:
                buf.tofile(str(path))
        except OSError:
            pass

    def forget(self, name: str) -> None:
        """Borra una firma (p. ej. si se aprendió mal) para que se vuelva a pedir."""
        self._cache.pop(name, None)
        path = self._path(name)
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
