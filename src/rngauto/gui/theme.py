"""Tema visual: oscuro y neutro, con un solo color de acento y colores de estado contenidos.

Las constantes de color se usan también en el pintado manual (``widgets.py``) y en el HTML de las etiquetas.
``STYLE`` es la hoja de estilo (QSS) de toda la aplicación; los widgets eligen su variante con ``objectName``.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir

# Los ``url()`` del QSS necesitan barras normales también en Windows.
_ASSETS = (Path(__file__).with_name("assets")).as_posix()
# El QSS se refiere a los iconos por un prefijo y no por la ruta: una ruta con apóstrofo (p. ej.
# C:/Users/O'Brien/…) haría que Qt descartase la hoja de estilos entera.
QDir.addSearchPath("rngauto", _ASSETS)

BG = "#0e1014"          # fondo de la ventana
SURFACE = "#15171c"     # barras y tarjetas
SURFACE_2 = "#1b1e24"   # campos, botones, tiles
SURFACE_3 = "#22262e"   # hover
BORDER = "#262a32"
BORDER_STRONG = "#333844"
TEXT = "#e4e6eb"
MUTED = "#8a909c"
FAINT = "#5b616d"
ACCENT = "#4a86e8"
ACCENT_HOVER = "#5b93ec"
SUCCESS = "#3fb950"
WARNING = "#d29922"
DANGER = "#e5534b"
GOLD = "#e3b341"  # shiny
VIDEO_BG = "#08090b"

FONT = '"Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif'
MONO = '"Cascadia Mono", "Consolas", monospace'

STYLE = f"""
* {{ font-family: {FONT}; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QWidget#root, QWidget#page {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QLabel#eyebrow {{ color: {MUTED}; font-size: 8.5pt; font-weight: 600; letter-spacing: 0.4px; }}
QLabel#title {{ font-size: 17pt; font-weight: 600; }}
QLabel#subtitle {{ color: {MUTED}; font-size: 10pt; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#faint {{ color: {FAINT}; font-size: 9pt; }}
QLabel#heading {{ font-size: 13pt; font-weight: 600; }}
QLabel#section {{ font-size: 10.5pt; font-weight: 600; }}
QLabel#big {{ font-size: 22pt; font-weight: 600; }}
QLabel#message {{ font-size: 12.5pt; font-weight: 500; }}
QLabel#hero {{ font-size: 30pt; font-weight: 600; }}
QLabel#heroCaption {{ color: {MUTED}; font-size: 10pt; }}
QLabel#caption {{ color: {MUTED}; font-size: 8pt; font-weight: 600; letter-spacing: 0.5px; }}
QLabel#kpi {{ font-size: 15pt; font-weight: 600; }}
QLabel#kpiSub {{ color: {MUTED}; font-size: 8.5pt; }}
QLabel#brand {{ font-size: 11pt; font-weight: 600; }}
QLabel#brandSub {{ color: {MUTED}; font-size: 8.5pt; }}
QLabel#brandAuthor {{ color: {FAINT}; font-size: 8.5pt; }}
QLabel#pill {{ background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 4px; padding: 2px 8px;
    color: {MUTED}; font-size: 9pt; }}

QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#tile {{ background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QFrame#topbar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
QFrame#bottombar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
QFrame#separator {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#vseparator {{ background: {BORDER}; max-width: 1px; min-width: 1px; border: none; }}

QPushButton {{ background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: 6px; padding: 7px 14px; }}
QPushButton:hover {{ background: {SURFACE_3}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {FAINT}; border-color: {BORDER}; background: {SURFACE}; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: #243247; border-color: #243247; color: #7d8aa0; }}
QPushButton#danger {{ background: {DANGER}; border: 1px solid {DANGER}; color: white; font-weight: 600; }}
QPushButton#danger:hover {{ background: #ec6a63; }}
QPushButton#ghost {{ background: transparent; border: 1px solid transparent; color: {MUTED}; padding: 6px 8px; }}
QPushButton#ghost:hover {{ background: {SURFACE_2}; color: {TEXT}; }}
QPushButton#ghost:checked {{ color: {TEXT}; }}
QPushButton#start {{ background: {SUCCESS}; border: 1px solid {SUCCESS}; color: #08200f; font-weight: 600;
    font-size: 11pt; padding: 10px 26px; }}
