"""Pinned python-dali 0.11 integration for USB capture and stalled-send recovery."""
import asyncio

from dali.driver.hid import tridonic
from dali.exceptions import CommunicationError

from .bus_monitor import get_monitor


class MonitoredTridonic(tridonic):
    """Capture reports and bound the library's otherwise unbounded USB wait."""

    response_timeout = 2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        get_monitor(self, driver_name="tridonic", raw_reports=True)

    async def _send_raw(self, command):
        try:
            return await asyncio.wait_for(super()._send_raw(command), self.response_timeout)
        except asyncio.TimeoutError as err:
            # Upstream only removes its outstanding entry on success. Closing
            # the connection clears stale sequence IDs and wakes other waiters.
            self._log.warning("Tridonic USB response timed out for %s; reconnecting", command)
            self.disconnect(reconnect=True)
            raise CommunicationError(f"Tridonic USB response timed out for {command}") from err
        except asyncio.CancelledError:
            if self._outstanding:
                self.disconnect(reconnect=True)
            raise

    def _handle_read(self, data):
        try:
            # Copy before library processing or caller buffer reuse. Capture
            # failures must never prevent or duplicate original driver handling.
            self._bus_monitor.capture_report(bytes(data))
        except Exception:
            self._bus_monitor.note_capture_error()
        finally:
            super()._handle_read(data)
