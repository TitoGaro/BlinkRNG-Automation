"""Detección del shiny al empezar el combate (BDSP), sin depender del idioma.

* ``find_sparkles``: se binariza la zona del rival con cuatro umbrales de amarillo/blanco, se buscan objetos
  conexos (4-vecindad, ≥20 px) y se reconoce cada destello por su forma: «bola» (4 rayos en diagonal) o
  «estrella» (5 puntas a 72°). alpha = bolas + estrellas del mejor umbral.
* ``ShinyEncounterWatcher``: los diálogos de combate parten la entrada en fases (antes de nada → animación del
  rival → animación del nuestro → resto). Se guarda el mayor alpha visto durante la animación del rival;
  además, si esa animación dura ≈3,9 s (lo que añade la animación shiny) suma ``DIALOG_ALPHA``.
  Termina al ver el menú de combate. Shiny si alpha ≥ 4.

El sonido de las estrellas (``shiny_sound.py``) suma ``SOUND_ALPHA`` cuando hay plantilla aprendida.
Detalles que se mantienen a propósito porque los umbrales están ajustados con ellos: el umbral de
«quitar el centro» se toma del píxel n.º 85 % en orden de barrido (no ordenado por distancia) y las regiones
se recorren de menor a mayor área.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from rngauto.i18n import t
from rngauto.vision.frames import NormBox

from .screens import _is_white, _stats, battle_menu_visible

WILD_BOX = NormBox(0.40, 0.02, 0.60, 0.93)  # zona del rival
OWN_BOX = NormBox(0.00, 0.10, 0.80, 0.80)  # zona del nuestro
THRESHOLDS = (0x60, 0x70, 0x80, 0x90)  # R y G mínimos de cada binarización (destellos amarillos o blancos)
STAR_ANGLE_TOLERANCE = 10.0  # grados de desviación admitidos en la dirección de cada rayo

OVERALL_THRESHOLD = 4.0  # alpha total a partir del cual se da por shiny
DIALOG_ALPHA = 3.5  # suma si la animación del rival dura lo que una animación shiny
SOUND_ALPHA = 5.0  # suma si el sonido coincide con la plantilla: basta por sí solo
SHINY_ANIMATION_S = 3.9  # lo que dura la animación del rival cuando es shiny
DIALOG_WINDOW_S = (SHINY_ANIMATION_S - 0.3, SHINY_ANIMATION_S + 0.5)  # duración que cuenta como animación shiny


def battle_dialog_visible(image: np.ndarray) -> bool:
    """Cuadro de texto de combate («¡Un … salvaje!», «¡Adelante, …!»): interior blanco a la izquierda y abajo y
    marco con detalle a ambos lados."""
    if image is None or image.ndim != 3 or image.size == 0:
        return False
    if not _is_white(*_stats(image, NormBox(0.07, 0.835, 0.008, 0.12))):
        return False
    if not _is_white(*_stats(image, NormBox(0.50, 0.91, 0.40, 0.05))):
        return False
    return (float(_stats(image, NormBox(0.04, 0.835, 0.02, 0.12))[1].sum()) >= 50
            and float(_stats(image, NormBox(0.965, 0.835, 0.02, 0.12))[1].sum()) >= 50)


# --- destellos -----------------------------------------------------------------------------------------------
@dataclass
class SparkleSet:
    """Destellos encontrados en una imagen, con sus recuadros en coordenadas de la zona analizada."""

    balls: list[tuple[int, int, int, int]] = field(default_factory=list)  # (x0, y0, x1, y1) en la zona
    stars: list[tuple[int, int, int, int]] = field(default_factory=list)

    @property
    def alpha(self) -> float:
        """Puntuación de los destellos: uno por cada bola o estrella."""
        return float(len(self.balls) + len(self.stars))


def _angle(dx: float, dy: float) -> float:
    """Ángulo del vector en grados, en [0, 360)."""
    a = math.degrees(math.atan2(dy, dx))
    return a + 360 if a < 0 else a


class _Radial:
    """Análisis radial de un objeto: quita el 85 % central y mira cómo quedan los rayos que sobresalen."""

    def __init__(self, mask: np.ndarray, area: int, screen_area: int) -> None:
        self.ok = False
        self.h, self.w = mask.shape
        self.area = area
        if area < 20 or area * 1920 * 1080 > 10000 * screen_area:  # tope de 10 000 px escalado a 1080p
            return
        ys, xs = np.nonzero(mask)  # orden de barrido (ver la nota del módulo)
        self.cx, self.cy = int(xs.mean()), int(ys.mean())
        stop = int(0.85 * area)
        d2 = (xs - self.cx) ** 2 + (ys - self.cy) ** 2
        threshold = int(d2[max(stop, 1) - 1])
        yy, xx = np.mgrid[0:self.h, 0:self.w]
        self.ring = mask & (((xx - self.cx) ** 2 + (yy - self.cy) ** 2) >= threshold)
        n, labels, stats, cents = cv2.connectedComponentsWithStats(self.ring.astype(np.uint8), connectivity=4)
        regions = sorted(((int(stats[i, cv2.CC_STAT_AREA]), i) for i in range(1, n)), key=lambda r: r[0])
        self.centers = [(int(cents[i][0]), int(cents[i][1])) for _, i in regions]
        self.ok = True

    def _angles(self, k: int) -> list[float]:
        """Ángulos (sin repetir, ordenados) de los ``k`` primeros rayos respecto al centro del objeto."""
        return sorted({_angle(x - self.cx, y - self.cy) for x, y in self.centers[:k]})

    def is_ball(self) -> bool:
        """Destello «bola»: cuatro rayos en las diagonales (45°, 135°…) y todo el anillo pegado a ellas."""
        if not self.ok or len(self.centers) < 4 or self.area * 3 > self.w * self.h * 2:
            return False
        for a in self._angles(4):
            n = a - 45
            if abs(n - 90 * round(n / 90)) > STAR_ANGLE_TOLERANCE:
                return False
        ys, xs = np.nonzero(self.ring)
        distance = math.hypot(self.cx, self.cy)
        off = np.abs(np.abs(ys - self.cy) - np.abs(xs - self.cx))
        return not bool((off > 2 + distance / 5).any())

    def is_star(self) -> bool:
        """Destello «estrella»: cinco puntas separadas 72° entre sí, en un objeto más o menos cuadrado."""
        if not self.ok or len(self.centers) < 5 or self.w * self.h < 100:
            return False
        if self.w > 2 * self.h or self.h > 2 * self.w:
            return False
        angles = self._angles(5)
        for ref in angles:
            points, bad = 0, 0
            for test in angles:
                n = test - ref
                q = round(n / 72)
                points |= 1 << (q % 5)
                if abs(n - 72 * q) > STAR_ANGLE_TOLERANCE:
                    bad += 1
            if points & 0x1F == 0x1F and bad == 0:  # las cinco posiciones ocupadas y ninguna desviada
                return True
        return False


def _sparkles_in_mask(mask: np.ndarray, screen_area: int) -> SparkleSet:
    """Clasifica como bola o estrella cada objeto conexo de la máscara binaria con un tamaño razonable."""
    out = SparkleSet()
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
    max_area = 10000 * screen_area / (1920 * 1080)
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < 20 or area > max_area:
            continue
        x, y, w, h = (int(stats[i, k]) for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH,
                                                 cv2.CC_STAT_HEIGHT))
        if w * h < 20:
            continue
        radial = _Radial(labels[y:y + h, x:x + w] == i, area, screen_area)
        if radial.is_ball():
            out.balls.append((x, y, x + w, y + h))
        elif radial.is_star():
            out.stars.append((x, y, x + w, y + h))
    return out


def find_sparkles(image: np.ndarray, box: NormBox = WILD_BOX) -> SparkleSet:
    """Destellos shiny en ``box`` de un frame BGR (≥720p). Devuelve el umbral con más destellos."""
    if image is None or image.ndim != 3 or image.size == 0:
        return SparkleSet()
    screen_area = image.shape[0] * image.shape[1]
    region = box.crop(image)
    r, g = region[..., 2], region[..., 1]
    best = SparkleSet()
    for level in THRESHOLDS:
        found = _sparkles_in_mask(((r >= level) & (g >= level)).astype(np.uint8), screen_area)
        if found.alpha > best.alpha:
            best = found
    return best


# --- entrada en combate --------------------------------------------------------------------------------------
@dataclass
class ShinyVerdict:
    """Resultado de la entrada en combate: cada fuente de alpha por separado y el total."""

    sparkle_alpha: float  # mejor alpha de destellos durante la animación del rival
    wild_animation_s: float | None  # lo que duró la animación del rival (entre diálogos)
    dialog_alpha: float
    sound_alpha: float
    own_alpha: float  # destellos durante la animación del nuestro (no cuenta: solo para el registro)
    finished: bool  # se llegó al menú de combate
    sparkle_t: float | None = None  # cuándo se vio el mejor destello del rival (mismo reloj que los frames)
    sound_error: float | None = None  # mejor coincidencia con la plantilla del sonido (1 = nada)

    @property
    def alpha(self) -> float:
        return self.sparkle_alpha + self.dialog_alpha + self.sound_alpha

    @property
    def is_shiny(self) -> bool:
        return self.alpha >= OVERALL_THRESHOLD

    def describe(self) -> str:
        """Resumen de una línea para el registro."""
        anim = "?" if self.wild_animation_s is None else f"{self.wild_animation_s:.2f} s"
        return t("alpha {alpha:.1f} (destellos {sparkle:.0f}, animación {anim} → "
                 "{dialog:.1f}, sonido {sound:.1f}; el nuestro {own:.0f})",
                 "alpha {alpha:.1f} (sparkles {sparkle:.0f}, animation {anim} → "
                 "{dialog:.1f}, sound {sound:.1f}; ours {own:.0f})",
                 alpha=self.alpha, sparkle=self.sparkle_alpha, anim=anim, dialog=self.dialog_alpha,
                 sound=self.sound_alpha, own=self.own_alpha)


# Fases de la entrada, separadas por cada aparición de un diálogo de combate.
BEFORE, WILD, OWN, POST = range(4)


class ShinyEncounterWatcher:
    """Se le dan los frames desde la A final hasta el menú de combate (``feed`` devuelve True al terminar).

    ``sparkle_every_s`` limita el coste: los destellos se buscan como mucho cada tantos segundos (la animación
    shiny dura ~1 s y los destellos se ven en muchos frames)."""

    def __init__(self, sparkle_every_s: float = 0.0) -> None:
        self.state = BEFORE
        self.dialog_on = False
        self.dialog_off_t: float | None = None
        self.wild_animation_s: float | None = None
        self.own_animation_s: float | None = None
        self.best_wild = SparkleSet()
        self.best_wild_t: float | None = None
        self.best_own = SparkleSet()
        self.sound_alpha = 0.0
        self.sound_error: float | None = None
        self.finished = False
        self.sparkle_every_s = sparkle_every_s
        self._last_sparkle_t = -1e9
        self.log: list[tuple[float, str]] = []

    def feed(self, image: np.ndarray, t: float) -> bool:
        """Procesa la imagen del instante ``t``: avanza de fase con cada diálogo y busca destellos en la fase del
        rival y en la del nuestro. Devuelve ``True`` cuando aparece el menú de combate."""
        if self.finished:
            return True
        if battle_menu_visible(image):
            self._close_phase(t)
            self.finished = True
            self.log.append((t, "menú de combate"))
            return True
        on = battle_dialog_visible(image)
        if on != self.dialog_on:
            self.dialog_on = on
            if not on:
                self.dialog_off_t = t
                self.log.append((t, "diálogo fuera"))
            else:
                self._close_phase(t)
                self.state = min(POST, self.state + 1)
                self.log.append((t, f"diálogo → fase {('antes', 'rival', 'nuestro', 'resto')[self.state]}"))
        if self.state in (WILD, OWN) and t - self._last_sparkle_t >= self.sparkle_every_s:
            self._last_sparkle_t = t
            box = WILD_BOX if self.state == WILD else OWN_BOX
            found = find_sparkles(image, box)
            best = self.best_wild if self.state == WILD else self.best_own
            if found.alpha > best.alpha:
                if self.state == WILD:
                    self.best_wild, self.best_wild_t = found, t
                else:
                    self.best_own = found
                self.log.append((t, f"destellos {found.alpha:.0f} ({len(found.balls)} bolas, "
                                    f"{len(found.stars)} estrellas) en fase {self.state}"))
        return False

    def _close_phase(self, t: float) -> None:
        """Mide el hueco sin diálogo de la fase que termina: es lo que duró su animación."""
        if self.dialog_off_t is None:
            return
        gap = t - self.dialog_off_t
        if self.state == WILD:
            self.wild_animation_s = gap
        elif self.state == OWN:
            self.own_animation_s = gap

    def add_sound(self, detected: bool, error: float | None = None) -> None:
        """Anota el resultado del detector de sonido; si lo detectó, suma ``SOUND_ALPHA``."""
        self.sound_error = error
        if detected:
            self.sound_alpha = SOUND_ALPHA

    def verdict(self) -> ShinyVerdict:
        """Veredicto con lo visto hasta ahora. La duración de la animación del rival suma ``DIALOG_ALPHA`` si cae en
        ``DIALOG_WINDOW_S``."""
        dialog = 0.0
        if self.wild_animation_s is not None and DIALOG_WINDOW_S[0] <= self.wild_animation_s <= DIALOG_WINDOW_S[1]:
            dialog = DIALOG_ALPHA
        return ShinyVerdict(self.best_wild.alpha, self.wild_animation_s, dialog, self.sound_alpha,
                            self.best_own.alpha, self.finished, self.best_wild_t, self.sound_error)
