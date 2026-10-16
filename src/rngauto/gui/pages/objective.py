"""Paso 1 — Objetivo: Pokémon, cómo lo quieres y modo de trabajo.

Cada cambio se vuelca al momento en ``AppState.selection`` (y en las preferencias lo que se recuerda entre
sesiones). La estimación de lo raro que es el objetivo se calcula en segundo plano.
"""
from __future__ import annotations

import random

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

from rngauto.core.pokemon import NATURES_EN, NATURES_ES, STAT_NAMES, STAT_NAMES_ES, IVRange, ShinyFilter, TargetCriteria
from rngauto.games.bdsp.encounters import StaticTargetFinder, encounter_species, static_template, static_templates
from rngauto.games.bdsp.static_strategy import Mode, fmt_duration
from rngauto.games.bdsp.xorshift import XorshiftState
from rngauto.i18n import fmt_int, is_spanish, t

from .. import theme
from ..widgets import ROLE_BADGE, ROLE_HINT, ROLE_STATE, Background, ChoiceDelegate, card, hbox, label
from .base import Page

# Las claves son identificadores internos (no se muestran): el texto visible sale de ``_iv_preset_label``.
IV_PRESETS = {
    "Cualquiera": None,
    "Perfecto (6×31)": (31, 31, 31, 31, 31, 31),
    "Especial (31/x/31/31/31/31)": (31, 0, 31, 31, 31, 31),
    "Físico (31/31/31/x/31/31)": (31, 31, 31, 0, 31, 31),
    "Personalizado": "custom",
}


def _iv_preset_label(key: str) -> str:
    """Texto visible de un preset de IVs en el idioma actual."""
    return {
        "Cualquiera": t("Cualquiera", "Any"),
        "Perfecto (6×31)": t("Perfecto (6×31)", "Perfect (6×31)"),
        "Especial (31/x/31/31/31/31)": t("Especial (31/x/31/31/31/31)", "Special (31/x/31/31/31/31)"),
        "Físico (31/31/31/x/31/31)": t("Físico (31/31/31/x/31/31)", "Physical (31/31/31/x/31/31)"),
        "Personalizado": t("Personalizado", "Custom"),
    }.get(key, key)


def _category_label(category: str) -> str:
    """Nombre visible de una categoría de encuentro en el idioma actual."""
    return {
        "legend": t("Legendario", "Legendary"),
        "mythic": t("Singular", "Mythical"),
        "ramanas_pure": t("Parque Hansa", "Ramanas Park"),
        "ramanas_strange": t("Parque Hansa", "Ramanas Park"),
        "roamer": t("Errante", "Roaming"),
        "stationary": t("Estático", "Stationary"),
        "fossil": t("Fósil", "Fossil"),
        "gift": t("Regalo", "Gift"),
        "starter": t("Inicial", "Starter"),
    }.get(category, category)


# Categorías que se anuncian como «Próximamente» si aún no tienen encuentro configurado.
LEGENDARY_CATEGORIES = ("legend", "ramanas_pure", "ramanas_strange", "roamer", "mythic")

