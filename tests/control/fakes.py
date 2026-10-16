"""Dobles de prueba del enlace con el ESP32: tuberías en memoria, transporte con fallos y un ESP32 emulado
(lado dispositivo de PABotBase2).

Implementa lo que el host necesita (reset, versión, tamaños, mensajes de identificación, estado,
cola de comandos con ``CQ_COMMAND_FINISHED`` y ``CQ_CANCEL``). La semántica real del firmware (cerrado)
puede diferir.
"""
from __future__ import annotations

import collections
import queue
import random
import struct
import threading
import time
from dataclasses import dataclass, field

from rngauto.control.pabotbase2 import (
    ASK_BUFFER_SLOTS, ASK_PACKET_SIZE, ASK_RESET, ASK_VERSION, CONTROLLER_NS1_WIRELESS_PRO, MSG_CHANGE_CONTROLLER_MODE,
    MSG_CONTROLLER_LIST, MSG_CQ_CANCEL, MSG_CQ_CAPACITY, MSG_CQ_COMMAND_FINISHED, MSG_DEVICE_IDENTIFIER,
    MSG_DEVICE_NAME, MSG_FIRMWARE_VERSION, MSG_NS1_OEM_BUTTONS, MSG_PROTOCOL_VERSION, MSG_READ_CONTROLLER_MODE,
    MSG_REQUEST_STATUS, MSG_RET_DATA, MSG_RET_U32, MSG_RET_U32_DATA, PID_ESP32, PINNED_FIRMWARE, RET_BUFFER_SLOTS,
    RET_PACKET_SIZE, RET_RESET, RET_VERSION, RSC_PROTOCOL_VERSION, Message, MessageAssembler, Packet, RscEndpoint,
    encode_message,
)
from rngauto.control.transport import Transport
from rngauto.i18n import t


@dataclass
class ExecutedCommand:
    cmd_id: int
    duration_ms: int
    buttons: bytes
    started_at: float
    finished_at: float = 0.0
    cancelled: bool = False


@dataclass
class FakeConfig:
    rsc_version: int = RSC_PROTOCOL_VERSION
    mlc_version: int = 2026061804
    program_id: int = PID_ESP32
    firmware: int = PINNED_FIRMWARE
    name: str = field(default_factory=lambda: t("PABotBase2-ESP32 (emulado)", "PABotBase2-ESP32 (emulated)"))
    controllers: tuple[int, ...] = (CONTROLLER_NS1_WIRELESS_PRO, 0x1181, 0x1182)
    queue_capacity: int = 32
    packet_size: int = 64
    buffer_slots: int = 16
    time_scale: float = 1.0  # <1 acelera la ejecución de comandos en tests
    paired: bool = True


