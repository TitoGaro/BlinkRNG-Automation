"""Clase base de los pasos de la aplicación guiada: cabecera común, cuerpo con scroll y ganchos de navegación."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from rngauto.i18n import t

from ..state import AppState


class Page(QWidget):
    """Paso de la aplicación guiada. ``completion_changed`` avisa a la ventana para habilitar «Siguiente».

    Las subclases añaden su contenido a ``self.body`` y redefinen ``is_complete``/``missing`` y, si tienen
    vídeo, temporizadores o hilos, ``on_enter``/``on_leave`` para arrancarlos y pararlos: solo la página
    visible debe consumir recursos.
    """

    completion_changed = Signal()
    step_title = ""
    step_hint = ""  # línea de ayuda en la barra lateral

    def __init__(self, state: AppState, title: str, subtitle: str) -> None:
        super().__init__()
        self.state = state
        self.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("page")
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(32, 24, 32, 24)
        self.body.setSpacing(16)
        self.overline = QLabel("")
        self.overline.setObjectName("caption")
        self.overline.setVisible(False)
        self.body.addWidget(self.overline)
        title_label = QLabel(title)
        title_label.setObjectName("title")
        s = QLabel(subtitle)
        s.setObjectName("subtitle")
        s.setWordWrap(True)
        self.body.addWidget(title_label)
        self.body.addWidget(s)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def set_step(self, index: int, total: int) -> None:
        """Muestra «PASO n DE total» encima del título."""
        self.overline.setText(t("PASO {n} DE {total}", "STEP {n} OF {total}", n=index + 1, total=total))
        self.overline.setVisible(True)

    def is_complete(self) -> bool:
        """El paso tiene lo necesario para pasar al siguiente."""
        return True

    def missing(self) -> str:
        """Qué falta para poder continuar (texto para el usuario)."""
        return ""

    def on_enter(self) -> None:
        """La página pasa a ser la visible."""
        pass

    def on_leave(self) -> None:
        """La página deja de verse (o se cierra la ventana): debe parar lo que tenga en marcha."""
        pass