QPushButton#start:hover {{ background: #4cc65c; }}
QPushButton#start:disabled {{ background: #1d3324; border-color: #1d3324; color: #5f7a66; }}
QPushButton#stop {{ background: {DANGER}; border: 1px solid {DANGER}; color: white; font-weight: 600;
    font-size: 11pt; padding: 10px 26px; }}
QPushButton#stop:disabled {{ background: #3a2224; border-color: #3a2224; color: #8e6d6f; }}

QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{ background: {SURFACE_2}; border: 1px solid {BORDER_STRONG};
    border-radius: 5px; padding: 5px 8px; min-height: 18px; }}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QLineEdit:hover {{ border-color: #3f4553; }}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url(rngauto:chevron_down.svg); width: 12px; height: 12px; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QComboBox QAbstractItemView {{ background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; padding: 4px;
    selection-background-color: {SURFACE_3}; selection-color: {TEXT}; outline: none; }}

QListWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px; outline: none; }}
QListWidget::item {{ padding: 8px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {SURFACE_3}; color: {TEXT}; }}
QListWidget#steps, QListWidget#choices {{ background: transparent; border: none; padding: 0; }}

QPlainTextEdit {{ background: {VIDEO_BG}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px;
    font-family: {MONO}; font-size: 8.5pt; color: #c5c9d2; }}

QProgressBar {{ background: {SURFACE_2}; border: none; border-radius: 3px; height: 6px; max-height: 6px;
    text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QProgressBar#slim {{ height: 4px; max-height: 4px; border-radius: 2px; }}

QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border-radius: 3px; border: 1px solid #4a505d;
    background: {SURFACE_2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url(rngauto:check.svg); }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 8px; border: 1px solid #4a505d;
    background: {SURFACE_2}; }}
QRadioButton::indicator:checked {{ border: none; image: url(rngauto:radio_dot.svg); }}

QToolButton {{ background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: 6px; padding: 5px 10px; }}
QToolButton:hover {{ background: {SURFACE_3}; }}
QToolButton:checked {{ border-color: {ACCENT}; }}
QToolButton#link {{ background: transparent; border: none; color: {MUTED}; padding: 4px 2px; }}
QToolButton#link:hover, QToolButton#link:checked {{ color: {TEXT}; }}

QSlider::groove:horizontal {{ height: 4px; background: {BORDER_STRONG}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {MUTED}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; background: {TEXT}; }}
QSlider::sub-page:horizontal:disabled, QSlider::handle:horizontal:disabled {{ background: {FAINT}; }}

QGroupBox {{ border: 1px solid {BORDER}; border-radius: 6px; margin-top: 14px; padding: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED}; }}
QToolTip {{ background: {SURFACE_3}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px 6px; }}
QScrollArea {{ border: none; background: transparent; }}
QTableWidget {{ background: {SURFACE}; gridline-color: {BORDER}; border: 1px solid {BORDER}; border-radius: 6px;
    selection-background-color: {SURFACE_3}; selection-color: {TEXT}; }}
QTableWidget::item {{ padding: 6px 10px; }}
QHeaderView {{ background: {SURFACE}; border: none; }}
QHeaderView::section {{ background: {SURFACE}; border: none; border-bottom: 1px solid {BORDER}; padding: 7px 10px;
    color: {MUTED}; font-weight: 600; font-size: 8.5pt; }}
QTableCornerButton::section {{ background: {SURFACE}; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {BORDER_STRONG}; border-radius: 4px; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: #454b58; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMessageBox {{ background: {SURFACE}; }}
QMessageBox QLabel {{ font-size: 10pt; }}
QDialog {{ background: {SURFACE}; }}
QMenu {{ background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {SURFACE_3}; }}
"""
