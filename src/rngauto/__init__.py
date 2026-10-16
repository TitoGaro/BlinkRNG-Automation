"""rngauto: automatización de RNG (BDSP Blink RNG). Autor: TitoGaro."""
import os

__version__ = "1.0.0"
__author__ = "TitoGaro"

# Debe fijarse ANTES de que cualquier módulo importe cv2: con las transformaciones por hardware de Media
# Foundation, abrir la capturadora en Windows puede tardar minutos.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
