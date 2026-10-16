"""Detección automática de hardware: puertos serie (ESP32), capturadoras (por nombre) y su audio.

Todo es heurístico (por nombre de dispositivo): la interfaz propone y el usuario puede corregir. Salvo
``probe_cameras``, nada de aquí abre los dispositivos.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from rngauto.i18n import t

# Chips puente USB-serie habituales en placas ESP32 y textos que los delatan en la descripción del puerto.
USB_SERIAL_HINTS = ("CP210", "CH340", "CH910", "CH343", "USB-SERIAL", "USB SERIAL", "UART", "ESP32", "SILICON LABS")
# Nombres típicos de cámaras integradas o webcams, que nunca son la capturadora.
WEBCAM_HINTS = ("webcam", "web cam", "integrated", "integrada", "facetime", "ir camera", "front", "rear", "hd camera")


@dataclass(frozen=True)
class SerialPortInfo:
    """Puerto serie del sistema y si parece una placa ESP32."""

    device: str
    description: str
    likely_esp32: bool

    @property
    def label(self) -> str:
        return f"{self.device} — {self.description}" + (t("  (probable ESP32)", "  (likely ESP32)")
                                                         if self.likely_esp32 else "")


def list_serial_ports() -> list[SerialPortInfo]:
    """Puertos serie disponibles, los que parecen ESP32 primero."""
    from serial.tools import list_ports

    ports = []
    for p in list_ports.comports():
        text = f"{p.description} {p.manufacturer or ''} {p.hwid}".upper()
        ports.append(SerialPortInfo(p.device, p.description or p.device, any(h in text for h in USB_SERIAL_HINTS)))
    return sorted(ports, key=lambda p: (not p.likely_esp32, p.device))


@dataclass(frozen=True)
class CameraInfo:
    """Dispositivo de vídeo. Los campos de imagen solo se rellenan si se ha sondeado (``probe_cameras``)."""

    index: int
    name: str = ""
    opened: bool = True
    width: int = 0
    height: int = 0
    frames: bool = False
    thumbnail: np.ndarray | None = None

    @property
    def is_webcam(self) -> bool:
        return any(h in self.name.lower() for h in WEBCAM_HINTS)

    @property
    def label(self) -> str:
        name = self.name or t("Dispositivo {index}", "Device {index}", index=self.index)
        if self.thumbnail is None and self.width == 0:
            return name + ("  (webcam)" if self.is_webcam else "")
        if not self.frames:
            return t("{name}: sin imagen", "{name}: no image", name=name)
        return f"{name}: {self.width}x{self.height}"


def list_cameras() -> list[CameraInfo]:
    """Capturadoras por nombre e índice MSMF, sin abrirlas (no enciende la webcam)."""
    try:
        import cv2
        from cv2_enumerate_cameras import enumerate_cameras

        return [CameraInfo(c.index, c.name) for c in enumerate_cameras(cv2.CAP_MSMF)]
    except Exception:  # librería no disponible: se sondean índices
        return [CameraInfo(c.index, c.name, c.opened, c.width, c.height, c.frames, c.thumbnail)
                for c in probe_cameras(4) if c.opened]


def find_capture_card(cameras: list[CameraInfo]) -> CameraInfo | None:
    """La capturadora es el dispositivo de vídeo que no parece una webcam."""
    candidates = [c for c in cameras if not c.is_webcam]
    return candidates[0] if candidates else None


def probe_cameras(max_index: int = 4, read_timeout_s: float = 2.0, indices: list[int] | None = None) -> list[CameraInfo]:
    """Abre brevemente cada índice y toma una miniatura. Solo bajo petición del usuario: abrir un índice puede
    encender la webcam o quitarle la capturadora a otro programa."""
    import os

    # Debe fijarse antes de importar cv2. Con las transformaciones por hardware de MSMF, algunas capturadoras
    # tardan mucho en abrir o no entregan imagen.
    os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
    import cv2

    names: dict[int, str] = {}
    try:
        from cv2_enumerate_cameras import enumerate_cameras

        names = {c.index: c.name for c in enumerate_cameras(cv2.CAP_MSMF)}
    except Exception:
        pass
    found: list[CameraInfo] = []
    for index in indices if indices is not None else range(max_index):
        cap = cv2.VideoCapture(index, cv2.CAP_MSMF)
        if not cap.isOpened():
            cap.release()
            found.append(CameraInfo(index, names.get(index, ""), opened=False))
            continue
        # Se pide 1080p para saber la resolución máxima real (la que devuelve el dispositivo tras pedirla).
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        image = None
        deadline = time.monotonic() + read_timeout_s
        while time.monotonic() < deadline:
            ok, img = cap.read()
            if ok and img is not None:
                image = img
                break
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        thumb = cv2.resize(image, (320, int(320 * image.shape[0] / image.shape[1]))) if image is not None else None
        found.append(CameraInfo(index, names.get(index, ""), True, w, h, image is not None, thumb))
    return found


@dataclass(frozen=True)
class AudioInputInfo:
    """Entrada de audio de PortAudio (``sounddevice``). ``index`` es el índice global del dispositivo."""

    index: int
    name: str
    samplerate: float
    channels: int


def find_capture_audio_candidates(video_name: str = "") -> list[AudioInputInfo]:
    """Entradas de la capturadora en cada API de audio de Windows, mejor primero.

    Windows publica el mismo dispositivo una vez por API (MME, DirectSound, WASAPI…) y con el nombre a veces
    truncado; se buscan las demás copias por los primeros caracteres del nombre. ``AudioPassthrough`` las
    prueba en orden hasta que una funciona.
    """
    first = find_capture_audio(video_name)
    if first is None:
        return []
    try:
        import sounddevice as sd

        devices = sd.query_devices()
    except Exception:
        return [first]
    base = first.name.split("(")[-1].rstrip(")").lower() if "(" in first.name else first.name.lower()
    out = [first]
    for i, d in enumerate(devices):
        if i != first.index and d["max_input_channels"] > 0 and base[:12] in d["name"].lower():
            out.append(AudioInputInfo(i, d["name"], float(d["default_samplerate"]), min(2, int(d["max_input_channels"]))))
    return out


def find_capture_audio(video_name: str = "") -> AudioInputInfo | None:
    """Entrada de audio de la capturadora (p. ej. «USB2 Digital Audio» junto a «USB2 Video»).

    Se prefiere WASAPI (menor latencia) y un nombre que comparta palabra con el de la capturadora.
    """
    try:
        import sounddevice as sd
    except Exception:
        return None
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return None
    # Palabras significativas del nombre de la capturadora de vídeo (sin las genéricas).
    tokens = {tok.lower() for tok in video_name.replace("(", " ").replace(")", " ").split() if len(tok) >= 3}
    tokens -= {"video", "capture", "device", "camera"}
    best: tuple[int, int] | None = None
    for i, d in enumerate(devices):
        if d["max_input_channels"] < 1:
            continue
        name = d["name"].lower()
        api = hostapis[d["hostapi"]]["name"]
        score = 0
        if tokens and any(tok in name for tok in tokens):
            score += 10
        if any(h in name for h in ("hdmi", "capture", "usb2", "cam link", "elgato", "digital audio")):
            score += 5
        # Micrófonos, «mezcla estéreo» y dispositivos virtuales de Windows (asignador, controlador primario).
        if any(h in name for h in ("micr", "mezcla", "stereo mix", "asignador", "primario", "mapper")):
            score -= 20
        if score <= 0:
            continue
        score += {"Windows WASAPI": 3, "Windows DirectSound": 2, "MME": 1}.get(api, 0)
        if best is None or score > best[0]:
            best = (score, i)
    if best is None:
        return None
    d = devices[best[1]]
    return AudioInputInfo(best[1], d["name"], float(d["default_samplerate"]), min(2, int(d["max_input_channels"])))
