"""Configuración común de los tests."""
import pytest

from rngauto import i18n


@pytest.fixture(autouse=True)
def _spanish_by_default(monkeypatch):
    """Los tests comprueban los textos en español salvo que fijen otro idioma; se restaura al acabar.

    También se fija el idioma «del sistema»: la aplicación lo usa la primera vez (sin preferencias guardadas) y
    los tests no pueden depender de en qué idioma esté el Windows de cada máquina."""
    monkeypatch.setattr(i18n, "system_language", lambda: "es")
    previous = i18n.language()
    i18n.set_language("es")
    yield
    i18n.set_language(previous)
