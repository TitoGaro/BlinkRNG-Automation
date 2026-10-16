"""Tipos y ajustes de la ejecución de un encuentro estático: modos, estado que ve la interfaz
(``RunStatus``), configuración (``StaticRunConfig``), dependencias inyectadas (``StaticDeps``) y constantes."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from rngauto.control.controller import Controller
from rngauto.core.calibration import AttemptRecord, CalibrationHistory
from rngauto.core.clock import Clock
from rngauto.core.encounter import EncounterConfiguration
from rngauto.core.pokemon import TargetCriteria
from rngauto.core.timeline import Timeline
from rngauto.core.tracker import AdvanceClock
from rngauto.i18n import is_spanish
from rngauto.vision.blink_signal import EyeMatcher
from rngauto.vision.frames import Frame
from rngauto.vision.screen import ScreenSignatures

from ..encounters import StaticTargetFinder
from ..fast_advance import AdvanceMethod, AdvanceStats
from ..identify import Observation
from ..shiny_sound import SoundTemplate


SCREEN_NAMES_EN = {"pokedex": "Pokédex", "menu": "menu", "dialog": "dialog box"}
SCREEN_NAMES_ES = {"pokedex": "Pokédex", "menu": "menú", "dialog": "cuadro de diálogo"}
FAST_FAIL_MAX = 3  # fallos de una acción de Pokédex (en intentos distintos) antes de dejar de usarla
POKEMON_MEAN_INTERVAL_S = 7.785  # media de rand_float(3, 12) + 0,285: un parpadeo del legendario cada ~7,8 s


def fmt_duration(seconds: float) -> str:
    """Duración legible: «45 s», «12 min 05 s», «1 h 03 min»."""
    s = max(0, int(round(seconds)))
    if s < 90:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


class Mode(Enum):
    """Grado de control que tiene el programa sobre el juego."""

    AUTOMATIC = "automatic"
    ASSISTED = "assisted"  # el programa calcula; el usuario aprueba el plan y verifica el resultado
    DRY_RUN = "dry_run"  # no se envía nada al juego: se calcula y se muestra el plan


class Operator(Protocol):
    """Interlocutor humano de la estrategia (la interfaz): preguntas, avisos y observaciones del resultado."""

    def confirm(self, message: str, timeout_s: float | None = None) -> bool | None:
        """Sí/No; ``None`` si no hay respuesta en ``timeout_s`` (o se paró)."""
        ...
    def notify(self, message: str) -> None: ...
    def ask_observation(self, fields: list[str]) -> Observation | None: ...
    def wait_ready(self, message: str) -> bool: ...  # True solo si el usuario confirmó «Listo»


class AutoOperator:
    """Operador para modo automático/simulado: confirma todo y no puede observar."""

    def confirm(self, message: str, timeout_s: float | None = None) -> bool | None:
        return True

    def notify(self, message: str) -> None:
        pass

    def ask_observation(self, fields: list[str]) -> Observation | None:
        return None

    def wait_ready(self, message: str) -> bool:
        return True


class FrameReader(Protocol):
    def read(self) -> Frame | None: ...


def screen_name(name: str) -> str:
    """Nombre legible de una pantalla conocida en el idioma actual (el propio ``name`` si no se conoce)."""
    return (SCREEN_NAMES_ES if is_spanish() else SCREEN_NAMES_EN).get(name, name)


@dataclass(frozen=True)
class RunStatus:
    """Instantánea legible por la interfaz (se emite en cada cambio relevante)."""

    phase: str = "IDLE"
    message: str = ""
    attempt: int = 1
    blinks: int = 0
    blinks_needed: int = 0
    provisional: bool = False
    state_hex: str = ""
    clock: AdvanceClock | None = None  # para calcular el advance actual en la UI
    target_advance: int | None = None
    target_desc: str = ""
    next_action: str = ""
    next_action_t: float | None = None  # mismo reloj que la estrategia
    delay_desc: str = ""
    confidence: float | None = None
    last_score: float | None = None
    threshold: float | None = None
    advance_offset: int = 0  # avances acumulados por avances rápidos (los advances se cuentan desde la base)
    calibrating: bool = False  # COLLECT: el detector aún ajusta su umbral (la cuenta de parpadeos es provisional)
    seed_desc: str = ""  # «Confirmada ✓ (35 parpadeos + 2)», «Comprobando…»: para la interfaz
    phase_elapsed_s: float = 0.0  # COLLECT: tiempo observando
    calibration_window_s: float = 0.0  # COLLECT: datos que necesita el umbral definitivo


@dataclass
class StaticRunConfig:
    """Parámetros de una ejecución: qué se busca, modo, márgenes de tiempo y límites de cada fase.

    Los tiempos van en segundos de reloj de la estrategia; los valores por defecto sirven para la consola real.
    """

    criteria: TargetCriteria
    mode: Mode = Mode.AUTOMATIC
    lead_s: float = 0.10  # adelanto de envío (latencia vídeo + mando); se mide/calibra
    prep_s: float = 5.0
    approval_s: float = 45.0  # margen extra en modo asistido para leer y aprobar el plan
    max_search_advances: int = 200_000
    max_plan_tries: int = 60
    max_attempts: int = 10
    observe_fields: tuple[str, ...] = ("is_shiny", "nature", "ivs")
    identify_radius: int = 40
    blink_threshold_window_s: float = 180.0
    min_plateau_blinks: int = 20
    collect_timeout_s: float = 1800.0
    battle_timeout_s: float = 90.0
    keep_awake_s: float = 180.0
    keep_awake_guard_s: float = 15.0  # no se toca el mando tan cerca de una pulsación planificada
    desync_window: int = 6  # se declara desincronización con 3 incoherentes entre los últimos 6 parpadeos
    scene_min_score: float = 0.5  # durante la espera, el ojo se sigue viendo con al menos esta correlación
    # Para DAR POR ENCONTRADA la escena (al empezar, tras reiniciar el juego o al volver de un menú) hace falta
    # más: la pantalla de título («Pulsa el botón A») llega a 0.5-0.65 con la plantilla del ojo y la escena real
    # da ~0.95 (medido en grabaciones de consola). Con 0.5 un reinicio se daría por terminado en el título.
    scene_found_score: float = 0.75
    scene_timeout_s: float = 120.0
    scene_lost_s: float = 10.0  # sin ver el ojo durante la espera ⇒ volver a observar
    max_blink_silence_s: float = 90.0  # sin parpadeos confirmados (≈ imposible si todo va bien)
    frame_gap_timeout_s: float = 5.0
    # Avance rápido (Pokédex) + reidentificación
    fast_advance: bool = True
    fast_advance_min_gain_s: float = 60.0  # solo si ahorra al menos esto frente a esperar sin hacer nada
    fast_advance_k_sigma: float = 2.0
    max_fast_advances: int = 150
    reidentify_blinks: int = 7
    reidentify_confirm_blinks: int = 2
    reidentify_timeout_s: float = 360.0
    # Objeto registrado que muestra el mensaje de la escena (la caña): «+» y, si hay varios registrados,
    # su dirección en la rueda (NONE/UP/DOWN/LEFT/RIGHT).
    registered_item_direction: str = "NONE"
    # Reinicio sin ayuda: HOME → X → «Cerrar» → A repetida (salta logos, selector de usuario y título) hasta
    # volver a ver el ojo. Requiere que el personaje esté guardado mirando a un sitio donde A no hace nada.
    reset_from_home: bool = False
    reset_timeout_s: float = 75.0  # por intento de reinicio (se hacen dos)
    max_auto_restarts: int = 3  # reinicios seguidos por imprevistos sin completar un intento antes de parar
    # Desplazar el objetivo ±1-2 para desempatar intentos dudosos (ver ``target_bump``). Apagado:
    # apuntaría a un advance vecino del shiny; los dudosos ya cuentan en la estimación del delay.
    calibration_bump: bool = False
    # Combate automático (consola real): detectar shiny (PARAR) o capturar con la Master Ball y leer la ficha.
    auto_battle: bool = False
    party_slot: int = 6  # puesto en el que queda el capturado (equipo de 5 antes de capturar)
    battle_start_timeout_s: float = 30.0  # tras la A final, el combate debe empezar en este tiempo
    clips_keep: int = 30  # combates normales que se guardan (sonido) para ajustar el detector de sonido


@dataclass
class StaticDeps:
    """Todo lo que la estrategia necesita del exterior (reloj, vídeo, mando, detectores, persistencia).

    Inyectarlo permite ejecutar la misma estrategia contra la Switch simulada y contra la real.
    """

    clock: Clock
    frames: FrameReader
    controller: Controller
    finder: StaticTargetFinder
    encounter: EncounterConfiguration
    matcher: EyeMatcher
    calibration: CalibrationHistory
    in_battle: Callable[[Frame], bool] | None  # None: lo confirma el operador
    observe: Callable[[list[str]], Observation | None]
    reset_game: Callable[[], None] | None  # None: lo hace el usuario (modo asistido)
    operator: Operator = field(default_factory=AutoOperator)
    timeline: Timeline = field(default_factory=Timeline)
    on_attempt: Callable[[AttemptRecord], None] | None = None
    on_status: Callable[[RunStatus], None] | None = None
    fast_methods: Sequence[AdvanceMethod] = ()
    fast_stats: AdvanceStats = field(default_factory=AdvanceStats)
    screens: ScreenSignatures = field(default_factory=lambda: ScreenSignatures(None))
    audio: Any = None  # AudioRing con el sonido reciente de la capturadora (opcional)
    shiny_sound: SoundTemplate | None = None  # plantilla del sonido shiny (se aprende con el primer shiny)
    shiny_sound_path: Path | None = None  # dónde guardar la plantilla al aprenderla
    clips_dir: Path | None = None  # grabaciones de cada entrada en combate
    # Ojo de otra preparación (p. ej. delante de Regigigas, para reidentificar allí): id → matcher o None.
    eye_matcher_for: Callable[[str], EyeMatcher | None] | None = None
