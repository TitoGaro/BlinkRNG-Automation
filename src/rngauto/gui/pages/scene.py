"""Paso 3 — Escena: colocarse en la escena de parpadeos y marcar el ojo con el ratón.

El usuario congela un fotograma, arrastra un rectángulo ajustado al ojo abierto y lo guarda. Se guarda el
recorte como plantilla y una zona de búsqueda más amplia (×2,5) en la que se buscará el ojo en directo.
Algunos encuentros necesitan un segundo ojo, delante del legendario, para reidentificar el estado allí.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QComboBox, QLabel, QPushButton

from rngauto.app.setup_store import load_eye_setup, save_eye_setup
from rngauto.i18n import t
from rngauto.vision.frames import NormBox

from .. import theme
from ..widgets import VideoView, bgr_to_qimage, card, hbox, label
from .base import Page


class ScenePage(Page):
    """Paso «Escena»: instrucciones de colocación, preparación de la partida y marcado del ojo (o los ojos)."""

    @property
    def step_title(self) -> str:
        return t("Escena", "Scene")

    @property
    def step_hint(self) -> str:
        return t("Marca el ojo de tu personaje", "Mark your character's eye")

    def __init__(self, state) -> None:
        super().__init__(state, t("Prepara la escena y marca el ojo", "Set up the scene and mark the eye"),
                         t("El programa observa el ojo de tu personaje para leer el RNG. Solo hay que decirle dónde está.",
                           "The program watches your character's eye to read the RNG. You only need to tell it "
                           "where it is."))
        self.instructions = label("", None)
        self.where_card = card(self.instructions, title=t("Dónde ponerte", "Where to stand"))
        self.body.addWidget(self.where_card)

        # Preparación de la partida: solo en encuentros cuya escena de parpadeos es el mensaje de Serbal (con la caña).
        self.prep_text = label(t(
            "Haz esto una sola vez en el juego antes de empezar:<br>"
            "• Equipo de <b>5 Pokémon</b> (el capturado entrará en el 6.º hueco) y el <b>primero no shiny</b>.<br>"
            "• Al menos <b>una Master Ball</b> en la Bolsa (la busca por su icono, esté donde esté).<br>"
            "• En Opciones: <b>«Poner mote»</b> desactivado y <b>velocidad del texto rápida</b>.<br>"
            "• Mejor que nadie del equipo vaya a aprender un movimiento ni a evolucionar al capturar (si pasa, "
            "elige «Conservar movimientos», pero tarda más).<br>"
            "• La <b>caña registrada</b> en la rueda de «+».<br>"
            "• La <b>partida guardada</b> en el punto de inicio, mirando a la cámara (el programa nunca guarda).<br>"
            "• En la consola, tu perfil de usuario debe ser el único o el <b>primero de la izquierda</b>.<br>"
            "Después usa la caña para que salga el mensaje del profesor Serbal y marca el ojo.",
            "Do this once in the game before you start:<br>"
            "• A party of <b>5 Pokémon</b> (the caught one goes into the 6th slot), with a <b>non-shiny first one</b>.<br>"
            "• At least <b>one Master Ball</b> in the Bag (it finds it by its icon, wherever it is).<br>"
            "• In Options: <b>Give Nicknames</b> turned off and <b>text speed set to Fast</b>.<br>"
            "• Ideally, no party member should learn a move or evolve when you catch it (if it happens, "
            "choose to keep the current moves, but it takes longer).<br>"
            "• The <b>fishing rod registered</b> in the \"+\" wheel.<br>"
            "• The <b>game saved</b> at the starting point, facing the camera (the program never saves).<br>"
            "• On the console, your user profile must be the only one or the <b>first on the left</b>.<br>"
            "Then use the rod to bring up Professor Rowan's message and mark the eye."), None)
        self.item_dir = QComboBox()
        for key, text in state.ITEM_DIRECTIONS.items():
            self.item_dir.addItem(text, key)
        self.item_dir.currentIndexChanged.connect(self._item_dir_changed)
        self.prep_card = card(self.prep_text,
                              hbox(QLabel(t("¿Dónde está la caña en la rueda de «+»?",
                                            "Where is the rod in the \"+\" wheel?")), self.item_dir, None),
                              title=t("Preparación de la partida", "Game setup"))
        self.body.addWidget(self.prep_card)

        # Encuentros con reidentificación delante del legendario: dos ojos, uno en cada sitio.
        self.eye_choice = QComboBox()
        self.eye_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.eye_choice.currentIndexChanged.connect(self._eye_choice_changed)
        self.eye_choice_row = card(hbox(QLabel(t("Este encuentro necesita <b>dos ojos</b>. ¿Cuál vas a marcar?",
                                                 "This encounter needs <b>two eyes</b>. Which one will you mark?")),
                                        self.eye_choice, None), title=t("Ojos", "Eyes"))
        # Antes de «Dónde ponerte»: de qué ojo se marque dependen las instrucciones.
        self.body.insertWidget(self.body.indexOf(self.where_card), self.eye_choice_row)

        self.video = VideoView(min_height=340)
        self.video.roi_selected.connect(self._on_roi)
        self.freeze_btn = QPushButton(t("Congelar imagen para marcar el ojo", "Freeze frame to mark the eye"))
        self.freeze_btn.setObjectName("primary")
        self.freeze_btn.clicked.connect(self._freeze)
        self.live_btn = QPushButton(t("Volver al vídeo en directo", "Back to live video"))
        self.live_btn.clicked.connect(self._live)
        self.save_btn = QPushButton(t("Guardar este ojo", "Save this eye"))
        self.save_btn.setObjectName("primary")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        self.crop = QLabel()
        self.crop.setFixedSize(120, 120)
        self.crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop.setStyleSheet(f"background:{theme.SURFACE_2}; border-radius:8px;")
        self.status = label("", "muted")
        self.body.addWidget(card(hbox(self.freeze_btn, self.live_btn, None, self.crop, self.save_btn),
                                 self.status, self.video, title=t("Vídeo", "Video")))
        self.body.addStretch(1)

        self._frozen = None  # fotograma congelado sobre el que se marca el ojo
        self._roi: NormBox | None = None  # rectángulo marcado, pendiente de guardar

    def _item_dir_changed(self) -> None:
        self.state.set_item_direction(self.item_dir.currentData() or "")
        self.completion_changed.emit()

    def _needs_item(self) -> bool:
        """El encuentro usa la caña registrada para abrir el mensaje: hay que saber su dirección en la rueda."""
        enc = self.state.selection.encounter
        return enc is not None and enc.blink_scene.message

    def _eye_id(self) -> str:
        """Identificador del ojo que se está marcando: el elegido en el desplegable o el del encuentro."""
        enc = self.state.selection.encounter
        return self.eye_choice.currentData() or (enc.id if enc is not None else "")

    def _eye_choice_changed(self) -> None:
        if self.isVisible():
            self.on_enter(keep_choice=True)

    def on_enter(self, keep_choice: bool = False) -> None:
        """Rehace las instrucciones para el encuentro y el ojo elegidos y vuelve al vídeo en directo.

        Con ``keep_choice`` no se reconstruye el desplegable de ojos (se llama al cambiar de ojo).
        """
        self.state.forget_eye_cache()  # el ojo pudo marcarse en otra visita: mirar los ficheros
        enc = self.state.selection.encounter
        self.prep_card.setVisible(self._needs_item())
        if not keep_choice:
            self.eye_choice.blockSignals(True)
            self.eye_choice.clear()
            if enc is not None and enc.extra_eyes:
                self.eye_choice.addItem(t("1. En el punto de inicio (donde guardas la partida)",
                                          "1. At the starting point (where you save the game)"), enc.id)
                for i, eye in enumerate(enc.extra_eyes, start=2):
                    self.eye_choice.addItem(t("{i}. Delante del legendario (para reidentificar allí)",
                                              "{i}. In front of the legendary (to re-identify there)", i=i), eye)
                # Se preselecciona el primer ojo que falte por marcar.
                missing = next((i for i in range(self.eye_choice.count())
                                if load_eye_setup(self.state.data_dir, self.eye_choice.itemData(i)) is None), 0)
                self.eye_choice.setCurrentIndex(missing)
            self.eye_choice.blockSignals(False)
        self.eye_choice_row.setVisible(self.eye_choice.count() > 0)
        idx = self.item_dir.findData(self.state.item_direction)
        self.item_dir.blockSignals(True)
        self.item_dir.setCurrentIndex(max(0, idx))
        self.item_dir.blockSignals(False)
        s = self.state.session
        if enc is None:
            return
        start = enc.start
        second = self._eye_id() != enc.id
        steps = [
            f"<b>{start.name or enc.display_name}</b>",
            start.description,
            start.instructions,
        ] if not second else [
            t("<b>Delante del legendario</b> (ojo para reidentificar allí: sus parpadeos gastan RNG y el programa "
              "vuelve a situarse delante de él antes de hablarle)",
              "<b>In front of the legendary</b> (eye for re-identifying there: its blinks consume RNG and the "
              "program re-syncs in front of it before talking to it)"),
            enc.approach_eye_instructions or t(
                "Sin guardar la partida, ve hasta el legendario, ponte justo delante "
                "mirando hacia la cámara y usa la caña para que salga el mensaje del profesor Serbal.",
                "Without saving, walk up to the legendary, stand right in front of it facing the camera and use "
                "the rod to bring up Professor Rowan's message."),
            t("Cuando lo guardes, <b>vuelve a cargar la partida</b> (o regresa al punto de inicio): la partida tiene que "
              "seguir guardada en el punto de inicio.",
              "Once you have saved it, <b>reload the game</b> (or go back to the starting point): the game must "
              "remain saved at the starting point."),
        ]
        steps += [
            t("Cuando estés quieto en esa posición, pulsa <b>Congelar imagen</b>, arrastra un rectángulo "
              "<b>ajustado al ojo abierto</b> de tu personaje y pulsa <b>Guardar este ojo</b>.",
              "While standing still in that position, click <b>Freeze frame</b>, drag a rectangle "
              "<b>tight around the open eye</b> of your character and click <b>Save this eye</b>."),
            t("Si el ojo se ve muy pequeño, puedes activar el zoom de la Switch (pulsa HOME dos veces si lo "
              "tienes activado en Ajustes → Sistema → Zoom).",
              "If the eye looks very small, you can use the Switch's zoom (press HOME twice if it is "
              "enabled in System Settings → System → Zoom)."),
        ]
        self.instructions.setText("<br>".join(x for x in steps if x))
        if s is None:  # sin capturadora: las instrucciones sí, el vídeo no
            return
        if s.demo:  # sesión simulada (tests): el ojo viene dado por la escena sintética
            self.instructions.setText(t("<b>Modo demo:</b> la escena simulada ya está preparada; el ojo se ha "
                                        "configurado automáticamente. Puedes continuar.",
                                        "<b>Demo mode:</b> the simulated scene is already set up; the eye has been "
                                        "configured automatically. You can continue."))
            self.video.set_box(s.sim.scene.eye_box)
        else:
            setup = load_eye_setup(self.state.data_dir, self._eye_id())
            if setup is not None:
                self.video.set_box(setup.norm_box)
                self.status.setText(f'<span style="color:{theme.SUCCESS}">'
                                    + t("Ya tienes este ojo guardado ({created}). Puedes continuar o volver a marcarlo.",
                                        "This eye is already saved ({created}). You can continue or mark it again.",
                                        created=setup.created)
                                    + "</span>")
            else:
                self.status.setText(t("Este ojo aún no está marcado.", "This eye has not been marked yet."))
        self._live()

    def on_leave(self) -> None:
        self.video.attach(None)

    def _source(self):
        """Fuente del vídeo en directo para ``VideoView.attach``."""
        s = self.state.session
        return s.latest() if s is not None else None

    def _live(self) -> None:
        """Vuelve al vídeo en directo y desactiva la selección."""
        self._frozen = None
        self.video.enable_selection(False)
        self.video.attach(self._source)

    def _freeze(self) -> None:
        """Congela el último fotograma para poder marcar el ojo sin que se mueva."""
        s = self.state.session
        frame = s.latest() if s is not None else None
        if frame is None:
            self.status.setText(t("Aún no llega imagen de la capturadora.", "No picture from the capture card yet."))
            return
        self._frozen = frame
        self.video.attach(None)
        self.video.set_frame(frame.image)
        self.video.enable_selection(True)
        self.status.setText(t("Arrastra un rectángulo ajustado al ojo abierto.",
                              "Drag a rectangle tight around the open eye."))

    def _on_roi(self, box: NormBox) -> None:
        """Rectángulo marcado: muestra el recorte, avisa si es muy pequeño y habilita «Guardar este ojo»."""
        if self._frozen is None:
            return
        self._roi = box
        img = self._frozen.image
        x, y, w, h = box.to_pixels(img.shape[1], img.shape[0])
        crop = img[y:y + h, x:x + w]
        pix = QPixmap.fromImage(bgr_to_qimage(crop)).scaled(116, 116, Qt.AspectRatioMode.KeepAspectRatio)
        self.crop.setPixmap(pix)
        self.video.set_box(box, theme.ACCENT)
        self.save_btn.setEnabled(True)
        warn = t(" (muy pequeño: si la detección falla, prueba con zoom)",
                 " (very small: if detection fails, try zoom)") if min(w, h) < 12 else ""
        self.status.setText(t("Ojo de {w}×{h} píxeles{warn}. Si es correcto, pulsa «Guardar este ojo».",
                              "Eye of {w}×{h} pixels{warn}. If it looks right, click \"Save this eye\".",
                              w=w, h=h, warn=warn))

    def _save(self) -> None:
        """Guarda la plantilla del ojo y su zona de búsqueda, e indica si falta otro ojo por marcar."""
        enc = self.state.selection.encounter
        if enc is None or self._frozen is None or self._roi is None:
            return
        img = self._frozen.image
        x, y, w, h = self._roi.to_pixels(img.shape[1], img.shape[0])
        # Plantilla = recorte exacto; zona de búsqueda = el rectángulo ampliado, para tolerar pequeños movimientos.
        # La altura del fotograma sirve para reescalar si luego cambia la resolución de captura.
        try:
            save_eye_setup(self.state.data_dir, self._eye_id(), img[y:y + h, x:x + w].copy(), self._roi.expanded(2.5),
                           img.shape[0])
            self.state.forget_eye_cache()
        except OSError as exc:  # disco lleno, carpeta sin permisos…: se dice aquí y se puede reintentar
            self.status.setText(f'<span style="color:{theme.DANGER}">'
                                + t("No se pudo guardar el ojo: {error}", "Could not save the eye: {error}", error=exc)
                                + "</span>")
            return
        self.video.set_box(self._roi.expanded(2.5))
        self.save_btn.setEnabled(False)
        pending = [self.eye_choice.itemText(i) for i in range(self.eye_choice.count())
                   if load_eye_setup(self.state.data_dir, self.eye_choice.itemData(i)) is None]
        if pending:
            self.status.setText(f'<span style="color:{theme.SUCCESS}">' + t("Ojo guardado.", "Eye saved.") + "</span> "
                                + t("Falta otro: «{name}». Elígelo arriba y márcalo igual.",
                                    "One more is needed: \"{name}\". Choose it above and mark it the same way.",
                                    name=pending[0]))
        else:
            self.status.setText(f'<span style="color:{theme.SUCCESS}">'
                                + t("Ojo guardado. Continúa al siguiente paso "
                                    "para comprobar que se detectan los parpadeos.",
                                    "Eye saved. Continue to the next step to check that blinks are detected.")
                                + "</span>")
        self._live()
        self.state.changed.emit()
        self.completion_changed.emit()

    def is_complete(self) -> bool:
        return self.state.eye_ready and (not self._needs_item() or bool(self.state.item_direction))

    def missing(self) -> str:
        if self.state.eye_ready and self._needs_item() and not self.state.item_direction:
            return t("Indica en qué dirección de la rueda de «+» tienes la caña",
                     "Indicate which direction of the \"+\" wheel the rod is on")
        enc = self.state.selection.encounter
        if enc is not None and enc.extra_eyes and load_eye_setup(self.state.data_dir, enc.id) is not None:
            return t("Marca también el ojo delante del legendario (elígelo en «¿Cuál vas a marcar?»)",
                     "Also mark the eye in front of the legendary (choose it under \"Which one will you mark?\")")
        return t("Marca el ojo de tu personaje y pulsa «Guardar este ojo»",
                 "Mark your character's eye and click \"Save this eye\"")
