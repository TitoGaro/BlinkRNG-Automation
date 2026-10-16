"""Puente entre la estrategia (hilo de trabajo) y los diálogos (hilo de la interfaz).

Las llamadas del operador bloquean el hilo de trabajo hasta que el usuario responde. Nunca se llaman
en el momento de una pulsación temporizada (la estrategia pide aprobación del plan antes).

Qt solo permite crear ventanas en el hilo de la interfaz: el hilo de trabajo emite una petición con una «caja»
(un ``threading.Event`` y el valor de respuesta), la interfaz muestra el diálogo y rellena la caja al cerrarse.
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
    QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

from rngauto.core.pokemon import NATURES_EN, NATURES_ES, stat_labels
from rngauto.core.stats import StatsInconsistent, iv_ranges_from_stats
from rngauto.games.bdsp.identify import Observation
from rngauto.games.bdsp.static_gen import StaticTemplate
from rngauto.i18n import is_spanish, t

from . import theme


class ObservationDialog(QDialog):
    """El usuario indica lo que ve: shiny, naturaleza y stats (con IVs deducidas en directo).

    «Guardar resultado» solo se habilita tras responder si es shiny, que es el único dato obligatorio.
    """

    def __init__(self, templates: list[StaticTemplate], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.templates = templates
        self.template = templates[0]
        self.setWindowTitle(t("¿Qué Pokémon ha salido?", "Which Pokémon appeared?"))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        intro = QLabel(t("Captura a {names} (o derrótalo) y abre su <b>pantalla de datos</b>. Indica lo que veas; "
                         "lo que no sepas, déjalo en blanco.",
                         "Catch {names} (or defeat it) and open its <b>Summary</b>. Enter what you see; leave "
                         "anything you don't know blank.",
                         names="/".join(tpl.name for tpl in templates)))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.species = QComboBox()
        for tpl in templates:
            self.species.addItem(tpl.name, tpl)
        self.species.currentIndexChanged.connect(self._species_changed)
        if len(templates) > 1:  # sale uno al azar (p. ej. Latias o Latios): sus stats base son distintas
            lay.addWidget(QLabel(t("<b>¿Cuál ha salido?</b>", "<b>Which one appeared?</b>")))
            lay.addWidget(self.species)

        lay.addWidget(QLabel(t("<b>¿Es shiny?</b> (obligatorio: en combate brilla al aparecer y en sus datos "
                               "tiene una estrella)",
                               "<b>Is it shiny?</b> (required: it sparkles when it appears in battle and has a star "
                               "on its Summary)")))
        self.shiny_group = QButtonGroup(self)
        row = QHBoxLayout()
        for i, text in ((1, t("Sí", "Yes")), (2, t("No", "No"))):
            rb = QRadioButton(text)
            self.shiny_group.addButton(rb, i)
            row.addWidget(rb)
        lay.addLayout(row)

        lay.addWidget(QLabel(t("<b>Naturaleza</b>", "<b>Nature</b>")))
        self.nature = QComboBox()
        self.nature.addItem(t("No lo sé", "Unknown"), None)
        for i, n in enumerate(NATURES_ES if is_spanish() else NATURES_EN):
            self.nature.addItem(n, i)
        lay.addWidget(self.nature)

        lay.addWidget(QLabel(t("<b>Stats</b> (nivel {level}; 0 = no lo sé)", "<b>Stats</b> (level {level}; 0 = unknown)",
                               level=self.template.level)))
        grid = QGridLayout()
        self.stats: list[QSpinBox] = []
        for i, name in enumerate(stat_labels()):
            sb = QSpinBox()
            sb.setRange(0, 999)
            sb.valueChanged.connect(self._update_ivs)
            grid.addWidget(QLabel(name), 0, i)
            grid.addWidget(sb, 1, i)
            self.stats.append(sb)
        lay.addLayout(grid)
        self.nature.currentIndexChanged.connect(self._update_ivs)
        self.ivs_label = QLabel("IVs: —")
        self.ivs_label.setStyleSheet(f"color:{theme.MUTED};")
        lay.addWidget(self.ivs_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(t("Guardar resultado", "Save result"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t("Cancelar", "Cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._ivs = None
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setEnabled(False)
        self.shiny_group.idClicked.connect(lambda _i: ok_btn.setEnabled(True))

    def _species_changed(self) -> None:
        self.template = self.species.currentData()
        self._update_ivs()

    def _update_ivs(self) -> None:
        """Recalcula los rangos de IVs con la naturaleza y los 6 stats; si faltan datos o no cuadran, lo dice."""
        nature = self.nature.currentData()
        values = tuple(sb.value() for sb in self.stats)
        self._ivs = None
        if nature is None or 0 in values:
            self.ivs_label.setText(t("IVs: completa la naturaleza y los 6 stats para deducirlas",
                                     "IVs: fill in the nature and all 6 stats to work them out"))
            self.ivs_label.setStyleSheet(f"color:{theme.MUTED};")
            return
        try:
            self._ivs = iv_ranges_from_stats(self.template.base_stats, self.template.level, nature, values)
        except StatsInconsistent as exc:
            self.ivs_label.setText(t("{error}. Revisa los números o la naturaleza.", "{error}. Check the numbers or the nature.",
                                     error=exc))
            self.ivs_label.setStyleSheet(f"color:{theme.WARNING};")
            return
        text = " / ".join(f"{r.lo}-{r.hi}" if r.lo != r.hi else str(r.lo) for r in self._ivs)
        self.ivs_label.setText(t("IVs deducidas: <b>{ivs}</b>", "Calculated IVs: <b>{ivs}</b>", ivs=text))
        self.ivs_label.setStyleSheet(f"color:{theme.SUCCESS};")

    def observation(self) -> Observation | None:
        """Lo introducido como ``Observation``; ``None`` si no se indicó ni el shiny ni la naturaleza."""
        shiny = {-1: None, 1: True, 2: False}[self.shiny_group.checkedId()]
        nature = self.nature.currentData()
        if shiny is None and nature is None:
            return None
        return Observation(is_shiny=shiny, nature=nature, ivs=self._ivs)


class GuiOperator(QObject):
    """Implementa el protocolo ``Operator`` mostrando diálogos en el hilo de la interfaz.

    Los diálogos no son modales: la PARADA (y F12) siguen funcionando con un diálogo abierto. Al parar, los
    diálogos pendientes se cierran solos y el hilo de trabajo deja de esperar.
    """

    _request = Signal(str, str, object)  # tipo, mensaje, caja de respuesta
    _close = Signal(object)  # caja cuyo diálogo hay que cerrar (p. ej. por tiempo agotado)
    message = Signal(str)
    stop_requested = Signal()  # el usuario pulsó «Detener» en un diálogo

    def __init__(self, parent_widget: QWidget, templates: list[StaticTemplate]) -> None:
        super().__init__()
        self.parent_widget = parent_widget
        self.templates = templates  # especies que pueden salir en el encuentro
        self._request.connect(self._show, Qt.ConnectionType.QueuedConnection)
        self._close.connect(self._close_box, Qt.ConnectionType.QueuedConnection)
        self._cancel = threading.Event()
        self._open: dict[int, QDialog] = {}

    def cancel_pending(self) -> None:
        """Deja de esperar respuestas y cierra los diálogos abiertos (llamar desde la interfaz)."""
        self._cancel.set()
        for dlg in list(self._open.values()):
            dlg.reject()

    def _ask(self, kind: str, msg: str, timeout_s: float | None = None):
        """Pide un diálogo a la interfaz y espera la respuesta (se llama desde el hilo de trabajo).

        Se espera en tramos de 0,1 s para atender la PARADA (``cancel_pending``) y el plazo ``timeout_s``;
        en ambos casos devuelve ``None``. Al agotarse el plazo también se cierra el diálogo.
        """
        box = {"event": threading.Event(), "value": None, "id": id(object())}
        self._request.emit(kind, msg, box)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while not box["event"].wait(0.1):
            if self._cancel.is_set():
                return None
            if deadline is not None and time.monotonic() > deadline:
                self._close.emit(box)
                return None
        return box["value"]

    @Slot(object)
    def _close_box(self, box: dict) -> None:
        """Cierra el diálogo asociado a ``box`` si sigue abierto (plazo agotado en ``_ask``)."""
        dlg = self._open.get(box["id"])
        if dlg is not None:
            dlg.reject()

    @Slot(str, str, object)
    def _show(self, kind: str, msg: str, box: dict) -> None:
        """Crea y muestra el diálogo pedido (en el hilo de la interfaz).

        ``kind`` es ``"observe"`` (ficha del Pokémon), ``"confirm"`` (Sí/No) o ``"ready"`` (Listo/Detener).
        Pase lo que pase, al cerrarse el diálogo se rellena la caja y se libera al hilo que espera.
        """
        if self._cancel.is_set():
            box["event"].set()
            return
        try:
            if kind == "observe":
                dlg = ObservationDialog(self.templates, self.parent_widget)

                def finished(result: int) -> None:
                    box["value"] = dlg.observation() if result == QDialog.DialogCode.Accepted else None
            else:
                dlg = QMessageBox(self.parent_widget)
                dlg.setWindowTitle(t("Confirmar", "Confirm") if kind == "confirm" else t("Tu turno", "Your turn"))
                dlg.setText(msg)
                if kind == "confirm":
                    dlg.setIcon(QMessageBox.Icon.Question)
                    yes = dlg.addButton(t("Sí", "Yes"), QMessageBox.ButtonRole.YesRole)
                    no = dlg.addButton(t("No", "No"), QMessageBox.ButtonRole.NoRole)
                else:
                    dlg.setIcon(QMessageBox.Icon.Information)
                    yes = dlg.addButton(t("Listo", "Ready"), QMessageBox.ButtonRole.AcceptRole)
                    stop = dlg.addButton(t("Detener", "Stop"), QMessageBox.ButtonRole.RejectRole)
                dlg.setDefaultButton(yes)

                def finished(_result: int) -> None:
                    # Solo cuenta lo que se pulsa de verdad: cerrar con la X o Esc no es «No» ni «Listo» (None),
                    # p. ej. «¿ha salido shiny?» cerrado sin querer no debe llevar a abrir la ficha.
                    clicked = dlg.clickedButton()
                    if kind == "confirm":
                        box["value"] = True if clicked is yes else False if clicked is no else None
                    else:
                        box["value"] = True if clicked is yes else None
                    if kind == "ready" and clicked is stop:
                        self.stop_requested.emit()
        except Exception:  # si el diálogo no se pudo crear, el hilo de trabajo no debe quedarse esperando
            box["event"].set()
            raise

        def done(result: int) -> None:
            try:
                finished(result)
            finally:
                self._open.pop(box["id"], None)
                box["event"].set()
                dlg.deleteLater()

        dlg.finished.connect(done)
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        self._open[box["id"]] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    # --- protocolo Operator ---
    def confirm(self, message: str, timeout_s: float | None = None) -> bool | None:
        """``True``/``False`` según la respuesta; ``None`` si no hubo respuesta a tiempo o se paró."""
        value = self._ask("confirm", message, timeout_s)
        return None if value is None else bool(value)

    def notify(self, message: str) -> None:
        """Aviso informativo: va al registro de la página de ejecución, sin diálogo ni espera."""
        self.message.emit(message)

    def wait_ready(self, message: str) -> bool:
        """``True`` solo si se pulsó «Listo»."""
        return self._ask("ready", message + t("\n\nPulsa «Listo» cuando esté preparado (o «Detener»).",
                                              "\n\nPress Ready when you are set (or Stop).")) is True

    def ask_observation(self, fields: list[str]) -> Observation | None:
        """Abre la ficha para que el usuario introduzca lo que ve; ``None`` si cancela o se para.

        ``fields`` forma parte del protocolo ``Operator``; este diálogo siempre pide todos los datos.
        """
        return self._ask("observe", "")
