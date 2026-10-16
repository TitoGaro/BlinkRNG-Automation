"""Idioma de la interfaz y de los mensajes (español / inglés).

Los textos van en el código con sus dos versiones juntas, para que nunca se desincronicen::

    from rngauto.i18n import t
    label.setText(t("Conecta la Switch", "Connect the Switch"))
    t("Faltan {n} avances", "{n} advances left", n=42)

El idioma se fija al arrancar (preferencias del usuario o, la primera vez, el de Windows) y al cambiarlo la
interfaz se reconstruye. ``t`` es seguro desde cualquier hilo: solo lee una variable.
"""
from __future__ import annotations

import locale

LANGUAGES = {"es": "Español", "en": "English"}
DEFAULT = "en"

_current = DEFAULT


def system_language() -> str:
    """Idioma de Windows si es uno de los disponibles; si no, inglés."""
    try:
        code = (locale.getlocale()[0] or "").lower()
    except (ValueError, TypeError):
        code = ""
    if not code:
        try:
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            # Los 10 bits bajos del LANGID son el idioma principal (LANG_SPANISH = 0x0A), sin la variante regional.
            code = "es" if (lang_id & 0x3FF) == 0x0A else "en"
        except (AttributeError, OSError):
            code = ""
    return "es" if code.startswith(("es", "spanish")) else DEFAULT


def set_language(code: str) -> None:
    """Cambia el idioma actual; un código desconocido deja el predeterminado."""
    global _current
    _current = code if code in LANGUAGES else DEFAULT


def language() -> str:
    """Código del idioma actual (``"es"`` o ``"en"``)."""
    return _current


def is_spanish() -> bool:
    return _current == "es"


def t(es: str, en: str, /, **fmt: object) -> str:
    """Texto en el idioma actual; con ``fmt``, se formatea con ``str.format``."""
    text = es if _current == "es" else en
    return text.format(**fmt) if fmt else text


def pick(values: dict[str, str] | None, fallback: str = "") -> str:
    """Valor localizado de un diccionario ``{"es": …, "en": …}`` (p. ej. textos de los TOML)."""
    if not values:
        return fallback
    return values.get(_current) or values.get("es") or values.get("en") or fallback


def fmt_int(n: float) -> str:
    """Número entero con el separador de miles del idioma: «12.345» en español, «12,345» en inglés."""
    text = f"{round(n):,}"
    return text.replace(",", ".") if _current == "es" else text
