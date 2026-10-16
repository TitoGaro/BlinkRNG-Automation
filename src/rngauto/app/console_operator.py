"""Operador de consola para ``rngauto run``: el programa propone, el usuario confirma y observa.

Implementa el ``Operator`` de la estrategia con ``input``/``print``. La verificación la hace el usuario
tecleando lo que ve: shiny, naturaleza y stats (de los stats se deducen rangos de IV exactos).
En modo automático, ``auto_confirm`` responde «sí» a las confirmaciones sin preguntar.
"""
from __future__ import annotations

from collections.abc import Callable

from rngauto.core.pokemon import nature_index, stat_labels
from rngauto.core.stats import StatsInconsistent, iv_ranges_from_stats
from rngauto.games.bdsp.identify import Observation
from rngauto.games.bdsp.static_gen import StaticTemplate
from rngauto.i18n import t

class ConsoleOperator:
    """Operador por consola. ``input_fn`` y ``print_fn`` se inyectan para poder probarlo sin terminal."""

    def __init__(
        self,
        template: StaticTemplate,
        input_fn: Callable[[str], str] = input,
        print_fn: Callable[[str], None] = print,
        auto_confirm: bool = False,
    ) -> None:
        self.template = template
        self.input = input_fn
        self.print = print_fn
        self.auto_confirm = auto_confirm

    def notify(self, message: str) -> None:
        self.print(message)

    def confirm(self, message: str, timeout_s: float | None = None) -> bool | None:
        """Pregunta sí/no; cualquier respuesta que no sea afirmativa cuenta como «no». ``timeout_s`` se ignora."""
        if self.auto_confirm:
            self.print(t("{message} → sí (automático)", "{message} → yes (automatic)", message=message))
            return True
        answer = self.input(t("{message} [s/N] ", "{message} [y/N] ", message=message)).strip().lower()
        return answer in ("s", "si", "sí", "y", "yes")

    def wait_ready(self, message: str) -> bool:
        self.input(t("{message} — pulsa Enter cuando esté listo ", "{message} — press Enter when ready ",
                     message=message))
        return True

    def ask_observation(self, fields: list[str]) -> Observation | None:
        """Pide shiny, naturaleza y stats; ``None`` si no se sabe nada. Los stats solo se piden con naturaleza,
        porque sin ella no se pueden convertir en IV."""
        self.print(t("Verificación: mira el Pokémon obtenido (captúralo para ver la pantalla de datos).",
                     "Verification: check the Pokémon you got (catch it to see its Summary)."))
        shiny = self._ask_shiny()
        nature = self._ask_nature()
        ivs = None
        if nature is not None:
            ivs = self._ask_stats(nature)
        if shiny is None and nature is None:
            return None
        return Observation(is_shiny=shiny, nature=nature, ivs=ivs)

    def _ask_shiny(self) -> bool | None:
        while True:
            a = self.input(t("¿Es shiny? [s/n, vacío = no sé] ", "Is it shiny? [y/n, empty = unknown] ")).strip().lower()
            if a == "":
                return None
            if a in ("s", "si", "sí", "y"):
                return True
            if a in ("n", "no"):
                return False

    def _ask_nature(self) -> int | None:
        while True:
            a = self.input(t("Naturaleza (p. ej. Modesta; vacío = no sé): ",
                             "Nature (e.g. Modest; empty = unknown): ")).strip()
            if not a:
                return None
            try:
                return nature_index(a)
            except ValueError:
                self.print(t("  No la reconozco; escríbela como aparece en el juego.",
                             "  Not recognized; type it as it appears in the game."))

    def _ask_stats(self, nature: int):
        """Pide los 6 stats y devuelve los rangos de IV compatibles; repite si son incoherentes."""
        tpl = self.template
        while True:
            a = self.input(t(
                "Stats a nivel {level} ({labels}), separados por espacios; vacío = omitir: ",
                "Stats at level {level} ({labels}), separated by spaces; empty = skip: ",
                level=tpl.level, labels=" ".join(stat_labels()),
            )).strip()
            if not a:
                return None
            try:
                stats = tuple(int(x) for x in a.replace(",", " ").split())
                if len(stats) != 6:
                    raise ValueError
                ranges = iv_ranges_from_stats(tpl.base_stats, tpl.level, nature, stats)
            except ValueError as exc:
                msg = str(exc) if isinstance(exc, StatsInconsistent) else t("Necesito 6 números", "6 numbers are needed")
                self.print(t("  {msg}. Revisa la lectura (y que la naturaleza sea correcta).",
                             "  {msg}. Check the values (and that the nature is correct).", msg=msg))
                continue
            self.print(t("  IVs deducidos: ", "  Deduced IVs: ")
                       + " ".join(f"{r.lo}-{r.hi}" if r.lo != r.hi else str(r.lo) for r in ranges))
            return ranges
