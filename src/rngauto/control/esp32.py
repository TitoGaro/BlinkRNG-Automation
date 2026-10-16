"""``ESP32Controller``: mando real vía ESP32 con firmware PABotBase2 (emula un Pro Controller inalámbrico).

Handshake: ASK_RESET → ASK_VERSION → ASK_PACKET_SIZE → ASK_BUFFER_SLOTS (capa RSC) y después, ya con
mensajes MLC, PROTOCOL_VERSION, DEVICE_IDENTIFIER, FIRMWARE_VERSION, DEVICE_NAME, SET_LOGGING_FLAG,
CONTROLLER_LIST, CQ_CAPACITY y READ_CONTROLLER_MODE.
Versiones fijadas: si no coinciden, se rechaza la conexión (el protocolo cambia entre versiones del firmware).

Estructura: ``PABotBase2Client`` habla el protocolo (peticiones, cola de comandos, avisos del dispositivo) y
``ESP32Controller`` lo adapta al contrato ``Controller`` (hilo de salud, PARADA, cierre seguro).
"""
from __future__ import annotations

import logging
import random
import struct
import threading
import time
from dataclasses import dataclass, field

from rngauto.i18n import t

from .controller import (
    Button, Command, Controller, ControllerError, ControllerNotReady, ControllerState, ControllerStatus, DPad,
)
from .pabotbase2 import (
    ASK_BUFFER_SLOTS, ASK_PACKET_SIZE, ASK_RESET, ASK_VERSION, CONTROLLER_NS1_WIRELESS_PRO, MLC_PROTOCOL_MAJOR,
    MSG_CHANGE_CONTROLLER_MODE, MSG_CONSOLE_DISCONNECT, MSG_CONTROLLER_LIST, MSG_CQ_CANCEL, MSG_CQ_CAPACITY,
    MSG_CQ_COMMAND_DROPPED, MSG_CQ_COMMAND_FINISHED, MSG_DEVICE_IDENTIFIER, MSG_DEVICE_NAME, MSG_FIRMWARE_VERSION,
    MSG_NS1_OEM_BUTTONS, MSG_PROTOCOL_VERSION, MSG_READ_CONTROLLER_MODE, MSG_REQUEST_STATUS, MSG_RET, MSG_RET_DATA,
    MSG_RET_U32, MSG_RET_U32_DATA, MSG_SET_LOGGING_FLAG, PID_ESP32, PID_ESP32_S3, PINNED_FIRMWARE, RET_BUFFER_SLOTS,
    RET_PACKET_SIZE, RET_VERSION, RSC_PROTOCOL_VERSION, Message, MessageAssembler, Packet, RscEndpoint,
    encode_message,
)
from .transport import Transport

log = logging.getLogger(__name__)

STALL_MARGIN_S = 5.0  # retraso máximo de la cola sobre lo previsto antes de dar el mando por perdido

# --- Codificación del estado del mando (bytes 3..12 del informe 0x30 de Nintendo) ---------------
_B3 = {Button.Y: 0, Button.X: 1, Button.B: 2, Button.A: 3, Button.R: 6, Button.ZR: 7}
_B4 = {Button.MINUS: 0, Button.PLUS: 1, Button.RSTICK: 2, Button.LSTICK: 3, Button.HOME: 4, Button.CAPTURE: 5}
_B5 = {Button.L: 6, Button.ZL: 7}
_DPAD = {  # bits de button5: 0 abajo, 1 arriba, 2 derecha, 3 izquierda
    DPad.NONE: 0, DPad.DOWN: 0x01, DPad.UP: 0x02, DPad.RIGHT: 0x04, DPad.LEFT: 0x08,
    DPad.UP_RIGHT: 0x06, DPad.UP_LEFT: 0x0A, DPad.DOWN_RIGHT: 0x05, DPad.DOWN_LEFT: 0x09,
}


def stick_u12(value: float) -> int:
    """Float [-1, 1] → u12 con centro 2048: negativo ×2048, positivo ×2047 (así ±1 llega a 0 y 4095)."""
    value = max(-1.0, min(1.0, value))
    return int(round(value * 2048 + 2048)) if value <= 0 else int(round(value * 2047 + 2048))


