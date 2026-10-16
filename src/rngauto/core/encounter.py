"""``EncounterConfiguration``: todo lo específico de un encuentro, como datos (TOML).

Añadir un Pokémon = añadir su fichero ``encounters/<juego>/<id>.toml``. La estrategia (estático,
roamer...) es código genérico que interpreta esta configuración; no hay ``if pokemon == ...``.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rngauto.i18n import pick, t

SCHEMA_VERSION = 1


class ConfigError(ValueError):
    """Configuración de encuentro inválida o incompleta (el mensaje indica fichero y clave)."""

    pass


@dataclass(frozen=True, slots=True)
class SceneSpec:
    """Quién consume RNG en una escena (ver ``games/bdsp/scene.py``)."""

    npc: int = 0
    pokemon_npc: int = 0
    player_rolls: bool = True
    player_phase: int = 0


@dataclass(frozen=True, slots=True)
class VisualCheck:
    """Comprobación visual: plantilla buscada en una caja normalizada (x, y, w, h en 0..1)."""

    template: str
    box: tuple[float, float, float, float]
    min_score: float = 0.6


@dataclass(frozen=True, slots=True)
class StartPoint:
    """Dónde debe estar el jugador antes de empezar y cómo comprobarlo en pantalla.

    Textos en español y en inglés; ``name``, ``description`` e ``instructions`` dan el del idioma actual."""

    name_es: str
    description_es: str = ""
    check: VisualCheck | None = None
    instructions_es: str = ""  # qué decir al usuario si no está ahí
    name_en: str = ""
    description_en: str = ""
    instructions_en: str = ""

    @property
    def name(self) -> str:
        return _local(self.name_es, self.name_en)

    @property
    def description(self) -> str:
        return _local(self.description_es, self.description_en)

    @property
    def instructions(self) -> str:
        return _local(self.instructions_es, self.instructions_en)


@dataclass(frozen=True, slots=True)
class RouteStep:
    """Paso de preparación/ruta: acción → observación esperada, con plazo y reintentos."""

    action: str  # p. ej. "press A", "move up 600ms"
    expect: VisualCheck | None = None
    timeout_s: float = 10.0
    retries: int = 0


@dataclass(frozen=True, slots=True)
class BlinkSceneSpec:
    """Escena en la que se observan los parpadeos del jugador para obtener la semilla."""

    eye_template: str
    box: tuple[float, float, float, float]
    scene: SceneSpec = SceneSpec()
    zoom: bool = False  # activar zoom de consola antes de observar
    reference_height: int = 1080  # altura del frame en el que se recortó la plantilla
    min_blinks: int = 40
    confirmation_blinks: int = 6
    # La escena se observa con un mensaje en pantalla (p. ej. el del profesor Serbal al usar la caña: así el
    # personaje no gira la cabeza). Se quita con A antes de actuar y se recupera usando el objeto registrado.
    message: bool = False


@dataclass(frozen=True, slots=True)
class TimelinePhase:
    """Fase del timeline. Se dispara con ``trigger`` y produce, en este orden: ``blank_reads``
    llamadas inmediatas, una pausa sin eventos de ``pause_s`` segundos, ``fixed_delay`` llamadas
    fijas y, a continuación, la escena ``scene`` (eventos temporizados)."""

    name: str
    trigger: str  # botón o acción ("A", "walk_up"...)
    blank_reads: int = 0
    pause_s: float = 0.0
    fixed_delay: int = 0
    scene: SceneSpec = SceneSpec()
    # Pasos tras la pulsación de inicio y antes de la final (p. ej. caminar hasta el legendario y hablarle).
    # No cambian el RNG: el modelo de la fase ya cuenta los parpadeos durante ese tiempo.
    steps: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class FinalAction:
    """Pulsación final que genera el Pokémon y restricciones de cuándo hacerla."""

    trigger: str = "A"
    final_delay: int = 0  # prior; la calibración lo refina
    min_timeline_s: float = 20.0  # duración mínima entre el inicio del timeline y la pulsación final
    min_gap_margin_s: float = 0.12  # margen mínimo a cada lado de la pulsación final


@dataclass(frozen=True, slots=True)
class EncounterConfiguration:
    """Configuración completa de un encuentro, tal como se carga de su TOML (inmutable)."""

    id: str
    version: str
    game: str  # "bdsp"
    strategy: str  # "static", "roamer"...
    template: str  # clave de la plantilla de generación (p. ej. "dialga")
    games: tuple[str, ...]  # ("BD",), ("SP",), ("BD", "SP")
    display_name_es: str  # ``display_name``: el del idioma actual
    start: StartPoint
    blink_scene: BlinkSceneSpec
    timeline: tuple[TimelinePhase, ...]
    final: FinalAction
    preparation: tuple[RouteStep, ...] = ()
    verification: tuple[str, ...] = ("shiny",)
    # «user»: el Pokémon no entra en combate al generarse (Cresselia huye y queda errante): lo captura el usuario
    # y avisa; después el programa lee su ficha, calibra y reinicia. «auto» (por defecto): combate automático.
    capture: str = "auto"
    # Especies que pueden salir (p. ej. los tres Regis de la misma losa): misma generación, distintas
    # estadísticas base. Al leer la ficha se usa la que cuadre con las estadísticas. Vacío = la plantilla.
    species: tuple[str, ...] = ()
    recovery: str = "restart_game"
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    # Acciones de avance rápido permitidas en la escena de espera (ids de ``data/fast_advance.toml``).
    # Vacío si en esa escena no se puede abrir el menú.
    fast_advance: tuple[str, ...] = ("pokedex_peek", "pokedex_scroll")
    # Aproximación al legendario: pasos (mismo formato que las acciones de avance rápido) que empiezan
    # ``approach_lead_s`` segundos antes de la pulsación que inicia el timeline (hablar con él).
    approach: tuple[dict[str, Any], ...] = ()
    approach_lead_s: float = 10.0
    # Con un paso «reidentify_pokemon»: segundos antes del inicio del timeline en que empiezan los pasos de
    # detrás.
    approach_after_lead_s: float = 20.0
    # Con un paso «reidentify_pokemon»: cuánto adelanta el contador el legendario durante la ruta (se aprende en
    # cada intento; 0 = estimarlo por su ritmo medio de parpadeo).
    approach_extra_advances: int = 0
    # Con un paso «reidentify_pokemon»: ventana de búsqueda (avances respecto a lo previsto por el reloj de la
    # semilla). Por defecto, la de Regigigas (ruta corta a pie); con un viaje en medio (Cresselia: barco) hay que
    # ampliarla.
    approach_reidentify_window: tuple[int, int] = (-40, 200)
    # Con un paso «reidentify_pokemon»: si el objetivo aún queda lejos al reidentificar delante del legendario,
    # acercarse con la Pokédex allí mismo y volver a reidentificar (Cresselia: el barco desvía el contador ±2000,
    # así que el plan de la casa apunta por debajo y el resto se ajusta delante de ella).
    approach_front_fast_advance: bool = False
    # Dónde ponerse para marcar el ojo de la reidentificación delante del legendario (paso «Escena»); en español
    # y en inglés (``approach_eye_instructions``: el del idioma actual).
    approach_eye_instructions_es: str = ""
    status: str = "sin_probar"  # «comprobado» (en consola real), «sin_probar» o «borrador»
    display_name_en: str = ""
    approach_eye_instructions_en: str = ""

    @property
    def display_name(self) -> str:
        return _local(self.display_name_es, self.display_name_en) or self.id

    @property
    def approach_eye_instructions(self) -> str:
        return _local(self.approach_eye_instructions_es, self.approach_eye_instructions_en)

    @property
    def label(self) -> str:
        """Identificador con versión que se guarda en cada intento (``<id>-v<versión>``)."""
        return f"{self.id}-v{self.version}"

    @property
    def extra_eyes(self) -> tuple[str, ...]:
        """Ojos que hay que marcar además del de inicio: los de las reidentificaciones delante del legendario
        (pasos ``reidentify_pokemon`` de la ruta, p. ej. Regigigas)."""
        return tuple(dict.fromkeys(str(s["eye"]) for s in self.approach
                                   if s.get("kind") == "reidentify_pokemon" and s.get("eye")))


def _box(value: Any, where: str) -> tuple[float, float, float, float]:
    """Valida una caja ``[x, y, w, h]`` normalizada que cabe en el frame (con tolerancia de redondeo)."""
    if not (isinstance(value, list) and len(value) == 4 and all(isinstance(v, (int, float)) for v in value)):
        raise ConfigError(t("{where}: se esperaba [x, y, w, h] normalizado",
                            "{where}: expected a normalised [x, y, w, h]", where=where))
    x, y, w, h = (float(v) for v in value)
    if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.0001 and y + h <= 1.0001):
        raise ConfigError(t("{where}: caja fuera de [0, 1]: {value}", "{where}: box outside [0, 1]: {value}",
                             where=where, value=value))
    return (x, y, w, h)


def _window(value: Any) -> tuple[int, int]:
    """``[desde, hasta]`` en avances, con ``desde < hasta``."""
    try:
        lo, hi = (int(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(t("approach.reidentify_window: se esperaba [desde, hasta] ({value})",
                             "approach.reidentify_window: expected [from, to] ({value})", value=value)) from exc
    if lo >= hi:
        raise ConfigError(t("approach.reidentify_window: «desde» debe ser menor que «hasta» ({value})",
                             "approach.reidentify_window: «from» must be less than «to» ({value})", value=value))
    return lo, hi


def _local(es: str, en: str) -> str:
    """Texto del idioma actual (se resuelve al usarse, no al cargar), con el español como respaldo."""
    return pick({"es": es, "en": en})


def _text(table: dict[str, Any], key: str) -> tuple[str, str]:
    """Texto de usuario de una tabla del TOML: (``key`` en español, ``key_en`` en inglés)."""
    return str(table.get(key, "")), str(table.get(f"{key}_en", ""))


def _check(raw: dict | None, where: str) -> VisualCheck | None:
    """``VisualCheck`` de una tabla del TOML; ``None`` si no hay comprobación."""
    if not raw:
        return None
    return VisualCheck(raw["template"], _box(raw["box"], where), float(raw.get("min_score", 0.6)))


def _scene(raw: dict | None) -> SceneSpec:
    """``SceneSpec`` de una tabla del TOML; la fase del jugador debe caer entre 0 y el número de NPC."""
    raw = raw or {}
    scene = SceneSpec(
        npc=int(raw.get("npc", 0)),
        pokemon_npc=int(raw.get("pokemon_npc", 0)),
        player_rolls=bool(raw.get("player_rolls", True)),
        player_phase=int(raw.get("player_phase", 0)),
    )
    if scene.npc < 0 or scene.pokemon_npc < 0 or not 0 <= scene.player_phase <= scene.npc:
        raise ConfigError(t("Escena inválida: {raw}", "Invalid scene: {raw}", raw=raw))
    return scene


def parse_encounter(raw: dict[str, Any], source: str = "<memoria>") -> EncounterConfiguration:
    """Convierte el TOML ya cargado en ``EncounterConfiguration``.

    Cualquier clave ausente o valor mal tipado se convierte en ``ConfigError`` con ``source`` delante, para que
    el mensaje diga qué fichero corregir."""
    try:
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ConfigError(t("schema_version debe ser {v}", "schema_version must be {v}", v=SCHEMA_VERSION))
        enc = raw["encounter"]
        start = raw.get("start", {})
        blink = raw["blink_scene"]
        phases = tuple(
            TimelinePhase(
                name=p.get("name", f"fase{i + 1}"),
                trigger=p["trigger"],
                blank_reads=int(p.get("blank_reads", 0)),
                pause_s=float(p.get("pause_s", 0.0)),
                fixed_delay=int(p.get("fixed_delay", 0)),
                scene=_scene(p.get("scene")),
                steps=tuple(dict(x) for x in p.get("steps", [])),
            )
            for i, p in enumerate(raw.get("timeline", []))
        )
        final = raw.get("final", {})
        return EncounterConfiguration(
            id=enc["id"],
            version=str(enc["version"]),
            game=enc.get("game", "bdsp"),
            strategy=enc["strategy"],
            template=enc["template"],
            games=tuple(enc.get("games", ["BD", "SP"])),
            display_name_es=str(enc.get("display_name", enc["id"])),
            display_name_en=str(enc.get("display_name_en", "")),
            status=str(enc.get("status", "borrador" if "draft" in str(enc["version"]) else "sin_probar")),
            start=StartPoint(
                name_es=_text(start, "name")[0],
                name_en=_text(start, "name")[1],
                description_es=_text(start, "description")[0],
                description_en=_text(start, "description")[1],
                check=_check(start.get("check"), f"{source}: start.check"),
                instructions_es=_text(start, "instructions")[0],
                instructions_en=_text(start, "instructions")[1],
            ),
            blink_scene=BlinkSceneSpec(
                eye_template=blink["eye_template"],
                box=_box(blink["box"], f"{source}: blink_scene.box"),
                scene=_scene(blink.get("scene")),
                zoom=bool(blink.get("zoom", False)),
                reference_height=int(blink.get("reference_height", 1080)),
                min_blinks=int(blink.get("min_blinks", 40)),
                confirmation_blinks=int(blink.get("confirmation_blinks", 6)),
                message=bool(blink.get("message", False)),
            ),
            timeline=phases,
            final=FinalAction(
                trigger=final.get("trigger", "A"),
                final_delay=int(final.get("final_delay", 0)),
                min_timeline_s=float(final.get("min_timeline_s", 20.0)),
                min_gap_margin_s=float(final.get("min_gap_margin_s", 0.12)),
            ),
            preparation=tuple(
                RouteStep(s["action"], _check(s.get("expect"), f"{source}: preparation"),
                          float(s.get("timeout_s", 10.0)), int(s.get("retries", 0)))
                for s in raw.get("preparation", [])
            ),
            verification=tuple(raw.get("verification", {}).get("observe", ["shiny"])),
            capture=str(raw.get("verification", {}).get("capture", "auto")),
            species=tuple(raw.get("verification", {}).get("species", [])),
            recovery=raw.get("recovery", {}).get("method", "restart_game"),
            notes=raw.get("notes", ""),
            extra=raw.get("extra", {}),
            fast_advance=tuple(raw.get("fast_advance", {}).get("methods", ["pokedex_peek", "pokedex_scroll"])),
            approach=tuple(dict(x) for x in raw.get("approach", {}).get("steps", [])),
            approach_lead_s=float(raw.get("approach", {}).get("lead_s", 10.0)),
            approach_after_lead_s=float(raw.get("approach", {}).get("after_reidentify_lead_s", 20.0)),
            approach_extra_advances=int(raw.get("approach", {}).get("extra_advances", 0)),
            approach_reidentify_window=_window(raw.get("approach", {}).get("reidentify_window", (-40, 200))),
            approach_front_fast_advance=bool(raw.get("approach", {}).get("front_fast_advance", False)),
            approach_eye_instructions_es=_text(raw.get("approach", {}), "reidentify_eye_instructions")[0],
            approach_eye_instructions_en=_text(raw.get("approach", {}), "reidentify_eye_instructions")[1],
        )
    except KeyError as exc:
        raise ConfigError(t("{source}: falta la clave {exc}", "{source}: missing key {exc}", source=source, exc=exc)) from None
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"{source}: {exc}") from None


def load_encounter(path: Path) -> EncounterConfiguration:
    """Lee y valida un fichero de encuentro."""
    with path.open("rb") as f:
        return parse_encounter(tomllib.load(f), str(path))


def load_encounters(directory: Path) -> dict[str, EncounterConfiguration]:
    """Carga todos los encuentros de un directorio, indexados por ``id``; un ``id`` repetido es un error."""
    out: dict[str, EncounterConfiguration] = {}
    for path in sorted(directory.glob("*.toml")):
        cfg = load_encounter(path)
        if cfg.id in out:
            raise ConfigError(t("Encuentro duplicado: {id} ({path})", "Duplicate encounter: {id} ({path})", id=cfg.id, path=path))
        out[cfg.id] = cfg
    return out
