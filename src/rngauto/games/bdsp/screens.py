"""Reconocimiento de pantallas fijas de BDSP.

``menu_visible``: el menú del botón X (cuadrícula de 2×4 iconos sobre un panel blanco). Se reconoce por cinco
franjas verticales blancas entre las columnas de iconos y, en el centro, variedad de color (los iconos).
Si aparece el aviso «nuevo» arriba, las franjas empiezan algo más abajo; se aceptan ambas variantes.

Distribución del menú (útil para navegar):  fila 1: Pokédex, Pokémon, Mochila, Ficha de entrenador;
fila 2: Mapa, Cápsulas, Opciones, Regalo misterioso. «+» abre el mapa desde el menú; al salir del mapa el
cursor queda sobre «Mapa», justo debajo de «Pokédex».
"""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from rngauto.vision.frames import NormBox
from rngauto.vision.imageio import imread

# Franjas blancas del menú X (alto en fracción de la imagen); con el aviso «nuevo» empiezan más abajo.
STRIPE_TOP = 0.110
STRIPE_TOP_BELOW_BANNER = 0.170
STRIPE_BOTTOM = 0.598
CROSS_BOX = NormBox(0.20, 0.15, 0.60, 0.37)  # zona de los iconos: tiene que tener variedad de color


def _stats(image: np.ndarray, box: NormBox) -> tuple[np.ndarray, np.ndarray]:
    """Media y desviación típica por canal (BGR) de la región ``box``."""
    region = box.crop(image).reshape(-1, image.shape[2]).astype(np.float64)
    return region.mean(axis=0), region.std(axis=0)


def _is_white(mean: np.ndarray, std: np.ndarray, min_rgb_sum: float = 500, max_stddev_sum: float = 10) -> bool:
    """¿Región blanca? Clara (suma de canales alta), uniforme (poca desviación) y gris (proporciones de canal
    ≈ 1/3)."""
    total = float(mean.sum())
    if total < min_rgb_sum or float(std.sum()) > max_stddev_sum:
        return False
    ratio = mean / total
    return float(np.linalg.norm(ratio - 1 / 3)) <= 0.08


def menu_visible(image: np.ndarray) -> bool:
    """¿Está abierto el menú del botón X? Ver la descripción del módulo."""
    if image is None or image.ndim != 3 or image.size == 0:
        return False
    if float(_stats(image, CROSS_BOX)[1].sum()) < 100:
        return False
    for top in (STRIPE_TOP, STRIPE_TOP_BELOW_BANNER):
        if all(_is_white(*_stats(image, NormBox(0.160 + 0.166 * i, top, 0.015, STRIPE_BOTTOM - top)))
               for i in range(5)):
            return True
    return False


def dialog_visible(image: np.ndarray) -> bool:
    """Cuadro de diálogo del mundo abierto (p. ej. el mensaje del profesor Serbal, el del pedestal o el del
    legendario): interior blanco junto al borde izquierdo y marco con detalle (no liso) a ambos lados. No se usa
    una franja junto al borde derecho porque con textos largos la primera línea la invade (comprobado con capturas
    reales); en su lugar se usa la franja blanca bajo el borde superior, que el texto nunca ocupa."""
    if image is None or image.ndim != 3 or image.size == 0:
        return False
    if not _is_white(*_stats(image, NormBox(0.25, 0.820, 0.50, 0.006))):
        return False
    if not _is_white(*_stats(image, NormBox(0.21, 0.835, 0.008, 0.12))):
        return False
    return (float(_stats(image, NormBox(0.18, 0.835, 0.02, 0.12))[1].sum()) >= 30
            and float(_stats(image, NormBox(0.822, 0.835, 0.02, 0.12))[1].sum()) >= 30)


def dialog_arrow_visible(image: np.ndarray) -> bool:
    """Flechita ▼ de «sigue» en el borde derecho del cuadro de texto: el texto ha terminado y hay más detrás.
    Las preguntas y el último texto antes de un suceso no la llevan. Triángulo blanco sobre la banda gris del
    borde (medido en capturas reales; no depende del idioma)."""
    if image is None or image.ndim != 3 or image.size == 0:
        return False
    p = NormBox(0.812, 0.925, 0.022, 0.045).crop(image).reshape(-1, 3).astype(int)
    return bool(p.size) and float((p.min(axis=1) > 225).mean()) >= 0.12


def _rgb_ratio(mean_bgr: np.ndarray) -> np.ndarray:
    """Proporción de cada canal (en orden RGB) sobre la suma: el color sin el brillo."""
    rgb = mean_bgr[::-1]
    return rgb / max(1e-6, float(rgb.sum()))


def _button(image: np.ndarray, box: NormBox, ratio_rgb: tuple[float, float, float], max_std: float) -> bool:
    """¿Hay en ``box`` un botón del color ``ratio_rgb``? Exige algo de detalle (el texto) pero no demasiado."""
    mean, std = _stats(image, box)
    s = float(std.sum())
    return 30 <= s <= max_std and float(np.linalg.norm(_rgb_ratio(mean) - np.array(ratio_rgb))) <= 0.2


