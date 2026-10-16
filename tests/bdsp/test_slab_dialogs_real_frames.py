"""Ruta de la losa (Parque Hansa) contra una «consola» con las capturas reales y retraso de vídeo.

Reproduce los fallos del 2026-10-05:

* con frames viejos en la cola (el vídeo llega con retraso), el programa veía la pregunta sin la caja Sí/No y
  volvía a pulsar A, contestando «Sí» a ciegas y pasándose toda la secuencia;
* la caja Sí/No dejaba de reconocerse con 1 px de desplazamiento o el ruido de la compresión (mismo efecto);
* en el Espacio Puro, el tramo siguiente (cruceta + A) ya estaba en cola cuando se veía el texto del legendario:
  esa A de más lo cerraba y el combate empezaba antes de la A final.

Las capturas no se distribuyen: si no están en ``~/.rngauto`` el test se salta.
"""
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from rngauto.control.controller import Button, DPad
from rngauto.games.bdsp.battle import choice_box_visible
from rngauto.games.bdsp.fast_advance import MacroStep
from rngauto.games.bdsp.screens import dialog_visible
from rngauto.games.bdsp.static_strategy import StaticEncounterRun
from rngauto.vision.frames import Frame

EXP = Path.home() / ".rngauto" / "exploracion" / "20261005-004112"
pytestmark = [pytest.mark.local, pytest.mark.skipif(not (EXP / "021_tras_losa_1s.png").exists(), reason="sin capturas reales")]

FPS = 52.0
LATENCY_S = 0.35  # de la pulsación a verse en el vídeo
QUEUE = 32  # frames que pueden quedar en la cola de la capturadora


def img(name):
    return cv2.imdecode(np.fromfile(EXP / f"{name}.png", np.uint8), cv2.IMREAD_COLOR)


class SlabConsole:
    """Pantallas en orden; cada A cambia de pantalla según la secuencia real (con su retraso)."""

    def __init__(self, box_latency_s: float = LATENCY_S):
        self.box_latency_s = box_latency_s  # la caja Sí/No puede tardar más en verse que un texto
        self.t = 0.0
        self.state = "quieto"
        self.changes = deque()  # (instante en que se ve, nuevo estado)
        self.queue = deque()
        self.presses = []
        self.images = {
            "quieto": img("014_girado"),
            "pedestal": img("015_losa_a1"), "pregunta": img("016_losa_a2"), "caja": img("017_losa_a3"),
            "procesando": img("018_losa_si_1s"), "colocada": img("020_losa_si_7s"), "brilla": img("021_tras_losa_1s"),
            "fogonazo": img("024_brillo_1s"),
        }
        self.after_si_until = None

    def _visible_state(self, t):
        while self.changes and self.changes[0][0] <= t:
            self.state = self.changes.popleft()[1]
        if self.state == "procesando" and self.after_si_until is not None and t >= self.after_si_until:
            self.state = "colocada"
        return self.state

    def press_a(self):
        self.presses.append(self.t)
        nxt = {"quieto": "pedestal", "pedestal": "pregunta", "pregunta": "caja", "caja": "procesando",
               "colocada": "brilla", "brilla": "fogonazo"}
        # el estado que verá la consola cuando le llegue la pulsación (ignora A mientras «procesa» el Sí)
        pending = self.changes[-1][1] if self.changes else self.state
        if pending in nxt:
            new = nxt[pending]
            self.changes.append((self.t + (self.box_latency_s if new == "caja" else LATENCY_S), new))
            if new == "procesando":
                self.after_si_until = self.t + LATENCY_S + 4.0

    def next_frame(self):
        self.t += 1 / FPS
        # la cola guarda frames ya capturados: lo que se lee puede ser de hace ~QUEUE/FPS s
        self.queue.append(Frame(self.images[self._visible_state(self.t)], self.t, len(self.presses)))
        while len(self.queue) > QUEUE:
            self.queue.popleft()
        return self.queue.popleft() if len(self.queue) > QUEUE // 2 else self.queue[0]


