# Third-party notices

RNG Automation (`rngauto`) is licensed under **GPL-3.0-or-later** (see [`LICENSE`](LICENSE)). This file lists
the third-party work it is derived from or depends on. All of it is under licenses compatible with the GPL-3.0.

## Code derived from or based on other projects

| Project | License | What rngauto takes from it |
|---|---|---|
| [Pokémon Automation](https://github.com/PokemonAutomation/Arduino-Source) | MIT © 2021 Alexander J. Yee | PABotBase2 protocol (`control/pabotbase2.py`, `control/esp32.py`), structural screen detectors (`games/bdsp/screens.py`), shiny sparkle and sound detection (`games/bdsp/shiny.py`, `games/bdsp/shiny_sound.py`), plateau threshold (`vision/blink_signal.py`), delay calibration (`core/calibration.py`) |
| [Project_Xs](https://github.com/niart120/Project_Xs) (and the [Lincoln-LM fork](https://github.com/Lincoln-LM/Project_Xs)) | MIT © 2021 niart120 | Xorshift128 and state recovery from blinks (`games/bdsp/xorshift.py`, `core/gf2.py`, `games/bdsp/recovery.py`, `games/bdsp/reidentify_pokemon.py`) |
| [PokeFinder](https://github.com/Admiral-Fish/PokeFinder) | GPL-3.0 © Admiral-Fish and contributors | Reference for BDSP static encounter generation (`games/bdsp/static_gen.py`), static encounter table checked against it (`games/bdsp/data/static_encounters.json`). Its test vectors are only read locally and are not included |

The **PABotBase2 firmware** flashed onto the ESP32 is **not distributed** with rngauto. It is downloaded from
Pokémon Automation, and its binaries are for **non-commercial use only**.

## Runtime dependencies

They are installed by `pip` from PyPI; rngauto does not redistribute them.

| Package | License |
|---|---|
| [NumPy](https://numpy.org) | BSD-3-Clause (bundled components: 0BSD, MIT, Zlib, CC0-1.0) |
| [opencv-python](https://github.com/opencv/opencv-python) | Apache-2.0 (its Windows wheels bundle FFmpeg, LGPL-2.1) |
| [pyserial](https://github.com/pyserial/pyserial) | BSD-3-Clause |
| [PySide6 / Shiboken6](https://doc.qt.io/qtforpython-6/) (Qt for Python) | LGPL-3.0-only (also GPL-2.0/3.0) |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | MIT (uses PortAudio, MIT-style) |
| [cv2_enumerate_cameras](https://github.com/lukehugh/cv2_enumerate_cameras) | MIT |
| [cffi](https://github.com/python-cffi/cffi) (dependency of sounddevice) | MIT-0 |

Development only: [pytest](https://pytest.org) (MIT).

## Game image

`src/rngauto/games/bdsp/data/plantillas/master_ball.png` is a 45×45 px crop of the in-game Bag screen of
*Pokémon Brilliant Diamond / Shining Pearl*. It is used only as a matching template, so the program can check
that the cursor is on the Master Ball before throwing it. The image is © Nintendo, Creatures Inc. and
GAME FREAK inc. It is included for interoperability in a non-commercial project and will be removed at the
rights holder's request.

## Trademarks

Pokémon, Pokémon Brilliant Diamond and Pokémon Shining Pearl are trademarks of Nintendo, Creatures Inc. and
GAME FREAK inc. Nintendo Switch is a trademark of Nintendo. This project is not affiliated with, endorsed by or
sponsored by any of them. The names are used only to say which game the program works with.

---

## Pokémon Automation — MIT License

```text
MIT License

Copyright (c) 2021 Alexander J. Yee

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Project_Xs — MIT License

```text
MIT License

Copyright (c) 2021 niart120

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## PokeFinder — GNU General Public License v3.0

The full text is the same as [`LICENSE`](LICENSE).
