import ctypes
import unittest
from urllib.parse import parse_qs, urlsplit

from autodispatch.host import configure_backdrop, desktop_url


class FakeDwm:
    def __init__(self, backdrop_result=0, frame_result=0):
        self.backdrop_result = backdrop_result
        self.frame_result = frame_result
        self.attributes = []
        self.frame_calls = 0

    def DwmSetWindowAttribute(self, hwnd, key, pointer, size):
        value = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value
        self.attributes.append((key, value))
        return self.backdrop_result if key == 38 and value == 3 else 0

    def DwmExtendFrameIntoClientArea(self, hwnd, margins):
        self.frame_calls += 1
        return self.frame_result


class DesktopMaterialTests(unittest.TestCase):
    def test_demo_mode_survives_widget_and_task_detail_navigation(self):
        widget = desktop_url('http://127.0.0.1:1234/?mode=demo#/preview', '/widget')
        console = desktop_url(widget, '/console', 'IP-01')
        parts = urlsplit(console)
        self.assertEqual(parts.fragment, '/console')
        self.assertEqual(parse_qs(parts.query)['mode'], ['demo'])
        self.assertEqual(parse_qs(parts.query)['host'], ['desktop'])
        self.assertEqual(parse_qs(parts.query)['task'], ['IP-01'])
        self.assertNotIn('task', parse_qs(urlsplit(widget).query))

    def test_acrylic_requires_both_backdrop_and_frame_success(self):
        dwm = FakeDwm()
        self.assertEqual(configure_backdrop(1, dwm)['mode'], 'acrylic')
        self.assertEqual(dwm.frame_calls, 1)

    def test_unsupported_backdrop_returns_fallback_without_frame_extension(self):
        dwm = FakeDwm(backdrop_result=-2147024809)
        result = configure_backdrop(1, dwm)
        self.assertEqual(result['mode'], 'fallback')
        self.assertEqual(result['hresult'], -2147024809)
        self.assertEqual(dwm.frame_calls, 0)

    def test_failed_frame_extension_rolls_back_the_backdrop(self):
        dwm = FakeDwm(frame_result=-1)
        self.assertEqual(configure_backdrop(1, dwm)['mode'], 'fallback')
        self.assertEqual(dwm.attributes[-1], (38, 1))

    def test_disabled_system_transparency_is_not_reported_as_acrylic(self):
        dwm = FakeDwm()
        result = configure_backdrop(1, dwm, transparency=False)
        self.assertEqual(result, {'mode': 'fallback', 'reason': 'transparency-disabled'})
        self.assertEqual(dwm.frame_calls, 0)