def make_run(console):
    run = object.__new__(StaticEncounterRun)
    run._next_frame = lambda ctx: console.next_frame()
    run._press = lambda ctx, button, label: console.press_a() if button is Button.A else None
    run.d = SimpleNamespace(controller=None)
    ctx = SimpleNamespace(now=lambda: console.t, log=lambda *a, **k: None, check=lambda: None)
    return run, ctx


def test_slab_sequence_stops_on_the_last_text():
    console = SlabConsole()
    run, ctx = make_run(console)
    steps = [MacroStep.from_dict(s) for s in (
        {"kind": "dialog_until_choice", "ms": 12000},
        {"kind": "press", "button": "A"},
        {"kind": "dialog_next", "ms": 12000},
        {"kind": "dialog_settle", "ms": 6000},
    )]
    assert run._dialog_step(ctx, steps[0], "losa")
    assert console._visible_state(console.t) == "caja"
    run._press(ctx, Button.A, "Sí")  # el paso «press» de la ruta
    for _ in range(int(0.4 * FPS)):
        console.next_frame()
    assert run._dialog_step(ctx, steps[2], "losa")
    assert run._dialog_step(ctx, steps[3], "losa")
    for _ in range(int(1.0 * FPS)):
        console.next_frame()
    # exactamente: hablar, pedestal, pregunta (abre la caja), Sí, «Has colocado» → queda «…a brillar!»
    assert len(console.presses) == 5
    assert console._visible_state(console.t) == "brilla"


SLAB_STEPS = (
    {"kind": "dialog_until_choice", "ms": 12000},
    {"kind": "press", "button": "A"},
    {"kind": "dialog_next", "ms": 12000},
    {"kind": "dialog_settle", "ms": 6000},
)


def test_slow_choice_box_is_never_answered_blindly():
    """La caja tarda 1,2 s en verse tras la A (antes, a los 0,85 s se volvía a pulsar: «Sí» a ciegas)."""
    console = SlabConsole(box_latency_s=1.2)
    run, ctx = make_run(console)
    assert run._dialog_step(ctx, MacroStep.from_dict(SLAB_STEPS[0]), "losa")
    assert len(console.presses) == 3  # hablar, pedestal, pregunta (abre la caja)
    assert console._visible_state(console.t) == "caja"


def test_choice_box_survives_small_shifts_and_compression():
    box, question = img("017_losa_a3"), img("016_losa_a2")
    assert choice_box_visible(box) and not choice_box_visible(question)
    h, w = box.shape[:2]
    for dx in (-4, -2, 0, 2, 4):
        for dy in (-4, -2, -1, 1, 2, 4):
            m = np.float32([[1, 0, dx], [0, 1, dy]])
            shifted = cv2.warpAffine(box, m, (w, h), borderMode=cv2.BORDER_REPLICATE)
            assert choice_box_visible(shifted), (dx, dy)
    for quality in (90, 70, 50):
        jpg = cv2.imdecode(cv2.imencode(".jpg", box, [cv2.IMWRITE_JPEG_QUALITY, quality])[1], cv2.IMREAD_COLOR)
        assert choice_box_visible(jpg), quality
    for size in ((1280, 720), (640, 360)):
        assert choice_box_visible(cv2.resize(box, size, interpolation=cv2.INTER_AREA)), size


TALK_SHOWS_S = 0.62  # de la A a verse el texto del legendario (estimado con el registro de Raikou, 2026-10-05)


