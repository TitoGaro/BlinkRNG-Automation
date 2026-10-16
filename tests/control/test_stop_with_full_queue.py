"""PARADA con la cola del ESP32 llena: ninguna pulsación puede salir después de la cancelación."""
import threading
import time

from rngauto.control.controller import NEUTRAL, Button, Command, ControllerState
from rngauto.control.esp32 import ESP32Controller
from rngauto.control.pabotbase2 import MSG_CQ_CANCEL, MSG_NS1_OEM_BUTTONS

from .fakes import FakeConfig, FakeESP32, pipe_pair


def test_no_button_after_emergency_stop_while_waiting_for_queue_room():
    host, dev = pipe_pair()
    device = FakeESP32(dev, FakeConfig(queue_capacity=4, time_scale=1.0)).start()
    ctrl = ESP32Controller(host, health_period_s=10)
    try:
        ctrl.connect()
        # Cuatro comandos largos llenan la cola; el quinto (A) espera hueco cuando llega la PARADA.
        batch = [Command(ControllerState(Button.B), 2000, "lote")] * 4 + [
            Command(ControllerState(Button.A), 80, "tras-parada"), Command(NEUTRAL, 80, "neutro")]
        errors = []
        worker = threading.Thread(target=lambda: _submit(ctrl, batch, errors), daemon=True)
        worker.start()
        time.sleep(0.5)
        ctrl.emergency_stop()
        worker.join(5)
        time.sleep(0.5)
        ops = [m.opcode for m in device.received]
        last_cancel = max(i for i, op in enumerate(ops) if op == MSG_CQ_CANCEL)
        assert MSG_NS1_OEM_BUTTONS not in ops[last_cancel + 1:]  # nada sale después de la última cancelación
        assert errors  # el lote se interrumpe con un error, no sigue como si nada
        ctrl.resume()
        ctrl.submit([Command(NEUTRAL, 20, "tras-reanudar")])  # tras «resume» se puede volver a enviar
    finally:
        ctrl.close()
        device.stop()


def _submit(ctrl, batch, errors):
    try:
        ctrl.submit(batch)
    except Exception as exc:  # lo esperado: «Mando detenido»
        errors.append(exc)
