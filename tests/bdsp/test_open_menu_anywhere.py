"""Tras «Listo» (Cresselia), el programa abre el menú esté donde esté el usuario. Con capturas reales."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from rngauto.control.controller import Button
from rngauto.games.bdsp.battle import open_menu_from_anywhere

EXP = Path.home() / ".rngauto" / "exploracion" / "20261005-004112"
pytestmark = [pytest.mark.local, pytest.mark.skipif(not (EXP / "005_menu.png").exists(), reason="sin capturas reales")]


def img(name):
    return cv2.imdecode(np.fromfile(EXP / f"{name}.png", np.uint8), cv2.IMREAD_COLOR)


class Console:
    """Pantallas reales y qué hace cada botón en ellas (B sale/cancela; X abre el menú desde el mapa)."""

    SCREENS = {"mapa": "004_sin_mensaje", "menu": "005_menu", "pokedex": "009_pokedex", "texto": "015_losa_a1"}
    B = {"pokedex": "menu", "menu": "mapa", "texto": "mapa", "mapa": "mapa"}

    def __init__(self, screen):
        self.screen, self.t, self.presses = screen, 0.0, []
        self.images = {k: img(v) for k, v in self.SCREENS.items()}

    def now(self):
        return self.t

    def frame(self):
        self.t += 0.02
        return self.images[self.screen]

    def fresh(self, delay_s=0.25):
        self.t += delay_s
        return self.frame()

    def press(self, button, label):
        self.presses.append(button.name)
        if button is Button.B:
            self.screen = self.B[self.screen]
        elif button is Button.X and self.screen == "mapa":
            self.screen = "menu"

    def dpad(self, direction, label):
        pass

    def log(self, kind, message):
        pass


@pytest.mark.parametrize("start, presses", [
    ("menu", []),               # ya está abierto: no se toca nada
    ("pokedex", ["B"]),         # en la Pokédex: B vuelve al menú
    ("mapa", ["B", "B", "X"]),  # en el mapa: B no hace nada, X lo abre
    ("texto", ["B", "B", "X"]),  # un texto en pantalla: B lo cierra y X abre el menú
])
def test_opens_the_menu_from_anywhere(start, presses):
    console = Console(start)
    open_menu_from_anywhere(console)
    assert console.screen == "menu"
    assert console.presses == presses
