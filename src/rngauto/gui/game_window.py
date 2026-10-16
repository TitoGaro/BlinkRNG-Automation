"""Ventana «Ver juego»: la imagen de la capturadora en grande, sin necesitar otro programa para ver el juego.

Doble clic = pantalla completa; Esc = salir de pantalla completa.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from rngauto.i18n import t

from .state import AppState
from .widgets import VideoView


class GameWindow(QWidget):
    """Ventana de nivel superior (sin padre) con el vídeo de la sesión activa.

    No abre la capturadora: muestra los fotogramas que ya recibe la sesión, así que se puede abrir y cerrar en
    cualquier momento sin afectar a la detección.
    """

    def __init__(self, state: AppState) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.state = state
        self.setWindowTitle(t("Juego — RNG Automation", "Game — RNG Automation"))
        self.resize(1280, 720)
        self.setStyleSheet("background:#000;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.video = VideoView(min_height=200)
        self.video.set_placeholder(t("Sin imagen: conecta la capturadora en el paso «Conexión»",
                                     "No video: connect the capture card in the Connection step"))
        lay.addWidget(self.video)
        # La fuente consulta ``state.session`` en cada sondeo: la sesión puede cambiar con la ventana abierta.
        self.video.attach(lambda: self.state.session.latest() if self.state.session is not None else None)

    def mouseDoubleClickEvent(self, _event) -> None:  # noqa: N802
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(event)
