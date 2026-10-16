# User guide

[Español](03-guia-puesta-en-marcha.md)

Where to save for each legendary, with screenshots: [encounter guide](encounter-guide.md).

## Opening the app

Double-click **`RNG Automation.bat`** (in the `rng-automation` folder). A window opens with six
steps on the left. You can only move on when the current step is ready; if something is missing, the bottom
right corner tells you what.

The language (Español or English) is chosen at the bottom of the left sidebar. The first time, it follows
your Windows language. It cannot be changed while a run is in progress.

> **All in one:** you do not need any other program. The game's picture and sound are shown and
> played in the app (the "Game view" button at the top opens it in a separate window, full screen on
> double-click; the speaker button next to it turns the sound on or off). If a recording or streaming program is open with the capture
> card, close it: Windows does not let two programs use it at the same time.

## Before you start (once)

- **Controller (ESP32):** flash the PABotBase2 firmware and pair it with the Switch by following
  [Pokémon Automation's guides](https://pokemonautomation.github.io/ControllerList.html) ("ESP32" option).
- **Switch:** turn off auto-sleep (System Settings → Sleep Mode). Waiting for a shiny can take hours. The
  program nudges the right stick every 3 minutes so the screen does not dim, but the console must not go
  to sleep.
- **User profile:** on the console, your profile must be the only one or the first on the left. When it
  restarts the game, the program selects it by pressing A.
- **PC:** while the app is working, Windows does not go to sleep or turn off the screen (the program takes
  care of this). Keep the laptop plugged in.
- **Physical controller:** do not use it while the program is working. The Switch would respond to both
  controllers.
- **Save file (so it can catch and read the Summary by itself):**
  - a party of **5** Pokémon (the caught one goes into the 6th slot), with a **non-shiny first one**;
  - at least **one Master Ball** in the Bag (it finds it by its icon, wherever it is in the pocket);
  - in the game options, **Give Nicknames turned off** and **text speed set to Fast**;
  - ideally, no party member should learn a move or evolve when you catch it. If it happens, the program
    chooses to keep the current moves, but it loses time;
  - for encounters observed with Professor Rowan's message (all except Dialga and Palkia), the **fishing
    rod registered** in the "+" wheel. In the Scene step you tell the app which direction of the wheel it is
    on;
  - the game saved at the starting point, facing the camera. The program never saves: restarting always
    brings you back to that point.

## The six steps

| Step | What you do | What the program does |
|---|---|---|
| **1. Target** | Choose the Pokémon, whether you want it shiny, the nature, the IVs and the mode. "Fast advance with the Pokédex (recommended)" is on by default | Tells you how rare that target is and how long it will take to reach it |
| **2. Connection** | Plug in the ESP32 and the capture card; if needed, pair the controller in "Change Grip/Order" | Finds and connects them by itself, shows the picture, turns on the sound and tells you what is missing |
| **3. Scene** | Stand where it tells you, click "Freeze frame to mark the eye", draw a box around the eye with the mouse and click "Save this eye". If the encounter uses the rod, choose its direction under "Where is the rod in the "+" wheel?". For **Regigigas, Cresselia, Darkrai and Heatran** there are **two eyes**: the one at the starting point and another one in front of the legendary (without saving: go up to it, mark the eye and reload) | Saves the eye template (only needed once per encounter). In front of the legendary, the program re-syncs taking its blinks into account too (they consume RNG) |
| **4. Detection** | Stand still for about 1–2 minutes | Shows a live magnifier of the eye (red frame when it blinks), the graph and the blinks it detects. You can continue once 3 blinks are detected (or by ticking "I have checked it and want to continue anyway") |
| **5. Run** | Click **Start** and leave the controller alone | Reads the RNG, finds the target, presses at the exact moment, checks for shiny, catches it, reads the Summary, calibrates and restarts, over and over |
| **6. History** | — | Attempts and learned calibration |

### During a run

- **Blinks (progress bar):** most encounters need 33 blinks (up to 40 if 33 are not enough) plus 2 more to
  confirm; Arceus, Dialga, Palkia and Uxie need 40 plus 6. That takes about 4–6 minutes. The count starts
  with the first blink; for the first ~3 minutes it shows "≈" because the detector is still tuning its
  sensitivity to your eye (when it finishes, the count may change by one or two).
- **Plan:** in **Assisted** mode a window shows the target and the waiting time; accept it. From then on the
  program presses **by itself**, at the exact moment. If you do not answer in time, the window closes by
  itself and the plan is recalculated. In this mode it also asks you before the first fast advance.
- **Fast advance:** if the target is far away, the program opens the Pokédex, scrolls through it for as long
  as needed and closes it. It then **re-identifies** the state with about 7 blinks and confirms it with 2
  more. It repeats this with shorter and shorter scrolls (at the end it just opens and closes the Pokédex)
  until the wait is short. If it overshoots the target, it goes for the next one that meets your criteria.
  - The first time, it asks whether it sees the Pokédex correctly in the picture: answer "Yes" and from
    then on it will recognise it by itself.
  - It reaches the Pokédex by itself, wherever the menu cursor is: X, "+" (map), B (the cursor ends up on
    the Map, right below the Pokédex), UP and A.
  - It checks every step against the picture: that the menu is open before using the D-pad (otherwise UP
    would move your character) and that the Pokédex is showing before scrolling. If something does not
    match, it **stops pressing**: it closes whatever is open and keeps waiting normally.
  - It learns how many advances each action gives in your game and saves that for next time.
- **Countdown:** shows the time until the next press (and the approximate clock time). You can leave it
  working.
- **Battle:** it detects the battle by itself and checks whether the Pokémon is shiny: the sparkles in the
  picture, how long the intro animation lasts and, once it has learned it, the sound of the stars.
  - **If it is shiny, it stops** with the battle menu on screen, lets you know and does not touch the
    controller again: you catch it yourself. It saves the recording of that battle and learns the sound for
    next time.
  - **If it is not**, it catches it quickly: Bag, Poké Balls pocket, Master Ball (by its icon), Use.
- **Summary:** it goes through the text boxes (if a party member wants to learn a move, it keeps the
  current moves), opens the Summary of the caught Pokémon and reads its nature and stats, regardless of the
  game language. It reads the numbers with Windows OCR. It gets the nature from the colours: red for the stat
  that goes up and blue for the one that goes down; if there are no colours, the nature is neutral and it
  reads which one on the Trainer Memo page (in any game language; if it cannot tell, it carries on with "one of
  the 5 neutral natures"). From that it works out the IVs and which advance you got.
- **Calibration and restart:** it adjusts the *delay* with the result, closes the game from HOME (without
  saving), opens it again and repeats. If it cannot read the Summary, it asks you for the data in a window.
- **Stop** (red button at the top, or the **F12** key): immediately stops whatever is running, closes any
  pending windows and leaves the controller in neutral. The program's windows never block the stop.

### Latency

You do not need to measure it. The press lands in the middle of a ±0.5 s window, and any remaining offset
is absorbed by the *delay* calibration after each attempt.

### Where things are saved

In the `%USERPROFILE%\.rngauto\` folder ("Open data folder" button in History):

- `setups\`: the eyes you have marked.
- `calibration\`: what it has learned for each encounter.
- `fast_advance\` and `screens\`: what it has learned for fast advance (advances per action and the Pokédex
  screen). "Advanced options → Relearn the Pokédex screen" forgets the screen.
- `sessions\`: the detailed log of each session, including short recordings of the eye (`ojo_XX.avi`) while
  blinks are being counted. If detection fails, they can be used to analyse the problem without repeating
  the test.
- `logs\errores.log`: if something fails unexpectedly.

## Troubleshooting

| Symptom | Solution |
|---|---|
| Controller "not found" | Plug in the ESP32 with a USB data cable; the page detects it by itself within a few seconds |
| "The ESP32 is not responding" when connecting the controller | The ESP32 needs the Pokémon Automation firmware (`PABotBase2-ESP32-2026092800.bin`). How to flash and pair it: [Pokémon Automation's guides](https://pokemonautomation.github.io/ControllerList.html) ("ESP32" option) |
| "Not paired" | On the Switch: Controllers → Change Grip/Order. Wait a few seconds: it turns green by itself. If the board loses power, you need to pair it again |
| "Is in use by another program" | Close any other program or window of this app that uses the port and click "Reconnect" |
| The capture card shows no picture | Close any other program using it and click "Choose another capture card" |
| Blinks are not detected | Draw a tighter box around the eye; if it is very small, turn on the Switch zoom and mark it again |
| "I need your help" | The program has stopped because something does not add up (it does not act blindly). Read the message, fix it and click Start |
| "Fast advance cancelled" | The game did not show the menu or the Pokédex in time (the technical details say which). If you changed capture card, use "Relearn the Pokédex screen" |
| Something is slow and not responding (controller or capture card) | No operation waits forever: after a few seconds a warning appears and you can try again. If it keeps happening, unplug the device and plug it back in |

**If it does not go away, report it:** click "Report the problem" in the error message, or the info button
(bottom left) → "Report a problem". The app prepares a report with the version, your setup and the latest
events (without your user name or your folders), lets you review it and opens the GitHub form already filled
in. For detection problems, also attach the eye video of the session ("Open logs folder" → `sessions`). The
app keeps the video of the last 5 runs and all the text logs.

## Encounter status

In the Target step, the list separates the encounters verified on a real console ("Verified on a console")
from those that use the same mechanism but have not been tested yet ("Same mechanism, not tested yet"). The
current list is also in the [README](../README.en.md#supported-encounters).

With an untested encounter, do a session in **Dry run** mode first: it checks that detection and planning
work with your capture card before anything is pressed.

## Advanced use (command line)

The same processes are available as commands (`rngauto --help`): `encounters`, `targets`, `simulate`,
`recover`, `diagnose`, `run` and `record`. You do not need them for normal use.

## Cresselia (and others that do not start a battle when generated)

Cresselia is generated **when you talk to it** and then flees (it becomes a roamer), so there is no battle to
watch. The flow is:

1. The program finds the seed, travels to Cresselia, re-identifies in front of it (its blinks consume RNG)
   and talks to it at the exact moment.
2. The **"Your turn"** message appears: go and catch it yourself, **without saving**. With a party of 5, it
   goes into the 6th slot.
3. Let go of the controller and click **"Ready"**, wherever you are (map, party, Bag…). Before touching
   anything, the program asks whether it is shiny. If you answer "No", it backs out of whatever is open with
   B, opens the menu, goes to its Summary, works out which advance it landed on, adjusts the delay and
   restarts the game for the next attempt.
4. If you answer "Yes", or if the advance it got may be the shiny one, it **stops** and asks you to save the
   game. It never restarts with the shiny in your hands. "Stop" in the message ends the run at any time.
