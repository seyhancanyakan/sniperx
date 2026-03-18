"""PinShot Bot — Sound Alerts"""

import subprocess
import logging

logger = logging.getLogger("pinshot.sound")


class SoundAlert:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def play_alert(self, alert_type: str = "signal"):
        if not self.enabled:
            return
        try:
            freq = {"signal": 1000, "tp_hit": 1500, "sl_hit": 500, "zone": 800}
            f = freq.get(alert_type, 1000)
            subprocess.Popen(
                ["beep", "-f", str(f), "-l", "200"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass  # Sound is optional, fail silently
