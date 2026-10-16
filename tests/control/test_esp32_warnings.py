"""Las incidencias del ESP32 (comandos descartados, desconexiones) llegan al registro una sola vez."""
from rngauto.control.controller import RecordingController
from rngauto.control.esp32 import ESP32Controller

from .fakes import pipe_pair


def test_dropped_commands_and_disconnects_are_reported_once():
    host_end, _dev_end = pipe_pair()
    ctrl = ESP32Controller(host_end)
    assert ctrl.take_warnings() == []
    ctrl.client.dropped_commands = 2
    ctrl.client.console_disconnects.append(1)
    warnings = ctrl.take_warnings()
    assert len(warnings) == 2 and "2" in warnings[0]
    assert ctrl.take_warnings() == []  # ya avisadas
    ctrl.client.dropped_commands = 3
    assert len(ctrl.take_warnings()) == 1


def test_other_controllers_have_nothing_to_report():
    assert RecordingController().take_warnings() == []
