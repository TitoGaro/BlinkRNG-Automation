"""Autocomprobación del ejecutable (``--self-test``): la integración continua la usa antes de publicar."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rngauto.gui.main import main  # noqa: E402


def test_self_test_passes_and_writes_its_report(tmp_path):
    report = tmp_path / "self-test.txt"
    assert main(["rngauto", f"--self-test={report}"]) == 0
    assert report.read_text(encoding="utf-8").strip() == "self-test: OK"
