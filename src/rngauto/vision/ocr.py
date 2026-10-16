"""OCR de Windows (Windows.Media.Ocr) para leer cifras de la pantalla.

Se usa PowerShell 5.1 (viene con Windows 10/11) y el idioma de OCR del perfil del usuario: las cifras se
reconocen igual en cualquier idioma. Sin dependencias de Python extra. Tarda ~1-2 s por imagen, así que se
compone una sola imagen con todo lo que haya que leer.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from rngauto.i18n import t
from rngauto.vision.imageio import imwrite

SCRIPT = Path(__file__).with_name("ocr.ps1")


class OcrUnavailable(RuntimeError):
    """El OCR de Windows no está disponible o no respondió (el llamador puede pedir los datos a mano)."""

    pass


def ocr_words(image: np.ndarray, timeout_s: float = 20.0) -> list[tuple[float, float, str]]:
    """(x, y del centro en píxeles, texto) de cada palabra reconocida en ``image`` (BGR).

    La imagen pasa por un PNG temporal porque el script de PowerShell solo sabe abrir ficheros; se borra siempre
    al terminar. ``timeout_s`` acota la espera: un PowerShell colgado no bloquea la automatización."""
    if os.name != "nt":
        raise OcrUnavailable(t("El OCR de Windows solo está disponible en Windows", "Windows OCR is only available on Windows"))
    fd, name = tempfile.mkstemp(suffix=".png", prefix="rngauto-ocr-")
    os.close(fd)
    try:
        if not imwrite(name, image):
            raise OcrUnavailable(t("No se pudo preparar la imagen para el OCR", "Could not prepare the image for OCR"))
        # Sin ventana de consola: si no, aparecería un parpadeo de PowerShell en cada lectura.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(SCRIPT), "-Path", name],
                capture_output=True, timeout=timeout_s, creationflags=flags)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OcrUnavailable(t("El OCR de Windows no respondió: {exc}", "Windows OCR did not respond: {exc}", exc=exc)) from exc
        if proc.returncode != 0:
            raise OcrUnavailable(t("El OCR de Windows falló: {err}", "Windows OCR failed: {err}",
                                    err=proc.stderr.decode("utf-8", "replace").strip()[:200]))
        out = []
        # Formato «x|y|texto». Las coordenadas pueden salir con coma decimal según la configuración regional.
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            try:
                out.append((float(parts[0].replace(",", ".")), float(parts[1].replace(",", ".")), parts[2].strip()))
            except ValueError:
                continue
        return out
    finally:
        try:
            os.remove(name)
        except OSError:
            pass
