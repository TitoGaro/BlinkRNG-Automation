"""Combate y captura automáticos contra una «consola» que enseña capturas reales de la Switch del usuario.

Las capturas no se distribuyen (son del juego): si no están en ``~/.rngauto`` el test se salta.
"""
from pathlib import Path

import cv2
import numpy as np
import pytest

from rngauto.control.controller import Button
from rngauto.games.bdsp.battle import BattleError, choice_box_visible, finish_capture, open_stats_page, throw_master_ball

RUN = Path.home() / ".rngauto" / "ejecuciones" / "20261005-014026-regice_hansa"
pytestmark = [pytest.mark.local, pytest.mark.skipif(not (RUN / "062_g3.png").exists(), reason="sin capturas reales de la consola")]

CHOICES = {7, 10, 19, 22, 25}  # «Sustituir un movimiento / Conservar movimientos» tras estas capturas


def img(name: str) -> np.ndarray:
    return cv2.imdecode(np.fromfile(RUN / f"{name}.png", np.uint8), cv2.IMREAD_COLOR)


class FakeConsole:
    """Máquina de pantallas mínima: cada botón lleva a la captura real que se vio en la consola."""

    def __init__(self, master_at: int = 7) -> None:
        self.t = 0.0
        self.screen = "battle"
        self.pocket = 0
        self.item = 0
        self.master_at = master_at
        self.post = 0
        self.cursor_second = False
        self.slot = 1
        self.page = 1
        self.inputs: list[str] = []
        self.chose_replace = False
        self.cache: dict[str, np.ndarray] = {}

    def _img(self, name: str) -> np.ndarray:
        if name not in self.cache:
            self.cache[name] = img(name)
        return self.cache[name]

    # --- BattleIO ---
    def now(self) -> float:
        return self.t

    def log(self, kind: str, message: str) -> None:
        pass

    def frame(self) -> np.ndarray:
        self.t += 1 / 30
        s = self.screen
        if s == "battle":
            return self._img("012_menu_combate_1")
        if s == "bag":
            if self.pocket == 0:
                return self._img("014_bolsillo")
            return self._img("024_busca_master" if self.item == self.master_at else "017_busca_master")
        if s == "usar":
            return self._img("025_usar_salir")
        if s == "throw":
            return self._img("056_post_30") if self.t > 1e9 else self._img("011_c3_9")  # combate sin menú
        if s == "post":
            return self._img(f"{26 + self.post:03d}_post_{self.post:02d}")
        if s == "overworld":
            return self._img("002_tras_mash")
        if s == "menu":
            return self._img("058_menu_tras_mapa")
        if s == "map":
            return self._img("002_tras_mash")
        if s == "party":
            return self._img("059_equipo")
        if s == "summary":
            return self._img({1: "060_g1", 2: "061_g2", 3: "062_g3"}[self.page])
        raise AssertionError(s)

    def fresh(self, delay_s: float = 0.25) -> np.ndarray:
        self.t += delay_s
        return self.frame()

    def dpad(self, direction: str, label: str) -> None:
        self.inputs.append(direction)
        self.t += 0.32
        s = self.screen
        if s == "battle":
            pass
        elif s == "bag":
            if direction == "RIGHT":
                self.pocket = min(3, self.pocket + 1)
            elif direction == "LEFT":
                self.pocket = max(0, self.pocket - 1)
            elif direction == "DOWN":
                self.item = (self.item + 1) % 12
            elif direction == "UP":
                self.item = (self.item - 1) % 12
        elif s == "post" and direction == "DOWN":
            self.cursor_second = True
        elif s == "party" and direction == "DOWN":
            self.slot = self.slot % 6 + 1
        elif s == "summary" and direction == "RIGHT":
            self.page = min(3, self.page + 1)

    def press(self, button: Button, label: str) -> None:
        self.inputs.append(button.name)
        self.t += 0.16
        s = self.screen
        if button is Button.A:
            if s == "battle" and self.inputs[-3:-1] == ["DOWN", "DOWN"]:
                self.screen = "bag"
            elif s == "bag" and self.pocket == 1 and self.item == self.master_at:
                self.screen = "usar"
            elif s == "usar":
                self.screen = "post"  # la animación de la bola se salta: se va a los textos
            elif s == "post":
                if self.post in CHOICES and not self.cursor_second:
                    self.chose_replace = True
                self.cursor_second = False
                if self.post >= 30:
                    self.screen = "overworld"
                else:
                    self.post += 1
            elif s == "party":
                self.screen = "submenu"
            elif s == "submenu":
                self.screen = "summary"
        elif button is Button.X and s == "overworld":
            self.screen = "menu"
        elif button is Button.PLUS and s == "menu":
            self.screen = "map"
        elif button is Button.B and s == "map":
            self.screen = "menu"
        if s == "menu" and button is Button.A:
            self.screen = "party"

    # el submenú del equipo se ve igual que el equipo
    def _submenu(self) -> np.ndarray:
        return self._img("059_equipo")


def _frame_with_submenu(console: FakeConsole):
    original = console.frame

    def frame() -> np.ndarray:
        if console.screen == "submenu":
            console.t += 1 / 30
            return console._img("059_equipo")
        return original()

    console.frame = frame
    return console


def test_choice_box_only_on_the_move_question():
    seen = [k for k in range(31) if choice_box_visible(img(f"{26 + k:03d}_post_{k:02d}"))]
    assert set(seen) == CHOICES


def test_full_capture_sequence_on_real_frames():
    console = _frame_with_submenu(FakeConsole(master_at=7))
    throw_master_ball(console)
    assert console.screen == "post"
    finish_capture(console)
    assert console.screen == "menu" and not console.chose_replace  # siempre «Conservar movimientos»
    page, notes = open_stats_page(console, party_slot=6)
    assert console.screen == "summary" and console.page == 3 and console.slot == 6
    assert page is not None and notes is not None
    assert "R" not in console.inputs  # nunca «Guardar»


def test_master_ball_above_the_cursor_is_found_going_up():
    console = FakeConsole(master_at=10)  # p. ej. al final de la lista: se encuentra bajando y dando la vuelta
    throw_master_ball(console)
    assert console.screen == "post"


def test_no_master_ball_is_fatal():
    console = FakeConsole(master_at=99)
    with pytest.raises(BattleError) as err:
        throw_master_ball(console, max_scroll=12)
    assert err.value.fatal
