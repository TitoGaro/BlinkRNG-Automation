"""Protocolo PABotBase2 (lado PC): lo que habla el firmware del ESP32, implementado en Python.

Capas (de abajo arriba):
  * RSC: paquetes ``0x81 | seq | len | opcode | payload | CRC32C(LE)``; CRC sembrado con el id de
    sesión (``0xFFFFFFFF`` solo para ``ASK_RESET``), sin XOR final; acks, retransmisión, ventana.
  * Stream: bytes ordenados dentro de ``ASK_STREAM_DATA`` (offset u16).
  * MLC: mensajes ``u16 longitud total | u8 opcode | u8 id`` + datos, dentro del stream.

``esp32.py`` usa ``RscEndpoint`` en el papel de host; el emulador de dispositivo de los tests
(``tests/control/fakes.py``) usa el mismo endpoint con ``role="device"``.
"""
from __future__ import annotations

import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from rngauto.i18n import t

from .transport import Transport

# --- Constantes RSC (capa de paquetes) ------------------------------------------------------------
# Tabla completa del protocolo; no todas se usan aquí, pero se mantienen para tenerlo documentado entero.
MAGIC = 0x81
RSC_PROTOCOL_VERSION = 2026061800
RETRANSMIT_FLAG = 0x80
OPCODE_MASK = 0x7F
ASK_RESET, RET_RESET = 0x01, 0x41
ASK_VERSION, RET_VERSION = 0x02, 0x42
ASK_PACKET_SIZE, RET_PACKET_SIZE = 0x03, 0x43
ASK_BUFFER_SLOTS, RET_BUFFER_SLOTS = 0x04, 0x44
ASK_STREAM_DATA, RET_STREAM_DATA = 0x12, 0x52
RET_BUFFER_BYTES = 0x45
ACK_OPCODES = {RET_RESET, RET_VERSION, RET_PACKET_SIZE, RET_BUFFER_SLOTS, RET_BUFFER_BYTES, RET_STREAM_DATA}
INFO_OPCODES = set(range(0x10, 0x1A)) | set(range(0x20, 0x29))
INVALID_LENGTH, INVALID_CHECKSUM, UNKNOWN_OPCODE = 0x30, 0x31, 0x32
REORDER_WINDOW = 128
HOST_INITIAL_PACKET_SIZE = 24  # tamaño de paquete seguro hasta conocer el real (RET_PACKET_SIZE)
STREAM_HEADER = 6  # PacketHeaderData: cabecera RSC (4) + offset del stream (2)
OVERHEAD = STREAM_HEADER + 4  # + CRC32: bytes de cada paquete de stream que no son datos

# --- Constantes MLC (capa de mensajes y mandos) ---------------------------------------------------
MLC_PROTOCOL_MAJOR = 20260618
MSG_LOG_STRING = 0x01
MSG_REQUEST_DROPPED = 0x10
MSG_RET, MSG_RET_U32, MSG_RET_DATA, MSG_RET_U32_DATA = 0x11, 0x12, 0x13, 0x14
MSG_PROTOCOL_VERSION = 0x20
MSG_FIRMWARE_VERSION = 0x21
MSG_DEVICE_IDENTIFIER = 0x22
MSG_DEVICE_NAME = 0x23
MSG_CONTROLLER_LIST = 0x24
MSG_SET_LOGGING_FLAG = 0x25
MSG_CQ_CAPACITY = 0x26
MSG_REQUEST_SESSION_NUM = 0x30
MSG_REQUEST_STATUS = 0x31
MSG_READ_CONTROLLER_MODE = 0x32
MSG_CHANGE_CONTROLLER_MODE = 0x33
MSG_CONSOLE_DISCONNECT = 0x37
MSG_CQ_COMMAND_DROPPED = 0x40
MSG_CQ_CANCEL = 0x41
MSG_CQ_REPLACE_ON_NEXT = 0x42
MSG_CQ_COMMAND_FINISHED = 0x43
MSG_NS1_OEM_BUTTONS = 0x97

PID_ESP32 = 0x10
PID_ESP32_S3 = 0x12
CONTROLLER_NS1_WIRELESS_PRO = 0x1180
# Versión de firmware con la que se ha validado esta reimplementación. Otra versión solo genera un aviso
# (o un error con ``strict_firmware``): el protocolo cambia entre versiones del firmware sin compatibilidad.
PINNED_FIRMWARE = 2026092800


# --- CRC-32C (polinomio reflejado 0x82F63B78) ----------------------------------------------------
def _make_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_TABLE = _make_table()


def pabb_crc(seed: int, data: bytes) -> int:
    """CRC-32C sembrado con ``seed`` y sin inversión final (la variante que usa el firmware)."""
    c = seed & 0xFFFFFFFF
    for b in data:
        c = _TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c