class PureSpaceConsole:
    """Espacio Puro: el legendario está a ``need_ms`` de cruceta. Una A pegada a él abre su texto; otra A lo cierra
    y empieza el combate (lo que NO debe pasar antes de la A final). El mando ejecuta la cola en orden, como el
    ESP32: ``submit`` vuelve enseguida."""

    def __init__(self, need_ms: int):
        self.need_ms = need_ms
        self.t = 0.0
        self.free_at = 0.0  # cuándo termina lo que hay en la cola del mando
        self.walks: list[tuple[float, int]] = []
        self.a_times: list[float] = []
        self.overworld, self.dialog = img("030_andar_2s"), img("031_regice_a1_08s")
        self.index = 0

    def submit(self, commands):
        for cmd in commands:
            start = max(self.t, self.free_at)
            if cmd.state.dpad is not DPad.NONE:
                self.walks.append((start, cmd.duration_ms))
            if cmd.state.buttons & Button.A:
                self.a_times.append(start)
            self.free_at = start + cmd.duration_ms / 1000

    def _walked_ms(self, t: float) -> float:
        return sum(ms for start, ms in self.walks if start + ms / 1000 <= t)

    def talk_and_close(self) -> tuple[float | None, float | None]:
        talk = next((a for a in self.a_times if self._walked_ms(a) >= self.need_ms), None)
        close = next((a for a in self.a_times if talk is not None and a > talk + 0.05), None)
        return talk, close

    def next_frame(self):
        self.t += 1 / FPS
        self.index += 1
        talk, close = self.talk_and_close()
        shown = talk is not None and self.t >= talk + TALK_SHOWS_S and (close is None or self.t < close + LATENCY_S)
        return Frame(self.dialog if shown else self.overworld, self.t, self.index)


@pytest.mark.parametrize("need_ms", [900, 1500, 1800, 2600])
def test_walk_talk_never_sends_an_extra_a(need_ms):
    console = PureSpaceConsole(need_ms)
    assert dialog_visible(console.dialog) and not dialog_visible(console.overworld)
    run = object.__new__(StaticEncounterRun)
    run._next_frame = lambda ctx: console.next_frame()
    run.d = SimpleNamespace(controller=console)
    ctx = SimpleNamespace(now=lambda: console.t, log=lambda *a, **k: None, check=lambda: None)
    step = MacroStep.from_dict({"kind": "walk_talk", "button": "UP", "ms": 8000})
    assert run._walk_talk(ctx, step, "timeline")
    for _ in range(int(3.0 * FPS)):  # lo que queda hasta la A final: nada en cola debe cerrar su texto
        console.next_frame()
    talk, close = console.talk_and_close()
    assert talk is not None and close is None
    assert console.t - talk < 8.0


@pytest.mark.parametrize("pattern, expected", [
    ([("dialogo", 6.0)], None),                                       # su texto sigue hasta la A final
    ([("dialogo", 2.0), ("mapa", 0.2), ("dialogo", 4.0)], None),      # parpadeo breve (temblor): no cuenta
    ([("dialogo", 0.5), ("mapa", 1.2), ("dialogo", 4.0)], None),      # Regigigas despierta: hueco de ~1 s
    ([("dialogo", 2.0), ("mapa", 4.0)], 2.0),                         # se cerró: combate antes de tiempo
])
def test_legendary_text_closing_early_is_detected(pattern, expected):
    images = {"dialogo": img("031_regice_a1_08s"), "mapa": img("030_andar_2s")}
    frames, t = [], 0.0
    for name, seconds in pattern:
        for _ in range(int(seconds * FPS)):
            t += 1 / FPS
            frames.append(Frame(images[name], t, len(frames)))
    clock = SimpleNamespace(t=0.0)

    def next_frame(ctx):
        frame = frames.pop(0)
        clock.t = frame.t_s
        return frame

    run = object.__new__(StaticEncounterRun)
    run._next_frame = next_frame
    ctx = SimpleNamespace(now=lambda: clock.t, log=lambda *a, **k: None, check=lambda: None)
    gone = run._dialog_gone_before(ctx, until=t - 0.1)
    if expected is None:
        assert gone is None
    else:
        assert gone == pytest.approx(expected, abs=0.05)
