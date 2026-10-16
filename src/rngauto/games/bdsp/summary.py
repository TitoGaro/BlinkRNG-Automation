"""Lectura de la ficha del Pokémon (página «Características»), sin depender del idioma del juego.

* Cifras: OCR de Windows sobre las seis posiciones fijas del hexágono (PS arriba, Ataque y Defensa a la
  derecha, Velocidad abajo, Def. Esp. y At. Esp. a la izquierda). Las cifras se leen igual en cualquier idioma.
* Naturaleza: el juego pinta en rojo (con flecha ↑) la estadística que sube y en azul (↓) la que baja. Con eso
  se sabe la naturaleza exacta; si no hay colores es una de las 5 neutras (dan las mismas estadísticas).

Las regiones están medidas en capturas reales a 1080p y se expresan en fracciones de la imagen.
"""
from __future__ import annotations

import re

import cv2
import numpy as np

from rngauto.core.stats import StatsInconsistent, iv_ranges_from_stats
from rngauto.i18n import t
from rngauto.vision.frames import NormBox
from rngauto.vision.ocr import ocr_words

from .identify import Observation
from .screens import _is_white, _stats

# Orden interno de estadísticas: PS, Ataque, Defensa, At. Esp., Def. Esp., Velocidad
STAT_VALUE_BOXES = (  # centradas en cada cifra (±0,022 en vertical: no tocan las etiquetas)
    NormBox(0.200, 0.174, 0.132, 0.044),  # PS («213/213»)
    NormBox(0.339, 0.291, 0.120, 0.044),  # Ataque
    NormBox(0.339, 0.443, 0.120, 0.044),  # Defensa
    NormBox(0.073, 0.291, 0.120, 0.044),  # At. Esp.
    NormBox(0.073, 0.443, 0.120, 0.044),  # Def. Esp.
    NormBox(0.205, 0.547, 0.120, 0.044),  # Velocidad
)
# Etiquetas (y flechas) de las cinco que puede cambiar la naturaleza, en orden interno 1..5
STAT_LABEL_BOXES = {
    1: NormBox(0.355, 0.245, 0.150, 0.050),
    2: NormBox(0.355, 0.487, 0.150, 0.050),
    3: NormBox(0.028, 0.245, 0.147, 0.056),  # sin el borde azul del panel (x ≈ 0,020-0,023)
    4: NormBox(0.028, 0.487, 0.147, 0.050),
    5: NormBox(0.170, 0.585, 0.200, 0.052),
}
# Orden de la tabla de naturalezas (Ataque, Defensa, Velocidad, At. Esp., Def. Esp.) → orden interno
_NATURE_ORDER = (1, 2, 5, 3, 4)
NEUTRAL_NATURES = frozenset({0, 6, 12, 18, 24})
COLOR_MIN_FRACTION = 0.03  # etiquetas coloreadas reales: ≥ 0,055; sin color: 0
# Las neutras no tienen colores: se leen en «Notas del Entrenador». Nombres en los idiomas del juego.
NEUTRAL_NAMES = {
    0: ("hardy", "fuerte", "hardi", "robust", "ardita", "がんばりや", "노력", "勤奋", "勤奮"),
    6: ("docile", "docil", "sanft", "すなお", "온순", "坦率"),
    12: ("serious", "seria", "serieux", "ernst", "まじめ", "성실", "认真", "認真"),
    18: ("bashful", "timida", "pudique", "zaghaft", "ritrosa", "てれや", "수줍음", "害羞"),
    24: ("quirky", "rara", "bizarre", "kauzig", "furba", "きまぐれ", "변덕", "浮躁"),
}
NATURE_TEXT_BOX = NormBox(0.04, 0.17, 0.46, 0.09)  # renglón «Naturaleza: …» de «Notas del Entrenador»


def stats_page_visible(image: np.ndarray) -> bool:
    """Página «Características» de la ficha: panel gris claro a ambos lados y el hexágono azul en medio."""
    if image is None or image.ndim != 3 or image.size == 0:
        return False
    if not (_is_white(*_stats(image, NormBox(0.025, 0.36, 0.020, 0.06)))
            and _is_white(*_stats(image, NormBox(0.460, 0.36, 0.030, 0.06)))):
        return False
    mean, std = _stats(image, NormBox(0.255, 0.36, 0.020, 0.04))
    b, g, r = (float(x) for x in mean)
    return b > r + 50 and float(std.sum()) < 60


