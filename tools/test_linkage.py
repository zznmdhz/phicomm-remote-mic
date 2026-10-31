"""Offline linkage regression tests; no Bluetooth, audio device or key injection.

Run with app/python/python.exe tools/test_linkage.py.
"""
import ctypes
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import remote_mic as mic


class LinkageTests(unittest.TestCase):
    def setUp(self):
        self.app = mic.RemoteMic()
        self.app.hotkey_enabled = True
        self.app.hotkey = mic.load_hotkey()
        self.app.audio = mic.AudioPipe()
        self.taps = []
        self.tap = patch.object(mic, 'send_dictation_tap', self.capture)
        self.tap.start()
        self.worker = threading.Thread(target=self.app._linkage_watchdog)
        self.worker.start()

    def capture(self, hotkey):
        self.taps.append((time.monotonic(), dict(hotkey)))

    def tearDown(self):
        self.app._linkage_stop.set()
        self.worker.join(1)
        self.tap.stop()
        self.assertFalse(self.worker.is_alive())

    def packet(self):
        self.app.audio.last_packet = time.monotonic()
        self.app.audio.speech_event.set()

    def wait_taps(self, count, timeout=0.7):
        deadline = time.monotonic() + timeout
        while len(self.taps) < count and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(self.taps), count)

    def test_two_utterances_and_release_deadline(self):
        for count in (0, 2):
            start = time.monotonic()
            self.packet()
            self.wait_taps(count + 1)
            self.assertLess(self.taps[-1][0] - start, 0.1)
            for _ in range(6):
                time.sleep(0.04)
                self.packet()
            last = self.app.audio.last_packet
            self.wait_taps(count + 2)
            self.assertGreaterEqual(self.taps[-1][0] - last, mic.LINKAGE_SILENCE)
            self.assertLess(self.taps[-1][0] - last, 0.5)
            time.sleep(0.05)
            self.assertEqual(len(self.taps), count + 2)

    def test_enable_after_connection(self):
        self.app.hotkey_enabled = False
        self.packet()
        time.sleep(0.08)
        self.assertEqual(self.taps, [])
        self.app.hotkey_enabled = True
        self.packet()
        self.wait_taps(1)

    def test_disable_finishes_once_with_original_hotkey(self):
        original = dict(self.app.hotkey)
        self.packet()
        self.wait_taps(1)
        self.app.hotkey = {'mods': [], 'mod_vks': [], 'key': 'f9', 'vk': 0x78}
        self.app.hotkey_enabled = False
        self.wait_taps(2)
        self.assertEqual(self.taps[1][1], original)
        self.app._linkage_stop.set()
        self.worker.join(1)
        self.assertEqual(len(self.taps), 2)

    def test_disconnect_finishes_once(self):
        self.packet()
        self.wait_taps(1)
        self.app.audio = None
        self.worker.join(1)
        self.assertEqual(len(self.taps), 2)

    def test_stop_finishes_before_worker_exits(self):
        self.packet()
        self.wait_taps(1)
        self.app.stop()
        self.worker.join(1)
        self.assertEqual(len(self.taps), 2)


class InjectionTests(unittest.TestCase):
    def test_combo_order_extended_flags_and_visibility(self):
        events, held = [], set()
        class User32:
            def keybd_event(self, vk, scan, flags, extra):
                events.append((vk, flags))
                if flags & mic.KEYEVENTF_KEYUP:
                    held.discard(vk)
                else:
                    held.add(vk)

            def GetAsyncKeyState(self, vk):
                return 0x8000 if vk in held else 0

        hk = {'mods': ['ctrl'], 'mod_vks': [0xA3], 'key': 'down', 'vk': 0x28}
        with patch.object(ctypes.windll, 'user32', User32()):
            self.assertTrue(mic.send_dictation_tap(hk, verify=True))
        self.assertEqual(events, [(0xA3, 1), (0x28, 1), (0x28, 3), (0xA3, 3)])
        self.assertFalse(held)

    def test_filtered_key_is_not_reported_as_success(self):
        class User32:
            def keybd_event(self, *args):
                pass

            def GetAsyncKeyState(self, vk):
                return 0

        with patch.object(ctypes.windll, 'user32', User32()):
            self.assertFalse(mic.send_dictation_tap(mic.DEFAULT_HOTKEY, verify=True))

    def test_invalid_ime_key_falls_back_to_left_alt(self):
        hk = {'mods': [], 'mod_vks': [], 'key': '??', 'vk': 229}
        with patch.object(mic, 'load_config', return_value={'hotkey': hk}):
            self.assertEqual(mic.load_hotkey()['vk'], 0xA4)
        self.assertFalse(mic.valid_hotkey(hk))


if __name__ == '__main__':
    unittest.main(verbosity=2)
