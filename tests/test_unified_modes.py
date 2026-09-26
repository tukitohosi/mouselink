import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "open_bridge"))
from bridge import BridgeConfig, KVMController, keyboard
from pointer_boundary import AbsolutePointer
from absolute_protocol import Status


class UnifiedModeTests(unittest.TestCase):
    def controller(self, mode):
        controller = KVMController(BridgeConfig(mode=mode, absolute_enabled=True))
        controller._is_active = True
        controller._pointer = AbsolutePointer(x=160, edge_enabled=mode != "locked")
        controller._bridge = Mock()
        controller._bridge.status = Status()
        controller._bridge.send_absolute_report.return_value = True
        return controller

    def test_edge_and_mixed_require_ble_confirmation_and_fresh_push(self):
        for mode in ("edge", "mixed"):
            c = self.controller(mode)
            c._handle_raw_mouse_move(-20, 0)
            self.assertFalse(c._exit_requested)
            self.assertEqual(c._pointer.x, 0)
            self.assertIsNone(c._pointer.edge_sent_at)
            seq = c._sequence
            ack_time = time.monotonic()
            c._bridge.status = Status(ack_sequence=seq, ack_accepted=True,
                                      ack_x=0, ack_y=16384, ack_at=ack_time)
            c._confirm_absolute_edge()
            c._handle_raw_mouse_move(-20, 0)
            self.assertFalse(c._exit_requested)
            with patch("bridge.time.monotonic", return_value=ack_time + .2):
                c._handle_raw_mouse_move(-20, 0)
            self.assertTrue(c._exit_requested)

    def test_locked_mode_ignores_confirmed_edge(self):
        c = self.controller("locked")
        c._pointer.x = 0
        c._pointer.sent(0, 16384, time.monotonic() - 1, True)
        c._handle_raw_mouse_move(-100, 0)
        self.assertFalse(c._exit_requested)

    def test_standalone_chord_only_exits_modes_that_enable_it(self):
        for mode in ("edge", "mixed", "locked"):
            c = self.controller(mode)
            c._handle_key_press(keyboard.Key.ctrl_l)
            c._handle_key_press(keyboard.Key.alt_l)
            c._handle_key_release(keyboard.Key.ctrl_l)
            c._handle_key_release(keyboard.Key.alt_l)
            self.assertEqual(c._exit_requested, mode != "edge")

    def test_failed_absolute_send_releases_to_windows(self):
        c = self.controller("mixed")
        c._bridge.send_absolute_report.return_value = False
        c._handle_raw_mouse_move(-100, 0)
        self.assertTrue(c._exit_requested)
        self.assertIsNone(c._pointer.edge_sent_at)

    def test_cleanup_releases_absolute_buttons_and_keyboard(self):
        c = self.controller("mixed")
        c._state.mouse_buttons = 1
        c._state.active_modifiers = 1
        c._release_all_remote_inputs()
        c._bridge.send_absolute_report.assert_called_with(0, 160, 16384, 0, 1)
        c._bridge.send_keyboard_report.assert_called_with(0, [])

    def test_reader_failure_closes_serial_for_usb_reconnect(self):
        import threading
        import serial
        from bridge import SerialBridge
        bridge = SerialBridge(BridgeConfig())
        connection = Mock(is_open=True, in_waiting=0)
        connection.read.side_effect = serial.SerialException("unplugged")
        connection.close.side_effect = lambda: setattr(connection, "is_open", False)
        bridge._serial = connection
        bridge._ble_connected = True
        bridge._read_status_loop(connection, threading.Event())
        self.assertFalse(bridge.is_connected)
        self.assertFalse(bridge.ble_connected)


if __name__ == "__main__":
    unittest.main()
