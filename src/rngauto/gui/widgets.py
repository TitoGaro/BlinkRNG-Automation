"""Componentes reutilizables de la interfaz.

Contiene los bloques de maquetación (tarjetas, filas), los indicadores (chips de estado, tiles, fases), los
delegados que pintan las listas de la barra lateral y del objetivo, el vídeo en directo con selección de región,
la lupa y la gráfica del ojo, y ``Background``, que ejecuta trabajo lento fuera del hilo de la interfaz.
"""
from __future__ import annotations

import threading
import traceback
from collections.abc import Callable

import cv2
import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget

from rngauto.i18n import t
from rngauto.vision.frames import NormBox

from . import theme


def bgr_to_qimage(image: np.ndarray) -> QImage:
    """Copia una imagen BGR de OpenCV a una ``QImage`` independiente (no comparte memoria con el array)."""
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def card(*widgets: QWidget, title: str | None = None, spacing: int = 10) -> QFrame:
    """Tarjeta con título opcional y los widgets apilados en vertical."""
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(20, 16, 20, 18)
    lay.setSpacing(spacing)
    if title:
        heading = QLabel(title)
        heading.setObjectName("section")
        lay.addWidget(heading)
    for w in widgets:
        lay.addWidget(w)
    return frame


def panel(spacing: int = 10) -> tuple[QFrame, QVBoxLayout]:
    """Tarjeta vacía y su disposición vertical (para componer a mano)."""
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(20, 16, 20, 18)
    lay.setSpacing(spacing)
    return frame, lay


def label(text: str, kind: str | None = None, wrap: bool = True) -> QLabel:
    """Etiqueta de texto enriquecido; ``kind`` es el ``objectName`` que le da estilo en el QSS del tema."""
    lbl = QLabel(text)
    if kind:
        lbl.setObjectName(kind)
    lbl.setWordWrap(wrap)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    return lbl


class StatusChip(QLabel):
    """Indicador de estado: punto de color, nombre y detalle (gris pendiente, verde ok, ámbar aviso, rojo error)."""

    COLORS = {"idle": theme.FAINT, "ok": theme.SUCCESS, "warn": theme.WARNING, "error": theme.DANGER,
              "busy": theme.ACCENT}

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.state = "idle"
        self.setTextFormat(Qt.TextFormat.RichText)
        self.set_state("idle", t("sin conectar", "not connected"))

    def set_state(self, state: str, text: str) -> None:
        self.state = state
        color = self.COLORS.get(state, theme.FAINT)
        self.setText(f'<span style="color:{color}; font-size:9pt;">&#9679;</span>&nbsp;&nbsp;'
                     f'<span style="color:{theme.TEXT}; font-weight:600;">{self.name}</span>&nbsp;&nbsp;'
                     f'<span style="color:{theme.MUTED};">{text}</span>')
        self.setToolTip(f"{self.name}: {text}")

    def text(self) -> str:  # noqa: D102 — incluye el estado: las páginas comparan este valor para detectar cambios
        return f"{self.state}:{super().text()}"


