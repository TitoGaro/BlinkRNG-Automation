"""Host PABotBase2 contra el ESP32 emulado (sin hardware real)."""
import time

import pytest

from rngauto.control.controller import Button, Command, ControllerState, DPad, RecordingController
from rngauto.control.esp32 import ESP32Controller, ProtocolMismatch, encode_buttons, encode_command, pack_stick
from rngauto.control.pabotbase2 import (
    ASK_RESET, ASK_STREAM_DATA, ASK_VERSION, RET_RESET, PacketParser, encode_message, encode_packet, pabb_crc,
)

from .fakes import FakeConfig, FakeESP32, FaultyTransport, pipe_pair


def test_crc32c_check_value():
    assert pabb_crc(0xFFFFFFFF, b"123456789") ^ 0xFFFFFFFF == 0xE3069283


def test_worked_examples_byte_exact():
    s = 0x12345678
    assert encode_packet(0, ASK_RESET, s.to_bytes(4, "little"), s).hex(" ") == "81 00 0c 01 78 56 34 12 c6 b0 7c 1e"
    assert encode_packet(0, RET_RESET, b"", s).hex(" ") == "81 00 08 41 b4 68 95 5b"
    assert encode_packet(1, ASK_VERSION, b"", s).hex(" ") == "81 01 08 02 82 14 ff ac"
    stream = encode_packet(4, ASK_STREAM_DATA, b"\x00\x00" + encode_message(0x20, 0), s)
    assert stream.hex(" ") == "81 04 0e 12 00 00 04 00 20 00 b3 72 6a 4d"
    a80 = encode_command(0, Command(ControllerState(Button.A), 80))
    assert a80.hex(" ") == "10 00 97 00 50 00 08 00 00 00 08 80 00 08 80 00"


def test_button_and_stick_encoding():
    assert pack_stick(0, 0) == bytes((0x00, 0x08, 0x80))
    assert pack_stick(1, 1) == bytes((0xFF, 0xFF, 0xFF))
    assert pack_stick(-1, -1) == bytes((0x00, 0x00, 0x00))
    enc = encode_buttons(ControllerState(Button.HOME | Button.ZL | Button.Y, dpad=DPad.UP_LEFT))
    assert enc[0] == 0x01 and enc[1] == 0x10 and enc[2] == 0x80 | 0x0A


def test_parser_resyncs_after_garbage_and_bad_crc():
    s = 7
    good = encode_packet(3, ASK_VERSION, b"", s)
    bad = bytearray(encode_packet(4, ASK_VERSION, b"", s))
    bad[-1] ^= 0xFF
    parser = PacketParser()
    pkts = parser.feed(b"\x00\x13\x81" + bytes(bad) + good[:5], s) + parser.feed(good[5:], s)
    assert [p.seq for p in pkts] == [3]
    assert parser.bad_crc >= 1


@pytest.fixture
def link():
    host_end, dev_end = pipe_pair()
    device = FakeESP32(dev_end, FakeConfig(time_scale=0.1)).start()
    ctrl = ESP32Controller(host_end, health_period_s=0.2)
    yield ctrl, device
    ctrl.close()
    device.stop()


def test_handshake_and_mode_selection(link):
    ctrl, device = link
    status = ctrl.connect()
    info = ctrl.client.info
    assert info.program_id == 0x10 and info.firmware == 2026092800 and not info.firmware_mismatch
    assert 0x1180 in info.controllers and info.queue_capacity == 32
    assert ctrl.client.rsc.max_packet == 64 and ctrl.client.rsc.window == 16
    time.sleep(0.1)
    assert device.mode == 0x1180
    status = ctrl.refresh_status()
    assert status.connected and status.console_ready, status.detail


def test_commands_execute_in_order_and_finish(link):
    ctrl, device = link
    ctrl.connect()
    ctrl.press(Button.A, 80, 80)
    ctrl.move(DPad.UP, 200, 50)
    ctrl.wait_idle(timeout_s=5)
    labels = [(c.duration_ms, c.buttons[0], c.buttons[2]) for c in device.executed]
    assert labels == [(80, 0x08, 0), (80, 0, 0), (200, 0, 0x02), (50, 0, 0)]
    assert len(ctrl.client.finished_timestamps) == 4


def test_emergency_stop_clears_queue(link):
    ctrl, device = link
    ctrl.connect()
    ctrl.hold(Button.B, 5000)
    ctrl.hold(Button.A, 5000)
    time.sleep(0.15)
    ctrl.emergency_stop()
    ctrl.wait_idle(timeout_s=1)
    time.sleep(0.2)
    assert device.executed and device.executed[0].cancelled
    assert len(device.executed) == 1 and not device.queue


def test_close_cancels_the_device_queue_before_closing(link):
    """Al cerrar la app con comandos encolados, el ESP32 no debe seguir ejecutándolos."""
    ctrl, device = link
    ctrl.connect()
    for _ in range(6):
        ctrl.hold(Button.B, 5000)
    time.sleep(0.15)
    ctrl.close()
    time.sleep(0.3)
    assert device.executed and device.executed[0].cancelled
    assert len(device.executed) == 1 and not device.queue


def test_retransmission_survives_packet_loss():
    host_end, dev_end = pipe_pair()
    device = FakeESP32(FaultyTransport(dev_end, drop_prob=0.1, seed=1), FakeConfig(time_scale=0.05)).start()
    ctrl = ESP32Controller(FaultyTransport(host_end, drop_prob=0.1, seed=2), health_period_s=10)
    try:
        for _ in range(5):  # el reset no se retransmite: el usuario/host reintenta la conexión
            try:
                ctrl.connect()
                break
            except Exception:
                ctrl.client.close()
                ctrl = ESP32Controller(FaultyTransport(host_end, drop_prob=0.1, seed=3), health_period_s=10)
        for i in range(10):
            ctrl.press(Button.A, 20, 20)
        ctrl.wait_idle(timeout_s=15)
        presses = [c for c in device.executed if c.buttons[0] == 0x08]
        assert len(presses) == 10
        assert ctrl.client.rsc.stats["resent"] > 0
    finally:
        ctrl.close()
        device.stop()


def test_version_mismatch_is_refused():
    host_end, dev_end = pipe_pair()
    device = FakeESP32(dev_end, FakeConfig(mlc_version=2025010100)).start()
    ctrl = ESP32Controller(host_end)
    try:
        with pytest.raises(ProtocolMismatch):
            ctrl.connect()
    finally:
        ctrl.close()
        device.stop()


def test_recording_controller_dry_run():
    rec = RecordingController(clock=lambda: 10.0)
    rec.connect()
    rec.press(Button.A, 100, 100, label="final")
    rec.wait(1000)
    assert rec.pressed_labels() == ["final"]
    assert rec.log[-1].scheduled_start == pytest.approx(10.2)
    rec.emergency_stop()
    assert rec.stops == 1
