"""Avance rápido del RNG (Pokédex) y reidentificación por parpadeos.

En BDSP el RNG avanza mucho más deprisa con ciertas acciones de menú que esperando: la Pokédex es la más
eficaz de las que se pueden automatizar con seguridad. Cifras orientativas: entrar y salir de la
Pokédex ≈ 150 llamadas; recorrer sus fichas arriba y abajo, miles. El número exacto varía (depende de la
partida, de cuántas fichas haya y del tiempo de carga), así que tras cada acción se **reidentifica** el
estado con ~7 parpadeos del jugador sin recuperarlo desde cero: partiendo del estado conocido, se busca el
patrón de parpadeos (posiciones, tipos y ausencia de parpadeo en el resto de tiradas) en una ventana
alrededor de lo esperado. Lo medido alimenta una
estimación por acción (mediana y dispersión) que se guarda para próximas sesiones.

Las acciones son datos (``data/fast_advance.toml``): pulsaciones y esperas, sin lógica por Pokémon.
"""
from __future__ import annotations

import json
import math
import statistics
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from rngauto.control.controller import NEUTRAL, Button, Command, ControllerState, DPad, split_long
from rngauto.core.tracker import AdvanceClock
from rngauto.i18n import fmt_int, pick, t

from .blink import RollAssignment, assign_rolls
from .blink_pipeline import BlinkRecovery, EventLike
from .recovery import AmbiguousPattern, BlinkObservation, RecoveryError, RecoveryResult, reidentify, reidentify_kinds
from .xorshift import Xorshift128, XorshiftState

DATA_FILE = Path(__file__).with_name("data") / "fast_advance.toml"
REIDENTIFY_MAX_BLINKS = 20  # si con tantos parpadeos no se reidentifica, se recupera el estado desde cero


# --- Acciones -----------------------------------------------------------------------------------

# Pasos de diálogo: los ejecuta la estrategia mirando la imagen, no son comandos fijos de mando.
DIALOG_KINDS = ("dialog_until_choice", "dialog_next", "dialog_settle", "walk_talk", "talk_seek")