class ObjectivePage(Page):
    """Paso «Objetivo»: lista de encuentros, criterios (shiny, naturaleza, IVs), modo y opciones avanzadas.

    La lista agrupa los encuentros configurados por su estado de prueba y añade, desactivados, los legendarios
    que aún no lo están. Se completa al elegir un encuentro.
    """

    @property
    def step_title(self) -> str:
        return t("Objetivo", "Target")

    @property
    def step_hint(self) -> str:
        return t("Pokémon y criterios", "Pokémon and criteria")

    def __init__(self, state) -> None:
        super().__init__(state, t("¿Qué Pokémon quieres conseguir?", "Which Pokémon do you want?"),
                         t("Elige el encuentro, cómo lo quieres y cómo quieres trabajar. Todo lo demás lo calcula el programa.",
                           "Choose the encounter, what you want it to be like and how you want to work. "
                           "The program works out everything else."))
        # --- Pokémon ---
        self.list = QListWidget()
        self.list.setObjectName("choices")
        self.list.setItemDelegate(ChoiceDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setMinimumHeight(300)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        def header(text: str) -> None:
            """Título de sección de la lista (no seleccionable)."""
            item = QListWidgetItem(text)
            item.setData(ROLE_STATE, "header")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)

        groups = (("comprobado", t("Comprobados en consola", "Verified on a console"),
                   (t("Comprobado", "Verified"), theme.SUCCESS)),
                  ("sin_probar", t("Mismo mecanismo, sin probar aún", "Same mechanism, not tested yet"),
                   (t("Sin probar", "Untested"), theme.WARNING)),
                  ("borrador", t("Borradores", "Drafts"), (t("Borrador", "Draft"), theme.MUTED)))
        known = {g for g, _, _ in groups}  # un estado desconocido se agrupa con «sin probar»
        ready = []
        for status, title, badge in groups:
            members = sorted((e for e in state.encounters.values()
                              if e.status == status or (status == "sin_probar" and e.status not in known)),
                             key=lambda e: e.display_name)
            if not members:
                continue
            header(title)
            for enc in members:
                species = encounter_species(enc)
                hint = t("Nivel {level}", "Level {level}", level=species[0].level)
                if len(species) > 1:  # misma sala y losa: no se sabe cuál saldrá
                    names = [tpl.name for tpl in species]
                    hint += t(" · sale uno al azar", " · one appears at random")
                    if not all(n in enc.display_name for n in names):  # «Regis» no dice cuáles
                        hint += t(": {first} o {last}", ": {first} or {last}",
                                  first=", ".join(names[:-1]), last=names[-1])
                item = QListWidgetItem(enc.display_name)
                item.setData(Qt.ItemDataRole.UserRole, enc.id)
                item.setData(ROLE_STATE, "item")
                item.setData(ROLE_HINT, hint)
                item.setData(ROLE_BADGE, badge)
                self.list.addItem(item)
                ready += [enc.template, *(tpl.key for tpl in species)]
        # La aplicación trabaja con legendarios; Mew y Jirachi se excluyen porque son regalos, no encuentros.
        soon = [tpl for tpl in static_templates().values()
                if tpl.key not in ready and tpl.category in LEGENDARY_CATEGORIES and tpl.key not in ("mew", "jirachi")]
        if soon:
            header(t("Próximamente", "Coming soon"))
        for tpl in soon:
            item = QListWidgetItem(tpl.name)
            item.setData(ROLE_STATE, "soon")
            item.setData(ROLE_HINT, _category_label(tpl.category))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
        self.list.currentItemChanged.connect(self._on_pick)
        self.body.addWidget(card(self.list, title="Pokémon"))

        # --- Criterios ---
        crit = QWidget()
        cl = QVBoxLayout(crit)
        cl.setContentsMargins(0, 0, 0, 0)
        self.shiny = QCheckBox(t("Quiero que sea shiny", "I want it shiny"))
        self.shiny.setChecked(True)
        self.nature = QComboBox()
        self.nature.addItem(t("Cualquiera", "Any"), None)
        for i, n in enumerate(NATURES_ES if is_spanish() else NATURES_EN):
            self.nature.addItem(n, i)
        self.preset = QComboBox()
        for key in IV_PRESETS:
            self.preset.addItem(_iv_preset_label(key), key)
        self.iv_box = QWidget()
        grid = QGridLayout(self.iv_box)
        grid.setContentsMargins(0, 0, 0, 0)
        self.ivs: list[QSpinBox] = []
        for i, name in enumerate(STAT_NAMES_ES if is_spanish() else STAT_NAMES):
            sb = QSpinBox()
            sb.setRange(0, 31)
            sb.setPrefix("≥ ")
            sb.valueChanged.connect(self._criteria_changed)
            grid.addWidget(QLabel(name), 0, i)
            grid.addWidget(sb, 1, i)
            self.ivs.append(sb)
        self.iv_box.setVisible(False)  # solo se muestra con el preset «Personalizado»
        self.rarity = label("", "muted")
        for w in (self.shiny,):
            w.stateChanged.connect(self._criteria_changed)
        self.nature.currentIndexChanged.connect(self._criteria_changed)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        cl.addWidget(self.shiny)
        cl.addWidget(QLabel(t("Naturaleza", "Nature")))
        cl.addWidget(self.nature)
        cl.addWidget(QLabel(t("IVs (mínimos)", "IVs (minimum)")))
        cl.addWidget(self.preset)
        cl.addWidget(self.iv_box)
        cl.addWidget(self.rarity)
        cl.addStretch(1)
        crit_card = card(crit, title=t("Cómo lo quieres", "What you want"))

        # --- Modo ---
        modes = QWidget()
        ml = QVBoxLayout(modes)
        ml.setContentsMargins(0, 0, 0, 0)
        self.mode_group = QButtonGroup(self)
        options = [
            (Mode.AUTOMATIC, t("Automático  (recomendado)", "Automatic  (recommended)"),
             t("Lo hace todo solo, intento tras intento: semilla, espera, combate, captura, ficha, calibración y "
               "reinicio del juego. Si sale shiny se para y te avisa para que lo captures tú.",
               "Does everything by itself, attempt after attempt: seed, wait, battle, capture, Summary, calibration "
               "and game restart. If a shiny appears, it stops and lets you know so you can catch it yourself.")),
            (Mode.ASSISTED, t("Asistido", "Assisted"),
             t("Igual, pero antes de cada intento te enseña el plan para que lo apruebes.",
               "The same, but before each attempt it shows you the plan for approval.")),
            (Mode.DRY_RUN, t("Ensayo (no pulsa nada)", "Dry run (presses nothing)"),
             t("Comprueba que detecta los parpadeos, encuentra el estado y calcula un plan, sin tocar el juego.",
               "Checks that blinks are detected, finds the state and calculates a plan, without touching the game.")),
        ]
        for i, (mode, title, desc) in enumerate(options):
            rb = QRadioButton(title)
            rb.setProperty("mode", mode.value)
            self.mode_group.addButton(rb, i)
            ml.addWidget(rb)
            d = label(desc, "muted")
            d.setContentsMargins(28, 0, 0, 6)
            ml.addWidget(d)
            if mode is state.selection.mode:
                rb.setChecked(True)
        self.mode_group.buttonClicked.connect(self._criteria_changed)
        ml.addStretch(1)
        self.body.addWidget(hbox(crit_card, card(modes, title=t("Modo", "Mode")), spacing=16))

        # --- Avance rápido ---
        self.fast = QCheckBox(t("Avance rápido con la Pokédex (recomendado)", "Fast advance with the Pokédex (recommended)"))
        self.fast.setChecked(state.fast_advance_enabled)
        self.fast.toggled.connect(state.set_fast_advance)
        fast_help = label(t(
            "Si falta mucho para el objetivo, el programa abre la Pokédex y la recorre (más o menos según lo que "
            "falte) para adelantar el RNG, y después reidentifica el estado con unos 7 parpadeos. Así una espera "
            "de horas se queda en minutos. Llega solo a la Pokédex (pasando por el mapa), esté donde esté el "
            "cursor del menú. La primera vez te preguntará si ve bien la Pokédex.",
            "If the target is far away, the program opens the Pokédex and scrolls through it (more or less depending "
            "on how far it is) to advance the RNG, then re-identifies the state with about 7 blinks. That turns a "
            "wait of hours into minutes. It reaches the Pokédex by itself (via the map), wherever the menu cursor "
            "is. The first time, it will ask you whether it sees the Pokédex correctly."), "muted")
        self.body.addWidget(card(self.fast, fast_help, title=t("Avance rápido", "Fast advance")))

        # --- Avanzado ---
        adv = QGroupBox(t("Opciones avanzadas (normalmente no hace falta tocarlas)",
                          "Advanced options (usually no need to change them)"))
        adv.setCheckable(True)
        adv.setChecked(False)
        al = QGridLayout(adv)
        self.tid = QSpinBox()
        self.tid.setRange(0, 65535)
        self.tid.setValue(state.prefs.tid)
        self.sid = QSpinBox()
        self.sid.setRange(0, 65535)
        self.sid.setValue(state.prefs.sid)
        self.lead = QDoubleSpinBox()
        self.lead.setRange(0.0, 1.0)
        self.lead.setSingleStep(0.005)
        self.lead.setDecimals(3)
        self.lead.setSuffix(" s")
        self.lead.setValue(state.prefs.lead_s)
        self.tid.valueChanged.connect(self._ids_changed)
        self.sid.valueChanged.connect(self._ids_changed)
        self.lead.valueChanged.connect(self._lead_changed)
        al.addWidget(QLabel("TID (16 bits)"), 0, 0)
        al.addWidget(self.tid, 0, 1)
        al.addWidget(QLabel("SID (16 bits)"), 1, 0)
        al.addWidget(self.sid, 1, 1)
        al.addWidget(QLabel(t("Adelanto de pulsación (latencia)", "Press lead time (latency)")), 2, 0)
        al.addWidget(self.lead, 2, 1)
        al.addWidget(label(t("TID/SID no influyen en si sale shiny en BDSP; solo cambian el PID.",
                             "In BDSP, TID/SID do not affect whether it is shiny; they only change the PID."),
                           "muted"), 3, 0, 1, 2)
        relearn = QPushButton(t("Volver a aprender la pantalla de la Pokédex", "Relearn the Pokédex screen"))
        relearn.setToolTip(t("Úsalo si el avance rápido no reconoce la Pokédex (p. ej. cambiaste de capturadora)",
                             "Use this if fast advance does not recognise the Pokédex (e.g. you changed capture card)"))
        relearn.clicked.connect(self._forget_pokedex)
        al.addWidget(relearn, 4, 0, 1, 2)
        self.body.addWidget(adv)
        self.body.addStretch(1)

        self._bg = Background()
        self._bg.done.connect(self._show_rarity)
        self._bg.failed.connect(self._rarity_failed)
        from PySide6.QtCore import QTimer

        # Retrasa la estimación hasta que se dejan de cambiar criterios (cada una tarda en calcularse).
        self._rarity_timer = QTimer(self)
        self._rarity_timer.setSingleShot(True)
        self._rarity_timer.timeout.connect(self._estimate_rarity)
        self._estimating = False
        # Selección inicial: el último encuentro usado o, si no, el primero elegible.
        if state.prefs.last_encounter:
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) == state.prefs.last_encounter:
                    self.list.setCurrentRow(i)
        if self.list.currentItem() is None or self.list.currentItem().data(Qt.ItemDataRole.UserRole) is None:
            for i in range(self.list.count()):  # los comprobados van primero en la lista
                if self.list.item(i).data(Qt.ItemDataRole.UserRole):
                    self.list.setCurrentRow(i)
                    break
        self._criteria_changed()

    # --- lógica ---
    def _forget_pokedex(self) -> None:
        """Borra la firma aprendida de la pantalla de la Pokédex para que el avance rápido la vuelva a aprender."""
        from rngauto.vision.screen import ScreenSignatures

        ScreenSignatures(self.state.data_dir / "screens").forget("pokedex")
        QMessageBox.information(self, t("Avance rápido", "Fast advance"),
                                t("Hecho: la próxima vez que use la Pokédex te preguntará si "
                                  "la ve bien y la volverá a aprender.",
                                  "Done: the next time it uses the Pokédex, it will ask whether it sees it "
                                  "correctly and learn it again."))

    def _ids_changed(self) -> None:
        self.state.prefs.tid, self.state.prefs.sid = self.tid.value(), self.sid.value()
        self.state.save_prefs()

    def _lead_changed(self) -> None:
        self.state.prefs.lead_s = self.lead.value()
        self.state.save_prefs()

    def on_enter(self) -> None:
        # Se resincroniza con las preferencias sin emitir señales, para no volver a guardarlas.
        self.lead.blockSignals(True)
        self.lead.setValue(self.state.prefs.lead_s)
        self.lead.blockSignals(False)

    def _on_pick(self, item: QListWidgetItem | None) -> None:
        enc_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.state.selection.encounter = self.state.encounters.get(enc_id) if enc_id else None
        if enc_id:
            self.state.prefs.last_encounter = enc_id
        self._criteria_changed()

    def _preset_changed(self) -> None:
        """Aplica el preset de IVs a las casillas (sin disparar un recálculo por cada una) y recalcula una vez."""
        preset = IV_PRESETS[self.preset.currentData()]
        self.iv_box.setVisible(preset == "custom")
        if isinstance(preset, tuple):
            for sb, v in zip(self.ivs, preset):
                sb.blockSignals(True)
                sb.setValue(v)
                sb.blockSignals(False)
        elif preset is None:
            for sb in self.ivs:
                sb.blockSignals(True)
                sb.setValue(0)
                sb.blockSignals(False)
        self._criteria_changed()

    def criteria(self) -> TargetCriteria:
        """Criterios del objetivo según el formulario (los IVs son mínimos: cada uno entre el valor y 31)."""
        nature = self.nature.currentData()
        return TargetCriteria(
            shiny=ShinyFilter.SHINY if self.shiny.isChecked() else ShinyFilter.ANY,
            natures=frozenset({nature}) if nature is not None else None,
            ivs=tuple(IVRange(sb.value(), 31) for sb in self.ivs),
        )

    def _criteria_changed(self, *_args) -> None:
        """Vuelca criterios y modo a la selección, avisa a la ventana y programa la estimación de rareza."""
        sel = self.state.selection
        sel.criteria = self.criteria()
        checked = self.mode_group.checkedButton()
        if checked is not None:
            sel.mode = Mode(checked.property("mode"))
        self.state.changed.emit()
        self.completion_changed.emit()
        enc = sel.encounter
        if enc is None:
            self.rarity.setText("")
            return
        self.rarity.setText(t("Calculando lo raro que es este objetivo…", "Estimating how rare this target is…"))
        self._rarity_timer.start(500)

    def _estimate_rarity(self) -> None:
        """Cuenta en segundo plano cuántos advances de una muestra cumplen los criterios.

        Se usa un estado RNG pseudoaleatorio con semilla fija (resultado estable) y 40 000 advances. Si ya hay
        una estimación en curso, se reprograma en lugar de lanzar otra en paralelo.
        """
        sel = self.state.selection
        if sel.encounter is None:
            return
        if self._estimating:
            self._rarity_timer.start(500)
            return
        self._estimating = True
        criteria, template = sel.criteria, static_template(sel.encounter.template)

        def estimate():
            finder = StaticTargetFinder(template)
            rnd = random.Random(1)
            state = XorshiftState(*(rnd.getrandbits(32) for _ in range(4)))
            span = 40000
            return len(finder.search(state, criteria, 0, span, limit=10_000)), span

        self._bg.run(estimate, timeout_s=60.0, timeout_message="timeout")

    def _rarity_failed(self, _message: str) -> None:
        """La estimación es solo orientativa: si falla, se dice y se puede seguir."""
        self._estimating = False
        self.rarity.setText(t("No se pudo estimar lo raro que es este objetivo.",
                              "Could not estimate how rare this target is."))

    def _show_rarity(self, result) -> None:
        self._estimating = False
        hits, span = result
        if hits == 0:
            self.rarity.setText(f'<span style="color:{theme.WARNING}">'
                                + t("Objetivo muy raro: ninguno en {span} advances "
                                    "de prueba. Puede tardar mucho; considera relajar los criterios.",
                                    "Very rare target: none in {span} test advances. It may take a long time; "
                                    "consider relaxing the criteria.", span=fmt_int(span))
                                + "</span>")
            return
        every = span / hits
        wait = fmt_duration(every * 1.02)  # en la escena de parpadeos se asume ~1,02 s por advance
        count = fmt_int(every)
        self.rarity.setText(t("Aproximadamente 1 de cada {count} advances cumple estos criterios: espera típica "
                              "≈ {wait} en la escena (el programa espera solo).",
                              "About 1 in {count} advances meets these criteria: typical wait "
                              "≈ {wait} in the scene (the program waits by itself).", count=count, wait=wait)
                            if every > 1 else
                            t("Casi cualquier advance cumple estos criterios.",
                              "Almost any advance meets these criteria."))

    def is_complete(self) -> bool:
        return self.state.selection.encounter is not None

    def missing(self) -> str:
        return t("Elige un Pokémon de la lista", "Choose a Pokémon from the list")
