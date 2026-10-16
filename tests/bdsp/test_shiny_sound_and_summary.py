"""Sonido shiny (plantilla aprendida) y lectura de la naturaleza por colores, con datos sintéticos."""
import numpy as np
import pytest

from rngauto.games.bdsp.shiny_sound import SoundTemplate, detect, learn_template, spike_kernel
from rngauto.games.bdsp.summary import NEUTRAL_NATURES, STAT_LABEL_BOXES, _parse, nature_from_colors

RATE = 48_000


def chime(duration=0.6, start_hz=7000.0):
    """Tintineo agudo sintético: tres notas cortas por encima de 5 kHz con caída exponencial."""
    t = np.arange(int(duration * RATE)) / RATE
    out = np.zeros_like(t)
    for k, f in enumerate((start_hz, start_hz * 1.25, start_hz * 1.5)):
        t0 = k * 0.15
        m = t >= t0
        out[m] += np.sin(2 * np.pi * f * (t[m] - t0)) * np.exp(-(t[m] - t0) * 12)
    return 0.3 * out


def battle_noise(seconds, seed):
    """«Música» de combate: graves y medios con algo de ruido, sin el tintineo."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    music = sum(0.1 * np.sin(2 * np.pi * f * t) for f in (220, 330, 440, 660, 1200))
    return music + 0.01 * rng.standard_normal(t.size)


def stereo(x):
    return np.stack([x, x], axis=1).astype(np.float32)


def test_spike_kernel_matches_pa():
    k = spike_kernel(1024, 24_000)
    assert k.tolist() == [-4, -2, 0, 2, 4, 4, 2, 0, -2, -4]


def test_learned_sound_is_found_and_normal_battles_are_not():
    shiny = battle_noise(8, 1)
    at = int(3.0 * RATE)
    c = chime()
    shiny[at:at + c.size] += c
    normals = [stereo(battle_noise(8, s)) for s in (2, 3, 4)]
    tpl = learn_template(stereo(shiny), RATE, around_s=3.2, baseline=normals)
    assert 2.6 < float(tpl.note.split("aprendida en ")[1].split(" s")[0]) < 3.4
    # otra vez el mismo sonido, en otro momento y con otra música: se detecta
    again = battle_noise(8, 9)
    at2 = int(5.1 * RATE)
    again[at2:at2 + c.size] += 0.7 * c
    found = detect(stereo(again), RATE, tpl)
    assert found.detected and abs(found.at_s - 5.1) < 0.3
    for n in normals + [stereo(battle_noise(8, 7))]:
        assert not detect(n, RATE, tpl).detected


def test_template_roundtrip(tmp_path):
    tpl = SoundTemplate(np.ones((5, 7), np.float32), RATE, 0.4, "x")
    tpl.save(tmp_path / "s.npz")
    back = SoundTemplate.load(tmp_path / "s.npz")
    assert back.rate == RATE and back.threshold == pytest.approx(0.4) and back.spectra.shape == (5, 7)
    assert SoundTemplate.load(tmp_path / "nope.npz") is None


def _page(up=None, down=None):
    img = np.full((1080, 1920, 3), 239, np.uint8)
    for idx, box in STAT_LABEL_BOXES.items():
        x0, y0, w, h = box.to_pixels(1920, 1080)
        color = (60, 60, 60)
        if idx == up:
            color = (60, 60, 230)  # BGR: rojo
        elif idx == down:
            color = (230, 130, 40)  # BGR: azul
        img[y0 + h // 3:y0 + 2 * h // 3, x0 + w // 4:x0 + 3 * w // 4] = color
    return img


@pytest.mark.parametrize("up,down,nature", [(5, 3, 13), (1, 4, 4), (4, 5, 22), (2, 1, 5)])
def test_nature_from_label_colors(up, down, nature):
    assert nature_from_colors(_page(up, down)) == {nature}


def test_neutral_nature_has_no_colors():
    assert nature_from_colors(_page()) == NEUTRAL_NATURES


def test_hp_parsing():
    assert _parse("213/213", hp=True) == 213
    assert _parse("213213", hp=True) == 213
    assert _parse("2131213", hp=True) == 213
    assert _parse("57", hp=False) == 57
