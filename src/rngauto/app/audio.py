"""Sonido del juego: reenvía el audio de la capturadora a los altavoces para oír la consola en el PC.

``AudioPassthrough`` además reparte cada bloque entre sus ``taps``; ``AudioRing`` es uno de ellos y guarda
el sonido reciente para que la estrategia detecte el sonido shiny al entrar en combate.
"""
from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

from rngauto.i18n import t

from .discovery import AudioInputInfo


class AudioPassthrough:
    """Entrada (capturadora) → salida por defecto de la MISMA API de audio de Windows.

    PortAudio no admite un flujo dúplex entre APIs distintas (p. ej. entrada WASAPI y salida MME): se
    prueba cada entrada candidata con la salida por defecto de su propia API hasta que una funciona.
    """

    def __init__(self, source: AudioInputInfo | list[AudioInputInfo], volume: float = 0.8) -> None:
        self.candidates = source if isinstance(source, list) else [source]
        self.source = self.candidates[0]
        self.volume = volume
        self.muted = False
        self._stream = None
        self._lock = threading.Lock()
        self.error: str | None = None
        self.taps: list = []  # receptores de cada bloque de sonido (p. ej. el anillo para detectar el shiny)

    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        """Abre el flujo con la primera entrada candidata que funcione. Lanza ``RuntimeError`` si ninguna."""
        import sounddevice as sd

        # Se ejecuta en el hilo de audio de PortAudio: no debe bloquear ni lanzar. Un receptor que falla se
        # ignora para no cortar el sonido; el bloque se copia porque PortAudio reutiliza ``indata``.
        def callback(indata, outdata, frames, time_info, status) -> None:
            receivers = list(self.taps)
            if receivers:
                block = indata.copy()
                for tap in receivers:
                    try:
                        tap(block)
                    except Exception:
                        pass
            if self.muted:
                outdata.fill(0)
            else:
                np.multiply(indata[:, :outdata.shape[1]], self.volume, out=outdata)

        with self._lock:
            if self._stream is not None:
                return
            errors = []
            for src in self.candidates:
                try:
                    api = sd.query_devices(src.index)["hostapi"]
                    out = sd.query_hostapis(api)["default_output_device"]
                    if out is None or out < 0:
                        raise RuntimeError(t("sin salida por defecto en esta API", "no default output in this API"))
                    # Estéreo se reenvía tal cual; cualquier otra cosa, como mono → mono.
                    channels = (src.channels, 2) if src.channels == 2 else (1, 1)
                    stream = sd.Stream(device=(src.index, out), samplerate=src.samplerate, channels=channels,
                                       dtype="float32", latency="low", callback=callback)
                    stream.start()
                except Exception as exc:
                    errors.append(f"{src.name}: {exc}")
                    continue
                self._stream, self.source, self.error = stream, src, None
                return
            self.error = t("No se pudo reproducir el sonido de la capturadora ({errors})",
                           "Could not play the capture card audio ({errors})", errors="; ".join(errors[:2]))
            raise RuntimeError(self.error)

    def stop(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.abort()  # inmediato (stop() espera a vaciar búferes y puede tardar)
                    self._stream.close()
                except Exception:
                    pass
                finally:
                    self._stream = None

    def set_volume(self, volume: float) -> None:
        """Ganancia acotada a [0, 1.5]; se aplica en el siguiente bloque sin reabrir el flujo."""
        self.volume = max(0.0, min(1.5, volume))


class AudioRing:
    """Últimos ``seconds`` de sonido con su instante (``time.perf_counter``, el reloj de la estrategia).

    Se engancha a ``AudioPassthrough.taps``; ``since(t)`` devuelve el sonido desde ``t`` (para analizar la
    entrada en combate cuando ya ha terminado). Seguro entre hilos; nunca bloquea el hilo de audio."""

    def __init__(self, samplerate: int, seconds: float = 45.0) -> None:
        self.samplerate = int(samplerate)
        self.max_samples = int(seconds * samplerate)
        self._blocks: deque[tuple[float, np.ndarray]] = deque()  # (instante de la primera muestra, bloque)
        self._count = 0
        self._lock = threading.Lock()

    def __call__(self, block: np.ndarray) -> None:
        # Llamado desde el hilo de audio: el bloque acaba de llegar, así que su última muestra es «ahora».
        t_end = time.perf_counter()
        t0 = t_end - len(block) / self.samplerate
        with self._lock:
            self._blocks.append((t0, block))
            self._count += len(block)
            while self._blocks and self._count - len(self._blocks[0][1]) >= self.max_samples:
                self._count -= len(self._blocks.popleft()[1])

    def since(self, t: float, until: float | None = None) -> tuple[np.ndarray, float]:
        """(muestras, instante de la primera) desde ``t`` (o lo más antiguo que quede) hasta ``until``."""
        with self._lock:
            blocks = list(self._blocks)
        if not blocks:
            return np.zeros((0, 1), dtype=np.float32), t
        start_i = 0
        for i, (t0, b) in enumerate(blocks):
            if t0 + len(b) / self.samplerate > t:
                start_i = i
                break
        else:
            return np.zeros((0, blocks[-1][1].shape[1]), dtype=np.float32), t
        chosen = [b for t0, b in blocks[start_i:] if until is None or t0 < until]
        data = np.concatenate(chosen) if chosen else np.zeros((0, blocks[-1][1].shape[1]), dtype=np.float32)
        first_t = blocks[start_i][0]
        skip = max(0, int((t - first_t) * self.samplerate))
        return data[skip:], first_t + skip / self.samplerate
