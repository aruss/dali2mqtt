"""The isolated python-dali 0.11 private-method integration for USB capture."""
from dali.driver.hid import tridonic

from .bus_monitor import get_monitor


class MonitoredTridonic(tridonic):
    """Capture reports without replacing library transmission or bus-watch loops."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        get_monitor(self, driver_name="tridonic", raw_reports=True)

    def _handle_read(self, data):
        try:
            # Copy before library processing or caller buffer reuse. Capture
            # failures must never prevent or duplicate original driver handling.
            self._bus_monitor.capture_report(bytes(data))
        except Exception:
            self._bus_monitor.note_capture_error()
        finally:
            super()._handle_read(data)