@dataclass(frozen=True, slots=True)
class MacroStep:
    """Un paso de una acción:

    * ``press``: pulsar ``button`` durante ``ms`` ms; después ``wait_ms`` en neutro.
    * ``hold``: mantener la cruceta (UP/DOWN/LEFT/RIGHT) ``ms`` ms; después ``wait_ms`` en neutro. Con
      ``scalable`` su duración la decide el programa según lo que falte (recorrer más o menos fichas).
    * ``check``: comprobar en la imagen que se ve la pantalla ``screen`` (``menu`` o ``pokedex``) antes de
      seguir, esperando como mucho ``ms`` ms (por defecto 3000). Si no aparece, se cancela la acción y se
      cierra lo abierto: nunca se sigue pulsando a ciegas.
    * ``wait``: no pulsar nada durante ``ms`` ms.
    * Diálogos (sin leer el texto: valen en cualquier idioma). Antes de cada A se espera a que el texto termine
      de escribirse (imagen quieta), así ninguna A se pierde ni se adelanta:
      - ``dialog_until_choice``: A en cada texto hasta que sale una caja de elección (Sí/No); no la contesta.
      - ``dialog_next``: espera un texto terminado con la flechita ▼ («sigue») y pulsa A.
      - ``dialog_settle``: espera un texto quieto SIN flechita (el último antes de un suceso) y no pulsa.
    * ``reidentify_pokemon`` (solo en la aproximación): con el legendario ya en pantalla (parpadea y gasta RNG),
      observar ``blinks`` parpadeos del personaje con el ojo preparado para ``eye`` (id de preparación del ojo) y
      reidentificar teniéndolo en cuenta; después se re-planifica y los pasos siguientes empiezan
      ``after_lead_s`` (del encuentro) antes del inicio del timeline. Como mucho ``ms`` ms.
    * ``walk_talk``: caminar hacia ``button`` (un tramo largo y, si hace falta, otros cortos) y pulsar A tras
      cada tramo, de una en una, mirando si sale su texto antes de seguir: se habla con el legendario esté a la
      distancia que esté y nunca se pulsa una A de más sobre su texto; como mucho ``ms`` ms.
    * ``talk_seek``: hablar con un NPC al que se ha llegado andando hacia ``button`` hasta chocar: A; si no sale su
      texto, empujar hacia los dos lados perpendiculares (el NPC o el borde lo bloquean: solo gira) y A en cada uno.
      Vale esté el personaje en la fila del NPC o en la de al lado (muelle de Ciudad Canal: 3 filas, el marinero en
      la del medio); como mucho ``ms`` ms.
    """

    kind: str
    button: str = ""
    ms: int = 80
    wait_ms: int = 0
    screen: str = ""
    scalable: bool = False
    eye: str = ""  # ``reidentify_pokemon``: preparación del ojo que se usa
    blinks: int = 0  # ``reidentify_pokemon``: parpadeos a observar

    def __post_init__(self) -> None:
        if self.kind == "check":
            if not self.screen:
                raise ValueError(t("Un paso «check» necesita «screen»", "A «check» step needs «screen»"))
            if self.screen.startswith("!") and self.screen[1:] not in ("menu", "dialog"):
                raise ValueError(t("Comprobación negada solo para pantallas conocidas: {screen}",
                                     "Negated check only allowed for known screens: {screen}",
                                     screen=self.screen))
        elif self.kind == "press":
            Button[self.button]  # valida el nombre al cargar (KeyError): un TOML mal escrito falla antes de pulsar
        elif self.kind == "hold":
            DPad[self.button]
        elif self.kind in ("walk_talk", "talk_seek"):
            DPad[self.button]
        elif self.kind == "reidentify_pokemon":
            if not self.eye or self.blinks < 8:
                raise ValueError(t("«reidentify_pokemon» necesita «eye» y al menos 8 «blinks»",
                                   "«reidentify_pokemon» needs «eye» and at least 8 «blinks»"))
        elif self.kind in DIALOG_KINDS or self.kind == "wait":
            pass
        else:
            raise ValueError(t("Paso desconocido: {kind}", "Unknown step: {kind}", kind=self.kind))
        if self.scalable and self.kind != "hold":
            raise ValueError(t("Solo un paso «hold» puede ser escalable", "Only a «hold» step can be scalable"))

    @classmethod
    def from_dict(cls, s: dict) -> "MacroStep":
        """Paso a partir de su tabla TOML. En los pasos que miran la imagen, ``ms`` es un tope de espera y su
        valor por defecto depende del tipo."""
        default_ms = {"check": 3000, "dialog_until_choice": 15000, "dialog_next": 8000,
                      "dialog_settle": 8000, "walk_talk": 8000, "talk_seek": 15000,
                      "reidentify_pokemon": 300_000}.get(s["kind"], 80)
        default_wait = 0
        return cls(s["kind"], str(s.get("button", "")).upper(), int(s.get("ms", default_ms)),
                   int(s.get("wait_ms", default_wait)), str(s.get("screen", "")), bool(s.get("scalable", False)),
                   str(s.get("eye", "")), int(s.get("blinks", 0)))

    @property
    def duration_s(self) -> float:
        """Duración prevista del paso para planificar. Un ``check`` no cuenta (suele pasar al instante)."""
        if self.kind == "check":
            return 0.0
        if self.kind in DIALOG_KINDS or self.kind == "reidentify_pokemon":
            return self.ms / 1000  # peor caso
        return (self.ms + self.wait_ms) / 1000

    def commands(self, label: str) -> list[Command]:
        """Comandos de mando del paso (troceados con ``split_long``); vacío si el paso depende de la imagen."""
        if self.kind in ("check", "reidentify_pokemon") or self.kind in DIALOG_KINDS:
            return []  # los ejecuta la estrategia mirando la imagen
        if self.kind == "wait":
            return list(split_long(NEUTRAL, self.ms, label))
        state = ControllerState(Button[self.button]) if self.kind == "press" else ControllerState(dpad=DPad[self.button])
        return [*split_long(state, self.ms, label), *split_long(NEUTRAL, self.wait_ms, label)]


