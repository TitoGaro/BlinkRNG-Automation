"""Captura en vivo con OpenCV: hilo dedicado que sella cada frame al llegar y diagnóstico del dispositivo.

Comprobado en Windows 11 con OpenCV 5.0: DSHOW no abre por índice; MSMF sí. Hay que fijar
``OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0`` antes de importar cv2 para evitar
arranques lentos/fallidos de algunas capturadoras. Por eso los imports van después de ``os.environ``.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import threading  # noqa: E402
import time  # noqa: E402
from collections import deque  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from rngauto import i18n  # noqa: E402  (``i18n.t``: aquí ``t`` es el instante de un frame)

from .frames import Frame, SourceInfo  # noqa: E402

BACKENDS = {"msmf": cv2.CAP_MSMF, "dshow": cv2.CAP_DSHOW, "any": cv2.CAP_ANY}


def fourcc_to_str(value: float) -> str:
    """Decodifica el FOURCC que devuelve OpenCV (cuatro bytes little-endian en un float) a texto."""
    v = int(value)
    return "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00")


@dataclass
class CaptureStats:
    """Estadísticas de llegada de frames, escritas por el hilo de captura."""

    frames: int = 0
    duplicates: int = 0  # frames idénticos al anterior (la capturadora repite)
    gaps: int = 0  # saltos > 1,5 periodos (frames perdidos)
    started_at: float = 0.0
    last_frame_at: float = 0.0
    intervals: deque = field(default_factory=lambda: deque(maxlen=600))  # últimos ~10 s a 60 fps

    def _intervals(self) -> list[float]:
        """Copia de los intervalos. El hilo de captura añade mientras otro hilo lee: si la copia coincide con
        una escritura, ``deque`` lanza ``RuntimeError`` y se reintenta."""
        for _ in range(5):
            try:
                return list(self.intervals)
            except RuntimeError:
                continue
        return []

    @property
    def measured_fps(self) -> float:
        """FPS reales según el intervalo medio entre llegadas."""
        values = self._intervals()
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return 1.0 / mean if mean > 0 else 0.0

    @property
    def jitter_ms(self) -> float:
        """Desviación típica del intervalo entre frames, en ms."""
        values = self._intervals()
        if len(values) < 2:
            return 0.0
        return float(np.std(values) * 1000)


class CaptureDevice:
    """Fuente en vivo. Los frames se sellan con ``time.perf_counter()`` al recibirse."""

    def __init__(
        self,
        index: int,
        width: int = 1920,
        height: int = 1080,
        fps: float = 60.0,
        fourcc: str | None = None,
        backend: str = "msmf",
        buffer_frames: int = 32,  # ~0,5-1 s de vídeo; 240 frames 1080p ocuparían ~1,5 GB
        name: str = "",
    ) -> None:
        self.index = index
        self.requested = (width, height, fps, fourcc)
        self.backend = backend
        self.name = name or i18n.t("capturadora {index}", "capture device {index}", index=index)
        self._cap: cv2.VideoCapture | None = None
        self._buffer: deque[Frame] = deque(maxlen=buffer_frames)
        self._latest: Frame | None = None  # último frame recibido, aunque ya se haya consumido
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self.stats = CaptureStats()
        self._info: SourceInfo | None = None
        self.error: str | None = None

    # --- Ciclo de vida ------------------------------------------------------------------------
    def open(self) -> SourceInfo:
        """Abre el dispositivo, pide el formato deseado y arranca el hilo de captura.

        El driver puede no respetar lo pedido: ``SourceInfo`` refleja lo que informa tras configurarlo."""
        cap = cv2.VideoCapture(self.index, BACKENDS[self.backend])
        if not cap.isOpened():
            raise RuntimeError(i18n.t("No se pudo abrir {name} (índice {index}, backend {backend})",
                                      "Could not open {name} (index {index}, backend {backend})",
                                      name=self.name, index=self.index, backend=self.backend))
        width, height, fps, fourcc = self.requested
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        # Búfer mínimo en el driver: cuanto menos espere un frame allí, más fiel es su sello de tiempo.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        self._info = SourceInfo(
            self.name,
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            float(cap.get(cv2.CAP_PROP_FPS)),
            fourcc_to_str(cap.get(cv2.CAP_PROP_FOURCC)),
        )
        self._running.set()
        self.stats = CaptureStats(started_at=time.perf_counter())
        self._thread = threading.Thread(target=self._loop, name=f"capture-{self.index}", daemon=True)
        self._thread.start()
        return self._info

    def close(self) -> None:
        """El dispositivo se libera dentro del propio hilo de captura (liberarlo desde fuera mientras un
        ``read()`` está bloqueado, p. ej. sin señal HDMI, puede romper el driver)."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                return  # se liberará cuando el read() bloqueado vuelva
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "CaptureDevice":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def info(self) -> SourceInfo:
        """Formato efectivo del dispositivo abierto."""
        if self._info is None:
            raise RuntimeError(i18n.t("Dispositivo no abierto", "Device not open"))
        return self._info

    # --- Hilo de captura ----------------------------------------------------------------------
    def _loop(self) -> None:
        """Cuerpo del hilo: captura hasta que se pide cerrar y libera el dispositivo desde este mismo hilo."""
        try:
            self._capture_frames()
        finally:
            cap, self._cap = self._cap, None
            if cap is not None:
                cap.release()

    def _capture_frames(self) -> None:
        assert self._cap is not None
        index = 0
        previous: np.ndarray | None = None
        while self._running.is_set():
            ok, image = self._cap.read()
            # Sello inmediato tras ``read()``: es el instante de llegada que usa todo el modelo temporal.
            t = time.perf_counter()
            if not ok or image is None:
                self.error = i18n.t("La capturadora no entrega frames", "The capture device is not delivering frames")
                time.sleep(0.01)
                continue
            self.error = None
            st = self.stats
            if st.last_frame_at:
                dt = t - st.last_frame_at
                st.intervals.append(dt)
                expected = 1.0 / self._info.fps if self._info and self._info.fps > 0 else dt
                if dt > 1.5 * expected:
                    st.gaps += 1
            # Comparación submuestreada (1 de cada 8 píxeles por eje): basta para detectar un frame repetido y
            # es barata a 60 fps.
            if previous is not None and previous.shape == image.shape and np.array_equal(previous[::8, ::8], image[::8, ::8]):
                st.duplicates += 1
            previous = image
            st.frames += 1
            st.last_frame_at = t
            frame = Frame(image, t, index)
            index += 1
            with self._cond:
                self._buffer.append(frame)
                self._latest = frame
                self._cond.notify_all()

    # --- Consumo ------------------------------------------------------------------------------
    def read(self, timeout_s: float = 1.0) -> Frame | None:
        """Frame más antiguo pendiente (no se salta ninguno mientras quepa en el búfer)."""
        with self._cond:
            if not self._buffer:
                self._cond.wait(timeout_s)
            return self._buffer.popleft() if self._buffer else None

    def latest(self) -> Frame | None:
        """Último frame recibido, para mostrar en pantalla.

        Va aparte de la cola: aunque el detector la vacíe con ``read()``, la vista previa sigue teniendo imagen y
        no se congela."""
        return self._latest

    def __iter__(self) -> Iterator[Frame]:
        """Frames en orden mientras el dispositivo esté abierto."""
        while self._running.is_set():
            frame = self.read()
            if frame is not None:
                yield frame


