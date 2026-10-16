"""Configuración común de los tests."""
import pytest

from rngauto import i18n


@pytest.fixture(autouse=True)
def _spanish_by_default():
    """Los tests comprueban los textos en español salvo que fijen otro idioma; se restaura al acabar."""
    previous = i18n.language()
    i18n.set_language("es")
    yield
    i18n.set_language(previous)