@dataclass(frozen=True, slots=True)
class AdvanceMethod:
    """Acción de avance rápido. Fija (siempre igual) o escalable: consumo ≈ base + ritmo·segundos de cruceta,
    con la duración elegida entre ``min_hold_s`` y ``max_hold_s`` (suma de los pasos escalables).

    Se guardan los textos en los dos idiomas; ``name`` y ``description`` devuelven el del idioma actual al
    consultarlos (las acciones se cargan una vez y el idioma puede cambiar después)."""

    id: str
    name_es: str
    description_es: str
    steps: tuple[MacroStep, ...]
    prior_mean: float  # llamadas con la duración por defecto (orientativo; se aprende)
    prior_sd: float
    prior_rate: float = 0.0  # llamadas por segundo de cruceta a velocidad de crucero (solo escalables)
    min_hold_s: float = 0.0
    max_hold_s: float = 0.0
    prior_ramp_s: float = 0.8  # lo que tarda la lista en coger velocidad en cada tramo (se aprende)
    base_from: str = ""  # acción cuyo consumo es la parte fija (abrir y cerrar), p. ej. «pokedex_peek»
    name_en: str = ""
    description_en: str = ""

    @property
    def name(self) -> str:
        return pick({"es": self.name_es, "en": self.name_en})

    @property
    def description(self) -> str:
        return pick({"es": self.description_es, "en": self.description_en})

    @property
    def scalable(self) -> bool:
        return any(s.scalable for s in self.steps)

    @property
    def default_hold_s(self) -> float:
        return sum(s.ms for s in self.steps if s.scalable) / 1000

    def steps_for(self, hold_s: float | None = None) -> tuple[MacroStep, ...]:
        """Pasos con la cruceta mantenida ``hold_s`` segundos en total (repartidos en proporción)."""
        if hold_s is None or not self.scalable:
            return self.steps
        factor = hold_s / self.default_hold_s
        return tuple(replace(s, ms=max(1, int(round(s.ms * factor)))) if s.scalable else s for s in self.steps)

    def duration_s(self, hold_s: float | None = None) -> float:
        return sum(s.duration_s for s in self.steps_for(hold_s))

    def cruise_s(self, hold_s: float, ramp_s: float) -> float:
        """Segundos «a velocidad de crucero»: de cada tramo escalable (bajar, subir) se descuenta el arranque."""
        parts = [s.ms for s in self.steps if s.scalable]
        total = sum(parts)
        if not total:
            return 0.0
        return sum(max(0.0, hold_s * p / total - ramp_s) for p in parts)

    @property
    def screens(self) -> tuple[str, ...]:
        return tuple(s.screen for s in self.steps if s.kind == "check")


def load_methods(path: Path = DATA_FILE) -> dict[str, AdvanceMethod]:
    """Acciones de ``path`` por id. Valida cada paso y la coherencia de las escalables (``ValueError`` si no)."""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    out: dict[str, AdvanceMethod] = {}
    for m in raw.get("method", []):
        steps = tuple(MacroStep.from_dict(s) for s in m["steps"])  # se valida al cargar
        method = AdvanceMethod(m["id"], m["name"], m.get("description", ""), steps, float(m["prior_mean"]),
                               float(m["prior_sd"]), float(m.get("prior_rate", 0.0)),
                               float(m.get("min_hold_s", 0.0)), float(m.get("max_hold_s", 0.0)),
                               float(m.get("prior_ramp_s", 0.8)), str(m.get("base_from", "")),
                               str(m.get("name_en", "")), str(m.get("description_en", "")))
        if method.scalable and not (0 < method.min_hold_s <= method.default_hold_s <= method.max_hold_s
                                    and method.prior_rate > 0):
            raise ValueError(t("{id}: un método escalable necesita 0 < min_hold_s ≤ duración ≤ max_hold_s "
                               "y prior_rate > 0",
                               "{id}: a scalable method needs 0 < min_hold_s ≤ duration ≤ max_hold_s "
                               "and prior_rate > 0", id=m["id"]))
        out[m["id"]] = method
    return out


