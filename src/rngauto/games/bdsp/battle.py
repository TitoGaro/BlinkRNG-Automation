"""Combate y captura automáticos (BDSP), sin depender del idioma del juego.

La estrategia llama a este módulo cuando aparece el Pokémon objetivo: lanza la Master Ball, pasa los textos de
después de la captura y abre la ficha del capturado para leer sus estadísticas. Secuencia comprobada en consola:

1. Menú de combate → ABAJO ×2 (Bolsa) → A.
2. Bolsa: pestañas hasta «Poké Balls» (la activa es roja) → la Master Ball se busca por su icono → A → A
   («Usar»). La Master Ball no falla.
3. Tras la captura: A para pasar los textos (experiencia, subidas de nivel, registro en la Pokédex, «se ha unido
   al equipo»). Si alguien quiere aprender un movimiento sale la caja «Sustituir / Conservar»: ABAJO + A
   (conservar). Nunca se pulsa A con esa caja en pantalla: antes de cada A se espera a que el texto termine
   de escribirse y se mira la imagen ya quieta.
4. De vuelta en el mapa (X abre el menú) → «+» abre el mapa y B vuelve al menú con el cursor en «Mapa» (el
   cursor queda siempre en el mismo sitio) → ARRIBA (Pokédex) → DERECHA (Pokémon) → A → el capturado es el
   último del equipo → A → A (Datos) → DERECHA ×2 → página de estadísticas.

Todo va acotado en tiempo y comprobando la pantalla; si algo no cuadra se lanza ``BattleError`` (la
estrategia reinicia el juego o pide ayuda, nunca sigue a ciegas).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import numpy as np

from rngauto.control.controller import Button
from rngauto.i18n import t
from rngauto.vision.frames import NormBox
from rngauto.vision.screen import changed_fraction, thumbnail

from .screens import (
    BAG_TAB_X, _is_white, _stats, battle_menu_visible, dialog_visible, master_ball_selected,
    menu_visible,
)
from .shiny import battle_dialog_visible
from .summary import stats_page_visible

TEXT_REGION = NormBox(0.0, 0.55, 1.0, 0.45)  # textos y cajas de elección (parte baja)


class BattleError(RuntimeError):
    """Algo no cuadra durante el combate o la lectura de la ficha.

    Con ``fatal`` la estrategia no reintenta: hace falta que el usuario arregle algo en la partida.
    """

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal  # p. ej. no hay Master Ball: reiniciar no lo arregla


class BattleIO(Protocol):
    """Lo que necesita este módulo de la estrategia (o de una consola simulada en los tests)."""

    def now(self) -> float: ...
    def press(self, button: Button, label: str) -> None: ...  # pulsación corta; vuelve al terminar de enviarla
    def dpad(self, direction: str, label: str) -> None: ...
    def fresh(self, delay_s: float = 0.25) -> np.ndarray: ...  # imagen posterior (≥ delay_s) a la última pulsación
    def frame(self) -> np.ndarray: ...  # siguiente imagen
    def log(self, kind: str, message: str) -> None: ...


# Huecos blancos de la caja de dos opciones (arriba, entre opciones y abajo), medidos en las capturas reales de
# las dos cajas: «Sí / No» (losa) y «Sustituir / Conservar» (combate). Cada uno se busca dentro de su ventana.
CHOICE_GAPS = ((0.630, 0.672), (0.698, 0.732), (0.752, 0.795))
CHOICE_GAP_X = (0.72, 0.82)
CHOICE_GAP_MIN_H = 0.006  # alto mínimo del hueco (fracción de la imagen): ~6 px a 1080p


def _white_gap(image: np.ndarray, y0: float, y1: float) -> bool:
    """¿Hay entre ``y0`` e ``y1`` una franja de filas seguidas blancas (en ``CHOICE_GAP_X``)?"""
    h, w = image.shape[:2]
    region = image[int(y0 * h):int(y1 * h), int(CHOICE_GAP_X[0] * w):int(CHOICE_GAP_X[1] * w)].astype(np.int16)
    if region.size == 0:
        return False
    lo, hi = region.min(axis=2), region.max(axis=2)
    # Blanco de verdad (la caja mide ~250): el recuadro amarillo claro de la Pokédex (B≈212) no cuenta.
    white_rows = ((lo >= 225) & (hi - lo <= 30)).mean(axis=1) >= 0.98
    need, run = max(2, round(CHOICE_GAP_MIN_H * h)), 0
    for row_ok in white_rows:
        run = run + 1 if row_ok else 0
        if run >= need:
            return True
    return False


def choice_box_visible(image: np.ndarray) -> bool:
    """Caja de dos opciones a la derecha, sobre el texto («Sustituir un movimiento / Conservar movimientos»,
    «Sí / No»). Por su forma: tres huecos blancos (arriba, entre opciones y abajo) junto al borde derecho, fondo
    no blanco a su derecha y texto en los renglones. Validado con todas las capturas reales (solo se activa con
    esas cajas).

    Los huecos se buscan en una ventana y no a una altura fija: en la caja «Sí / No» el hueco entre opciones
    mide 15 px y una franja fija roza la «Sí» y el cursor ▶ (desviación 9,9 con tope 10). Con 1 px de
    desplazamiento o el ruido de la compresión la caja dejaba de detectarse y se pulsaba A, que elige «Sí» a
    ciegas."""
    if image is None or image.ndim != 3 or image.size == 0:
        return False
    if not all(_white_gap(image, y0, y1) for y0, y1 in CHOICE_GAPS):
        return False
    if _is_white(*_stats(image, NormBox(0.845, 0.66, 0.02, 0.10))):
        return False
    return any(float(_stats(image, NormBox(0.60, y, 0.225, 0.017))[1].sum()) >= 30 for y in (0.683, 0.738))


def wait_for(io: BattleIO, test: Callable[[np.ndarray], bool], timeout_s: float, streak: int = 1) -> np.ndarray | None:
    """Lee imágenes hasta que ``test`` se cumpla en ``streak`` seguidas; devuelve la última o ``None`` si se
    agota ``timeout_s``. Pedir varias seguidas evita fiarse de un frame de transición."""
    end = io.now() + timeout_s
    run = 0
    while io.now() < end:
        img = io.frame()
        run = run + 1 if test(img) else 0
        if run >= streak:
            return img
    return None


def settled(io: BattleIO, timeout_s: float = 2.5, still_s: float = 0.3) -> np.ndarray:
    """Imagen cuando la parte de los textos lleva ``still_s`` sin cambiar (el texto ya se ha escrito)."""
    end = io.now() + timeout_s
    img = io.fresh(0.25)  # nunca un frame de antes de que se vea el efecto de la última pulsación
    ref, since = thumbnail(img, TEXT_REGION), io.now()
    while io.now() < end:
        img = io.frame()
        cur = thumbnail(img, TEXT_REGION)
        if changed_fraction(ref, cur) > 0.01:
            ref, since = cur, io.now()
        elif io.now() - since >= still_s:
            return img
    return img


# --- 1-2. Master Ball -------------------------------------------------------------------------------------
BAG_OPEN_S = 1.5  # de la A en «Bolsa» a poder cambiar de bolsillo


def throw_master_ball(io: BattleIO, max_scroll: int = 40) -> None:
    """Desde el menú de combate abre la Bolsa, busca la Master Ball por su icono y la lanza.

    Comprueba que la selección sigue siendo la Master Ball antes de la última A. Lanza ``BattleError`` (fatal si
    no hay ninguna Master Ball) cuando algo no cuadra.
    """
    label = "Master Ball"
    if wait_for(io, battle_menu_visible, 10.0, streak=2) is None:
        raise BattleError(t("No aparece el menú de combate", "The battle menu does not appear"))
    end = io.now() + 1.0  # que el menú termine de animarse y ceda el lag de Columna Lanza
    while io.now() < end:
        io.frame()  # consume frames (y atiende la PARADA) en vez de dormir
    io.dpad("DOWN", label)
    io.dpad("DOWN", label)
    io.press(Button.A, label)
    # Secuencia fija, sin mirar las pestañas: la Bolsa se abre siempre en el Botiquín (la partida se recarga sin
    # guardar en cada intento) y las Poké Balls son el bolsillo de la derecha. Detectar la pestaña por colores
    # falla con Pokémon rojos en pantalla (p. ej. Ho-Oh).
    end = io.now() + BAG_OPEN_S  # animación de apertura de la Bolsa
    while io.now() < end:
        io.frame()
    io.dpad("RIGHT", label)  # Botiquín → Poké Balls
    found = _find_master_ball(io, label, max_scroll)
    # Red de seguridad: si no está (p. ej. la flecha se perdió por el lag), se recorren los demás bolsillos con
    # una búsqueda más corta, para no agotar el tiempo de la captura.
    for _ in range(len(BAG_TAB_X) - 1):
        if found:
            break
        io.log("warning", t("No veo la Master Ball en este bolsillo: pruebo en el siguiente",
                             "Master Ball not found in this pocket: trying the next one"))
        io.dpad("RIGHT", label)
        found = _find_master_ball(io, label, max(8, max_scroll // 2))
    if not found:
        raise BattleError(t("No encuentro la Master Ball en la Bolsa (hace falta al menos una)",
                           "Master Ball not found in the Bag (at least one is required)"), fatal=True)
    io.log("detection", t("Master Ball seleccionada (por su icono)", "Master Ball selected (by its icon)"))
    io.press(Button.A, label)  # menú «Usar / Salir» con el cursor en «Usar»
    if not master_ball_selected(io.fresh(0.5)):
        raise BattleError(t("Al elegir la Master Ball cambió la selección: no se lanza",
                             "The selection changed when choosing the Master Ball: not throwing it"))
    io.press(Button.A, label)
    io.log("input", t("Master Ball lanzada", "Master Ball thrown"))


def _find_master_ball(io: BattleIO, label: str, max_scroll: int) -> bool:
    """Recorre la lista del bolsillo actual (abajo y luego arriba) hasta tener la Master Ball seleccionada."""
    if master_ball_selected(io.fresh()):
        return True
    for direction, n in (("DOWN", max_scroll), ("UP", 2 * max_scroll)):
        for _ in range(n):
            io.dpad(direction, label)
            if master_ball_selected(io.fresh()):
                return True
    return False


# --- 3. Tras la captura ---------------------------------------------------------------------------------------
def finish_capture(io: BattleIO, timeout_s: float = 120.0) -> None:
    """Pasa los textos hasta volver al mapa y deja el menú X abierto.

    Ante la pregunta de aprender un movimiento elige «Conservar». Lanza ``BattleError`` si no vuelve al mapa en
    ``timeout_s``.
    """
    label = t("tras la captura", "after the capture")
    end = io.now() + timeout_s
    quiet_since: float | None = None
    probes = 0
    io.fresh(6.0)  # animación de la Master Ball (~8 s): no hace falta pulsar nada mientras
    while io.now() < end:
        img = settled(io)
        if choice_box_visible(img):
            io.log("detection", t("Pregunta de aprender movimiento: se conservan los actuales",
                                    "Move-learning prompt: keeping the current moves"))
            io.dpad("DOWN", label)
            if not choice_box_visible(io.fresh(0.2)):
                raise BattleError(t("La caja de elección desapareció antes de tiempo",
                                     "The choice box disappeared too early"))
            io.press(Button.A, label)
            quiet_since = None
            continue
        if menu_visible(img):
            return
        if dialog_visible(img) or battle_dialog_visible(img):
            quiet_since = None
            io.press(Button.A, label)
            continue
        # Sin texto: puede ser una transición, una pantalla que pide A (la ficha de la Pokédex) o ya el mapa. En
        # el mapa no se pulsa A (se hablaría con lo que haya delante): primero se espera y se prueba con X.
        quiet_since = io.now() if quiet_since is None else quiet_since
        if io.now() - quiet_since < 1.0:
            continue
        probes += 1
        io.press(Button.X, label)
        if wait_for(io, menu_visible, 1.8, streak=2) is not None:
            io.log("detection", t("De vuelta en el mapa", "Back on the map"))
            return
        if probes > 8:
            raise BattleError(t("No consigo volver al mapa tras la captura",
                                 "Unable to return to the map after the capture"))
        io.press(Button.A, label)  # no era el mapa: pantalla sin texto que se pasa con A
        quiet_since = None
    raise BattleError(t("Tras la captura no se vuelve al mapa a tiempo",
                         "Did not return to the map in time after the capture"))


# --- 4. Ficha -------------------------------------------------------------------------------------------------
def open_menu_from_anywhere(io: BattleIO, rounds: int = 5) -> None:
    """Deja abierto el menú X esté donde esté el juego (equipo, Bolsa, Pokédex, un texto, el mapa…): B para salir
    de lo que haya abierto (B siempre cancela o cierra; en el mapa no hace nada) y X para abrir el menú. Se comprueba
    con la imagen antes de cada pulsación: con el menú ya a la vista no se pulsa nada más."""
    label = t("abrir el menú", "open the menu")
    for _ in range(rounds):
        for _ in range(2):
            if wait_for(io, menu_visible, 0.8, streak=2) is not None:
                return
            io.press(Button.B, label)
            io.fresh(0.9)
        if wait_for(io, menu_visible, 0.8, streak=2) is not None:
            return
        io.press(Button.X, label)
        if wait_for(io, menu_visible, 2.0, streak=2) is not None:
            return
    raise BattleError(t("No consigo abrir el menú desde donde está el juego",
                         "Unable to open the menu from the current game screen"))


def open_stats_page(io: BattleIO, party_slot: int = 6) -> tuple[np.ndarray, np.ndarray]:
    """Desde el menú X abierto: ficha del Pokémon del puesto ``party_slot``. Devuelve (página de
    estadísticas, página «Notas del Entrenador»); la segunda dice la naturaleza cuando es neutra."""
    label = t("abrir la ficha", "open the Summary")
    if wait_for(io, menu_visible, 3.0, streak=2) is None:
        raise BattleError(t("El menú no está abierto", "The menu is not open"))
    # «+» y B devuelven al menú con el cursor siempre en «Mapa»: así el recorrido hasta «Pokémon» no depende de
    # dónde se quedó el cursor la vez anterior.
    io.press(Button.PLUS, label)  # mapa
    if wait_for(io, lambda f: not menu_visible(f), 4.0, streak=3) is None:
        raise BattleError(t("«+» no abrió el mapa", "«+» did not open the map"))
    io.fresh(0.6)
    io.press(Button.B, label)
    if wait_for(io, menu_visible, 4.0, streak=3) is None:
        raise BattleError(t("Al salir del mapa no vuelve el menú",
                             "The menu did not return after leaving the map"))
    io.dpad("UP", label)  # Pokédex
    io.dpad("RIGHT", label)  # Pokémon
    io.press(Button.A, label)
    if wait_for(io, lambda f: not menu_visible(f), 3.0, streak=3) is None:
        raise BattleError(t("No se abrió el equipo", "The party screen did not open"))
    io.fresh(1.0)
    for _ in range(max(0, party_slot - 1)):
        io.dpad("DOWN", label)
    io.press(Button.A, label)  # submenú con el cursor en «Datos»
    io.fresh(0.5)
    io.press(Button.A, label)
    io.fresh(1.2)
    io.dpad("RIGHT", label)
    notes = io.fresh(0.6)  # «Notas del Entrenador» (naturaleza escrita)
    io.dpad("RIGHT", label)
    img = wait_for(io, stats_page_visible, 4.0, streak=3)
    if img is None:
        raise BattleError(t("No llego a la página de estadísticas de la ficha",
                             "Unable to reach the stats page of the Summary"))
    return io.fresh(0.3), notes