@dataclass(frozen=True)
class CaptureDiagnostics:
    """Resultado de ``probe_device``: formato, ritmo real y si la imagen tiene contenido."""

    index: int
    opened: bool
    width: int = 0
    height: int = 0
    fourcc: str = ""
    reported_fps: float = 0.0
    measured_fps: float = 0.0
    frames: int = 0
    duplicates: int = 0
    gaps: int = 0
    jitter_ms: float = 0.0
    mean_brightness: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        """Apta para detectar parpadeos: ≥ 25 fps, ≥ 720p e imagen no negra."""
        return self.opened and self.frames > 0 and self.measured_fps >= 25 and self.height >= 720 and self.mean_brightness > 5

    def lines(self) -> list[str]:
        """Informe legible, una comprobación por línea (✓ bien, ~ aceptable, ✗ mal)."""
        if not self.opened:
            return [i18n.t("✗ Índice {index}: no se pudo abrir. {error}", "✗ Index {index}: could not be opened. {error}",
                           index=self.index, error=self.error).strip()]
        if self.frames == 0:
            return [i18n.t(
                "✗ Índice {index}: se abre pero no se reciben frames (¿en uso por otro programa? ¿sin señal HDMI?)",
                "✗ Index {index}: opens but no frames are received (in use by another program? no HDMI signal?)",
                index=self.index)]
        fps_mark = "✓" if self.measured_fps >= 55 else ("~" if self.measured_fps >= 25 else "✗")
        bright = self.mean_brightness > 5
        out = [
            i18n.t("{mark} Resolución {w}x{h} {fourcc}", "{mark} Resolution {w}x{h} {fourcc}",
                   mark="✓" if self.height >= 720 else "✗", w=self.width, h=self.height, fourcc=self.fourcc),
            i18n.t("{mark} {fps:.1f} FPS medidos (declarados {rep:.0f}), jitter {jitter:.1f} ms",
                   "{mark} {fps:.1f} FPS measured ({rep:.0f} reported), jitter {jitter:.1f} ms",
                   mark=fps_mark, fps=self.measured_fps, rep=self.reported_fps, jitter=self.jitter_ms),
            i18n.t("{mark} {dup} frames repetidos, {gaps} huecos de {frames}",
                   "{mark} {dup} duplicate frames, {gaps} gaps out of {frames}",
                   mark="✓" if self.duplicates / max(1, self.frames) < 0.1 else "~", dup=self.duplicates,
                   gaps=self.gaps, frames=self.frames),
            i18n.t("✓ Imagen con contenido", "✓ Image has content") if bright
            else i18n.t("✗ Imagen negra (¿Switch apagada o sin señal?)", "✗ Black image (Switch off or no signal?)"),
        ]
        if self.fourcc.upper() == "MJPG":
            out.append(i18n.t("~ Formato MJPEG: compresión con pérdida; vigilar la calidad de la señal del ojo",
                              "~ MJPEG format: lossy compression; keep an eye on the eye signal quality"))
        return out


def probe_device(index: int, seconds: float = 2.0, **kwargs) -> CaptureDiagnostics:
    """Abre un dispositivo, mide durante ``seconds`` y lo cierra. Abre hardware real: usar solo
    bajo petición explícita del usuario (la capturadora puede estar en uso)."""
    dev = CaptureDevice(index, **kwargs)
    try:
        info = dev.open()
    except RuntimeError as exc:
        return CaptureDiagnostics(index, False, error=str(exc))
    try:
        deadline = time.perf_counter() + seconds
        last: Frame | None = None
        while time.perf_counter() < deadline:
            frame = dev.read(timeout_s=0.5)
            if frame is not None:
                last = frame
        st = dev.stats
        return CaptureDiagnostics(
            index=index, opened=True, width=info.width, height=info.height, fourcc=info.fourcc,
            reported_fps=info.fps, measured_fps=st.measured_fps, frames=st.frames,
            duplicates=st.duplicates, gaps=st.gaps, jitter_ms=st.jitter_ms,
            mean_brightness=float(last.image.mean()) if last is not None else 0.0,
            error=dev.error or "",
        )
    finally:
        dev.close()
