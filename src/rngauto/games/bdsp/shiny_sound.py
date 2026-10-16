"""Sonido shiny (las estrellas al entrar el rival) por comparación de espectrogramas.

Se compara la plantilla del sonido con cada tramo del audio del combate:

* espectros de 2048 muestras (≈42 ms a 48 kHz) cada 512, en mono (media de los canales);
* solo 5-20 kHz (el tintineo es agudo; música y gritos quedan casi fuera) y convolución con un núcleo en
  «pico» que realza los tonos estrechos frente al ruido de banda ancha;
* error invariante a la escala: ‖T − s·A‖²/‖T‖² con la mejor escala s, que vale 1 − cos²(A, T). Coincide si el
  error es menor que el umbral.

La plantilla NO se distribuye (es un sonido del juego): se aprende del primer shiny que se vea con la
capturadora propia (``learn_template``) y se guarda en ``~/.rngauto/sonidos/``. Hasta entonces la
detección es solo visual (destellos + duración de la animación).
"""
from __future__ import annotations

import json
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from rngauto.i18n import t

FFT_N = 2048
STEP = FFT_N // 4  # solape del 75 % entre ventanas
LOW_HZ = 5000.0
HIGH_HZ = 20000.0
DEFAULT_THRESHOLD = 0.6  # provisional: se ajusta con el primer shiny real y los combates normales grabados


