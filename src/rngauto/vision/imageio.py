"""Lectura y escritura de imágenes con cualquier ruta.

En Windows, ``cv2.imread``/``cv2.imwrite`` no admiten rutas con caracteres fuera de la página de códigos del
sistema (p. ej. una carpeta de usuario «José» o «山田»): devuelven ``None``/``False`` sin más (comprobado con
OpenCV 5.0). Aquí Python lee y escribe el fichero y OpenCV solo codifica o decodifica en memoria.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def imread(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """Como ``cv2.imread``: ``None`` si el fichero no existe o no es una imagen."""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite(path: str | Path, image: np.ndarray) -> bool:
    """Como ``cv2.imwrite`` (el formato sale de la extensión): ``False`` si no se pudo."""
    ok, buffer = cv2.imencode(Path(path).suffix or ".png", image)
    if not ok:
        return False
    try:
        buffer.tofile(str(path))
    except OSError:
        return False
    return True
