"""«Informar de un problema»: muestra el informe de diagnóstico (``app/diagnostics.py``) para revisarlo y abre el
formulario de fallo de GitHub ya rellenado. Nada se envía sin que el usuario lo vea y pulse el botón."""
from __future__ import annotations

import os

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QDialog, QHBoxLayout, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from rngauto import i18n
from rngauto.app.diagnostics import build_report, issue_url
from rngauto.app.setup_store import DEFAULT_DATA_DIR
from rngauto.i18n import t

from . import theme
from .widgets import label


def _find_state(widget: QWidget | None):
    """Estado de la aplicación de la ventana que contiene ``widget`` (``None`` si no hay ventana principal)."""
    while widget is not None:
        state = getattr(widget, "state", None)
        if state is not None and hasattr(state, "selection"):
            return state
        widget = widget.parentWidget()
    return None


def context_from_state(state) -> tuple[dict[str, str], str, str]:
    """(datos para el informe, encuentro, montaje) a partir del estado de la aplicación. Sin estado, vacío."""
    if state is None:
        return {"Idioma": i18n.language()}, "", ""
    sel, session = state.selection, state.session
    encounter = f"{sel.encounter.id} ({sel.encounter.display_name})" if sel.encounter is not None else ""
    controller = state.pending_controller or (session.controller if session is not None else None)
    ctrl_text = ""
    if controller is not None:
        try:
            st = controller.status()
            ctrl_text = " · ".join(x for x in (st.device, f"firmware {st.firmware}" if st.firmware else "",
                                               "emparejado" if st.console_ready else "sin emparejar") if x)
        except Exception as exc:  # el informe no puede fallar por el mando
            ctrl_text = f"estado no disponible ({type(exc).__name__})"
    capture = session.capture_label if session is not None else ""
    context = {
        "Idioma": i18n.language(),
        "Encuentro": encounter,
        "Modo": sel.mode.value,
        "Mando": ctrl_text or "no conectado",
        "Capturadora": capture or "no conectada",
        "Ejecución en marcha": "sí" if state.running else "no",
    }
    setup = "\n".join(x for x in (f"Capturadora: {capture}" if capture else "",
                                  f"Mando: {ctrl_text}" if ctrl_text else "") if x)
    return context, encounter, setup


class ReportDialog(QDialog):
    """Informe de diagnóstico editable y botones para abrir GitHub, copiarlo o abrir la carpeta de registros."""

    def __init__(self, parent: QWidget | None = None, state=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Informar de un problema", "Report a problem"))
        self.resize(760, 560)
        state = state if state is not None else _find_state(parent)
        context, self._encounter, self._setup = context_from_state(state)
        self._data_dir = state.data_dir if state is not None else DEFAULT_DATA_DIR
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.addWidget(label(t(
            "Este informe ayuda a encontrar el fallo: versión, sistema, hardware y los últimos eventos y errores. "
            "Revísalo y quita lo que no quieras compartir: no incluye tu nombre de usuario ni tus carpetas. "
            "Al pulsar «Abrir en GitHub» se abre el formulario en el navegador; nada se envía hasta que lo publiques.",
            "This report helps find the problem: version, system, hardware and the latest events and errors. Review "
            "it and remove anything you do not want to share: it does not include your user name or your folders. "
            "\"Open on GitHub\" opens the form in your browser; nothing is sent until you submit it."), "muted"))
        self.text = QPlainTextEdit(build_report(self._data_dir, context))
        self.text.setStyleSheet(f"font-family: Consolas, monospace; font-size: 9pt; background: {theme.SURFACE_2};")
        lay.addWidget(self.text, 1)
        self.note = label("", "muted")
        lay.addWidget(self.note)
        buttons = QHBoxLayout()
        folder = QPushButton(t("Abrir carpeta de registros", "Open logs folder"))
        folder.clicked.connect(self._open_folder)
        copy = QPushButton(t("Copiar", "Copy"))
        copy.clicked.connect(self._copy)
        close = QPushButton(t("Cerrar", "Close"))
        close.clicked.connect(self.reject)
        github = QPushButton(t("Abrir en GitHub", "Open on GitHub"))
        github.setObjectName("primary")
        github.clicked.connect(self._open_github)
        buttons.addWidget(folder)
        buttons.addStretch(1)
        buttons.addWidget(copy)
        buttons.addWidget(close)
        buttons.addWidget(github)
        lay.addLayout(buttons)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.text.toPlainText())
        self.note.setText(t("Copiado al portapapeles.", "Copied to the clipboard."))

    def _open_folder(self) -> None:
        """Carpeta de datos: ``logs/errores.log`` y, en ``sessions``, el registro y el vídeo del ojo de cada
        ejecución (útiles como adjuntos si el fallo es de detección)."""
        if hasattr(os, "startfile"):
            os.startfile(str(self._data_dir))

    def _open_github(self) -> None:
        url, complete = issue_url(self.text.toPlainText(), self._encounter, self._setup)
        if not complete:  # no cabe en la dirección: se copia entero para pegarlo en el formulario
            self._copy()
            self.note.setText(t("El informe es largo: en el formulario va recortado y el completo está copiado "
                                "en el portapapeles para pegarlo.",
                                "The report is long: the form gets a shortened version and the full one is "
                                "copied to the clipboard so you can paste it."))
        QDesktopServices.openUrl(QUrl(url))


def offer_report(parent: QWidget | None, title: str, message: str, critical: bool = False) -> None:
    """Aviso de error con un botón para informar del problema."""
    box = QMessageBox(QMessageBox.Icon.Critical if critical else QMessageBox.Icon.Warning, title, message,
                      QMessageBox.StandardButton.Ok, parent)
    report = box.addButton(t("Informar del problema", "Report the problem"), QMessageBox.ButtonRole.ActionRole)
    box.exec()
    if box.clickedButton() is report:
        ReportDialog(parent).exec()