def encode_packet(seq: int, opcode: int, payload: bytes, session: int) -> bytes:
    """Construye un paquete RSC completo. Un ``len`` de 0 en la cabecera significa 256 bytes."""
    size = 4 + len(payload) + 4
    if size > 256:
        raise ValueError(t("Paquete RSC demasiado grande", "RSC packet too large"))
    body = bytes((MAGIC, seq & 0xFF, size & 0xFF, opcode & 0xFF)) + payload
    seed = 0xFFFFFFFF if opcode == ASK_RESET else session
    return body + struct.pack("<I", pabb_crc(seed, body))


@dataclass(frozen=True, slots=True)
class Packet:
    """Paquete RSC ya validado (CRC correcto)."""

    seq: int
    opcode: int  # con bit de retransmisión quitado
    retransmit: bool
    payload: bytes

    def u32(self) -> int:
        return struct.unpack_from("<I", self.payload)[0] if len(self.payload) >= 4 else 0


class PacketParser:
    """Reensamblado de paquetes con resincronización por byte mágico.

    Ante una longitud imposible o un CRC incorrecto descarta un solo byte y busca el siguiente ``0x81``:
    así recupera la alineación sin perder un paquete válido que empiece dentro de los bytes descartados.
    """

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad_crc = 0
        self.bad_length = 0

    def feed(self, data: bytes, session: int) -> list[Packet]:
        """Añade bytes recibidos y devuelve los paquetes completos y válidos; el resto queda en el búfer."""
        self.buf += data
        out: list[Packet] = []
        while True:
            start = self.buf.find(MAGIC)
            if start < 0:
                self.buf.clear()
                return out
            if start:
                del self.buf[:start]
            if len(self.buf) < 3:
                return out
            size = self.buf[2] or 256
            if size < 8:
                self.bad_length += 1
                del self.buf[:1]
                continue
            if len(self.buf) < size:
                return out
            raw = bytes(self.buf[:size])
            opcode_raw = raw[3]
            seed = 0xFFFFFFFF if opcode_raw == ASK_RESET else session
            crc = struct.unpack_from("<I", raw, size - 4)[0]
            if pabb_crc(seed, raw[:-4]) != crc:
                self.bad_crc += 1
                del self.buf[:1]  # resincronizar a partir del siguiente 0x81
                continue
            del self.buf[:size]
            out.append(Packet(raw[1], opcode_raw & OPCODE_MASK, bool(opcode_raw & RETRANSMIT_FLAG), raw[4:-4]))


# --- Mensajes MLC ---------------------------------------------------------------------------------
def encode_message(opcode: int, msg_id: int, payload: bytes = b"") -> bytes:
    """Mensaje MLC: la longitud incluye la cabecera de 4 bytes."""
    return struct.pack("<HBB", 4 + len(payload), opcode, msg_id & 0xFF) + payload


@dataclass(frozen=True, slots=True)
class Message:
    """Mensaje MLC recibido. ``id`` relaciona peticiones con respuestas y comandos con sus avisos."""

    opcode: int
    id: int
    payload: bytes

    def u32(self) -> int:
        return struct.unpack_from("<I", self.payload)[0] if len(self.payload) >= 4 else 0


class MessageAssembler:
    """Corta el stream (ya ordenado por RSC) en mensajes MLC.

    El stream no lleva marcas de sincronía: una longitud imposible lo deja inutilizable hasta reconectar,
    y ``corrupted`` lo indica para que el cliente lo notifique.
    """

    def __init__(self) -> None:
        self.buf = bytearray()
        self.corrupted = False

    def feed(self, data: bytes) -> list[Message]:
        self.buf += data
        out: list[Message] = []
        while len(self.buf) >= 4 and not self.corrupted:
            size = struct.unpack_from("<H", self.buf)[0]
            if size < 4:
                self.corrupted = True  # sin marcas de sincronía no hay forma de realinear: se deja de parsear
                break
            if len(self.buf) < size:
                break
            raw = bytes(self.buf[:size])
            del self.buf[:size]
            out.append(Message(raw[2], raw[3], raw[4:]))
        return out


# --- Endpoint RSC fiable --------------------------------------------------------------------------
@dataclass
class _Pending:
    """Paquete fiable enviado y aún sin confirmar (candidato a retransmisión)."""

    raw: bytes
    seq: int
    opcode: int
    payload: bytes
    sent_at: float
    resends: int = 0