def battle_menu_visible(image: np.ndarray) -> bool:
    """Menú de combate (Luchar / Pokémon / Bolsa / Huir) en un combate normal:
    recuadros blancos de PS del rival y propio, recuadro de la Poké Ball y los cuatro botones por su color
    (rojo, verde, amarillo, azul). No depende del idioma."""
    if image is None or image.ndim != 3 or image.size == 0:
        return False
    if not (_is_white(*_stats(image, NormBox(0.708, 0.070, 0.005, 0.028)))
            or _is_white(*_stats(image, NormBox(0.982, 0.070, 0.005, 0.028)))):
        return False
    if not any(_is_white(*_stats(image, b)) for b in (NormBox(0.140, 0.922, 0.100, 0.010),
                                                      NormBox(0.140, 0.910, 0.100, 0.010),
                                                      NormBox(0.405, 0.925, 0.100, 0.010))):
        return False
    if not (_is_white(*_stats(image, NormBox(0.890, 0.475, 0.02, 0.03)))
            and _is_white(*_stats(image, NormBox(0.960, 0.475, 0.03, 0.03)))):
        return False
    y = [0.585 + k * 0.1075 for k in range(4)]
    return (_button(image, NormBox(0.817, y[0], 0.150, 0.070), (0.541961, 0.231317, 0.226722), 150)
            and _button(image, NormBox(0.817, y[1], 0.150, 0.070), (0.255944, 0.582771, 0.161285), 150)
            and _button(image, NormBox(0.817, y[2], 0.150, 0.070), (0.485857, 0.414946, 0.099197), 100)
            and _button(image, NormBox(0.817, y[3], 0.150, 0.070), (0.250977, 0.269868, 0.479155), 100))


# --- Bolsa (validado con capturas reales) ---------------------------------------------------------------------
BAG_TAB_X = (0.5885, 0.6318, 0.6745, 0.7172)  # Botiquín, Poké Balls, objetos de combate, bayas
BAG_TAB_Y = 0.0954
MASTER_BALL_ICON = NormBox(0.4297, 0.7546, 0.0234, 0.0417)  # icono del objeto en el recuadro de descripción
_MASTER_BALL_TEMPLATE = Path(__file__).with_name("data") / "plantillas" / "master_ball.png"


BAG_TAB_MIN_WHITE = 0.5  # en la Bolsa real las 4 pestañas son ~55-70 % blancas; en menú/Pokédex/mapa, ≤ 44 %
BAG_TAB_MIN_BLUE = 0.08  # las no seleccionadas tienen el icono azul (~15 %); la seleccionada, 0 %


def bag_pocket(image: np.ndarray) -> int | None:
    """Bolsillo seleccionado de la Bolsa (la pestaña activa tiene icono y borde rojos; las demás, azules).

    ``None`` si no es la Bolsa: además del rojo se exige la forma de la fila de pestañas (las cuatro con fondo
    blanco y las no seleccionadas con icono azul). Sin eso, cualquier cosa roja en esas posiciones cuenta como
    pestaña: con un Pokémon rojo en pantalla (p. ej. Ho-Oh), antes de abrirse la Bolsa, se daba por abierta en
    «Poké Balls». Validado con capturas reales."""
    if image is None or image.ndim != 3 or image.size == 0:
        return None
    h, w = image.shape[:2]
    reds, whites, blues = [], [], []
    for x in BAG_TAB_X:
        cx, cy, rx, ry = int(x * w), int(BAG_TAB_Y * h), int(0.01875 * w), int(0.0333 * h)
        p = image[max(0, cy - ry):cy + ry, max(0, cx - rx):cx + rx].reshape(-1, 3).astype(int)
        if not p.size:
            return None
        # Rojo > 150 y dominante sobre verde y azul (el fondo amarillo no cuenta)
        reds.append(float(((p[:, 2] > 150) & (p[:, 2] > p[:, 1] + 20) & (p[:, 2] > p[:, 0] + 20)).mean()))
        whites.append(float((p.min(axis=1) > 200).mean()))
        blues.append(float(((p[:, 0] > p[:, 2] + 40) & (p[:, 0] > 120)).mean()))
    best = int(np.argmax(reds))
    others = sorted(reds)[-2]
    if not (reds[best] >= 0.05 and reds[best] > others * 1.5):
        return None
    if min(whites) < BAG_TAB_MIN_WHITE:
        return None
    if any(blues[i] < BAG_TAB_MIN_BLUE for i in range(len(BAG_TAB_X)) if i != best):
        return None
    return best


@lru_cache(maxsize=1)
def _master_ball_template() -> np.ndarray | None:
    """Icono de referencia de la Master Ball (``None`` si falta el fichero). Se lee una sola vez."""
    return imread(_MASTER_BALL_TEMPLATE)


def master_ball_selected(image: np.ndarray) -> bool:
    """¿El objeto seleccionado en la Bolsa es la Master Ball? Por el icono en color (todas las Poké Balls son
    redondas; en gris la Malla Ball se parecía 0,73) y por su morado (único entre las Poké Balls)."""
    tpl = _master_ball_template()
    if image is None or image.ndim != 3 or image.size == 0 or tpl is None:
        return False
    # Correlación normalizada con la plantilla: no depende del brillo de la capturadora.
    crop = cv2.resize(MASTER_BALL_ICON.crop(image), (tpl.shape[1], tpl.shape[0]), interpolation=cv2.INTER_AREA)
    a, b = crop.astype(np.float32).reshape(-1), tpl.astype(np.float32).reshape(-1)
    a0, b0 = a - a.mean(), b - b.mean()
    den = float(np.sqrt((a0 * a0).sum() * (b0 * b0).sum()))
    color_sim = float((a0 * b0).sum() / den) if den > 1e-6 else 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    purple = float(((hsv[:, 0] >= 135) & (hsv[:, 0] <= 165) & (hsv[:, 1] > 80) & (hsv[:, 2] > 80)).mean())
    return color_sim >= 0.85 and purple >= 0.2


# Pantallas que se reconocen sin aprendizaje previo (el resto se aprende la primera vez).
BUILTIN_SCREENS: dict[str, Callable[[np.ndarray], bool]] = {
    "menu": menu_visible, "dialog": dialog_visible, "battle_menu": battle_menu_visible,
}