def pack_stick(x: float, y: float) -> bytes:
    """12+12 bits empaquetados en 3 bytes, como en el informe del Pro Controller. +y = arriba.

    Las inclinaciones pequeñas no se corrigen para salir de la zona muerta del mando: aquí solo se usan
    inclinaciones completas.
    """
    ux, uy = min(stick_u12(x), 0xFFF), min(stick_u12(y), 0xFFF)
    return bytes((ux & 0xFF, (ux >> 8) | ((uy & 0x0F) << 4), uy >> 4))


def encode_buttons(state: ControllerState) -> bytes:
    """Estado del mando → 10 bytes: 3 de botones, 3 por stick y uno de relleno a cero."""
    b3 = sum(1 << bit for btn, bit in _B3.items() if state.buttons & btn)
    b4 = sum(1 << bit for btn, bit in _B4.items() if state.buttons & btn)
    b5 = sum(1 << bit for btn, bit in _B5.items() if state.buttons & btn) | _DPAD[state.dpad]
    return bytes((b3, b4, b5)) + pack_stick(*state.left) + pack_stick(*state.right) + b"\x00"


def encode_command(cmd_id: int, command: Command) -> bytes:
    """Mensaje ``NS1_OEM_BUTTONS``: duración u16 en ms seguida del estado del mando."""
    return encode_message(MSG_NS1_OEM_BUTTONS, cmd_id, struct.pack("<H", command.duration_ms) + encode_buttons(command.state))


# --- Cliente PABotBase2 -----------------------------------------------------------------------------
class ProtocolMismatch(ControllerError):
    """El dispositivo responde, pero con un protocolo, placa o firmware que no se admiten."""


@dataclass
class DeviceInfo:
    """Datos del dispositivo obtenidos en el handshake."""

    rsc_version: int = 0
    mlc_version: int = 0
    program_id: int = 0
    firmware: int = 0
    name: str = ""
    controllers: list[int] = field(default_factory=list)
    queue_capacity: int = 4
    controller_mode: int = 0
    firmware_mismatch: bool = False


