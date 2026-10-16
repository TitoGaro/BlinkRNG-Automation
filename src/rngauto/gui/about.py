"""Ventana «Créditos»: autor, agradecimientos, licencia y avisos de marcas.

Se abre con el botón «Créditos» de la barra lateral. El texto se genera al abrirla, en el idioma activo.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from rngauto import __author__, __version__
from rngauto.i18n import t

from . import theme

PA_URL = "https://github.com/PokemonAutomation"
PA_GUIDES_URL = "https://pokemonautomation.github.io/ControllerList.html"
POKEFINDER_URL = "https://github.com/Admiral-Fish/PokeFinder"
PROJECT_XS_URL = "https://github.com/niart120/Project_Xs"
PROJECT_XS_FORK_URL = "https://github.com/Lincoln-LM/Project_Xs"
PAPAJEFE_URL = "https://www.youtube.com/@PapaJefeYT"
SUPPORT_URL = "https://buymeacoffee.com/titogaro"  # donaciones voluntarias al autor


def _link(url: str, text: str) -> str:
    """Enlace HTML con el color de acento del tema en lugar del azul por defecto de Qt."""
    return f'<a href="{url}" style="color:{theme.ACCENT}; text-decoration:none;">{text}</a>'


def credits_html() -> str:
    """Texto de la ventana en el idioma actual (HTML sencillo de Qt)."""
    items = [
        t("{pa}, Alexander J. Yee y todo el equipo y la comunidad de Pokémon Automation: el firmware y el "
          "protocolo PABotBase2, sus {guides} y las ideas de la detección de pantallas y de shinies.",
          "{pa}, Alexander J. Yee and the whole Pokémon Automation team and community: the PABotBase2 firmware "
          "and protocol, their {guides} and the ideas behind the screen and shiny detection.",
          pa=_link(PA_URL, "<b>Pokémon Automation</b>"),
          guides=_link(PA_GUIDES_URL, t("guías de montaje", "hardware guides"))),
        t("{pf}, de Admiral-Fish y sus colaboradores: la referencia de cómo genera BDSP los encuentros estáticos.",
          "{pf}, by Admiral-Fish and its contributors: the reference for how BDSP generates static encounters.",
          pf=_link(POKEFINDER_URL, "<b>PokeFinder</b>")),
        t("<b>Project_Xs</b>, de {niart} y la {fork}: el estado del RNG a partir de los parpadeos.",
          "<b>Project_Xs</b>, by {niart}, and the {fork}: the RNG state from your character's blinks.",
          niart=_link(PROJECT_XS_URL, "niart120"),
          fork=_link(PROJECT_XS_FORK_URL, t("versión de Lincoln-LM", "Lincoln-LM version"))),
        t("{pj}, por sus vídeos de RNG de Pokémon.", "{pj}, for his Pokémon RNG videos.",
          pj=_link(PAPAJEFE_URL, "<b>PapaJefe</b>")),
    ]
    rows = "".join(f"<li style='margin-bottom:6px;'>{item}</li>" for item in items)
    return (
        f"<p style='font-size:13pt; font-weight:600; margin:0;'>RNG Automation {__version__}</p>"
        f"<p style='color:{theme.MUTED}; margin-top:2px;'>"
        + t("Hecho por <b>{author}</b>", "Made by <b>{author}</b>", author=__author__) + "</p>"
        "<p style='margin-top:8px;'>"
        + t("¿Te ha servido? {link}", "Has it helped you? {link}",
            link=_link(SUPPORT_URL, t("Invítame a un café", "Buy me a coffee")))
        + "</p>"
        f"<p style='margin-top:14px;'><b>{t('Agradecimientos', 'Acknowledgements')}</b></p>"
        f"<ul style='margin-left:-18px;'>{rows}</ul>"
        f"<p style='color:{theme.MUTED};'>"
        + t("Software libre con licencia GPL-3.0-or-later, sin ninguna garantía. Las licencias de terceros están "
            "en THIRD_PARTY_NOTICES.md.",
            "Free software under the GPL-3.0-or-later license, with no warranty. Third-party licenses are in "
            "THIRD_PARTY_NOTICES.md.")
        + "</p>"
        f"<p style='color:{theme.FAINT}; font-size:8.5pt;'>"
        + t("Pokémon y Nintendo Switch son marcas de Nintendo, Creatures Inc. y GAME FREAK inc. Proyecto no "
            "oficial de aficionados, sin relación con ellos.",
            "Pokémon and Nintendo Switch are trademarks of Nintendo, Creatures Inc. and GAME FREAK inc. "
            "Unofficial fan project, not affiliated with them.")
        + "</p>"
    )


class AboutDialog(QDialog):
    """Diálogo de créditos: texto enriquecido cuyos enlaces se abren en el navegador y un botón «Cerrar»."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Créditos", "Credits"))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        text = QLabel(credits_html())
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        text.setOpenExternalLinks(True)
        lay.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(t("Cerrar", "Close"))
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
