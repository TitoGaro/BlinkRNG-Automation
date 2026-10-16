"""Informe de diagnóstico («Informar de un problema») y limpieza de los vídeos del ojo."""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rngauto.app import diagnostics  # noqa: E402
from rngauto.app.diagnostics import anonymize, build_report, issue_url, timeline_tail  # noqa: E402
from rngauto.app.session import prune_eye_videos  # noqa: E402


def _session(data_dir: Path, name: str, events: list[dict]) -> Path:
    folder = data_dir / "sessions" / name
    folder.mkdir(parents=True)
    (folder / "timeline.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return folder


def test_personal_data_is_removed(monkeypatch):
    monkeypatch.setattr(diagnostics.getpass, "getuser", lambda: "jose")
    home = str(Path.home())
    text = anonymize(f"Error en {home}\\.rngauto\\logs y {home.replace(chr(92), '/')}/x; jose@ejemplo.com; Jose")
    assert home not in text and "jose" not in text.lower()
    assert "%USERPROFILE%" in text and "<email>" in text and "<usuario>" in text


def test_report_has_environment_events_and_errors(tmp_path):
    _session(tmp_path, "20261001-100000-dialga", [{"t_s": 1.0, "kind": "state", "message": "antigua"}])
    _session(tmp_path, "20261016-120000-cresselia", [
        {"t_s": 5.0, "kind": "state", "message": "→ COLLECT"},
        {"t_s": 6.0, "kind": "recovery", "message": "Aún sin solución", "data": {"progress": True}},
        {"t_s": 7.0, "kind": "error", "message": "No veo el ojo"},
    ])
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "errores.log").write_text("Traceback: ValueError", encoding="utf-8")
    report = build_report(tmp_path, {"Encuentro": "cresselia", "Mando": ""})
    assert "### Entorno" in report and "Encuentro:** cresselia" in report and "Mando" not in report
    assert "20261016-120000-cresselia" in report and "No veo el ojo" in report
    assert "Aún sin solución" not in report  # los avisos de progreso no aportan
    assert "Traceback: ValueError" in report


def test_timeline_tail_survives_damaged_lines(tmp_path):
    path = tmp_path / "timeline.jsonl"
    path.write_text('{"t_s": 1, "kind": "state", "message": "ok"}\n{ roto\n[1, 2]\n', encoding="utf-8")
    assert len(timeline_tail(path)) == 1


def test_long_reports_keep_the_environment_and_fit_in_the_url(tmp_path):
    report = "### Entorno\n- **Versión:** 1.0.0\n\n### Últimos eventos\n" + "\n".join(f"evento {i:05d} " + "x" * 60
                                                                                   for i in range(400))
    url, complete = issue_url(report, "cresselia", "Mando: ESP32")
    assert len(url) <= diagnostics.MAX_URL_CHARS and not complete
    fields = parse_qs(urlparse(url).query)
    assert fields["template"] == ["bug_report.yml"] and fields["encounter"] == ["cresselia"]
    text = fields["diagnostic"][0]
    assert text.startswith("### Entorno") and "evento 00399" in text and "evento 00000" not in text
    short_url, short_complete = issue_url("### Entorno\n- ok")
    assert short_complete and "Entorno" in parse_qs(urlparse(short_url).query)["diagnostic"][0]


def test_old_eye_videos_are_pruned_but_logs_are_kept(tmp_path):
    for i in range(7):
        folder = tmp_path / f"2026100{i}-100000-dialga"
        folder.mkdir()
        (folder / "ojo_01.avi").write_bytes(b"x" * 100)
        (folder / "ojo_01.frames.jsonl").write_text("{}", encoding="utf-8")
        (folder / "timeline.jsonl").write_text("{}", encoding="utf-8")
    freed = prune_eye_videos(tmp_path, keep=5)
    assert freed == 2 * (100 + 2)
    folders = sorted(tmp_path.iterdir())
    assert [bool(list(f.glob("ojo_*"))) for f in folders] == [False, False, True, True, True, True, True]
    assert all((f / "timeline.jsonl").exists() for f in folders)
    assert prune_eye_videos(tmp_path / "no_existe") == 0


def test_report_dialog_builds_without_hardware(tmp_path):
    from PySide6.QtWidgets import QApplication

    from rngauto.gui.report import ReportDialog
    from rngauto.gui.state import AppState

    app = QApplication.instance() or QApplication([])
    assert app is not None
    state = AppState(tmp_path)
    state.auto_hardware = False
    dialog = ReportDialog(None, state)
    text = dialog.text.toPlainText()
    assert "### Entorno" in text and "no conectado" in text and str(Path.home()) not in text
    state.close()


def test_a_huge_header_does_not_hang_the_url_builder():
    """Si el usuario escribe un texto enorme al principio del informe, el enlace se recorta y no se cuelga."""
    url, complete = issue_url("### Entorno\n" + "x" * 20000 + "\n\n### Últimos eventos\n```text\n```")
    assert len(url) <= diagnostics.MAX_URL_CHARS and not complete


def test_cut_jsonl_is_readable(tmp_path):
    path = tmp_path / "timeline.jsonl"
    path.write_bytes('{"t_s": 1, "kind": "state", "message": "ok"}\n{"message": "medio car'.encode() + "á".encode()[:1])
    assert len(timeline_tail(path)) == 1  # un corte a mitad de un carácter no rompe la lectura
