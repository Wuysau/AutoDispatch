import unittest

from autodispatch.host import HostApi, WidgetBridge, WindowBridge
from unittest.mock import patch
from types import SimpleNamespace


class FakeWindow:
    def __init__(self):
        self.on_top = False
        self.size = None

    def resize(self, width, height):
        self.size = (width, height)


class HostApiTests(unittest.TestCase):
    def test_drag_is_queued_on_the_selected_window_not_the_widget(self):
        api = HostApi('unused')
        api.window = SimpleNamespace(native=object())
        api.console = SimpleNamespace(native=object())
        with patch('autodispatch.host._on_ui_thread') as dispatch:
            result = WindowBridge(api, 'console').begin_drag()
        self.assertEqual(result, {'queued': True})
        self.assertIs(dispatch.call_args.args[0], api.console.native)
        self.assertEqual(api.results, {})

    def test_drag_refuses_missing_or_unknown_window(self):
        api = HostApi('unused')
        with self.assertRaises(ValueError):
            WindowBridge(api, 'unknown')
        with self.assertRaises(RuntimeError):
            WindowBridge(api, 'console').begin_drag()

    def test_widget_bridge_marshals_resize_without_command_queue(self):
        api = HostApi('unused')
        calls = []
        api.window = SimpleNamespace(native=SimpleNamespace(Invoke=lambda fn: fn()))
        api.apply = lambda request: calls.append(request)
        with patch.dict('sys.modules', {'System': SimpleNamespace(Action=lambda fn: fn)}):
            result = WidgetBridge(api).resize_widget(440, 151)
        self.assertEqual(result, {'width': 440, 'height': 151})
        self.assertEqual(calls, [{'action': 'size', 'target': 'widget', 'width': 440, 'height': 151}])
        self.assertEqual(api.results, {})

    def test_widget_bridge_rejects_invalid_geometry(self):
        bridge = WidgetBridge(HostApi('unused'))
        for width, height in [(0, 100), (440, 5000), (True, 100), ('440', 100)]:
            with self.assertRaises(ValueError):
                bridge.resize_widget(width, height)

    def test_calls_without_window_are_refused(self):
        api = HostApi("http://127.0.0.1:5174/#/console")
        self.assertFalse(api.resize(100, 100))
        self.assertFalse(api.set_always_on_top(True))

    def test_resize_and_pin_forward_to_window(self):
        api = HostApi("http://127.0.0.1:5174/#/console")
        window = FakeWindow()
        api.window = window
        self.assertTrue(api.resize("444", 190.0))
        self.assertEqual(window.size, (444, 190))
        self.assertTrue(api.set_always_on_top(1))
        self.assertTrue(window.on_top)


if __name__ == "__main__":
    unittest.main()
