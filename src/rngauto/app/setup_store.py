"""Preparación guardada (plantilla del ojo por encuentro, preferencias): la escribe la interfaz, nunca a mano.

Se guarda en la carpeta de datos (``~/.rngauto``) y tiene prioridad sobre la caja/plantilla del TOML.
Un fichero dañado o de otra versión se ignora: se vuelve a la configuración por defecto.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from rngauto import i18n
from rngauto.core.encounter import EncounterConfiguration
from rngauto.i18n import t
from rngauto.vision.imageio import imread, imwrite
from rngauto.vision.frames import NormBox

DEFAULT_DATA_DIR = Path.home() / ".rngauto"


@dataclass(frozen=True)
class EyeSetup:
    """Ojo marcado en el paso «Escena»: plantilla, caja normalizada y altura del frame de origen."""

    encounter: str
    template_file: str  # ruta absoluta del PNG
    box: tuple[float, float, float, float]
    reference_height: int
    created: str

    @property
    def norm_box(self) -> NormBox:
        return NormBox(*self.box)

    def load_template(self) -> np.ndarray | None:
        return imread(self.template_file)


def _write_atomic(path: Path, text: str) -> None:
    """Escribe en un temporal y lo renombra: un cierre a medias nunca deja el fichero a medio escribir."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _setup_path(data_dir: Path, encounter_id: str) -> Path:
    return data_dir / "setups" / f"{encounter_id}.json"


def save_eye_setup(data_dir: Path, encounter_id: str, template: np.ndarray, box: NormBox, reference_height: int) -> EyeSetup:
    """Guarda la plantilla (PNG) y su JSON en ``setups/``. Lanza ``OSError`` si no se puede escribir."""
    folder = data_dir / "setups"
    folder.mkdir(parents=True, exist_ok=True)
    png = folder / f"{encounter_id}_eye.png"
    if not imwrite(png, template):
        raise OSError(t("No se pudo guardar la plantilla en {path}", "Could not save the template to {path}", path=png))
    setup = EyeSetup(encounter_id, str(png), (box.x, box.y, box.w, box.h), reference_height,
                     dt.datetime.now().isoformat(timespec="seconds"))
    _write_atomic(_setup_path(data_dir, encounter_id), json.dumps(asdict(setup), indent=2))
    return setup


def load_eye_setup(data_dir: Path, encounter_id: str) -> EyeSetup | None:
    """Preparación guardada del encuentro, o ``None`` si no hay, está dañada o falta el PNG."""
    path = _setup_path(data_dir, encounter_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["box"] = tuple(float(v) for v in data["box"])
        setup = EyeSetup(**data)
    except (OSError, ValueError, TypeError, KeyError):  # fichero dañado o de otra versión: se vuelve a marcar el ojo
        return None
    return setup if Path(setup.template_file).exists() else None


def resolve_eye(enc: EncounterConfiguration, data_dir: Path, assets_dir: Path) -> tuple[np.ndarray, NormBox, int] | None:
    """Plantilla, caja y altura de referencia efectivas (preparación del usuario o assets del proyecto)."""
    setup = load_eye_setup(data_dir, enc.id)
    if setup is not None:
        tpl = setup.load_template()
        if tpl is not None:
            return tpl, setup.norm_box, setup.reference_height
    tpl = imread(assets_dir / enc.blink_scene.eye_template)
    if tpl is None:
        return None
    return tpl, NormBox(*enc.blink_scene.box), enc.blink_scene.reference_height


@dataclass
class Preferences:
    """Preferencias de la interfaz (``preferences.json``). ``extra`` guarda claves sueltas, p. ej. el idioma."""

    serial_port: str = ""
    capture_index: int = 1
    lead_s: float = 0.10
    tid: int = 0
    sid: int = 0
    last_encounter: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path) -> "Preferences":
        path = data_dir / "preferences.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Se ignoran claves desconocidas (de versiones anteriores o posteriores).
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError, AttributeError):  # dañado o de otra versión: valores por defecto
            return cls()

    def save(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(data_dir / "preferences.json", json.dumps(asdict(self), indent=2))


def apply_saved_language(data_dir: Path = DEFAULT_DATA_DIR) -> str:
    """Fija el idioma de los mensajes como la interfaz gráfica: el guardado en las preferencias o, si no hay,
    el de Windows. Para las herramientas de consola, que arrancan sin pasar por la interfaz."""
    try:
        saved = Preferences.load(data_dir).extra.get("language")
    except OSError:  # preferencias ilegibles: no impiden arrancar
        saved = None
    i18n.set_language(str(saved or i18n.system_language()))
    return i18n.language()
