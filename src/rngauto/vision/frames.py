"""Frames con timestamp y regiones normalizadas (independientes de la resolución de captura)."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from rngauto.i18n import t


@dataclass(frozen=True, slots=True)
class Frame:
    """Imagen capturada con su instante de llegada."""

    image: np.ndarray  # BGR uint8 (H, W, 3)
    t_s: float  # reloj monótono del host en la llegada (``time.perf_counter``) o del fichero en replay
    index: int  # número de frame en la fuente
    pts_s: float | None = None  # marca del contenedor/driver si existe

    @property
    def height(self) -> int:
        return self.image.shape[0]

    @property
    def width(self) -> int:
        return self.image.shape[1]


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Formato efectivo de una fuente de vídeo."""

    name: str
    width: int
    height: int
    fps: float
    fourcc: str = ""


class FrameSource(Protocol):
    """Interfaz común de las fuentes de frames (capturadora en vivo o vídeo grabado)."""

    def read(self) -> Frame | None:
        """Siguiente frame, o ``None`` si la fuente terminó o no hay frame disponible."""

    def info(self) -> SourceInfo: ...

    def close(self) -> None: ...

    def __iter__(self) -> Iterator[Frame]: ...


@dataclass(frozen=True, slots=True)
class NormBox:
    """Caja en coordenadas normalizadas 0..1: la misma configuración sirve para cualquier resolución."""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if not (0 <= self.x < 1 and 0 <= self.y < 1 and 0 < self.w <= 1 and 0 < self.h <= 1):
            raise ValueError(t("Caja normalizada inválida: {box}", "Invalid normalized box: {box}", box=self))
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError(t("La caja se sale de la imagen: {box}", "The box extends beyond the image: {box}", box=self))

    @classmethod
    def from_pixels(cls, x: int, y: int, w: int, h: int, ref_width: int, ref_height: int) -> "NormBox":
        """Convierte una ROI en píxeles absolutos, medida sobre una imagen de referencia, a normalizada."""
        return cls(x / ref_width, y / ref_height, w / ref_width, h / ref_height)

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """``(x, y, w, h)`` en píxeles para una imagen de ``width``×``height``; al menos 1×1."""
        x0 = int(round(self.x * width))
        y0 = int(round(self.y * height))
        x1 = int(round((self.x + self.w) * width))
        y1 = int(round((self.y + self.h) * height))
        return x0, y0, max(1, x1 - x0), max(1, y1 - y0)

    def crop(self, image: np.ndarray) -> np.ndarray:
        """Vista (sin copia) de la región de ``image``."""
        x, y, w, h = self.to_pixels(image.shape[1], image.shape[0])
        return image[y:y + h, x:x + w]

    def expanded(self, factor: float) -> "NormBox":
        """Caja agrandada alrededor de su centro (recortada a la imagen)."""
        cx, cy = self.x + self.w / 2, self.y + self.h / 2
        w, h = min(1.0, self.w * factor), min(1.0, self.h * factor)
        x = min(max(0.0, cx - w / 2), 1.0 - w)
        y = min(max(0.0, cy - h / 2), 1.0 - h)
        return NormBox(x, y, w, h)