class PABotBase2Client:
    """Cliente del protocolo PABotBase2 sobre un ``Transport``.

    Las respuestas y avisos llegan por el hilo de ``RscEndpoint`` (``_on_control``/``_on_stream``) y se
    entregan a los hilos que esperan mediante ``_cond``. Las pulsaciones se encolan en el dispositivo y se
    sigue cuáles faltan por terminar (``_outstanding``) para no desbordar su cola y saber cuándo acaba.
    """

    def __init__(self, transport: Transport, rsc_timeout_s: float = 0.3) -> None:
        self.rsc = RscEndpoint(transport, "host", on_stream=self._on_stream, on_control=self._on_control)
        self.rsc_timeout_s = rsc_timeout_s
        self.messages = MessageAssembler()
        self.info = DeviceInfo()
        self._cond = threading.Condition()
        self._responses: dict[int, Message] = {}
        self._rsc_values: dict[int, int] = {}
        self._req_id = 0
        self._cmd_id = 0
        self._outstanding: dict[int, float] = {}  # id de comando → instante de envío
        self._cancel_gen = 0  # sube con cada cancelación: un envío que esperaba hueco no debe salir después
        self._expected_done = 0.0  # instante (monótono) en que debería terminar todo lo encolado
        self.queue_limit = 4
        self.console_disconnects: list[int] = []
        self.dropped_commands = 0
        self.finished_timestamps: list[tuple[int, int, float]] = []  # (id, marca dispositivo, llegada host)
        self.logs: list[str] = []

    # --- callbacks del endpoint (se ejecutan en el hilo de E/S de RSC) ---
    def _on_control(self, pkt: Packet) -> None:
        if pkt.opcode in (RET_VERSION, RET_PACKET_SIZE, RET_BUFFER_SLOTS):
            with self._cond:
                self._rsc_values[pkt.opcode] = pkt.u32()
                self._cond.notify_all()

    def _on_stream(self, data: bytes) -> None:
        for msg in self.messages.feed(data):
            self._on_message(msg)

    def _on_message(self, msg: Message) -> None:
        with self._cond:
            if msg.opcode in (MSG_RET, MSG_RET_U32, MSG_RET_DATA, MSG_RET_U32_DATA):
                self._responses[msg.id] = msg
            elif msg.opcode == MSG_CQ_COMMAND_FINISHED:
                if self._outstanding.pop(msg.id, None) is not None:
                    self.finished_timestamps.append((msg.id, msg.u32(), time.monotonic()))
            elif msg.opcode == MSG_CQ_COMMAND_DROPPED:
                self.dropped_commands += 1
                self._outstanding.pop(msg.id, None)
            elif msg.opcode == MSG_CONSOLE_DISCONNECT:
                self.console_disconnects.append(msg.u32())
            elif msg.opcode <= 0x04:  # mensajes de registro del firmware
                self.logs.append(msg.payload.decode("utf-8", "replace"))
            self._cond.notify_all()

    # --- conexión ---
    def connect(self) -> DeviceInfo:
        """Handshake completo (ver docstring del módulo). Lanza ``ControllerNotReady`` si no hay respuesta y
        ``ProtocolMismatch`` si el dispositivo no es compatible."""
        self.rsc.start()
        # Sesión aleatoria: el CRC se siembra con ella, así los paquetes de una sesión anterior se descartan.
        session = random.getrandbits(32) or 1
        self.rsc.reset_session(session)
        seq = self.rsc.send_reliable(ASK_RESET, struct.pack("<I", session), timeout_s=1)
        if not self.rsc.wait_acked(seq, self.rsc_timeout_s):
            raise ControllerNotReady(t("El dispositivo no respondió al reset (¿puerto/baudios correctos? ¿firmware PABotBase2?)",
                                     "The device did not respond to the reset (correct port/baud rate? PABotBase2 firmware?)"))
        version = self._rsc_query(ASK_VERSION, RET_VERSION)
        if version // 100 != RSC_PROTOCOL_VERSION // 100 or version % 100 < RSC_PROTOCOL_VERSION % 100:
            raise ProtocolMismatch(t("Protocolo RSC incompatible: dispositivo {version}, se esperaba {expected}",
                                   "Incompatible RSC protocol: device {version}, expected {expected}",
                                   version=version, expected=RSC_PROTOCOL_VERSION))
        self.info.rsc_version = version
        self.rsc.max_packet = max(16, min(256, self._rsc_query(ASK_PACKET_SIZE, RET_PACKET_SIZE)))
        self.rsc.window = max(1, min(128, self._rsc_query(ASK_BUFFER_SLOTS, RET_BUFFER_SLOTS)))

        mlc = self.request_u32(MSG_PROTOCOL_VERSION)
        if mlc // 100 != MLC_PROTOCOL_MAJOR:
            raise ProtocolMismatch(t("Protocolo de mensajes incompatible: {mlc} (se esperaba {major}xx)",
                                   "Incompatible message protocol: {mlc} (expected {major}xx)",
                                   mlc=mlc, major=MLC_PROTOCOL_MAJOR))
        self.info.mlc_version = mlc
        self.info.program_id = self.request_u32(MSG_DEVICE_IDENTIFIER) & 0xFF
        if self.info.program_id not in (PID_ESP32, PID_ESP32_S3):
            raise ProtocolMismatch(t("Dispositivo no soportado (id 0x{pid:02X}); se esperaba ESP32",
                                   "Unsupported device (id 0x{pid:02X}); expected an ESP32", pid=self.info.program_id))
        self.info.firmware = self.request_u32(MSG_FIRMWARE_VERSION)
        self.info.firmware_mismatch = self.info.firmware != PINNED_FIRMWARE
        self.info.name = self.request(MSG_DEVICE_NAME).payload.split(b"\x00")[0].decode("utf-8", "replace")
        self.send_message(MSG_SET_LOGGING_FLAG, struct.pack("<I", 0))
        raw = self.request(MSG_CONTROLLER_LIST).payload
        self.info.controllers = list(struct.unpack(f"<{len(raw) // 4}I", raw[: len(raw) // 4 * 4]))
        self.info.queue_capacity = self.request_u32(MSG_CQ_CAPACITY)
        # Comandos pendientes a la vez: la capacidad declarada, acotada a [4, 64] (los ids son de 8 bits).
        self.queue_limit = min(max(4, min(255, self.info.queue_capacity)), 64)
        self.info.controller_mode = self.request_u32(MSG_READ_CONTROLLER_MODE)
        return self.info

    def _rsc_query(self, ask: int, ret: int, timeout_s: float = 2.0) -> int:
        """Consulta de control RSC (``ASK_*``) y espera del valor u32 de su ``RET_*``."""
        with self._cond:
            self._rsc_values.pop(ret, None)
        self.rsc.send_reliable(ask, b"", timeout_s)
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while ret not in self._rsc_values:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControllerNotReady(t("Sin respuesta RSC 0x{ask:02X}", "No RSC response 0x{ask:02X}", ask=ask))
                self._cond.wait(remaining)
            return self._rsc_values.pop(ret)

    def close(self) -> None:
        self.rsc.stop()

    # --- mensajes ---
    def send_message(self, opcode: int, payload: bytes = b"", msg_id: int = 0, timeout_s: float = 5.0,
                     force: bool = False) -> int:
        """Envía un mensaje MLC sin esperar respuesta. Devuelve el seq RSC del último paquete."""
        return self.rsc.send_stream(encode_message(opcode, msg_id, payload), timeout_s, force=force)

    def link_problem(self) -> str | None:
        """Motivo por el que el enlace ya no puede funcionar sin reconectar, o ``None``."""
        if self.messages.corrupted:
            return t("flujo de mensajes corrupto", "corrupted message stream")
        if self.rsc._thread is not None and not self.rsc.alive():
            return self.rsc.error or t("el hilo de comunicación se detuvo", "the communication thread stopped")
        return None

    def _next_req_id(self) -> int:
        self._req_id = self._req_id % 255 + 1  # 0 queda para "sin respuesta"
        return self._req_id

    def request(self, opcode: int, payload: bytes = b"", timeout_s: float = 2.0) -> Message:
        """Envía un mensaje con id propio y espera la respuesta con ese id (``ControllerNotReady`` si no llega)."""
        with self._cond:
            msg_id = self._next_req_id()
            self._responses.pop(msg_id, None)
        self.send_message(opcode, payload, msg_id)
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while msg_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControllerNotReady(t("Sin respuesta al mensaje 0x{op:02X}", "No response to message 0x{op:02X}", op=opcode))
                self._cond.wait(remaining)
            return self._responses.pop(msg_id)

    def request_u32(self, opcode: int, payload: bytes = b"") -> int:
        return self.request(opcode, payload).u32()

    # --- cola de comandos ---
    def _stalled(self) -> bool:
        """La cola debería haber terminado hace rato (suma de duraciones + margen) y no lo ha hecho:
        el ESP32 o el enlace con la Switch no responden. Evita esperas infinitas."""
        return bool(self._outstanding) and time.monotonic() > self._expected_done + STALL_MARGIN_S

    def send_command(self, command: Command, timeout_s: float = 8.0) -> int:
        """Encola un comando en el dispositivo. Si su cola está llena, espera a que termine alguno (con plazo
        y detección de cola atascada) para no provocar descartes."""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            generation = self._cancel_gen
            while len(self._outstanding) >= self.queue_limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stalled():
                    raise ControllerError(t("El mando no responde: la cola de pulsaciones no avanza",
                                            "The controller is not responding: the input queue is not moving"))
                self._cond.wait(min(remaining, 0.25))
                if self._cancel_gen != generation:  # una PARADA vació la cola mientras se esperaba: no se envía
                    raise ControllerNotReady(t("Comando cancelado por la PARADA", "Command cancelled by STOP"))
            self._cmd_id = (self._cmd_id + 1) & 0xFF
            cmd_id = self._cmd_id
            now = time.monotonic()
            self._outstanding[cmd_id] = now
            # Si la cola estaba vacía, el comando empieza ahora; si no, a continuación de lo encolado.
            self._expected_done = max(self._expected_done, now) + command.duration_ms / 1000
        try:
            self.rsc.send_stream(encode_command(cmd_id, command))
        except BaseException:  # no ha salido: no debe contar como pendiente (si no, la cola parece atascada)
            with self._cond:
                self._outstanding.pop(cmd_id, None)
            raise
        return cmd_id

    def wait_all(self, timeout_s: float | None = None) -> None:
        """Espera a que el dispositivo termine lo encolado. Sin ``timeout_s`` igualmente acaba: si la cola
        lleva más de ``STALL_MARGIN_S`` de retraso sobre lo previsto, se considera que el mando no responde."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._cond:
            while self._outstanding:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(t("El dispositivo no terminó los comandos a tiempo",
                                         "The device did not finish the commands in time"))
                if self._stalled():
                    raise ControllerError(t("El mando no responde: los comandos no terminan",
                                            "The controller is not responding: commands are not finishing"))
                self._cond.wait(0.05 if deadline is None else max(0.0, min(0.05, deadline - time.monotonic())))

    def cancel(self, wait_ack_s: float = 0.0) -> bool:
        """CQ_CANCEL: vacía la cola del dispositivo (vuelve a neutro) y olvida los pendientes.

        Si la ventana RSC está llena se envía igualmente pasados 0,3 s. Con ``wait_ack_s`` espera (con
        tope) a que el dispositivo lo confirme; devuelve si consta como recibido."""
        with self._cond:
            self._outstanding.clear()
            self._cancel_gen += 1
            self._cond.notify_all()
        seq = self.send_message(MSG_CQ_CANCEL, timeout_s=0.3, force=True)
        return wait_ack_s <= 0 or seq < 0 or self.rsc.wait_acked(seq, wait_ack_s)


class ESP32Controller(Controller):
    """Implementación real del contrato ``Controller`` sobre PABotBase2.

    Un hilo de salud pide el estado cada ``health_period_s``; si las respuestas dejan de llegar durante más
    de 2 s, ``status`` da el mando por desconectado.
    """

    def __init__(self, transport: Transport, port_label: str = "", health_period_s: float = 1.0,
                 strict_firmware: bool = False) -> None:
        self.transport = transport
        self.port_label = port_label
        self.client = PABotBase2Client(transport)
        self.health_period_s = health_period_s
        self.strict_firmware = strict_firmware
        self.reconnect_wait_s = 6.0  # espera máxima a que la Switch ya emparejada vuelva a conectar el mando
        self._status = ControllerStatus(False, False, detail=t("sin conectar", "not connected"))
        self._health: threading.Thread | None = None
        self._running = threading.Event()
        self._halted = threading.Event()  # tras una PARADA no sale ninguna pulsación hasta ``resume``
        self._last_status_at: float | None = None
        self._warned_dropped = 0
        self._warned_disconnects = 0

    @classmethod
    def open_serial(
        cls, port: str, baudrates: tuple[int, ...] = (921600, 115200), timeout_s: float = 4.0
    ) -> "ESP32Controller":
        """Abre el puerto una vez y prueba por turnos los ``baudrates`` durante ``timeout_s``: no se sabe de
        antemano a qué velocidad escucha el firmware, y si la placa se reinicia al abrir, el plazo le da
        tiempo a arrancar."""
        import time as _time

        from .transport import SerialTransport

        try:
            transport = SerialTransport(port, baudrates[0])
        except Exception as exc:  # puerto ocupado, inexistente...
            text = str(exc)
            if "PermissionError" in text or "denegado" in text or "denied" in text.lower():
                raise ControllerNotReady(t(
                    "{port} está ocupado por otro programa. Cierra cualquier otro programa u "
                    "otra ventana de esta aplicación que lo esté usando y vuelve a intentarlo.",
                    "{port} is in use by another program. Close any other program or any other "
                    "window of this application that is using it, then try again.", port=port)) from exc
            raise ControllerNotReady(t("No se puede abrir {port}: {exc}", "Cannot open {port}: {exc}",
                                       port=port, exc=exc)) from exc
        last: Exception | None = None
        deadline = _time.monotonic() + timeout_s
        try:
            while _time.monotonic() < deadline:
                for baud in baudrates:
                    transport.set_baudrate(baud)
                    ctrl = cls(transport, port_label=port)
                    try:
                        ctrl.connect()
                        return ctrl
                    except ControllerNotReady as exc:
                        last = exc
                        ctrl.client.close()  # se conserva el puerto abierto para el siguiente intento
                    except BaseException:
                        ctrl.client.close()
                        raise
        except BaseException:
            transport.close()  # nunca dejar el puerto ocupado si algo falla
            raise
        transport.close()
        raise ControllerNotReady(t(
            "No se recibe respuesta del ESP32 en {port}: ¿está flasheado con PABotBase2-ESP32-{fw}? ({last})",
            "No response from the ESP32 on {port}: is it flashed with PABotBase2-ESP32-{fw}? ({last})",
            port=port, fw=PINNED_FIRMWARE, last=last))

    def connect(self) -> ControllerStatus:
        info = self.client.connect()
        if info.firmware_mismatch and self.strict_firmware:
            raise ProtocolMismatch(t("Firmware {fw} distinto del fijado {pinned}", "Firmware {fw} differs from the pinned {pinned}",
                                   fw=info.firmware, pinned=PINNED_FIRMWARE))
        if info.controller_mode != CONTROLLER_NS1_WIRELESS_PRO:
            if CONTROLLER_NS1_WIRELESS_PRO not in info.controllers:
                raise ControllerError(t("El dispositivo no ofrece Pro Controller inalámbrico",
                                        "The device does not offer a wireless Pro Controller"))
            # CHANGE_CONTROLLER_MODE conserva el emparejamiento (RESET_TO_CONTROLLER lo borraría).
            self.client.send_message(MSG_CHANGE_CONTROLLER_MODE, struct.pack("<I", CONTROLLER_NS1_WIRELESS_PRO))
        self._running.set()
        self._health = threading.Thread(target=self._health_loop, name="esp32-health", daemon=True)
        self._health.start()
        try:
            status = self.refresh_status()
            # Tras arrancar o cambiar de modo, la reconexión Bluetooth con una Switch ya emparejada tarda unos s.
            deadline = time.monotonic() + self.reconnect_wait_s
            while not status.console_ready and time.monotonic() < deadline:
                time.sleep(0.5)
                status = self.refresh_status()
        except BaseException:
            self._running.clear()  # que el hilo de salud no siga escribiendo en un puerto reutilizado
            raise
        return status

    def refresh_status(self) -> ControllerStatus:
        """Pide el estado al ESP32 (bits: conectado, listo, emparejado) y actualiza el que ve ``status``."""
        msg = self.client.request(MSG_REQUEST_STATUS)
        mode = msg.u32()
        flags = msg.payload[4] if len(msg.payload) > 4 else 0
        connected, ready, paired = bool(flags & 1), bool(flags & 2), bool(flags & 4)
        self._last_status_at = time.monotonic()
        info = self.client.info
        link = t("conectado", "connected") if connected else t("sin conexión con la Switch", "not connected to the Switch")
        detail = t("modo 0x{mode:04X}, {link}, {ready}, {paired}", "mode 0x{mode:04X}, {link}, {ready}, {paired}",
                   mode=mode, link=link,
                   ready=t("listo", "ready") if ready else t("no listo", "not ready"),
                   paired=t("emparejado", "paired") if paired else t("sin emparejar", "not paired"))
        if info.firmware_mismatch:
            detail += t(" | aviso: firmware {fw} ≠ {pinned}", " | warning: firmware {fw} ≠ {pinned}",
                        fw=info.firmware, pinned=PINNED_FIRMWARE)
        self._status = ControllerStatus(True, connected and ready, info.name, str(info.firmware), detail, 0.0)
        return self._status

    def _health_loop(self) -> None:
        """Hilo de salud: refresca el estado y, si falla, explica por qué (enlace roto o sin respuesta)."""
        while self._running.is_set():
            time.sleep(self.health_period_s)
            try:
                self.refresh_status()
            except Exception as exc:
                age = time.monotonic() - (self._last_status_at or 0)
                problem = self.client.link_problem()
                detail = (t("enlace con el ESP32 roto ({problem}): pulsa «Reconectar»",
                            "ESP32 link broken ({problem}): press Reconnect", problem=problem) if problem
                          else t("sin respuesta del ESP32 desde hace {age:.1f} s ({exc})",
                                 "no response from the ESP32 for {age:.1f} s ({exc})", age=age, exc=exc))
                self._status = ControllerStatus(age < 2.0, False, self._status.device, self._status.firmware,
                                                detail, age)

    def status(self) -> ControllerStatus:
        # El último estado conocido solo vale si es reciente: sin respuesta en 2 s se da el mando por perdido.
        if self._last_status_at is not None:
            age = time.monotonic() - self._last_status_at
            s = self._status
            return ControllerStatus(s.connected and age < 2.0, s.console_ready and age < 2.0, s.device, s.firmware, s.detail, age)
        return self._status

    def _link_error(self, exc: BaseException) -> ControllerNotReady:
        """Un fallo de E/S o de la ventana RSC es «sin mando»: así la estrategia no intenta reiniciar el juego a
        ciegas (sin mando, tampoco se puede)."""
        problem = self.client.link_problem()
        return ControllerNotReady(t("Sin comunicación con el ESP32 ({why}): revisa el cable USB y pulsa «Reconectar»",
                                    "No communication with the ESP32 ({why}): check the USB cable and click "
                                    "“Reconnect”", why=problem or exc))

    def submit(self, commands: list[Command]) -> None:
        if not self._running.is_set():
            raise ControllerNotReady(t("ESP32 no conectado", "ESP32 not connected"))
        for cmd in commands:
            if self._halted.is_set():  # PARADA: lo que quedaba del lote no se envía
                raise ControllerNotReady(t("Mando detenido", "Controller stopped"))
            try:
                self.client.send_command(cmd)
            except (TimeoutError, OSError) as exc:
                raise self._link_error(exc) from exc
            if self._halted.is_set():
                # La PARADA llegó mientras se enviaba: otra cancelación desde este mismo hilo llega seguro
                # después de ese comando, así que tampoco se ejecuta.
                try:
                    self.client.cancel()
                except Exception:
                    log.warning("No se pudo repetir la cancelación tras la PARADA", exc_info=True)
                raise ControllerNotReady(t("Mando detenido", "Controller stopped"))

    def wait_idle(self, timeout_s: float | None = None) -> None:
        try:
            self.client.wait_all(timeout_s)
        except OSError as exc:
            raise self._link_error(exc) from exc

    def resume(self) -> None:
        self._halted.clear()

    def take_warnings(self) -> list[str]:
        # Solo se informa de lo nuevo desde la última llamada; los contadores del cliente son acumulados.
        out: list[str] = []
        dropped, disconnects = self.client.dropped_commands, len(self.client.console_disconnects)
        if dropped > self._warned_dropped:
            out.append(t("El ESP32 ha descartado {n} comando(s) (cola llena o PARADA)",
                         "The ESP32 dropped {n} command(s) (queue full or STOP)", n=dropped - self._warned_dropped))
        if disconnects > self._warned_disconnects:
            out.append(t("La Switch ha desconectado el mando {n} vez/veces",
                         "The Switch disconnected the controller {n} time(s)", n=disconnects - self._warned_disconnects))
        self._warned_dropped, self._warned_disconnects = dropped, disconnects
        return out

    def clear_queue(self, timeout_s: float = 1.0) -> bool:
        # A diferencia de ``emergency_stop``, no activa la PARADA: lo que se envíe después sí se ejecuta.
        try:
            return self.client.cancel(wait_ack_s=timeout_s)
        except Exception:
            return False

    def emergency_stop(self) -> None:
        """No bloquea: si el enlace está caído, la interfaz no debe congelarse esperando. Hasta ``resume`` (al
        empezar otra ejecución o una pulsación manual) no se envía nada más."""
        self._halted.set()

        def cancel() -> None:
            try:
                if not self.client.cancel(wait_ack_s=0.5):
                    log.warning("El ESP32 no ha confirmado la PARADA (CQ_CANCEL sin acuse)")
            except Exception:  # enlace caído: queda en el registro para poder diagnosticarlo
                log.warning("No se pudo enviar la PARADA al ESP32", exc_info=True)

        threading.Thread(target=cancel, name="esp32-cancel", daemon=True).start()

    def close(self) -> None:
        """Cancela la cola del ESP32 de forma síncrona (con tope de ~1 s) antes de cerrar: si se cerrara el
        puerto con comandos encolados, el ESP32 los seguiría ejecutando (p. ej. una cruceta de 20 s)."""
        was_running = self._running.is_set()
        self._running.clear()
        try:
            # Si el dispositivo lleva segundos sin contestar, esperar su acuse solo congelaría al que cierra.
            answering = time.monotonic() - self.client.rsc.last_rx < 3.0
            if was_running and self.client.link_problem() is None and answering:
                try:
                    self.client.cancel(wait_ack_s=0.6)
                except Exception:
                    pass  # enlace caído: no hay nada más que hacer
            self.client.close()
        finally:
            try:
                self.transport.close()
            except Exception:
                pass