def _color_fractions(image: np.ndarray, box: NormBox) -> tuple[float, float]:
    """Fracción de píxeles rojos y azules (los de la naturaleza) en la etiqueta ``box``."""
    p = box.crop(image).reshape(-1, 3).astype(int)
    if not p.size:
        return 0.0, 0.0
    b, g, r = p[:, 0], p[:, 1], p[:, 2]
    red = float(((r > 180) & (g < 120) & (b < 120)).mean())
    blue = float(((b > 170) & (r < 120) & (g < 200)).mean())
    return red, blue


def nature_from_colors(image: np.ndarray) -> frozenset[int]:
    """Naturalezas compatibles con los colores de las etiquetas (1 si hay rojo y azul; las 5 neutras si no)."""
    reds, blues = {}, {}
    for idx, box in STAT_LABEL_BOXES.items():
        reds[idx], blues[idx] = _color_fractions(image, box)
    up = max(reds, key=reds.get)
    down = max(blues, key=blues.get)
    has_up, has_down = reds[up] >= COLOR_MIN_FRACTION, blues[down] >= COLOR_MIN_FRACTION
    if not has_up and not has_down:
        return NEUTRAL_NATURES
    if has_up != has_down or up == down:
        raise ValueError(t("Colores de la naturaleza incoherentes (solo una flecha o la misma estadística)",
                           "Inconsistent nature colours (only one arrow, or both on the same stat)"))
    # Índice de naturaleza = 5 × estadística que sube + estadística que baja, en el orden de la tabla.
    return frozenset({5 * _NATURE_ORDER.index(up) + _NATURE_ORDER.index(down)})


def _ink(crop: np.ndarray) -> np.ndarray:
    """Solo el texto (gris oscuro y sin color): fuera las líneas azules del hexágono y los fondos."""
    c = crop.astype(int)
    hi, lo = c.max(axis=2), c.min(axis=2)
    text = (hi < 120) & (hi - lo < 45)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return np.where(text, gray, 255).astype(np.uint8)


