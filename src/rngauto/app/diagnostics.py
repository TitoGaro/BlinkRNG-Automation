"""Informe de diagnóstico para pedir ayuda o depurar un fallo: versión, sistema, hardware, encuentro, últimos
eventos de la ejecución y final de ``errores.log``.

Se anonimiza (la carpeta y el nombre del usuario de Windows y los emails no salen) y el usuario lo ve y lo puede
editar antes de enviarlo. Nada se envía solo: se abre GitHub en el navegador con el formulario rellenado.
"""
from __future__ import annotations

import getpass
import json
import platform
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

from rngauto import __version__

ISSUES_URL = "https://github.com/TitoGaro/BlinkRNG-Automation/issues/new"
MAX_URL_CHARS = 7500  # GitHub rechaza direcciones mucho más largas: el resto se copia y se pega a mano
TIMELINE_LINES = 80
ERROR_LOG_CHARS = 3000


def anonymize(text: str) -> str:
    """Quita lo que identifica a la persona: la carpeta de usuario, el nombre de usuario de Windows y emails."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)  # antes que el usuario: puede ir dentro del email
    home = str(Path.home())
    for variant in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
        text = text.replace(variant, "%USERPROFILE%")
    try:
        user = getpass.getuser()
    except Exception:  # sin variables de entorno de usuario: no hay nada que quitar
        user = ""
    if len(user) >= 3:
        text = re.sub(rf"(?i)\b{re.escape(user)}\b", "<usuario>", text)
    return text


def last_session(data_dir: Path) -> Path | None:
    """Carpeta de la ejecución más reciente que tenga registro (``sessions/<fecha>-<encuentro>``)."""
    sessions = data_dir / "sessions"
    if not sessions.is_dir():
        return None
    folders = sorted((p for p in sessions.iterdir() if (p / "timeline.jsonl").is_file()), key=lambda p: p.name)
    return folders[-1] if folders else None


def timeline_tail(path: Path, lines: int = TIMELINE_LINES) -> list[str]:
    """Últimos eventos del registro de una ejecución, en texto. Se omiten los avisos de progreso repetitivos
    («aún sin solución con N parpadeos»), que no aportan nada y ocupan mucho."""
    out: list[str] = []
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    last_shape, repeats = None, 0
    for line in raw:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or (event.get("data") or {}).get("progress"):
            continue
        kind = str(event.get("kind", ""))
        message = str(event.get("message", "")).replace("\n", " | ")
        # Eventos seguidos que solo cambian en los números (p. ej. el recuento de parpadeos): una línea y «×N».
        shape = (kind, re.sub(r"\d+", "#", message))
        if shape == last_shape and out:
            repeats += 1
            out[-1] = re.sub(r" \(×\d+\)$", "", out[-1]) + f" (×{repeats + 1})"
            continue
        last_shape, repeats = shape, 0
        out.append(f"{float(event.get('t_s', 0.0)):12.2f}  {kind:10} {message}")
    return out[-lines:]


def error_log_tail(log_dir: Path, chars: int = ERROR_LOG_CHARS) -> str:
    """Final de ``errores.log`` (los errores inesperados y sus trazas)."""
    try:
        text = (log_dir / "errores.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > chars:
        text = text[-chars:].split("\n", 1)[-1]  # empieza en una línea completa
    return text.strip()


def environment() -> dict[str, str]:
    """Versión de la aplicación, de Windows y de Python, y si se ejecuta el ejecutable o el código fuente."""
    return {
        "app": __version__,
        "windows": platform.platform(),
        "python": platform.python_version(),
        "build": "ejecutable" if getattr(sys, "frozen", False) else "código fuente",
    }


def build_report(data_dir: Path, context: dict[str, str]) -> str:
    """Informe en Markdown con el entorno, ``context`` (encuentro, modo, mando, capturadora…), los últimos eventos
    de la ejecución más reciente y el final de ``errores.log``. Ya anonimizado."""
    env = environment()
    rows = [f"- **Versión:** {env['app']} ({env['build']})", f"- **Windows:** {env['windows']}",
            f"- **Python:** {env['python']}"]
    rows += [f"- **{key}:** {value}" for key, value in context.items() if value]
    parts = ["### Entorno", *rows]
    session = last_session(data_dir)
    if session is not None:
        events = timeline_tail(session / "timeline.jsonl")
        if events:
            parts += ["", f"### Últimos eventos ({session.name})", "```text", *events, "```"]
    errors = error_log_tail(data_dir / "logs")
    if errors:
        parts += ["", "### errores.log (final)", "```text", errors, "```"]
    return anonymize("\n".join(parts))


def issue_url(report: str, encounter: str = "", setup: str = "") -> tuple[str, bool]:
    """Dirección del formulario de fallo de GitHub con los campos rellenados. Si el informe no cabe entero en la
    dirección, se conserva el entorno y se recorta lo más antiguo del resto, y devuelve ``(url, False)``: en ese
    caso hay que pegar el informe completo a mano."""
    base = {"template": "bug_report.yml", "version": __version__, "encounter": encounter, "setup": setup}
    head, sep, rest = report.partition("\n\n### ")
    rest = sep.lstrip("\n") + rest
    complete = True
    while True:
        text = head + ("\n\n" + rest if rest else "")
        url = f"{ISSUES_URL}?{urlencode({**base, 'diagnostic': text})}"
        if len(url) <= MAX_URL_CHARS or not rest:
            return url, complete
        complete = False
        cut = rest[len(rest) // 5:].split("\n", 1)
        rest = "(…)\n" + cut[-1] if len(cut) > 1 else ""  # quita el 20 % más antiguo, por líneas