@dataclass
class FakeESP32:
    transport: Transport
    config: FakeConfig = field(default_factory=FakeConfig)

    def __post_init__(self) -> None:
        self.rsc = RscEndpoint(self.transport, "device", on_stream=self._on_stream, on_control=self._on_control)
        self.rsc.max_packet = self.config.packet_size
        self.rsc.window = 32
        self.messages = MessageAssembler()
        self.mode = 0
        self.queue: collections.deque[tuple[int, int, bytes]] = collections.deque()
        self.executed: list[ExecutedCommand] = []
        self.received: list[Message] = []
        self._cond = threading.Condition()
        self._running = threading.Event()
        self._exec: threading.Thread | None = None
        self._cancel_epoch = 0

    def start(self) -> "FakeESP32":
        self._running.set()
        self.rsc.start()
        self._exec = threading.Thread(target=self._executor, name="fake-esp32-exec", daemon=True)
        self._exec.start()
        return self

    def stop(self) -> None:
        self._running.clear()
        with self._cond:
            self._cond.notify_all()
        self.rsc.stop()

    # --- RSC ---
    def _on_control(self, pkt: Packet) -> None:
        cfg = self.config
        if pkt.opcode == ASK_RESET and not pkt.retransmit:
            self.rsc.reset_session(pkt.u32())
            self.messages = MessageAssembler()
            with self._cond:
                self.queue.clear()
            self.rsc.send_oob(pkt.seq, RET_RESET)
        elif pkt.opcode == ASK_VERSION:
            self.rsc.send_oob(pkt.seq, RET_VERSION, struct.pack("<I", cfg.rsc_version))
        elif pkt.opcode == ASK_PACKET_SIZE:
            self.rsc.send_oob(pkt.seq, RET_PACKET_SIZE, struct.pack("<I", cfg.packet_size))
        elif pkt.opcode == ASK_BUFFER_SLOTS:
            self.rsc.send_oob(pkt.seq, RET_BUFFER_SLOTS, struct.pack("<I", cfg.buffer_slots))

    # --- MLC ---
    def _reply(self, opcode: int, msg_id: int, payload: bytes = b"") -> None:
        self.rsc.send_stream(encode_message(opcode, msg_id, payload))

    def _on_stream(self, data: bytes) -> None:
        for msg in self.messages.feed(data):
            self.received.append(msg)
            self._handle(msg)

    def _handle(self, msg: Message) -> None:
        cfg = self.config
        u32 = lambda v: struct.pack("<I", v)  # noqa: E731
        op = msg.opcode
        if op == MSG_PROTOCOL_VERSION:
            self._reply(MSG_RET_U32, msg.id, u32(cfg.mlc_version))
        elif op == MSG_FIRMWARE_VERSION:
            self._reply(MSG_RET_U32, msg.id, u32(cfg.firmware))
        elif op == MSG_DEVICE_IDENTIFIER:
            self._reply(MSG_RET_U32, msg.id, u32(cfg.program_id))
        elif op == MSG_DEVICE_NAME:
            self._reply(MSG_RET_DATA, msg.id, cfg.name.encode())
        elif op == MSG_CONTROLLER_LIST:
            self._reply(MSG_RET_DATA, msg.id, b"".join(u32(c) for c in cfg.controllers))
        elif op == MSG_CQ_CAPACITY:
            self._reply(MSG_RET_U32, msg.id, u32(cfg.queue_capacity))
        elif op == MSG_READ_CONTROLLER_MODE:
            self._reply(MSG_RET_U32, msg.id, u32(self.mode))
        elif op == MSG_CHANGE_CONTROLLER_MODE:
            self.mode = msg.u32()
        elif op == MSG_REQUEST_STATUS:
            flags = (0b111 if cfg.paired and self.mode else 0)
            self._reply(MSG_RET_U32_DATA, msg.id, u32(self.mode) + bytes((flags, 1)) + b"\x00" * 6)
        elif op == MSG_CQ_CANCEL:
            with self._cond:
                self.queue.clear()
                self._cancel_epoch += 1
                self._cond.notify_all()
        elif op == MSG_NS1_OEM_BUTTONS:
            ms = struct.unpack_from("<H", msg.payload)[0]
            with self._cond:
                self.queue.append((msg.id, ms, msg.payload[2:12]))
                self._cond.notify_all()

    def _executor(self) -> None:
        while self._running.is_set():
            with self._cond:
                while not self.queue and self._running.is_set():
                    self._cond.wait(0.05)
                if not self._running.is_set():
                    return
                cmd_id, ms, buttons = self.queue.popleft()
                epoch = self._cancel_epoch
            rec = ExecutedCommand(cmd_id, ms, buttons, time.monotonic())
            self.executed.append(rec)
            end = time.monotonic() + ms / 1000 * self.config.time_scale
            with self._cond:
                while time.monotonic() < end and self._cancel_epoch == epoch:
                    self._cond.wait(max(0.0, end - time.monotonic()))
                cancelled = self._cancel_epoch != epoch
            rec.finished_at = time.monotonic()
            rec.cancelled = cancelled
            if not cancelled:
                self._reply(MSG_CQ_COMMAND_FINISHED, cmd_id, struct.pack("<I", int(rec.finished_at * 1000) & 0xFFFFFFFF))


class _Pipe:
    def __init__(self) -> None:
        self.q: queue.Queue[bytes] = queue.Queue()
        self.pending = b""


class PipeEnd:
    """Extremo de una tubería bidireccional en memoria."""

    def __init__(self, rx: _Pipe, tx: _Pipe) -> None:
        self._rx, self._tx = rx, tx
        self.closed = False

    def write(self, data: bytes) -> None:
        if not self.closed:
            self._tx.q.put(bytes(data))

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        rx = self._rx
        if not rx.pending:
            try:
                rx.pending = rx.q.get(timeout=timeout_s) if timeout_s > 0 else rx.q.get_nowait()
            except queue.Empty:
                return b""
        out, rx.pending = rx.pending[:max_bytes], rx.pending[max_bytes:]
        return out

    def close(self) -> None:
        self.closed = True


def pipe_pair() -> tuple[PipeEnd, PipeEnd]:
    a, b = _Pipe(), _Pipe()
    return PipeEnd(a, b), PipeEnd(b, a)


class FaultyTransport:
    """Envoltorio que pierde o corrompe escrituras (inyección de fallos para tests)."""

    def __init__(self, inner: Transport, drop_prob: float = 0.0, corrupt_prob: float = 0.0, seed: int = 0) -> None:
        self.inner = inner
        self.drop_prob = drop_prob
        self.corrupt_prob = corrupt_prob
        self.rng = random.Random(seed)
        self.dropped = 0
        self.corrupted = 0

    def write(self, data: bytes) -> None:
        r = self.rng.random()
        if r < self.drop_prob:
            self.dropped += 1
            return
        if r < self.drop_prob + self.corrupt_prob and data:
            buf = bytearray(data)
            buf[self.rng.randrange(len(buf))] ^= 0x5A
            self.corrupted += 1
            data = bytes(buf)
        self.inner.write(data)

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        return self.inner.read(max_bytes, timeout_s)

    def close(self) -> None:
        self.inner.close()
