"""Iconos de línea propios (SVG en el código): monocromos y del color que se pida, sin depender de fuentes de
iconos ni de librerías externas."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Cuerpo SVG de cada icono: trazos en una caja de 24×24, sin relleno. Color y grosor los pone ``_svg``.
_PATHS = {
    "monitor": '<rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M8 20h8M12 16v4"/>',
    "volume": '<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>',
    "mute": '<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M17 9l5 6M22 9l-5 6"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
    "play": '<path d="M8 5.5v13l10.5-6.5L8 5.5z"/>',
    "arrow_left": '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    "arrow_right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9'
             'c-2.5-2.6-3.8-5.6-3.8-9s1.3-6.4 3.8-9z"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8M4 4v4h4M4 13a8 8 0 0 0 14.3 4.9L20 16M20 20v-4h-4"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "camera": '<rect x="3" y="6" width="18" height="13" rx="1.5"/><path d="M9 6l1.5-2h3L15 6"/><circle cx="12" cy="12.5" r="3.5"/>',
    "gamepad": '<rect x="2.5" y="7" width="19" height="10" rx="4"/><path d="M7 10.5v3M5.5 12h3"/>'
               '<circle cx="16" cy="11" r=".6"/><circle cx="18" cy="13" r=".6"/>',
    "folder": '<path d="M3 7.5V18a1.5 1.5 0 0 0 1.5 1.5h15A1.5 1.5 0 0 0 21 18V9a1.5 1.5 0 0 0-1.5-1.5H12L10 5H4.5A1.5 1.5 0 0 0 3 6.5z"/>',
    "alert": '<path d="M12 4l9 16H3L12 4z"/><path d="M12 10v4.5M12 17.2v.3"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.7v.3"/>',
    "star": '<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.8 6.8 19.6l1-5.8L3.5 9.7l5.9-.8L12 3.5z"/>',
}


def _svg(name: str, color: str, stroke: float) -> bytes:
    """Documento SVG completo del icono ``name`` con el color y el grosor de trazo indicados."""
    body = _PATHS[name]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>').encode()


@lru_cache(maxsize=256)
def pixmap(name: str, color: str, size: int = 18, stroke: float = 1.7, scale: float = 2.0) -> QPixmap:
    """Icono como imagen (a doble resolución para pantallas con escalado).

    Se cachea por argumentos: la interfaz pide los mismos iconos muchas veces (p. ej. en cada refresco del botón
    de sonido o al reconstruir la ventana).
    """
    px = int(size * scale)
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(_svg(name, color, stroke)))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pm.setDevicePixelRatio(scale)
    return pm


def icon(name: str, color: str, disabled: str | None = None, size: int = 18) -> QIcon:
    """``QIcon`` para botones; con ``disabled``, ese color se usa cuando el botón está desactivado."""
    ic = QIcon(pixmap(name, color, size))
    if disabled:
        ic.addPixmap(pixmap(name, disabled, size), QIcon.Mode.Disabled)
    return ic
