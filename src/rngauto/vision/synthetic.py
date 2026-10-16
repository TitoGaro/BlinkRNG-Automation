"""Vídeo sintético con verdad conocida: un ojo que parpadea según el RNG simulado.

Permite probar visión + recuperación de extremo a extremo sin Switch ni capturadora, y medir cómo
afectan ruido, compresión MJPEG, tamaño del ojo (zoom) y frames perdidos/duplicados.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import cv2
import numpy as np

from rngauto.core.observation import BlinkKind

from .frames import Frame, NormBox

SKIN = (150, 180, 220)  # BGR
SCLERA = (235, 235, 240)
IRIS = (60, 40, 30)


@dataclass(frozen=True, slots=True)
class GroundTruthBlink:
    """Parpadeo real del vídeo sintético (lo que el detector debería encontrar)."""

    t_s: float
    kind: BlinkKind
    advance: int = -1


@dataclass(frozen=True)
class SyntheticEyeScene:
    """Parámetros del vídeo sintético: geometría del ojo y degradaciones de la captura que se emulan."""

    width: int = 320
    height: int = 180
    fps: float = 60.0
    eye_center: tuple[float, float] = (0.5, 0.45)  # normalizado
    eye_px: int = 24  # ancho del ojo en píxeles (pequeño ≈ sin zoom, grande ≈ con zoom)
    noise_sigma: float = 2.0
    jpeg_quality: int | None = None  # emula MJPEG de la capturadora
    close_s: float = 0.12  # duración de un cierre
    double_gap_s: float = 0.30  # separación entre cierres de un doble parpadeo
    drop_prob: float = 0.0  # probabilidad de perder un frame
    dup_prob: float = 0.0  # probabilidad de repetir el frame anterior

    @property
    def eye_box(self) -> NormBox:
        """Caja de búsqueda alrededor del ojo (2× su tamaño)."""
        w = self.eye_px * 2.0 / self.width
        h = self.eye_px * 1.6 / self.height
        cx, cy = self.eye_center
        return NormBox(cx - w / 2, cy - h / 2, w, h)


def _eye_sprite(size: int, openness: float) -> np.ndarray:
    """Dibujo del ojo con apertura ``openness`` (0 = cerrado, 1 = abierto)."""
    h = max(4, int(size * 0.7))
    img = np.full((h, size, 3), SKIN, np.uint8)
    cx, cy = size // 2, h // 2
    ry = max(0, int(round((h / 2 - 1) * openness)))
    if ry >= 1:
        cv2.ellipse(img, (cx, cy), (size // 2 - 1, ry), 0, 0, 360, SCLERA, -1)
        cv2.circle(img, (cx, cy), max(1, min(ry, size // 5)), IRIS, -1)
    cv2.line(img, (1, cy), (size - 2, cy), (80, 90, 120), 1)  # párpado
    return img


class SyntheticRenderer:
    """Genera los frames del vídeo sintético con ruido y compresión opcionales."""

    # Niveles de apertura precalculados: dibujar el ojo en cada frame sería mucho más lento.
    LEVELS = 11

    def __init__(self, scene: SyntheticEyeScene, seed: int = 0) -> None:
        self.scene = scene
        self.rng = np.random.default_rng(seed)
        self.background = np.full((scene.height, scene.width, 3), (70, 110, 90), np.uint8)
        cv2.rectangle(self.background, (0, int(scene.height * 0.7)), (scene.width, scene.height), (90, 90, 90), -1)
        self.sprites = [_eye_sprite(scene.eye_px, i / (self.LEVELS - 1)) for i in range(self.LEVELS)]
        sh, sw = self.sprites[0].shape[:2]
        self.x0 = int(scene.eye_center[0] * scene.width - sw / 2)
        self.y0 = int(scene.eye_center[1] * scene.height - sh / 2)

    def render(self, openness: float) -> np.ndarray:
        """Frame completo con el ojo en la apertura indicada."""
        img = self.background.copy()
        sprite = self.sprites[int(round(np.clip(openness, 0, 1) * (self.LEVELS - 1)))]
        sh, sw = sprite.shape[:2]
        img[self.y0:self.y0 + sh, self.x0:self.x0 + sw] = sprite
        # Ruido y compresión solo alrededor del ojo: es lo único que se analiza y abarata los tests.
        x, y, w, h = self.scene.eye_box.expanded(2.0).to_pixels(self.scene.width, self.scene.height)
        region = img[y:y + h, x:x + w]
        if self.scene.noise_sigma > 0:
            noise = self.rng.normal(0, self.scene.noise_sigma, region.shape)
            region = np.clip(region.astype(np.float32) + noise, 0, 255).astype(np.uint8)
        if self.scene.jpeg_quality is not None:
            ok, enc = cv2.imencode(".jpg", region, [cv2.IMWRITE_JPEG_QUALITY, self.scene.jpeg_quality])
            region = cv2.imdecode(enc, cv2.IMREAD_COLOR)
        img[y:y + h, x:x + w] = region
        return img

    def template(self) -> np.ndarray:
        """Plantilla del ojo abierto (recorte limpio y sin ruido, con un margen quitado de cada borde)."""
        sprite = self.sprites[-1]
        m = max(1, self.scene.eye_px // 8)
        return sprite[m:-m, m:-m].copy()


def openness_at(t: float, blinks: list[GroundTruthBlink], close_s: float, gap_s: float) -> float:
    """Perfil triangular de cada cierre; doble = dos cierres separados ``gap_s``."""
    value = 1.0
    for b in blinks:
        if t < b.t_s - 0.01 or t > b.t_s + gap_s + close_s + 0.01:
            continue
        starts = [b.t_s] + ([b.t_s + gap_s] if b.kind == BlinkKind.DOUBLE else [])
        for s in starts:
            u = (t - s) / close_s
            if 0.0 <= u <= 1.0:
                value = min(value, abs(2 * u - 1))
    return value


def synthetic_frames(
    scene: SyntheticEyeScene,
    blinks: list[GroundTruthBlink],
    duration_s: float,
    *,
    start_t: float = 0.0,
    seed: int = 0,
    renderer: SyntheticRenderer | None = None,
) -> Iterator[Frame]:
    """Frames de ``duration_s`` segundos con los parpadeos dados, aplicando pérdidas y duplicados al azar.

    Un frame perdido no se entrega, pero su índice se consume: el hueco queda visible igual que en una captura
    real. ``seed`` hace el vídeo reproducible."""
    renderer = renderer or SyntheticRenderer(scene, seed)
    rng = np.random.default_rng(seed + 1)
    ordered = sorted(blinks, key=lambda b: b.t_s)
    n = int(duration_s * scene.fps)
    previous: np.ndarray | None = None
    j = 0
    for i in range(n):
        t = start_t + i / scene.fps
        # Se descartan los parpadeos ya terminados: solo los siguientes pueden afectar a este frame.
        while j < len(ordered) and ordered[j].t_s + scene.double_gap_s + scene.close_s < t - 0.05:
            j += 1
        if scene.drop_prob and rng.random() < scene.drop_prob:
            continue
        if previous is not None and scene.dup_prob and rng.random() < scene.dup_prob:
            yield Frame(previous, t, i)
            continue
        img = renderer.render(openness_at(t, ordered[j:j + 3], scene.close_s, scene.double_gap_s))
        previous = img
        yield Frame(img, t, i)
