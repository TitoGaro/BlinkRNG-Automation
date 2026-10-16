"""Grabación de cada entrada en combate (imagen reducida + sonido) y aprendizaje del sonido shiny.

* Cada combate guarda su sonido (``combate.wav``) y el veredicto (``veredicto.json``) en
  ``~/.rngauto/combates/<fecha>/``; el vídeo solo si es shiny o hay algo sospechoso. Se conservan los últimos
  ``keep`` combates normales: sirven de «negativos» para ajustar el umbral del sonido.
* Con el primer shiny se aprende la plantilla del sonido (``~/.rngauto/sonidos/shiny_bdsp.npz``) a partir de
  ese combate y de los normales guardados; a partir de ahí el sonido también cuenta en la detección.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from .shiny import ShinyVerdict
from .shiny_sound import SoundTemplate, learn_template, read_wav, write_wav

CLIP_SIZE = (960, 540)  # media resolución: basta para revisar el combate y ocupa la cuarta parte


class EncounterClip:
    """Imágenes de una entrada en combate, guardadas en memoria como JPEG reducidos.

    Comprimir al añadir y limitar a ``max_frames`` imágenes mantiene acotada la memoria; el vídeo solo se
    escribe a disco si al final hace falta.
    """

    def __init__(self, max_frames: int = 3000) -> None:
        self.frames: list[tuple[float, bytes]] = []
        self.max_frames = max_frames

    def add(self, t: float, image: np.ndarray) -> None:
        """Añade la imagen del instante ``t``; si ya hay ``max_frames``, la descarta."""
        if len(self.frames) >= self.max_frames:
            return
        small = cv2.resize(image, CLIP_SIZE, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.frames.append((t, buf.tobytes()))

    def save(self, folder: Path, *, verdict: ShinyVerdict, events: list[tuple[float, str]], t0: float,
             audio: np.ndarray | None = None, rate: int | None = None, audio_t0: float | None = None,
             video: bool = False) -> Path:
        """Escribe en ``folder`` el veredicto (``veredicto.json``), el sonido (``combate.wav``) y, con ``video``,
        el vídeo (``combate.avi``). Los tiempos se guardan relativos a ``t0``. Devuelve ``folder``."""
        folder.mkdir(parents=True, exist_ok=True)
        info = {
            "veredicto": asdict(verdict) | {"alpha": verdict.alpha, "is_shiny": verdict.is_shiny},
            "eventos": [(round(t - t0, 3), what) for t, what in events],
            "audio_inicio_s": None if audio_t0 is None else round(audio_t0 - t0, 3),
            "video_inicio_s": round(self.frames[0][0] - t0, 3) if self.frames else None,
        }
        (folder / "veredicto.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        if audio is not None and rate and len(audio):
            write_wav(folder / "combate.wav", audio, rate)
        if video and len(self.frames) >= 2:
            span = self.frames[-1][0] - self.frames[0][0]
            # fps real de lo capturado, para que el vídeo dure lo mismo que el combate
            fps = max(5.0, min(60.0, (len(self.frames) - 1) / span)) if span > 0 else 30.0
            writer = cv2.VideoWriter(str(folder / "combate.avi"), cv2.VideoWriter_fourcc(*"MJPG"), fps, CLIP_SIZE)
            try:
                for _, data in self.frames:
                    writer.write(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR))
            finally:
                writer.release()
        return folder


def new_clip_dir(base: Path, shiny: bool) -> Path:
    """Carpeta para un combate nuevo, con la fecha y hora; los shiny llevan el sufijo ``-SHINY``."""
    return base / (dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ("-SHINY" if shiny else ""))


def prune_normal_clips(base: Path, keep: int) -> None:
    """Borra los combates normales más antiguos y deja los ``keep`` últimos. Los shiny no se borran nunca."""
    normal = sorted(p for p in base.glob("*") if p.is_dir() and not p.name.endswith("SHINY"))
    for old in normal[:-keep] if keep > 0 else normal:
        shutil.rmtree(old, ignore_errors=True)


def normal_sounds(base: Path, limit: int = 15) -> list[tuple[np.ndarray, int]]:
    """Sonidos ``(muestras, frecuencia)`` de hasta ``limit`` combates normales, del más reciente al más antiguo.
    Los ficheros ilegibles se saltan."""
    out = []
    for p in sorted(base.glob("*"), reverse=True):
        if p.is_dir() and not p.name.endswith("SHINY") and (p / "combate.wav").exists():
            try:
                out.append(read_wav(p / "combate.wav"))
            except (OSError, ValueError):
                continue
            if len(out) >= limit:
                break
    return out


def learn_from_shiny(audio: np.ndarray, rate: int, sparkle_s: float, clips_base: Path, out_path: Path) -> SoundTemplate:
    """Plantilla del sonido shiny a partir de este combate (destellos vistos a ``sparkle_s`` del inicio del
    audio) comparada con los combates normales guardados; se guarda en ``out_path``."""
    baseline = [s for s, r in normal_sounds(clips_base) if r == rate]
    tpl = learn_template(audio, rate, sparkle_s, baseline=baseline or None)
    tpl.save(out_path)
    return tpl