def spike_kernel(num_freqs: int, half_rate: float) -> np.ndarray:
    """Núcleo en «pico»: rampa −4 → +4 → −4 de ≈200 Hz de ancho. Realza los tonos estrechos y apenas responde al
    ruido de banda ancha (sus coeficientes suman casi cero)."""
    intervals = int(199.21875 * num_freqs / half_rate + 0.5)
    slope = max(1, intervals // 2)
    up = [-4.0 + 8.0 * i / slope for i in range(slope + 1)]
    down = [-4.0 + 8.0 * (slope - i) / slope for i in range((intervals + 1) % 2, slope + 1)]
    return np.array(up + down, dtype=np.float32)


def to_mono(samples: np.ndarray) -> np.ndarray:
    """Audio en mono ``float32``: media de los canales si viene como (muestras, canales)."""
    raw = np.asarray(samples)
    s = raw.astype(np.float32)
    if raw.dtype.kind == "i":  # PCM entero (p. ej. un .wav de 16 bits): a [-1, 1] como el audio en directo
        s /= float(np.iinfo(raw.dtype).max + 1)
    if s.ndim == 2:
        s = s.mean(axis=1)
    return s


def spectra(mono: np.ndarray, rate: int) -> np.ndarray:
    """Espectros (ventanas × frecuencias) ya filtrados y convolucionados. Ventana k empieza en k·STEP."""
    mono = np.asarray(mono, dtype=np.float32)
    if mono.size < FFT_N:
        return np.zeros((0, 0), dtype=np.float32)
    n = (mono.size - FFT_N) // STEP + 1
    idx = np.arange(FFT_N)[None, :] + STEP * np.arange(n)[:, None]
    win = np.hanning(FFT_N).astype(np.float32)
    mags = np.abs(np.fft.rfft(mono[idx] * win, axis=1))[:, :FFT_N // 2].astype(np.float32)
    nf = FFT_N // 2
    half = rate / 2
    lo = int(LOW_HZ * nf / half + 0.5)
    hi = min(nf, int(HIGH_HZ * nf / half) + 1)
    band = mags[:, lo:hi]
    k = spike_kernel(nf, half)
    if band.shape[1] < k.size:
        return np.zeros((n, 0), dtype=np.float32)
    # convolución «válida» por filas (en realidad correlación; el núcleo es simétrico)
    windows = np.lib.stride_tricks.sliding_window_view(band, k.size, axis=1)
    return np.einsum("wfk,k->wf", windows, k).astype(np.float32)


@dataclass
class SoundTemplate:
    """Plantilla aprendida del sonido shiny: espectros del tramo, frecuencia de muestreo y umbral de error."""

    spectra: np.ndarray  # (ventanas, frecuencias)
    rate: int
    threshold: float = DEFAULT_THRESHOLD
    note: str = ""

    @property
    def duration_s(self) -> float:
        """Duración del tramo de audio que cubre la plantilla."""
        return (self.spectra.shape[0] - 1) * STEP / self.rate + FFT_N / self.rate

    def save(self, path: Path) -> None:
        """Guarda la plantilla en ``.npz`` (crea la carpeta si hace falta)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, spectra=self.spectra, rate=self.rate, threshold=self.threshold,
                            note=json.dumps(self.note))

    @classmethod
    def load(cls, path: Path) -> SoundTemplate | None:
        """Lee una plantilla guardada; ``None`` si no existe o está dañada."""
        try:
            with np.load(path) as z:
                return cls(z["spectra"].astype(np.float32), int(z["rate"]), float(z["threshold"]),
                           json.loads(str(z["note"])))
        except (OSError, KeyError, ValueError):
            return None


def match_errors(spec: np.ndarray, template: SoundTemplate) -> np.ndarray:
    """Error de la plantilla alineada en cada posición (1 − cos²); 1.0 = nada que ver."""
    t = template.spectra
    w = t.shape[0]
    if spec.shape[0] < w or spec.shape[1] != t.shape[1]:
        return np.ones(0, dtype=np.float32)
    tn = float((t * t).sum())
    if tn <= 0:
        return np.ones(spec.shape[0] - w + 1, dtype=np.float32)
    # Producto escalar y energía de cada tramo de ``w`` ventanas, vectorizados sobre todas las posiciones.
    views = np.lib.stride_tricks.sliding_window_view(spec, w, axis=0)  # (pos, F, w)
    dot = np.einsum("pfw,wf->p", views, t)
    energy = (spec * spec).sum(axis=1)
    an = np.convolve(energy, np.ones(w), mode="valid")
    with np.errstate(divide="ignore", invalid="ignore"):
        err = 1.0 - (dot * dot) / (an * tn)
    err[~np.isfinite(err)] = 1.0
    return np.clip(err, 0.0, 1.0).astype(np.float32)


@dataclass
class SoundMatch:
    """Resultado de :func:`detect`: si coincide, el mejor error y dónde se encontró."""

    detected: bool
    best_error: float
    at_s: float | None  # segundos desde el inicio del audio analizado


def detect(samples: np.ndarray, rate: int, template: SoundTemplate | None) -> SoundMatch:
    """Busca la plantilla en el audio. Sin plantilla, o con otra frecuencia de muestreo, no detecta nada."""
    if template is None or template.rate != rate:
        return SoundMatch(False, 1.0, None)
    err = match_errors(spectra(to_mono(samples), rate), template)
    if err.size == 0:
        return SoundMatch(False, 1.0, None)
    i = int(np.argmin(err))
    return SoundMatch(bool(err[i] <= template.threshold), float(err[i]), i * STEP / rate)


def learn_template(samples: np.ndarray, rate: int, around_s: float, *, search_s: float = 2.0,
                   length_s: float = 1.0, baseline: list[np.ndarray] | None = None) -> SoundTemplate:
    """Plantilla del tramo de ``length_s`` con más «tonos agudos» cerca de ``around_s`` (cuando se vieron los
    destellos). Si se dan grabaciones de combates normales (``baseline``), se elige el tramo que más se
    diferencia de ellas y el umbral se pone por debajo de lo que dan."""
    spec = spectra(to_mono(samples), rate)
    w = max(4, int(round((length_s * rate - FFT_N) / STEP)) + 1)
    if spec.shape[0] < w:
        raise ValueError(t("El audio es demasiado corto para aprender el sonido", "The audio is too short to learn the sound"))
    energy = np.clip(spec, 0, None).sum(axis=1)
    run = np.convolve(energy, np.ones(w), mode="valid")
    a = max(0, int((around_s - search_s) * rate / STEP))
    b = min(run.size, int((around_s + search_s) * rate / STEP) + 1)
    if b <= a:
        a, b = 0, run.size
    start = a + int(np.argmax(run[a:b]))
    tpl = SoundTemplate(spec[start:start + w].copy(), rate,
                        note=t("aprendida en {at:.2f} s (destellos en {around:.2f} s)",
                               "learned at {at:.2f} s (sparkles at {around:.2f} s)",
                               at=start * STEP / rate, around=around_s))
    if baseline:
        # El combate normal que más se parece marca el umbral: un 20 % por debajo, sin pasar del de por defecto.
        worst = min((detect(x, rate, tpl).best_error for x in baseline), default=1.0)
        tpl.threshold = float(min(DEFAULT_THRESHOLD, 0.8 * worst))
        tpl.note += t("; combates normales: error mínimo {worst:.2f} → umbral {threshold:.2f}",
                      "; normal battles: minimum error {worst:.2f} → threshold {threshold:.2f}",
                      worst=worst, threshold=tpl.threshold)
    return tpl


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Lee un WAV de 16 bits; devuelve ``(muestras (n, canales) en [-1, 1], frecuencia)``."""
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(t("Solo WAV de 16 bits", "Only 16-bit WAV is supported"))
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return data.reshape(-1, ch), rate


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    """Escribe ``samples`` (en [-1, 1], mono o (n, canales)) como WAV de 16 bits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    s = np.asarray(samples, dtype=np.float32)
    if s.ndim == 1:
        s = s[:, None]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(s.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(s, -1.0, 1.0) * 32767).astype("<i2").tobytes())