class Tile(QFrame):
    """Indicador compacto: título pequeño en mayúsculas, valor grande y una línea opcional debajo."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        cap = QLabel(caption.upper())
        cap.setObjectName("caption")
        self.value = QLabel("—")
        self.value.setObjectName("kpi")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sub = QLabel("")
        self.sub.setObjectName("kpiSub")
        self.sub.setWordWrap(True)
        self.sub.setVisible(False)
        lay.addWidget(cap)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)

    def set(self, value: str, sub: str = "") -> None:
        self.value.setText(value)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))


class PhaseStepper(QWidget):
    """Fases del intento como pasos unidos por una línea: hechos (verde), actual (acento) y pendientes."""

    def __init__(self, labels: list[str]) -> None:
        super().__init__()
        self.labels = labels
        self.current = -1
        self.highlight = theme.ACCENT
        self.setMinimumHeight(66)

    def set_current(self, index: int, color: str = theme.ACCENT) -> None:
        """Marca la fase ``index`` como actual; solo redibuja si cambia algo (se llama varias veces por segundo)."""
        if (index, color) != (self.current, self.highlight):
            self.current, self.highlight = index, color
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(self.labels)
        p.setFont(_font(9, True))
        widest = max((p.fontMetrics().horizontalAdvance(x) for x in self.labels), default=0)
        margin = max(40, widest / 2 + 6)  # el primer y el último texto van centrados en su círculo y deben caber
        w = self.width() - 2 * margin
        y = 16
        r = 9
        xs = [margin + (w * i / (n - 1) if n > 1 else w / 2) for i in range(n)]
        for i in range(n - 1):  # líneas entre círculos: verdes hasta la fase actual
            p.setPen(QPen(QColor(theme.SUCCESS if i < self.current else theme.BORDER_STRONG), 1.5))
            p.drawLine(QPointF(xs[i] + r + 5, y), QPointF(xs[i + 1] - r - 5, y))
        for i, (x, text) in enumerate(zip(xs, self.labels)):
            done, now = i < self.current, i == self.current
            if done:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(theme.SUCCESS))
                p.drawEllipse(QPointF(x, y), r, r)
                p.drawPixmap(QRectF(x - 6, y - 6, 12, 12).toRect(), _check_pixmap())
            else:
                p.setPen(QPen(QColor(self.highlight if now else theme.BORDER_STRONG), 1.5))
                p.setBrush(QColor(theme.SURFACE))
                p.drawEllipse(QPointF(x, y), r, r)
                if now:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(self.highlight))
                    p.drawEllipse(QPointF(x, y), 4, 4)
            p.setFont(_font(9, now))
            p.setPen(QColor(theme.TEXT if now else theme.MUTED if done else theme.FAINT))
            p.drawText(QRectF(x - margin, y + r + 7, 2 * margin, 20),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, text)
        p.end()


def _check_pixmap():
    """Marca de verificación blanca para los pasos hechos (import diferido: ``icons`` no hace falta al importar)."""
    from .icons import pixmap

    return pixmap("check", "#ffffff", 12, 2.4)


# Roles de datos propios de los elementos de lista que leen ``StepDelegate`` y ``ChoiceDelegate``.
ROLE_HINT = Qt.ItemDataRole.UserRole + 1  # línea secundaria
ROLE_STATE = Qt.ItemDataRole.UserRole + 2  # pasos: done | current | todo | locked; lista: header | item | soon
ROLE_BADGE = Qt.ItemDataRole.UserRole + 3  # (texto, color) de la etiqueta de la derecha


def _font(size: float, bold: bool = False) -> QFont:
    """Fuente para el pintado manual (el QSS del tema no se aplica dentro de ``paintEvent`` ni de los delegados)."""
    f = QFont("Segoe UI")
    f.setPointSizeF(size)
    f.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    return f


class StepDelegate(QStyledItemDelegate):
    """Paso de la barra lateral: círculo (número, ✓ o actual), título y una línea de ayuda."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), 52)

    def paint(self, p: QPainter, option, index) -> None:
        state = index.data(ROLE_STATE) or "todo"
        r = QRectF(option.rect.adjusted(8, 2, -8, -2))
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver) and state != "locked"
        if state == "current" or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.SURFACE_3 if state == "current" else theme.SURFACE_2))
            p.drawRoundedRect(r, 5, 5)
            if state == "current":
                p.setBrush(QColor(theme.ACCENT))
                p.drawRoundedRect(QRectF(r.left(), r.top() + 12, 2.5, r.height() - 24), 1.2, 1.2)
        c = QPointF(r.left() + 22, r.center().y())
        dim = state == "locked"
        if state == "done":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.SUCCESS))
            p.drawEllipse(c, 10, 10)
            p.drawPixmap(QRectF(c.x() - 6, c.y() - 6, 12, 12).toRect(), _check_pixmap())
        else:
            ring = theme.ACCENT if state == "current" else theme.BORDER_STRONG if dim else theme.FAINT
            p.setPen(QPen(QColor(ring), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, 10, 10)
            p.setFont(_font(8.5, True))
            p.setPen(QColor(theme.TEXT if state == "current" else theme.FAINT if dim else theme.MUTED))
            p.drawText(QRectF(c.x() - 10, c.y() - 10, 20, 20), Qt.AlignmentFlag.AlignCenter, str(index.row() + 1))
        x = r.left() + 44
        p.setFont(_font(10, state == "current"))
        p.setPen(QColor(theme.FAINT if dim else theme.TEXT))
        p.drawText(QRectF(x, r.top() + 7, r.right() - x, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   index.data(Qt.ItemDataRole.DisplayRole) or "")
        p.setFont(_font(8.5))
        p.setPen(QColor(theme.FAINT if dim else theme.MUTED))
        p.drawText(QRectF(x, r.top() + 26, r.right() - x, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   index.data(ROLE_HINT) or "")
        p.restore()


class ChoiceDelegate(QStyledItemDelegate):
    """Lista de elección con secciones: título, línea secundaria y etiqueta de estado a la derecha."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), 32 if index.data(ROLE_STATE) == "header" else 50)

    def paint(self, p: QPainter, option, index) -> None:
        kind = index.data(ROLE_STATE) or "item"
        r = QRectF(option.rect.adjusted(4, 2, -4, -2))
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if kind == "header":
            p.setFont(_font(8.5, True))
            p.setPen(QColor(theme.MUTED))
            p.drawText(r.adjusted(10, 8, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       (index.data(Qt.ItemDataRole.DisplayRole) or "").upper())
            p.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver) and kind != "soon"
        if selected or hover:
            p.setPen(QPen(QColor(theme.ACCENT), 1) if selected else Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.SURFACE_3 if selected else theme.SURFACE_2))
            p.drawRoundedRect(r, 5, 5)
        soon = kind == "soon"
        badge = index.data(ROLE_BADGE)
        right = r.right() - 12
        if badge:
            text, color = badge
            text = text.replace("✓", "").strip()  # la etiqueta ya va enmarcada en su color; la marca sobra
            p.setFont(_font(8, True))
            bw = p.fontMetrics().horizontalAdvance(text) + 14
            br = QRectF(right - bw, r.center().y() - 10, bw, 20)
            p.setPen(QPen(QColor(color), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(br.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            p.setPen(QColor(color))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, text)
            right = br.left() - 10
        x = r.left() + 14
        p.setFont(_font(10, True))
        p.setPen(QColor(theme.MUTED if soon else theme.TEXT))
        p.drawText(QRectF(x, r.top() + 7, right - x, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   index.data(Qt.ItemDataRole.DisplayRole) or "")
        p.setFont(_font(9))
        p.setPen(QColor(theme.BORDER if soon else theme.MUTED))
        p.drawText(QRectF(x, r.top() + 27, right - x, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   index.data(ROLE_HINT) or "")
        p.restore()


class VideoView(QWidget):
    """Vídeo fluido: se alimenta solo de una fuente (``attach``) y pinta cada frame nuevo en cuanto llega
    (sondeo a ~60 Hz, solo redibuja si hay frame nuevo). El escalado se hace una vez, con OpenCV (rápido) y
    al tamaño exacto del widget, así que pintar no cuesta casi nada. Dibuja una caja normalizada y permite
    seleccionar una región con el ratón (emite ``roi_selected`` en coordenadas normalizadas).

    El sondeo se detiene mientras el widget está oculto: las páginas que no se ven no gastan CPU.
    """

    roi_selected = Signal(object)  # NormBox

    def __init__(self, min_height: int = 240) -> None:
        super().__init__()
        self.setMinimumHeight(min_height)
        self._image: QImage | None = None
        self._buf: np.ndarray | None = None  # la QImage apunta a este array sin copiarlo: hay que mantenerlo vivo
        self._orig: np.ndarray | None = None  # imagen fija (congelada) para re-escalar al redimensionar
        self._aspect = 16 / 9
        self._box: NormBox | None = None
        self._box_color = QColor(theme.SUCCESS)
        self._selecting = False
        self._drag: tuple[QPointF, QPointF] | None = None
        self._placeholder = t("Sin imagen", "No video")
        self._source: Callable[[], object] | None = None
        self._last_key: tuple | None = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._pull)

    # --- fuente en directo ---
    def attach(self, source: Callable[[], object] | None) -> None:
        """Conecta la fuente del vídeo en directo.

        ``source()`` devuelve el último ``Frame`` (o ``None``) y se llama en el hilo de la interfaz, así que debe
        ser inmediata. ``attach(None)`` detiene el directo (p. ej. para mostrar una imagen congelada).
        """
        self._source = source
        self._last_key = None
        if source is not None:
            self._orig = None
            if self.isVisible():
                self._timer.start(15)
        else:
            self._timer.stop()

    def showEvent(self, event) -> None:  # noqa: N802
        if self._source is not None:
            self._timer.start(15)
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def _pull(self) -> None:
        """Tic del temporizador: pinta el frame de la fuente solo si es distinto del último mostrado."""
        try:
            frame = self._source() if self._source is not None else None
        except Exception:  # la sesión puede estar cerrándose: se reintenta en el siguiente tic
            frame = None
        if frame is None:
            return
        key = (frame.index, frame.t_s)
        if key == self._last_key:
            return
        self._last_key = key
        self._show(frame.image)

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    def set_frame(self, image: np.ndarray | None) -> None:
        """Imagen fija (p. ej. congelada para marcar el ojo)."""
        self._orig = image
        self._show(image)

    def _fit(self, w: int, h: int) -> tuple[int, int]:
        """Tamaño en píxeles de una imagen ``w``×``h`` encajada en el widget sin deformarla."""
        scale = min(max(1, self.width()) / w, max(1, self.height()) / h)
        return max(1, int(w * scale)), max(1, int(h * scale))

    def _show(self, image: np.ndarray | None) -> None:
        """Escala ``image`` al tamaño de pintado y la convierte a ``QImage`` (``None`` muestra el texto de espera)."""
        if image is None:
            self._image, self._buf = None, None
            self.update()
            return
        h, w = image.shape[:2]
        self._aspect = w / h
        tw, th = self._fit(w, h)
        if (tw, th) != (w, h):
            # Al reducir, lineal para no perder detalle; al ampliar, píxeles nítidos (sirve para ver el ojo).
            interp = cv2.INTER_LINEAR if tw < w else cv2.INTER_NEAREST
            image = cv2.resize(image, (tw, th), interpolation=interp)
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        self._buf = rgb
        self._image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888)
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        if self._orig is not None:
            self._show(self._orig)
        super().resizeEvent(event)

    def set_box(self, box: NormBox | None, color: str = theme.SUCCESS) -> None:
        """Recuadro superpuesto en coordenadas normalizadas (``None`` lo quita)."""
        if box == self._box and QColor(color) == self._box_color:
            return
        self._box = box
        self._box_color = QColor(color)
        self.update()

    def enable_selection(self, on: bool) -> None:
        """Activa o desactiva la selección de una región arrastrando con el ratón."""
        self._selecting = on
        self._drag = None
        self.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        self.update()

    def _target_rect(self) -> QRectF:
        """Zona del widget que ocupa la imagen (centrada, con bandas si la proporción no coincide)."""
        w, h = self.width(), self.height()
        if w / max(1, h) > self._aspect:
            tw, th = h * self._aspect, h
        else:
            tw, th = w, w / self._aspect
        return QRectF((w - tw) / 2, (h - th) / 2, tw, th)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.VIDEO_BG))
        if self._image is None:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
            return
        r = self._target_rect()
        if abs(self._image.width() - r.width()) <= 1 and abs(self._image.height() - r.height()) <= 1:
            p.drawImage(r.topLeft(), self._image)  # sin re-escalar: muy barato
        else:
            p.drawImage(r, self._image)
        if self._box is not None:
            b = self._box
            p.setPen(QPen(self._box_color, 2))
            p.drawRect(QRectF(r.x() + b.x * r.width(), r.y() + b.y * r.height(), b.w * r.width(), b.h * r.height()))
        if self._drag is not None:
            a, c = self._drag
            p.setPen(QPen(QColor(theme.ACCENT), 2, Qt.PenStyle.DashLine))
            p.drawRect(QRectF(a, c).normalized())

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if self._selecting and self._image is not None:
            self._drag = (e.position(), e.position())

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None:
            self._drag = (self._drag[0], e.position())
            self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        """Convierte el rectángulo arrastrado a coordenadas normalizadas de la imagen y emite ``roi_selected``."""
        if self._drag is None:
            return
        a, c = self._drag
        self._drag = None
        rect = QRectF(a, c).normalized().intersected(self._target_rect())
        r = self._target_rect()
        if rect.width() < 4 or rect.height() < 4:  # un clic o un arrastre mínimo no cuenta como selección
            self.update()
            return
        box = NormBox((rect.x() - r.x()) / r.width(), (rect.y() - r.y()) / r.height(),
                      rect.width() / r.width(), rect.height() / r.height())
        self.update()
        self.roi_selected.emit(box)


class EyeZoom(QWidget):
    """Lupa: la zona del ojo en directo, ampliada (píxeles grandes, sin suavizar) para ver si se sigue bien."""

    def __init__(self, size: int = 220) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self._image: QImage | None = None
        self._buf: np.ndarray | None = None
        self._border = QColor(theme.BORDER)
        self.caption = ""

    def set_crop(self, crop: np.ndarray | None, border: str = theme.BORDER) -> None:
        """Muestra el recorte ampliado (BGR o gris) con el marco del color indicado; ``None`` lo vacía."""
        self._border = QColor(border)
        if crop is None or crop.size == 0:
            self._image = None
        else:
            h, w = crop.shape[:2]
            scale = min(self.width() / w, (self.height() - 18) / h)  # 18 px abajo para el pie (``caption``)
            big = cv2.resize(crop, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_NEAREST)
            rgb = cv2.cvtColor(big, cv2.COLOR_BGR2RGB) if big.ndim == 3 else cv2.cvtColor(big, cv2.COLOR_GRAY2RGB)
            self._buf = rgb
            self._image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.VIDEO_BG))
        if self._image is not None:
            x = (self.width() - self._image.width()) // 2
            y = (self.height() - 18 - self._image.height()) // 2
            p.drawImage(x, y, self._image)
        p.setPen(QPen(self._border, 2))
        p.drawRect(self.rect().adjusted(1, 1, -1, -1))
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(0, self.height() - 18, self.width(), 16), Qt.AlignmentFlag.AlignCenter, self.caption)


class ScorePlot(QWidget):
    """Gráfica de la "apertura" del ojo en los últimos segundos (caídas = parpadeos)."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(110)
        self._t: list[float] = []
        self._v: list[float] = []
        self._threshold_value: float | None = None
        self._marks: list[float] = []
        self.window_s = 12.0

    def set_data(self, times: list[float], values: list[float], threshold_value: float | None, marks: list[float]) -> None:
        """Serie (tiempos y valores), umbral de parpadeo (línea roja) y tiempos de los parpadeos detectados."""
        self._t, self._v, self._threshold_value, self._marks = times, values, threshold_value, marks
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.VIDEO_BG))
        if len(self._t) < 2:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, t("La señal del ojo aparecerá aquí",
                                                                    "The eye signal will appear here"))
            return
        t_end = self._t[-1]
        t0 = t_end - self.window_s
        # Escala vertical automática; el umbral siempre queda dentro para que se vea la línea.
        lo, hi = min(self._v), max(self._v)
        if self._threshold_value is not None:
            lo = min(lo, self._threshold_value)
        span = max(1e-3, hi - lo)
        w, h = self.width(), self.height() - 8

        def pt(t: float, v: float) -> QPointF:
            return QPointF((t - t0) / self.window_s * w, 4 + (1 - (v - lo) / span) * h)

        for m in self._marks:
            if m >= t0:
                x = (m - t0) / self.window_s * w
                p.setPen(QPen(QColor(theme.WARNING), 1, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        if self._threshold_value is not None:
            p.setPen(QPen(QColor(theme.DANGER), 1, Qt.PenStyle.DashLine))
            y = pt(t0, self._threshold_value).y()
            p.drawLine(QPointF(0, y), QPointF(w, y))
        p.setPen(QPen(QColor(theme.ACCENT), 1.5))
        pts = [pt(ti, v) for ti, v in zip(self._t, self._v) if ti >= t0]
        for a, b in zip(pts, pts[1:]):
            p.drawLine(a, b)


class Background(QObject):
    """Ejecuta una función en otro hilo y devuelve el resultado al hilo de la interfaz.

    Se usa para todo lo que puede tardar o colgarse (enumerar puertos y cámaras, abrir la capturadora, conectar
    el ESP32, cálculos largos): la interfaz y el vídeo siguen respondiendo. Cada ``run`` tiene un número de
    generación; el resultado de una generación ya superada o caducada no sale por ``done``/``failed``.
    """

    done = Signal(object)
    failed = Signal(str)
    late = Signal(object)  # resultado que llegó después de agotarse el tiempo (para liberarlo: puerto, cámara…)
    _result = Signal(int, bool, object)  # generación, éxito, resultado o mensaje de error

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._active: int | None = None
        self._gen = 0
        # Se emite desde el hilo de trabajo: Qt la entrega en cola en el hilo de este objeto (el de la interfaz).
        self._result.connect(self._deliver)

    @property
    def running(self) -> bool:
        return self._active is not None

    def run(self, fn: Callable[[], object], timeout_s: float | None = None, timeout_message: str = "") -> None:
        """Ejecuta ``fn`` en otro hilo. Con ``timeout_s``, si no termina a tiempo la página recibe ``failed``
        (nunca se queda esperando para siempre) y el resultado tardío, si llega, sale por ``late``."""
        self._gen += 1
        gen = self._gen
        self._active = gen

        def work() -> None:
            try:
                result, ok = fn(), True
            except BaseException as exc:  # el error se muestra en la página, que siempre recibe una respuesta
                result, ok = str(exc) or f"{type(exc).__name__}\n\n{traceback.format_exc(limit=3)}", False
            try:
                self._result.emit(gen, ok, result)
            except RuntimeError:  # el objeto Qt ya se destruyó (ventana cerrada): no hay a quién avisar
                pass

        threading.Thread(target=work, daemon=True).start()
        if timeout_s is not None:
            QTimer.singleShot(int(timeout_s * 1000), self, lambda: self._expire(gen, timeout_message))

    def _expire(self, gen: int, message: str) -> None:
        """Plazo agotado: si la generación sigue activa, se da por fallida y se libera ``running``."""
        if self._active == gen:
            self._active = None
            self.failed.emit(message or t("La operación no respondió a tiempo", "The operation timed out"))

    def _deliver(self, gen: int, ok: bool, result: object) -> None:
        """Entrega el resultado; si ya no es la generación activa, solo lo reenvía por ``late`` (si fue bien)."""
        if self._active != gen:
            if ok:
                self.late.emit(result)
            return
        self._active = None
        if ok:
            self.done.emit(result)
        else:
            self.failed.emit(result)


def hbox(*widgets: QWidget | None, spacing: int = 10, stretch_last: bool = False) -> QWidget:
    """Fila horizontal sin márgenes; cada ``None`` es un espacio elástico."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for x in widgets:
        if x is None:
            lay.addStretch(1)
        else:
            lay.addWidget(x)
    if stretch_last:
        lay.addStretch(1)
    return w
