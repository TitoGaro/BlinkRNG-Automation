# Receta de PyInstaller para el ejecutable de Windows.
#
#   .venv\Scripts\python -m PyInstaller packaging\rngauto.spec --noconfirm
#
# Genera dist\BlinkRNG-Automation\ (carpeta con el .exe y sus librerías). Se distribuye la carpeta comprimida:
# arranca más rápido que un único .exe y los antivirus la marcan menos.
import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).parent  # noqa: F821 (lo define PyInstaller)
PKG = ROOT / "src" / "rngauto"
VERSION = re.search(r'__version__ = "([^"]+)"', (PKG / "__init__.py").read_text(encoding="utf-8")).group(1)
NUMBERS = tuple(int(n) for n in re.findall(r"\d+", VERSION)[:3]) + (0,)
NAME = "BlinkRNG-Automation"

# Datos que no son código: (origen, carpeta de destino dentro de la aplicación).
datas = [
    (str(ROOT / "encounters" / "bdsp" / "*.toml"), "encounters/bdsp"),
    (str(PKG / "games" / "bdsp" / "data" / "*.json"), "rngauto/games/bdsp/data"),
    (str(PKG / "games" / "bdsp" / "data" / "*.toml"), "rngauto/games/bdsp/data"),
    (str(PKG / "games" / "bdsp" / "data" / "plantillas" / "*.png"), "rngauto/games/bdsp/data/plantillas"),
    (str(PKG / "gui" / "assets" / "*.svg"), "rngauto/gui/assets"),
    (str(PKG / "gui" / "icon.png"), "rngauto/gui"),
    (str(PKG / "vision" / "ocr.ps1"), "rngauto/vision"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    # Módulos de Qt y de Python que la aplicación no usa: el paquete pesa bastante menos.
    excludes=["tkinter", "matplotlib", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtPdf",
              "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtNetwork"],
    noarchive=False,
)
# Librerías de Qt que arrastran algunos plugins pero la aplicación no usa (unos 40 MB): OpenGL por software,
# PDF, QML/Quick y las traducciones de Qt (los textos de la aplicación tienen su propia traducción).
_UNUSED = ("opengl32sw.dll", "qt6pdf", "qt6qml", "qt6quick", "qpdf.dll", "qt6virtualkeyboard")
a.binaries = [b for b in a.binaries if not any(u in Path(b[0]).name.lower() for u in _UNUSED)]
a.datas = [d for d in a.datas if "/translations/" not in d[0].replace("\\", "/")]

pyz = PYZ(a.pure)  # noqa: F821

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "TitoGaro"),
            StringStruct("FileDescription", "RNG Automation"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", NAME),
            StringStruct("LegalCopyright", "Copyright (C) 2026 TitoGaro. GPL-3.0-or-later"),
            StringStruct("OriginalFilename", f"{NAME}.exe"),
            StringStruct("ProductName", "RNG Automation"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    icon=str(ROOT / "packaging" / "rngauto.ico"),
    version=version_info,
    console=False,  # aplicación de ventana: sin consola negra detrás
    upx=False,  # comprimir con UPX dispara falsos positivos de los antivirus
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)  # noqa: F821
