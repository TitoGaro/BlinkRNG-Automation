"""Del vídeo a eventos de parpadeo con confianza.

Pipeline: ``Frame`` → ROI → ``EyeMatcher`` (ZNCC con la plantilla del ojo abierto) → serie de
``MatchSample`` → profundidad relativa al reposo → umbral (automático por meseta) → caídas →
``Blink`` (instante por centroide ponderado) → agrupación simple/doble → ``BlinkEvent``.

Se procesan **todos** los frames (no un sondeo periódico) para fechar cada parpadeo con precisión
sub-frame; los frames no medibles se cuentan aparte en vez de contar como ojo cerrado, y cada evento lleva
una confianza que el resto del programa usa para decidir si fiarse de él.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from rngauto.core.observation import BlinkKind
from rngauto.i18n import t

from .frames import Frame, NormBox

NO_MATCH = 1.0  # un frame no medible cuenta como "ojo abierto" (nunca inventa un parpadeo)
DOUBLE_BLINK_WINDOW_S = 0.55  # un segundo cierre antes de 0,55 s es la misma tirada (doble parpadeo)
DOUBLE_AMBIGUITY_S = 0.06  # cerca del límite, el tipo es dudoso
# Dos eventos distintos del mismo ojo nunca están más cerca que esto (un doble ya es un solo evento). Al
# recalcular con más muestras, el instante de un evento ya emitido puede moverse microsegundos: sin este
# margen se devolvería otra vez como si fuera nuevo.
MIN_EVENT_SEPARATION_S = DOUBLE_BLINK_WINDOW_S


@dataclass(frozen=True, slots=True)
class MatchSample:
    """Puntuación del ojo en un frame; ``valid=False`` si el frame no se pudo medir."""

    t_s: float
    score: float
    valid: bool = True
    frame: int = -1


class EyeMatcher:
    """Mejor correlación normalizada (``TM_CCOEFF_NORMED``) de la plantilla dentro de la caja.

    ``reference_height``: altura del frame en el que se recortó la plantilla; la ROI se reescala a esa
    referencia (1080p por defecto). ``upscale``/``blur_ksize`` emulan un "zoom digital": permiten medir
    si el zoom de consola es necesario con una capturadora concreta.
    """

    def __init__(
        self,
        template_bgr: np.ndarray,
        box: NormBox,
        reference_height: int = 1080,
        upscale: float = 1.0,
        blur_ksize: int = 0,
    ) -> None:
        gray = template_bgr if template_bgr.ndim == 2 else cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)
        if float(gray.std()) <= 0:
            raise ValueError(t("La plantilla del ojo es de un color plano: nunca detectaría un parpadeo",
                               "The eye template is a flat color: it would never detect a blink"))
        self.box = box
        self.reference_height = reference_height
        self.upscale = upscale
        self.blur_ksize = blur_ksize
        self.template = self._prep(gray, scale=upscale)

    def _prep(self, gray: np.ndarray, scale: float) -> np.ndarray:
        """Reescala y suaviza igual la plantilla y la ROI, para que la correlación compare lo mismo."""
        if scale != 1.0:
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        if self.blur_ksize > 1:
            gray = cv2.GaussianBlur(gray, (self.blur_ksize | 1, self.blur_ksize | 1), 0)
        return gray

    def score(self, image: np.ndarray) -> tuple[float, bool]:
        """(puntuación, válido). Puntuación alta = ojo abierto."""
        if image is None or image.size == 0:
            return NO_MATCH, False
        region = self.box.crop(image)
        if region.size == 0:
            return NO_MATCH, False
        gray = region if region.ndim == 2 else cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        # La ROI se lleva a la resolución de la plantilla: así sirve la misma con 720p o 1080p.
        scale = self.upscale * (self.reference_height / image.shape[0])
        gray = self._prep(gray, scale=scale)
        th, tw = self.template.shape[:2]
        if gray.shape[0] < th or gray.shape[1] < tw:
            return NO_MATCH, False
        result = cv2.matchTemplate(gray, self.template, cv2.TM_CCOEFF_NORMED)
        best = float(result.max())
        if not math.isfinite(best):
            return NO_MATCH, False
        return best, True


class EyeWatcher:
    """Puntúa cada frame con un ``EyeMatcher`` y guarda la serie de muestras de un ojo."""

    def __init__(self, matcher: EyeMatcher, label: str = "player") -> None:
        self.matcher = matcher
        self.label = label
        self.samples: list[MatchSample] = []

    def process(self, frame: Frame) -> MatchSample:
        """Mide un frame y añade su muestra."""
        score, valid = self.matcher.score(frame.image)
        sample = MatchSample(frame.t_s, score, valid, frame.index)
        self.samples.append(sample)
        return sample

# --- De muestras a parpadeos --------------------------------------------------------------------

def depths(samples: list[MatchSample]) -> tuple[np.ndarray, float]:
    """Profundidad relativa ``(reposo − score)/reposo`` en [0, 1]; reposo = mediana de las válidas."""
    if not samples:
        return np.zeros(0), 0.0
    scores = np.array([s.score if s.valid else NO_MATCH for s in samples], dtype=float)
    valid = np.array([s.valid for s in samples])
    resting = float(np.median(scores[valid])) if valid.any() else 0.0
    if resting <= 0:
        return np.zeros(len(samples)), resting
    d = np.clip((resting - scores) / resting, 0.0, 1.0)
    d[~valid] = 0.0
    return d, resting


def _count_dips(d: np.ndarray, threshold: float) -> int:
    """Número de cruces ascendentes del umbral (caídas de puntuación)."""
    above = d > threshold
    return int(np.count_nonzero(above[1:] & ~above[:-1]) + (1 if above.size and above[0] else 0))


def auto_threshold(
    samples: list[MatchSample],
    min_duration_s: float = 180.0,
    min_plateau_blinks: int = 20,
    lowest: float = 0.04,
    highest: float = 0.60,
    steps: int = 40,
) -> float | None:
    """Umbral en el centro (geométrico) de la meseta más ancha donde el nº de caídas apenas cambia.

    Con un umbral demasiado bajo el ruido cuenta como parpadeo y con uno demasiado alto se pierden parpadeos;
    entre ambos, el recuento se estabiliza en el número real. Los umbrales se prueban en escala logarítmica.
    ``None`` si aún no hay datos suficientes.
    """
    if len(samples) < 2 or samples[-1].t_s - samples[0].t_s < min_duration_s:
        return None
    d, _ = depths(samples)
    thresholds = np.exp(np.linspace(math.log(lowest), math.log(highest), steps))
    counts = [_count_dips(d, float(t)) for t in thresholds]
    best: tuple[int, int, int] | None = None  # (ancho, inicio, fin)
    c = 0
    while c < steps:
        if counts[c] < min_plateau_blinks:
            c += 1
            continue
        end = c
        while end + 1 < steps and counts[end + 1] >= min_plateau_blinks and abs(counts[end + 1] - counts[c]) <= 1:
            end += 1
        if best is None or end - c > best[0]:
            best = (end - c, c, end)
        c = end + 1
    if best is None:
        return None
    return float(math.exp((math.log(thresholds[best[1]]) + math.log(thresholds[best[2]])) / 2))


@dataclass(frozen=True, slots=True)
class Blink:
    """Una caída de la puntuación por encima del umbral (un cierre de ojo)."""

    t_s: float  # centroide ponderado por profundidad (precisión sub-frame)
    depth: float  # profundidad máxima
    frames: int  # frames por encima del umbral
    duration_s: float
    valid_ratio: float  # fracción de muestras válidas alrededor


MAX_BLINK_DURATION_S = 0.6  # una caída más larga no es un parpadeo (ojo tapado, menú, transición)


def extract_blinks(
    samples: list[MatchSample], threshold: float | None, max_duration_s: float = MAX_BLINK_DURATION_S
) -> list[Blink]:
    """Segmenta las caídas por encima de ``threshold`` y descarta las demasiado largas."""
    if len(samples) < 2 or threshold is None or threshold <= 0:
        return []
    d, _ = depths(samples)
    t = np.array([s.t_s for s in samples])
    valid = np.array([s.valid for s in samples])
    above = d > threshold
    n = len(d)
    # Segmentos [inicio, fin) por encima del umbral, vectorizado (solo se itera por segmentos).
    edges = np.diff(np.concatenate(([0], above.astype(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    blinks: list[Blink] = []
    for start, end in zip(starts.tolist(), ends.tolist()):
        if t[end - 1] - t[start] > max_duration_s:
            continue
        # El centroide incluye dos muestras a cada lado: los flancos por debajo del umbral también
        # aportan a la posición sub-frame.
        lo, hi = max(0, start - 2), min(n, end + 2)
        w = d[lo:hi]
        if w.sum() <= 0:
            continue
        centroid = float((w * t[lo:hi]).sum() / w.sum())
        # Validez medida en ±15 frames (~0,25 s a 60 fps): huecos de captura cerca del cierre lo hacen dudoso.
        ctx_lo, ctx_hi = max(0, start - 15), min(n, end + 15)
        blinks.append(Blink(
            t_s=centroid,
            depth=float(d[start:end].max()),
            frames=end - start,
            duration_s=float(t[end - 1] - t[start]) if end - start > 1 else 0.0,
            valid_ratio=float(valid[ctx_lo:ctx_hi].mean()),
        ))
    return blinks


@dataclass(frozen=True, slots=True)
class BlinkEvent:
    """Una tirada con parpadeo. ``kind=None`` si no se pudo distinguir simple/doble."""

    t_s: float
    kind: BlinkKind | None
    confidence: float
    depth: float
    frames: int


def event_confidence(first: Blink, threshold: float, ambiguous_kind: bool) -> float:
    """Confianza en [0, 1] = margen de profundidad × plausibilidad de duración × validez × tipo.

    * margen: ``(depth − umbral)/umbral`` saturado en 1 (una caída del doble del umbral es clara);
    * duración: un parpadeo real dura ~0,03–0,4 s; fuera de eso ×0,4;
    * validez: fracción de frames medibles alrededor del evento;
    * tipo dudoso (segundo cierre cerca del límite de 0,55 s): ×0,7.
    """
    margin = min(1.0, max(0.0, (first.depth - threshold) / threshold))
    duration_ok = 1.0 if 0.0 <= first.duration_s <= 0.4 else 0.4
    kind = 0.7 if ambiguous_kind else 1.0
    return round(max(0.0, min(1.0, (0.3 + 0.7 * margin) * duration_ok * first.valid_ratio * kind)), 4)


def group_blinks(
    blinks: list[Blink],
    threshold: float,
    double_window_s: float = DOUBLE_BLINK_WINDOW_S,
    ambiguity_s: float = DOUBLE_AMBIGUITY_S,
) -> list[BlinkEvent]:
    """Un segundo cierre antes de ``double_window_s`` convierte el evento en doble.

    En la zona dudosa (± ``ambiguity_s`` alrededor del límite) el segundo cierre también se consume, con
    tipo desconocido: dos tiradas reales están separadas ~1 s, así que un cierre a 0,55–0,61 s es la misma
    tirada; contarlo aparte inventaría una tirada fantasma."""
    events: list[BlinkEvent] = []
    i = 0
    while i < len(blinks):
        first = blinks[i]
        gap = blinks[i + 1].t_s - first.t_s if i + 1 < len(blinks) else math.inf
        ambiguous = abs(gap - double_window_s) < ambiguity_s
        if gap < double_window_s + ambiguity_s:
            kind: BlinkKind | None = BlinkKind.DOUBLE
            i += 2
        else:
            kind = BlinkKind.SINGLE
            i += 1
        if ambiguous:
            kind = None
        events.append(BlinkEvent(first.t_s, kind, event_confidence(first, threshold, ambiguous), first.depth, first.frames))
    return events


@dataclass(frozen=True, slots=True)
class SignalQuality:
    """Diagnóstico de la señal del ojo para decidir si la ROI y la plantilla sirven."""

    resting: float
    noise_mad: float  # dispersión (MAD) de la profundidad en reposo
    dip_depth: float  # mediana de profundidad de los parpadeos
    snr: float
    invalid_ratio: float

    def describe(self) -> str:
        return t("reposo {rest:.3f}, ruido {noise:.4f}, caída {dip:.3f}, SNR {snr:.1f}, frames no medibles {inv:.1%}",
                 "resting {rest:.3f}, noise {noise:.4f}, dip {dip:.3f}, SNR {snr:.1f}, unmeasurable frames {inv:.1%}",
                 rest=self.resting, noise=self.noise_mad, dip=self.dip_depth, snr=self.snr, inv=self.invalid_ratio)


def signal_quality(samples: list[MatchSample], threshold: float | None) -> SignalQuality:
    """Calcula ``SignalQuality``; sin umbral, el ruido se mide bajo 0,04 (el umbral mínimo de la búsqueda)."""
    d, resting = depths(samples)
    invalid = 1.0 - (sum(s.valid for s in samples) / len(samples)) if samples else 1.0
    if d.size == 0:
        return SignalQuality(resting, 0.0, 0.0, 0.0, invalid)
    rest = d[d <= (threshold or 0.04)]
    noise = float(np.median(np.abs(rest - np.median(rest)))) if rest.size else 0.0
    blinks = extract_blinks(samples, threshold) if threshold else []
    dip = float(np.median([b.depth for b in blinks])) if blinks else 0.0
    # 1,4826 · MAD estima la desviación típica de un ruido gaussiano.
    snr = dip / (noise * 1.4826 + 1e-6) if dip else 0.0
    return SignalQuality(resting, noise, dip, snr, invalid)


@dataclass
class BlinkEventStream:
    """API incremental para el bucle en vivo: se alimentan muestras y se recogen eventos cerrados.

    Un evento se emite cuando ya no puede cambiar (pasada la ventana de doble parpadeo). El umbral es
    automático hasta que se congela (``freeze``, tras la primera solución): a partir de ahí la numeración de
    los parpadeos ya no cambia al reinterpretar la señal con un umbral nuevo.
    """

    fixed_threshold: float | None = None
    min_duration_s: float = 180.0
    min_plateau_blinks: int = 20
    retention_s: float = 900.0
    samples: list[MatchSample] = field(default_factory=list)
    emitted_until: float = -math.inf
    _threshold: float | None = None

    @property
    def threshold(self) -> float | None:
        """Umbral en uso: el congelado si lo hay; si no, el último automático."""
        return self.fixed_threshold if self.fixed_threshold is not None else self._threshold

    def freeze(self) -> None:
        """Fija el umbral actual: a partir de aquí el recuento de parpadeos ya no se reinterpreta."""
        if self.threshold is not None:
            self.fixed_threshold = self.threshold

    def feed(self, sample: MatchSample) -> None:
        """Añade una muestra y descarta las más antiguas que ``retention_s``."""
        self.samples.append(sample)
        cutoff = sample.t_s - self.retention_s
        if self.samples and self.samples[0].t_s < cutoff:
            k = 0
            while k < len(self.samples) and self.samples[k].t_s < cutoff:
                k += 1
            del self.samples[:k]

    def all_events(self) -> list[BlinkEvent]:
        """Recalcula todos los eventos de la ventana retenida (con el umbral actualizado si no está fijo)."""
        if self.fixed_threshold is None:
            self._threshold = auto_threshold(self.samples, self.min_duration_s, self.min_plateau_blinks)
        th = self.threshold
        if th is None:
            return []
        return group_blinks(extract_blinks(self.samples, th), th)

    def final_events(self) -> list[BlinkEvent]:
        """Como ``all_events``, pero solo los eventos ya definitivos: el último parpadeo puede estar a medias
        (un doble del que aún no ha llegado el segundo cierre parecería simple). Mismo criterio que ``poll``."""
        if not self.samples:
            return []
        horizon = self.samples[-1].t_s - DOUBLE_BLINK_WINDOW_S - 0.15
        return [e for e in self.all_events() if e.t_s <= horizon]

    def preview_count(self) -> int:
        """Parpadeos vistos hasta ahora, también antes de tener el umbral definitivo (con uno provisional,
        para que el usuario vea que avanza: el definitivo necesita unos minutos de datos)."""
        th = self.threshold
        if th is None:
            th = auto_threshold(self.samples, min(20.0, self.min_duration_s), 2)
            if th is None:
                return 0
        return len(group_blinks(extract_blinks(self.samples, th), th))

    def poll(self) -> list[BlinkEvent]:
        """Eventos nuevos y ya definitivos desde la última llamada."""
        if not self.samples:
            return []
        # Un evento es definitivo cuando ya no puede llegar su segundo cierre; 0,15 s más de margen para
        # que termine la caída y el centroide no se mueva.
        horizon = self.samples[-1].t_s - DOUBLE_BLINK_WINDOW_S - 0.15
        new = [e for e in self.all_events() if self.emitted_until + MIN_EVENT_SEPARATION_S < e.t_s <= horizon]
        if new:
            self.emitted_until = new[-1].t_s
        return new
