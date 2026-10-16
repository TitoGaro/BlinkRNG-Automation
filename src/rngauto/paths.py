"""Rutas de los datos que acompañan a la aplicación (encuentros y plantillas).

Desde el código fuente están en la raíz del repositorio; en el ejecutable, junto a él (PyInstaller los copia a
la carpeta de la aplicación y la expone en ``sys._MEIPASS``). Los datos del usuario van aparte, en
``setup_store.DEFAULT_DATA_DIR``.
"""
from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    """Carpeta que contiene ``encounters/`` y ``assets/``."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[2]  # src/rngauto/paths.py → raíz del repositorio


ROOT = resource_root()
ENCOUNTERS_DIR = ROOT / "encounters" / "bdsp"
ASSETS_DIR = ROOT / "assets"