class RscEndpoint:
    """Extremo RSC (sirve para el host y, con ``role='device'``, para el emulador de los tests).

    Hilo propio de E/S: lee, despacha, envía acks y retransmite cada ``retransmit_s``. Los envíos se hacen
    desde el hilo que llama; ``_lock`` protege la ventana de pendientes y los contadores de secuencia, y
    ``_stream_lock`` impide que dos mensajes del stream se intercalen.
    """

    def __init__(
        self,
        transport: Transport,
        role: str = "host",
        on_stream: Callable[[bytes], None] | None = None,
        on_control: Callable[[Packet], None] | None = None,
        retransmit_s: float = 0.08,
        session: int = 0,
    ) -> None:
        self.t = transport
        self.role = role
        self.on_stream = on_stream
        self.on_control = on_control
        self.retransmit_s = retransmit_s
        self.session = session
        self.parser = PacketParser()
        # El host empieza con valores mínimos y los amplía tras preguntar al dispositivo (``connect``).
        self.max_packet = HOST_INITIAL_PACKET_SIZE if role == "host" else 64
        self.window = 1 if role == "host" else 32
        self._seq = 0
        self._stream_tx = 0
        self._stream_rx = 0
        self._rx_ahead: dict[int, bytes] = {}  # trozos del stream llegados antes de tiempo, por offset
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Condition()
        self._stream_lock = threading.Lock()
        self._running = threading.Event()
        self.error: str | None = None  # último error de E/S (p. ej. USB desconectado)
        self._thread: threading.Thread | None = None
        self.last_rx = 0.0
        self.stats = {"sent": 0, "resent": 0, "received": 0}

    # --- ciclo de vida ---
    def start(self) -> None:
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name=f"rsc-{self.role}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        with self._lock:
            self._lock.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def reset_session(self, session: int) -> None:
        """Empieza una sesión nueva: secuencias y offsets a cero y se olvida todo lo pendiente."""
        with self._lock:
            self.session = session & 0xFFFFFFFF
            self._seq = 0
            self._stream_tx = 0
            self._stream_rx = 0
            self._rx_ahead.clear()
            self._pending.clear()
            self.parser = PacketParser()
            self._lock.notify_all()

    # --- envío ---
    def _send_raw(self, raw: bytes) -> None:
        self.t.write(raw)
        self.stats["sent"] += 1

    def send_oob(self, seq: int, opcode: int, payload: bytes = b"") -> None:
        """Envía un paquete fuera de la ventana y sin esperar confirmación (acks y respuestas)."""
        self._send_raw(encode_packet(seq, opcode, payload, self.session))

    def send_reliable(self, opcode: int, payload: bytes = b"", timeout_s: float = 5.0) -> int:
        """Envía un paquete de control fiable y devuelve su seq. Espera hueco en la ventana hasta ``timeout_s``."""
        deadline = time.monotonic() + timeout_s
        with self._lock:
            while len(self._pending) >= self.window:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._running.is_set():
                    raise TimeoutError(t("Ventana RSC llena: el dispositivo no confirma paquetes",
                                         "RSC window full: the device is not acknowledging packets"))
                self._lock.wait(remaining)
            seq = self._seq
            self._seq = (self._seq + 1) & 0xFF
            raw = encode_packet(seq, opcode, payload, self.session)
            self._pending[seq] = _Pending(raw, seq, opcode, payload, time.monotonic())
        self._send_raw(raw)
        return seq

    def send_stream(self, data: bytes, timeout_s: float = 5.0, force: bool = False) -> int:
        """Envía bytes del stream y devuelve el seq del último paquete. El offset se asigna solo cuando hay
        hueco en la ventana (si se agota el tiempo no queda ningún hueco en el stream) y un mensaje completo
        no se intercala con otro hilo. Con ``force``, al agotarse el tiempo se envía igualmente por encima
        de la ventana (cancelación de emergencia: mejor un paquete de más que botones pulsados)."""
        chunk = max(1, self.max_packet - OVERHEAD)
        seq = -1
        with self._stream_lock:
            for i in range(0, len(data), chunk):
                part = data[i:i + chunk]
                deadline = time.monotonic() + timeout_s
                with self._lock:
                    while len(self._pending) >= self.window:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not self._running.is_set():
                            if force:
                                break
                            raise TimeoutError(t("Ventana RSC llena: el dispositivo no confirma paquetes",
                                                 "RSC window full: the device is not acknowledging packets"))
                        self._lock.wait(remaining)
                    offset = self._stream_tx
                    self._stream_tx = (self._stream_tx + len(part)) & 0xFFFF
                    seq = self._seq
                    self._seq = (self._seq + 1) & 0xFF
                    payload = struct.pack("<H", offset) + part
                    raw = encode_packet(seq, ASK_STREAM_DATA, payload, self.session)
                    self._pending[seq] = _Pending(raw, seq, ASK_STREAM_DATA, payload, time.monotonic())
                self._send_raw(raw)
        return seq

    def alive(self) -> bool:
        """El hilo de E/S sigue vivo (si muere, nada se confirma ni se retransmite)."""
        return self._thread is not None and self._thread.is_alive()

    def wait_acked(self, seq: int, timeout_s: float) -> bool:
        """Espera a que el paquete ``seq`` se confirme. Devuelve ``False`` si vence el plazo."""
        deadline = time.monotonic() + timeout_s
        with self._lock:
            while seq in self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lock.wait(remaining)
            return True

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def _ack(self, seq: int) -> None:
        with self._lock:
            if self._pending.pop(seq, None) is not None:
                self._lock.notify_all()

    # --- recepción ---
    def _loop(self) -> None:
        """Hilo de E/S. No lanza: los errores quedan en ``error`` y un fallo persistente de lectura lo detiene."""
        next_rtx = time.monotonic() + self.retransmit_s
        failures = 0
        while self._running.is_set():
            try:
                data = self.t.read(512, 0.005)
                failures = 0
            except Exception as exc:  # p. ej. cable desconectado
                failures += 1
                self.error = t("Error leyendo del puerto: {exc}", "Error reading from the port: {exc}", exc=exc)
                if failures >= 20:
                    self.error = t("Enlace serie perdido (20 lecturas fallidas seguidas): {exc}",
                                   "Serial link lost (20 consecutive failed reads): {exc}", exc=exc)
                    self._running.clear()  # que los envíos en espera fallen ya, no a los 5 s
                    with self._lock:
                        self._lock.notify_all()
                    return  # el estado del mando lo reflejará (alive() = False) y pedirá reconectar
                time.sleep(0.05)
                continue
            if data:
                for pkt in self.parser.feed(data, self.session):
                    self.last_rx = time.monotonic()
                    self.stats["received"] += 1
                    try:
                        self._dispatch(pkt)
                    except Exception as exc:
                        self.error = t("Error procesando un paquete 0x{op:02X}: {exc}", "Error processing packet 0x{op:02X}: {exc}",
                                       op=pkt.opcode, exc=exc)
            now = time.monotonic()
            if now >= next_rtx:
                try:
                    self._retransmit(now)
                except Exception as exc:
                    self.error = t("Error escribiendo en el puerto: {exc}", "Error writing to the port: {exc}", exc=exc)
                next_rtx = now + self.retransmit_s

    def _retransmit(self, now: float) -> None:
        """Reenvía con la marca de retransmisión lo que lleva ``2 * retransmit_s`` sin confirmar.

        ``ASK_RESET`` no se retransmite: si no llega, ``connect`` falla y se reintenta (quizá a otros baudios).
        La escritura se hace fuera del candado para no bloquear a los hilos que envían.
        """
        with self._lock:
            due = [p for p in self._pending.values()
                   if now - p.sent_at >= 2 * self.retransmit_s and p.opcode != ASK_RESET]
            for p in due:
                p.raw = encode_packet(p.seq, p.opcode | RETRANSMIT_FLAG, p.payload, self.session)
                p.sent_at = now
                p.resends += 1
        for p in due:
            self._send_raw(p.raw)
            self.stats["resent"] += 1

    def _dispatch(self, pkt: Packet) -> None:
        op = pkt.opcode
        if op == ASK_STREAM_DATA:
            self._on_stream_packet(pkt)
            return
        if op in ACK_OPCODES:
            self._ack(pkt.seq)
        if self.on_control:
            self.on_control(pkt)

    def _on_stream_packet(self, pkt: Packet) -> None:
        """Reordena el stream por offset y entrega a ``on_stream`` los bytes ya contiguos.

        En aritmética módulo 2^16, un offset en la mitad «futura» se guarda; uno en la mitad «pasada» es un
        duplicado ya entregado y solo se vuelve a confirmar.
        """
        if len(pkt.payload) < 2:
            return
        offset = struct.unpack_from("<H", pkt.payload)[0]
        data = pkt.payload[2:]
        delivered = b""
        with self._lock:
            delta = (offset - self._stream_rx) & 0xFFFF
            if delta < 0x8000:
                self._rx_ahead[offset] = data
                while self._stream_rx in self._rx_ahead:
                    part = self._rx_ahead.pop(self._stream_rx)
                    delivered += part
                    self._stream_rx = (self._stream_rx + len(part)) & 0xFFFF
        # Primero se entregan los datos (el offset ya avanzó: si algo fallara antes se perderían para
        # siempre y el ensamblador de mensajes se desalinearía) y luego se confirma, siempre (también los
        # duplicados del pasado), con bytes libres (un valor fijo). Si la confirmación no sale, el
        # dispositivo retransmite y el duplicado se vuelve a confirmar.
        try:
            if delivered and self.on_stream:
                self.on_stream(delivered)
        finally:
            try:
                self.send_oob(pkt.seq, RET_STREAM_DATA, struct.pack("<I", 4096))
            except Exception as exc:
                self.error = t("Error escribiendo en el puerto: {exc}", "Error writing to the port: {exc}", exc=exc)