# --- Lo aprendido por acción ---------------------------------------------------------------------

@dataclass
class AdvanceStats:
    """Llamadas medidas por acción: pares (segundos de cruceta, llamadas), persistentes.

    * Acción fija: mediana y dispersión de lo medido (con < 3 medidas, mezcla con lo orientativo).
    * Acción escalable: consumo = base + ritmo·(segundos «a velocidad de crucero»), ajustada por mínimos
      cuadrados y anclada a lo orientativo con dos pseudo-medidas que pierden peso con las medidas reales. La
      lista de la Pokédex tarda en coger velocidad al empezar a bajar y otra vez al empezar a subir (observado
      en consola): de cada tramo de cruceta solo cuenta lo que pasa del arranque. La duración del arranque se
      elige entre varias la que mejor explica lo medido.
    """

    samples: dict[str, list[tuple[float, int]]] = field(default_factory=dict)
    path: Path | None = None
    # Segundos desde que termina la acción hasta tener el estado reidentificado y confirmado (parpadeos
    # aleatorios: ~1-2 min). Durante ese tiempo el RNG sigue avanzando solo.
    reidentify_s: list[float] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "AdvanceStats":
        """Lee lo aprendido de ``path`` (últimas 50 medidas por acción). Si falta o está dañado, empieza vacío.

        Admite el formato antiguo (solo llamadas, sin segundos de cruceta) como medidas con 0 s.
        """
        samples: dict[str, list[tuple[float, int]]] = {}
        reid: list[float] = []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for k, v in data.get("samples", {}).items():
                samples[k] = [(float(x[0]), int(x[1])) if isinstance(x, list) else (0.0, int(x)) for x in v][-50:]
            reid = [float(x) for x in data.get("reidentify_s", [])][-50:]
        except (OSError, ValueError, AttributeError, TypeError, IndexError):
            samples, reid = {}, []
        return cls(samples, path, reid)

    def save(self) -> None:
        """Guarda en ``path`` si lo hay. Un error de disco se ignora: perder lo aprendido no debe parar la ejecución."""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: [[h, c] for h, c in v] for k, v in self.samples.items()}
            self.path.write_text(json.dumps({"samples": data, "reidentify_s": self.reidentify_s}, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass

    def add_reidentify(self, seconds: float) -> None:
        self.reidentify_s = (self.reidentify_s + [round(float(seconds), 1)])[-50:]
        self.save()

    def reidentify_estimate(self) -> tuple[float, float]:
        """(media, desviación) de lo que tarda en reidentificar tras una acción. A priori: 9 parpadeos (7 + 2 de
        confirmación) a uno cada ~8,2 s de media, con mucha variación."""
        xs = self.reidentify_s
        if len(xs) < 3:
            prior_mean, prior_sd = 85.0, 30.0
            if not xs:
                return prior_mean, prior_sd
            return (prior_mean * 2 + sum(xs)) / (2 + len(xs)), prior_sd
        med = statistics.median(xs)
        mad = statistics.median(abs(x - med) for x in xs) * 1.4826
        return float(med), max(mad, 12.0)

    def add(self, method_id: str, consumed: int, hold_s: float = 0.0) -> None:
        self.samples.setdefault(method_id, []).append((round(float(hold_s), 2), int(consumed)))
        self.samples[method_id] = self.samples[method_id][-50:]
        self.save()

    def estimate(self, method: AdvanceMethod, hold_s: float | None = None) -> tuple[float, float]:
        """(media, desviación) esperadas de llamadas consumidas (con ``hold_s`` segundos de cruceta)."""
        xs = self.samples.get(method.id, [])
        if not method.scalable:
            values = [c for _, c in xs]
            if len(values) < 3:
                if not values:
                    return method.prior_mean, method.prior_sd
                mean = (method.prior_mean + sum(values)) / (1 + len(values))
                return mean, max(method.prior_sd * 0.75, abs(mean - method.prior_mean))
            med = statistics.median(values)
            mad = statistics.median(abs(x - med) for x in values) * 1.4826
            return float(med), max(mad, 0.08 * med, 15.0)
        h = method.default_hold_s if hold_s is None else hold_s
        ramp, base, rate, resid = self._fit_scalable(method, xs)
        mean = max(1.0, base + rate * method.cruise_s(h, ramp))
        if len(xs) >= 3:
            sd = math.sqrt(sum(r * r for r in resid) / max(1, len(resid) - 2))
            return mean, max(sd, 0.06 * mean, 20.0)
        return mean, max(method.prior_sd * mean / method.prior_mean, 40.0)

    def _base(self, method: AdvanceMethod) -> float:
        """Parte fija (abrir y cerrar): lo medido en la acción ``base_from`` o, si no hay, lo orientativo."""
        values = [c for _, c in self.samples.get(method.base_from, [])] if method.base_from else []
        if values:
            return float(statistics.median(values))
        return method.prior_mean - method.prior_rate * method.cruise_s(method.default_hold_s, method.prior_ramp_s)

    def _fit_scalable(self, method: AdvanceMethod, xs: list[tuple[float, int]]):
        """(arranque, base, ritmo, residuos): base fija (abrir y cerrar); ritmo por mínimos cuadrados y
        arranque (0-2 s por tramo) el que mejor explica lo medido. Con pocas medidas pesa lo orientativo."""
        base = self._base(method)
        ramps = [k / 10 for k in range(0, 21)] if len(xs) >= 2 else [method.prior_ramp_s]
        best = None
        for ramp in ramps:
            # pseudo-medida orientativa (pierde peso a medida que hay medidas reales)
            pts = [(method.cruise_s(method.default_hold_s, ramp), method.prior_mean, 1.0 / (1 + len(xs)))]
            pts += [(method.cruise_s(hh, ramp), float(c), 1.0) for hh, c in xs]
            sxx = sum(w * x * x for x, _, w in pts)
            rate = max(0.0, sum(w * x * (c - base) for x, c, w in pts) / sxx) if sxx > 0 else method.prior_rate
            resid = [c - (base + rate * method.cruise_s(hh, ramp)) for hh, c in xs]
            sse = sum(r * r for r in resid)
            if best is None or sse < best[0] - 1e-9:
                best = (sse, ramp, base, rate, resid)
        return best[1:]


# --- Decisión ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FastAdvanceChoice:
    """Acción elegida por ``choose_method`` con su consumo esperado (media ± dispersión) y lo que faltaba."""

    method: AdvanceMethod
    expected: float
    sd: float
    remaining: int
    hold_s: float | None = None  # segundos de cruceta (acciones escalables)

    @property
    def steps(self) -> tuple[MacroStep, ...]:
        return self.method.steps_for(self.hold_s)

    def describe(self) -> str:
        hold = (t(" ({hold:.1f} s recorriendo fichas)", " ({hold:.1f} s scrolling through entries)",
                  hold=self.hold_s) if self.hold_s is not None else "")
        return t("{name}{hold}: ~{expected} avances (±{sd}); faltan {remaining}",
                 "{name}{hold}: ~{expected} advances (±{sd}); {remaining} left",
                 name=self.method.name, hold=hold, expected=fmt_int(self.expected), sd=fmt_int(self.sd),
                 remaining=fmt_int(self.remaining))


def choose_method(
    remaining: int,
    natural_rate: float,
    methods: Sequence[AdvanceMethod],
    stats: AdvanceStats,
    *,
    after_s: float = 35.0,
    min_gain_s: float = 60.0,
    k_sigma: float = 2.0,
) -> FastAdvanceChoice | None:
    """¿Merece la pena una acción de avance rápido y cuál? Cuenta completa:

    lo que consume la acción (media ± dispersión aprendidas)
    + lo que avanza solo mientras dura la acción y la reidentificación (tiempo aprendido, también aleatorio)
    + lo que tiene que quedar después para planificar y hacer la ruta (``after_s``)
    + ``k_sigma`` veces la incertidumbre conjunta  ≤  ``remaining`` (llamadas hasta la primera pulsación).

    Además tiene que ahorrar al menos ``min_gain_s`` frente a esperar sin hacer nada (si no, se espera: es
    predecible y no hay riesgo de pasarse). Se elige la acción (y, en las escalables, la duración) que más
    avanza. Pasarse no es grave (se va al siguiente objetivo válido), pero es tiempo perdido.
    """
    if natural_rate <= 0:
        return None
    rid_mean, rid_sd = stats.reidentify_estimate()

    def fits(m: AdvanceMethod, hold: float | None) -> tuple[float, float] | None:
        mean, sd = stats.estimate(m, hold)
        busy_s = m.duration_s(hold) + rid_mean
        total_sd = math.hypot(sd, natural_rate * rid_sd)
        need = mean + natural_rate * (busy_s + after_s) + k_sigma * total_sd
        gain_s = mean / natural_rate - busy_s
        return (mean, sd) if mean > 0 and need <= remaining and gain_s >= min_gain_s else None

    best: FastAdvanceChoice | None = None
    for m in methods:
        if m.scalable:
            lo, hi = m.min_hold_s, m.max_hold_s
            if fits(m, lo) is None:
                continue
            for _ in range(30):  # la mayor duración que cabe (el consumo crece con la duración)
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if fits(m, mid) is not None else (lo, mid)
            hold = round(lo, 1) if fits(m, round(lo, 1)) is not None else lo
            mean, sd = fits(m, hold)
            candidate = FastAdvanceChoice(m, mean, sd, remaining, hold)
        else:
            got = fits(m, None)
            if got is None:
                continue
            candidate = FastAdvanceChoice(m, got[0], got[1], remaining)
        if best is None or candidate.expected > best.expected:
            best = candidate
    return best


# --- Reidentificación ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Reidentified:
    """Resultado de ``reidentify_events``. ``note`` indica qué concesión hizo falta (vacío si el patrón era exacto)."""

    recovery: BlinkRecovery  # estado RE-BASADO: su base es la tirada del primer parpadeo usado
    start_advance: int  # llamadas desde la base anterior hasta esa tirada
    blinks_used: int
    note: str = ""


def reidentify_events(
    base: XorshiftState,
    events: Sequence[EventLike],
    npc: int,
    period_s: float,
    search_min: int,
    search_max: int,
    period_weight: float = 0.0,
) -> Reidentified:
    """Busca el patrón de ``events`` (ordenados, ya filtrados por confianza) a partir de ``base`` en
    ``[search_min, search_max)`` y devuelve el estado re-basado con su reloj.

    ``period_s``/``period_weight``: periodo de tirada ya medido (no cambia con los menús) y cuánta
    información lo respalda; se conserva para que una espera larga tras el avance no acumule error.

    Intentos, de más a menos estricto (se acepta solo un resultado único): patrón exacto; tolerando un
    parpadeo no detectado; quitando un parpadeo (posible falso positivo), el más dudoso primero.
    ``RecoveryError`` si es ambiguo o no aparece (hay que observar más o ampliar la ventana).
    """
    if len(events) < 3:
        raise RecoveryError(t("Muy pocos parpadeos para reidentificar", "Too few blinks to reidentify"))
    search_min = max(0, int(search_min))
    search_max = max(search_min + 1, int(search_max))
    assignment = assign_rolls([e.t_s for e in events], period_prior_s=period_s, period_tolerance=0.005)
    every = list(range(len(events)))
    attempts: list[tuple[str, list[int], int]] = [
        ("", every, 0), (t("tolerando 1 parpadeo no detectado", "tolerating 1 missed blink"), every, 1)]
    order = sorted(every, key=lambda i: (events[i].confidence, -abs(assignment.residuals[i])))
    attempts += [(t("sin el parpadeo nº {n} (posible falso positivo)", "without blink #{n} (possible false positive)",
                    n=i + 1), [j for j in every if j != i], 0)
                 for i in order]
    # Las salidas del RNG se generan una sola vez para todos los intentos (en Python puro son lo caro:
    # ~30 000 en la ventana amplia; repetirlas en cada intento dejaría segundos sin leer frames).
    span = assignment.rolls[-1] - assignment.rolls[0]
    kinds = reidentify_kinds(base, search_min, search_max - search_min + span * (npc + 1) + 1)
    last_error: RecoveryError | None = None
    for note, idx, tolerate in attempts:
        used = [BlinkObservation(assignment.rolls[i], _reliable_kind(events[i].kind), events[i].confidence) for i in idx]
        try:
            res = reidentify(base, used, npc, search_min=search_min, search_max=search_max, tolerate_missed=tolerate,
                             kinds=kinds)
        except RecoveryError as exc:
            if isinstance(exc, AmbiguousPattern):
                raise  # con menos restricciones solo puede ser más ambiguo
            last_error = exc
            continue
        return _rebase(base, res.advance, npc, [events[i] for i in idx], [assignment.rolls[i] for i in idx],
                       period_s, period_weight, len(events), note, search_max - search_min)
    raise last_error or RecoveryError(t("No se encontró el patrón", "Pattern not found"))


def _rebase(base: XorshiftState, advance_after_last: int, npc: int, events: list, rolls: list[int],
            period_s: float, period_weight: float, total: int, note: str, window: int) -> Reidentified:
    """Construye un ``BlinkRecovery`` cuya base es la tirada del primer parpadeo usado: así el resto de la
    estrategia (predicción, re-anclaje, planificación) funciona igual que tras una recuperación completa."""
    stride = npc + 1
    first = rolls[0]
    rel = [r - first for r in rolls]
    start = advance_after_last - rel[-1] * stride - 1  # llamadas desde la base hasta esa tirada
    rng = Xorshift128.from_state(base)
    rng.skip(start)
    obs = tuple(BlinkObservation(r, e.kind, e.confidence) for r, e in zip(rel, events))
    result = RecoveryResult(
        state=rng.state, npc=npc, observations=obs, excluded=(), shifted=(),
        redundant_equations=max(0, _pattern_bits(rel, obs) - _bits(window)), null_space_bits=0, mismatches=(),
    )
    # Periodo: el ya medido (los menús no lo cambian); fase: ajuste de los parpadeos observados.
    times = [e.t_s for e in events]
    phase = statistics.fmean(t - period_s * r for t, r in zip(times, rel))
    residuals = tuple((t - (phase + period_s * r)) / period_s for t, r in zip(times, rel))
    assignment = RollAssignment(tuple(rel), period_s, phase, residuals)
    last = rel[-1]
    clock = AdvanceClock(
        anchor_t=phase + period_s * last, anchor_advance=(last + 1) * stride, period_s=period_s,
        calls_per_tick=stride, period_sigma_s=max(0.0001, 0.02 / max(1.0, math.sqrt(period_weight) or 1.0)),
    )
    rec = BlinkRecovery(result, assignment, clock, len(events), total - len(events),
                        period_prior=(period_s, period_weight))
    return Reidentified(rec, start, len(events), note)


def _bits(n: int) -> int:
    """Bits que cuesta elegir una posición entre ``n`` (lo que «gasta» la búsqueda en la ventana)."""
    return max(1, math.ceil(math.log2(max(2, n))))


def _pattern_bits(rolls: list[int], obs: list[BlinkObservation]) -> int:
    """Información aproximada del patrón: ~0,54 bits por tirada (parpadea o no, p=1/8) + 1 por tipo."""
    span = max(rolls) - min(rolls) + 1
    return int(0.54 * span) + sum(1 for o in obs if o.kind is not None)


def _reliable_kind(kind):
    """El detector a veces pierde el segundo cierre de un doble (lo ve simple); lo contrario es raro. Por eso
    un «simple» se trata como tipo desconocido y un «doble» sí se exige."""
    return kind if kind is not None and int(kind) == 1 else None
