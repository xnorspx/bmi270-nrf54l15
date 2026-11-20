"""BLE transport: owns an asyncio loop in a daemon thread, posts events to a Queue.

No UI imports here - the same class is usable headless, which is what the
end-to-end test does.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from datetime import datetime

from bleak import BleakClient, BleakScanner

from .protocol import DEVICE_NAME, RX_UUID, TX_UUID, decode


class BleSource:
    def __init__(self) -> None:
        self.events: queue.Queue = queue.Queue()
        self.connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: BleakClient | None = None
        self._ready = threading.Event()
        self.address = ""

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ble-loop")
        self._thread.start()
        # submitting work before the loop exists silently drops the coroutine
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def submit(self, coro) -> None:
        if self._loop is None:
            self.events.put(("error", "BLE loop not running"))
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- actions ----------------------------------------------------------
    def connect(self) -> None:
        self.submit(self._connect())

    def disconnect(self) -> None:
        self.submit(self._disconnect())

    def command(self, text: str) -> None:
        self.submit(self._command(text))

    async def _connect(self) -> None:
        try:
            self.events.put(("log", f"scanning for {DEVICE_NAME} ..."))
            dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=12.0)
            if dev is None:
                self.events.put(("error", "device not found - is it powered and advertising?"))
                return
            self._client = BleakClient(dev, timeout=20.0)
            await self._client.connect()
            await self._client.start_notify(TX_UUID, self._on_notify)
            self.connected = True
            self.address = dev.address
            self.events.put(("connected", dev.address))
        except Exception as exc:                       # noqa: BLE001
            self.events.put(("error", f"connect failed: {exc}"))

    async def _disconnect(self) -> None:
        try:
            if self._client and self._client.is_connected:
                await self._client.stop_notify(TX_UUID)
                await self._client.disconnect()
        except Exception:
            pass
        self.connected = False
        self.events.put(("disconnected", ""))

    async def _command(self, text: str) -> None:
        try:
            if self._client and self._client.is_connected:
                await self._client.write_gatt_char(RX_UUID, (text + "\n").encode(),
                                                   response=False)
        except Exception as exc:                       # noqa: BLE001
            self.events.put(("error", f"command failed: {exc}"))

    def _on_notify(self, _char, data: bytearray) -> None:
        parsed = decode(bytes(data))
        if parsed:
            seq, dropped, rows = parsed
            self.events.put(("samples", (seq, dropped, rows)))
