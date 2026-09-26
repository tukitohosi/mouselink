"""Boundary policy for an absolute mouse, activated only after device calibration.

This model tracks commanded normalized screen coordinates, not observed iPad
coordinates. Its result is valid only for an experimentally verified mapping.
"""

from dataclasses import dataclass, field


@dataclass
class AbsolutePointer:
    x: float = 655.0
    y: float = 16384.0
    settle_seconds: float = 0.15
    edge_sent_at: float | None = None
    edge_enabled: bool = True
    pending: dict = field(default_factory=dict)

    @staticmethod
    def _clamp(value):
        return max(0.0, min(32767.0, value))

    @property
    def report_position(self):
        return round(self.x), round(self.y)

    def move(self, dx, dy, now, buttons=0):
        """Update target, then report whether a later outward push can leave."""
        was_at_left = self.x == 0
        self.x = self._clamp(self.x + dx)
        self.y = self._clamp(self.y + dy)
        if self.x > 0 or buttons:
            self.edge_sent_at = None
            self.pending.clear()
        return bool(
            self.edge_enabled and not buttons and was_at_left and self.x == 0
            and dx < 0 and self.edge_sent_at is not None
            and now - self.edge_sent_at >= self.settle_seconds
        )

    def sent(self, x, y, now, success, buttons=0):
        """Mark a BLE-confirmed left-edge report, never a serial write alone."""
        if not success:
            self.edge_sent_at = None
        elif x == 0 and self.x == 0 and not buttons:
            if self.edge_sent_at is None:
                self.edge_sent_at = now
        else:
            self.edge_sent_at = None

    def submitted(self, sequence, x, y, now, success, buttons=0):
        if not success:
            self.edge_sent_at = None
            self.pending.clear()
            return
        self.pending = {k: v for k, v in self.pending.items() if now - v[2] < 0.7}
        if self.x == 0 and x == 0 and not buttons:
            self.pending[sequence] = (x, y, now)

    def acknowledge(self, sequence, x, y, buttons, accepted, now):
        command = self.pending.pop(sequence, None)
        if command is None or command[:2] != (x, y) or not 0 <= now - command[2] < 0.7:
            return
        self.sent(x, y, now, accepted, buttons)


def rotate_position(x, y, clockwise_quarters=0):
    """Optional calibrated HID orientation; default is OS-managed orientation."""
    for _ in range(clockwise_quarters % 4):
        x, y = 32767 - y, x
    return x, y
