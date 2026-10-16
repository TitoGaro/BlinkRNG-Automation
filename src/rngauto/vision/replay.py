"""Replay: grabar sesiones reales una vez y re-ejecutar el pipeline offline con los tiempos originales.

Una grabación = vídeo (``.mp4``/``.avi``) + ``<nombre>.frames.jsonl`` con, por frame, el índice y el
timestamp de llegada original. Sin sidecar, el tiempo se deriva del contenedor (índice / fps).
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import cv2

from rngauto.i18n import t

from .frames import Frame, SourceInfo


def sidecar_path(video: Path) -> Path:
    """Fichero de tiempos asociado a un vídeo: ``captura.mp4`` → ``captura.frames.jsonl``."""
    return video.with_suffix(".frames.jsonl")


class VideoFileSource:
    """``FrameSource`` sobre un vídeo grabado.

    Con ``realtime=True`` entrega cada frame a su hora original (para probar el bucle en vivo); si no, tan rápido
    como se decodifica."""

    def __init__(self, path: Path, realtime: bool = False, use_sidecar: bool = True) -> None:
        self.path = Path(path)
        self._cap = cv2.VideoCapture(str(self.path))
        if not self._cap.isOpened():
            raise FileNotFoundError(t("No se puede abrir el vídeo {path}", "Cannot open the video {path}", path=self.path))
        self.realtime = realtime
        # Algunos contenedores no declaran FPS (OpenCV devuelve 0): se asume 30.
        self._fps = float(self._cap.get(cv2.CAP_PROP_FPS)) or 30.0
        self._times: list[float] | None = None
        side = sidecar_path(self.path)
        if use_sidecar and side.exists():
            with side.open(encoding="utf-8") as f:
                self._times = [json.loads(line)["t_s"] for line in f if line.strip()]
        self._index = 0
        self._wall0: float | None = None

    def info(self) -> SourceInfo:
        return SourceInfo(
            self.path.name,
            int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            self._fps,
        )

    def read(self) -> Frame | None:
        """Siguiente frame con su instante original (del sidecar o, si falta, índice / fps)."""
        ok, image = self._cap.read()
        if not ok:
            return None
        i = self._index
        self._index += 1
        pts = i / self._fps
        t = self._times[i] if self._times is not None and i < len(self._times) else pts
        if self.realtime:
            # Reloj de pared alineado con el primer frame; los demás se esperan respecto a él.
            if self._wall0 is None:
                self._wall0 = time.perf_counter() - t
            delay = self._wall0 + t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        return Frame(image, t, i, pts)

    def close(self) -> None:
        self._cap.release()

    def __iter__(self) -> Iterator[Frame]:
        while (frame := self.read()) is not None:
            yield frame

    def __enter__(self) -> "VideoFileSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FrameRecorder:
    """Graba frames a vídeo + sidecar de tiempos. ``codec='FFV1'``/``'MJPG'`` según calidad deseada."""

    def __init__(self, path: Path, fps: float, size: tuple[int, int], codec: str = "MJPG") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*codec), fps, size)
        if not self._writer.isOpened():
            raise RuntimeError(t("No se pudo crear el vídeo {path} con el códec {codec}",
                                 "Could not create the video {path} with the {codec} codec", path=self.path, codec=codec))
        self._side = sidecar_path(self.path).open("w", encoding="utf-8")
        self.frames = 0

    def write(self, frame: Frame) -> None:
        """Añade el frame al vídeo y su instante de llegada al sidecar."""
        self._writer.write(frame.image)
        self._side.write(json.dumps({"index": frame.index, "t_s": frame.t_s}) + "\n")
        self.frames += 1

    def close(self) -> None:
        self._writer.release()
        self._side.close()

    def __enter__(self) -> "FrameRecorder":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
