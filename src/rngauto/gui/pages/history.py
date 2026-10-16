"""Paso 6 — Historial: intentos, calibración aprendida y acceso a los datos.

Lee lo que guardan las ejecuciones en la carpeta de datos: ``calibration/*.json`` (una calibración por
encuentro) y ``sessions/*/attempts.jsonl`` (un intento por línea). Los ficheros dañados o con registros de
forma inesperada se saltan sin impedir ver el resto.
"""
from __future__ import annotations

import json
import os

from PySide6.QtWidgets import QPushButton, QTableWidget, QTableWidgetItem

from rngauto.core.calibration import CalibrationHistory
from rngauto.i18n import t

from ..widgets import card, hbox, label
from .base import Page

MAX_ROWS = 1000  # los intentos más recientes (con miles, la tabla tardaría en dibujarse)


def columns() -> tuple[str, ...]:
    """Cabeceras de la tabla en el idioma actual (se evalúa al construir la página, no al importar)."""
    return (t("Fecha", "Date"), t("Encuentro", "Encounter"), t("Objetivo", "Target"), t("Obtenido", "Actual"),
            t("Diferencia", "Difference"), t("Resultado", "Outcome"))


def outcome_labels() -> dict[str, str]:
    """Texto visible de cada resultado guardado (las claves son los valores en disco y no se traducen)."""
    return {"hit": t("acierto", "hit"), "miss": t("fallo", "miss"), "ambiguous": t("dudoso", "ambiguous"),
            "no_match": t("sin coincidencias", "no match"), "pending": t("pendiente", "pending"),
            "error": t("error", "error")}


class HistoryPage(Page):
    """Paso «Historial»: calibración aprendida por encuentro y tabla de intentos, del más reciente al más antiguo.

    Siempre está accesible desde la barra lateral y se recarga cada vez que se entra o al pulsar «Actualizar».
    """

    @property
    def step_title(self) -> str:
        return t("Historial", "History")

    @property
    def step_hint(self) -> str:
        return t("Intentos y calibración", "Attempts and calibration")

    def __init__(self, state) -> None:
        super().__init__(state, t("Historial y calibración", "History and calibration"),
                         t("Cada intento enseña al programa cuánto se desvía tu montaje. Esta calibración se guarda "
                           "y se reutiliza la próxima vez.",
                           "Each attempt teaches the program how far off your setup is. This calibration is saved "
                           "and reused next time."))
        self.calibration = label("", None)
        self.body.addWidget(card(self.calibration, title=t("Calibración aprendida", "Learned calibration")))
        cols = columns()
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(300)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.empty = label(t("Aún no hay intentos: aparecerán aquí en cuanto termine el primero.",
                             "No attempts yet: they will appear here as soon as the first one finishes."), "muted")
        open_btn = QPushButton(t("Abrir carpeta de datos", "Open data folder"))
        open_btn.clicked.connect(lambda: os.startfile(str(state.data_dir)) if hasattr(os, "startfile") else None)
        refresh = QPushButton(t("Actualizar", "Refresh"))
        refresh.clicked.connect(self.on_enter)
        self.body.addWidget(card(self.empty, self.table, hbox(refresh, open_btn, None), title=t("Intentos", "Attempts")))
        self.body.addStretch(1)

    def _row(self, a: dict) -> tuple[str, ...] | None:
        """Fila de la tabla para un intento guardado; ``None`` si el registro no tiene la forma esperada."""
        resolved, target = a.get("resolved"), a.get("target_advance")
        # Los ficheros pueden venir editados a mano o de otra versión: se valida el tipo antes de restar.
        if resolved is not None and not isinstance(resolved, int) or target is not None and not isinstance(target, int):
            return None
        delta = None if resolved is None or target is None else resolved - target
        enc = self.state.encounters.get(str(a.get("encounter", "")))
        raw_outcome = str(a.get("outcome", ""))
        outcome = outcome_labels().get(raw_outcome, raw_outcome)
        observation = a.get("observation")
        if isinstance(observation, dict) and str(observation.get("is_shiny")) == "True":
            outcome = "★ shiny · " + outcome
        return (str(a.get("started_at", "")).replace("T", " "),
                enc.display_name if enc else str(a.get("encounter", "")),
                "" if target is None else str(target), "?" if resolved is None else str(resolved),
                "—" if delta is None else f"{delta:+d}", outcome)

    def on_enter(self) -> None:
        """Vuelve a leer la calibración y los intentos (como mucho ``MAX_ROWS``) desde la carpeta de datos."""
        cal_dir = self.state.data_dir / "calibration"
        lines = []
        for path in sorted(cal_dir.glob("*.json")) if cal_dir.exists() else []:
            try:
                hist = CalibrationHistory.from_dict(json.loads(path.read_text(encoding="utf-8")))
                summary = hist.estimate_final_delay().describe()
            except (OSError, ValueError, KeyError, TypeError, AttributeError):  # dañado: no impide ver el resto
                continue
            demo = " (demo)" if path.stem.endswith("-demo") else ""  # calibraciones de sesiones simuladas
            enc = self.state.encounters.get(hist.encounter)
            name = enc.display_name if enc is not None else hist.encounter
            lines.append(f"<b>{name}{demo}</b>: {summary}")
        self.calibration.setText("<br>".join(lines) or t("Aún no hay intentos registrados.", "No attempts recorded yet."))
        rows = []
        sessions = self.state.data_dir / "sessions"
        # Las carpetas de sesión empiezan por fecha y hora: el orden inverso de nombre es el más reciente primero.
        for path in sorted(sessions.glob("*/attempts.jsonl"), reverse=True) if sessions.exists() else []:
            try:
                lines_in_file = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines_in_file):  # lo más reciente primero
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                if isinstance(a, dict):
                    row = self._row(a)
                    if row is not None:
                        rows.append(row)
            if len(rows) >= MAX_ROWS:
                break
        rows = rows[:MAX_ROWS]
        self.table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, v in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(v))
        self.table.resizeColumnsToContents()
        self.empty.setVisible(not rows)
        self.table.setVisible(bool(rows))