def _value_row(image: np.ndarray, scale: float = 1.0, gap: int = 120) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """Las seis cifras limpias, a la misma altura y en fila, y el tramo horizontal de cada una. El OCR de
    Windows lee mejor un renglón con números que cifras sueltas («75» solo a veces no lo devuelve)."""
    factor = scale * 1080 / max(1, image.shape[0])
    parts = []
    for box in STAT_VALUE_BOXES:
        g = _ink(box.crop(image))
        g = cv2.resize(g, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        ys, xs = np.nonzero(g < 160)
        if ys.size:
            g = g[max(0, ys.min() - 6):ys.max() + 7, max(0, xs.min() - 6):xs.max() + 7]
        parts.append(g)
    h = max(p.shape[0] for p in parts)
    margin = 30
    spans, x = [], margin
    padded = []
    for i, p in enumerate(parts):
        right = gap if i < len(parts) - 1 else 0
        padded.append(cv2.copyMakeBorder(p, (h - p.shape[0]) // 2, h - p.shape[0] - (h - p.shape[0]) // 2, 0, right,
                                         cv2.BORDER_CONSTANT, value=255))
        spans.append((x - gap // 2, x + p.shape[1] + gap // 2))
        x += p.shape[1] + right
    row = cv2.copyMakeBorder(np.hstack(padded), margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=255)
    return cv2.cvtColor(row, cv2.COLOR_GRAY2BGR), spans


def _parse(text: str, hp: bool) -> int | None:
    """Número leído por el OCR (con las confusiones típicas letra/cifra corregidas); ``None`` si no es fiable.

    Para los PS devuelve el máximo de «actual/máximo», también cuando la barra se pierde o se lee como cifra.
    """
    t = text.replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1").replace("|", "/")
    groups = re.findall(r"\d+", t)
    if not groups:
        return None
    if hp:
        if len(groups) >= 2:
            return int(groups[-1])  # PS máximos (tras capturarlo están completos: «213/213»)
        g = groups[0]
        if len(g) % 2 == 0 and g[: len(g) // 2] == g[len(g) // 2:]:
            return int(g[: len(g) // 2])  # la barra perdida: «213213»
        if len(g) % 2 == 1 and len(g) >= 5 and g[: len(g) // 2] == g[len(g) // 2 + 1:]:
            return int(g[: len(g) // 2])  # la barra leída como cifra
        return None
    return int(groups[0]) if len(groups) == 1 else None


def _clean(text: str) -> str:
    """Corrige las confusiones típicas del OCR (O→0, l/I→1, |→/) y pega la barra de los PS."""
    t = text.replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1").replace("|", "/")
    return re.sub(r"\s*/\s*", "/", t)


def read_stat_values(image: np.ndarray) -> tuple[int, ...] | None:
    """Las seis estadísticas (PS máximos, Ataque, Defensa, At. Esp., Def. Esp., Velocidad) o None.

    Se prueba con varios tamaños y cada cifra se toma de la primera lectura que la dé (por posición)."""
    found: list[int | None] = [None] * 6
    for scale in (0.8, 1.5, 1.0):
        row, spans = _value_row(image, scale)
        words = ocr_words(row)
        for i, (x0, x1) in enumerate(spans):
            if found[i] is not None:
                continue
            text = _clean(" ".join(t for x, _, t in sorted(words) if x0 <= x < x1))
            if i == 0:
                found[i] = _parse(text, hp=True)
            elif re.fullmatch(r"\d{1,3}", text):
                found[i] = int(text)
        if all(v is not None for v in found):
            return tuple(int(v) for v in found)
    return None


def _plain(text: str) -> str:
    """Minúsculas y sin tildes ni diacríticos, para comparar nombres de naturaleza en cualquier idioma."""
    import unicodedata

    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _close(a: str, b: str) -> bool:
    """Igual o con una letra de diferencia (el OCR a veces se equivoca en una), solo en palabras largas."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= 1


def neutral_nature_from_text(notes: np.ndarray) -> int | None:
    """Cuál de las 5 neutras dice la página «Notas del Entrenador» (OCR del renglón de la naturaleza)."""
    crop = NATURE_TEXT_BOX.crop(notes)
    big = cv2.resize(crop, None, fx=1080 / max(1, notes.shape[0]), fy=1080 / max(1, notes.shape[0]),
                     interpolation=cv2.INTER_CUBIC)
    words = [_plain(w).strip(".:,;!¡ ") for _, _, w in ocr_words(big)]
    found = {n for n, names in NEUTRAL_NAMES.items() for w in words for name in names if w and _close(w, _plain(name))}
    return found.pop() if len(found) == 1 else None


def read_summary(image: np.ndarray, base_stats: tuple[int, ...], level: int,
                 notes: np.ndarray | None = None) -> tuple[Observation | None, str]:
    """(observación, explicación). La observación es None si algo no cuadra (mejor preguntar que calibrar mal).
    ``notes``: página «Notas del Entrenador», para saber cuál de las neutras es."""
    obs, why, _ = read_summary_any(image, [("", base_stats, level)], notes)
    return obs, why


def read_summary_any(image: np.ndarray, species: list[tuple[str, tuple[int, ...], int]],
                     notes: np.ndarray | None = None) -> tuple[Observation | None, str, str]:
    """Como ``read_summary`` con varias especies posibles (nombre, estadísticas base, nivel): se queda con la
    única cuyas estadísticas cuadran (p. ej. Regirock, Regice o Registeel: sus bases son muy distintas).

    Devuelve ``(observación, explicación, nombre de la especie)``; el nombre va vacío si no se identifica."""
    try:
        natures = nature_from_colors(image)
    except ValueError as exc:
        return None, str(exc), ""
    if len(natures) > 1 and notes is not None:
        try:
            named = neutral_nature_from_text(notes)
        except Exception:
            named = None
        if named is not None:
            natures = frozenset({named})
    values = read_stat_values(image)
    if values is None:
        return None, t("No se pudieron leer las seis estadísticas", "Could not read the six stats"), ""
    # Las neutras no cambian ninguna estadística: cualquiera de ellas sirve para deducir los IV.
    nature = next(iter(natures))
    fits = []
    for name, base_stats, level in species:
        try:
            fits.append((name, iv_ranges_from_stats(base_stats, level, nature, values)))
        except StatsInconsistent:
            continue
    if len(fits) != 1:
        what = (t("ninguna especie", "no species") if not fits else
                t("varias especies ({names})", "several species ({names})", names=", ".join(n for n, _ in fits)))
        return None, t("Estadísticas leídas {values}: cuadran con {what}", "Stats read {values}: they match {what}",
                       values=values, what=what), ""
    name, ivs = fits[0]
    # La ficha no dice si es shiny: None (no «False»), para que la verificación no descarte los advances shiny.
    obs = Observation(is_shiny=None, nature=nature if len(natures) == 1 else None,
                      natures=None if len(natures) == 1 else natures, ivs=ivs)
    from rngauto.core.pokemon import nature_name

    shown = t("neutra (sin saber cuál)", "neutral (unknown which)") if len(natures) > 1 else nature_name(nature)
    who = f"{name}: " if name else ""
    return obs, t("{who}estadísticas {values}, naturaleza {nature}", "{who}stats {values}, nature {nature}",
                  who=who, values=values, nature=shown), name
