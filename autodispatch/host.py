"""Desktop presentation host; never starts or stops the dispatcher."""
from __future__ import annotations
import ctypes
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from .control import atomic_json
from .model import Blocked
from .store import CoordinatorLock

ORIGIN = (20, 20)
EXPANDED = (440, 390)
TAB = (52, 52)
CONSOLE_SIZE = (1000, 940)
CONSOLE_ORIGIN = (480, 40)
UI_HOST_FILE = 'ui-host.json'
UI_ALIVE_FILE = 'ui-host-alive'
UI_RESULT_FILE = 'ui-host-results.json'
TICK_MS = 150


def desktop_url(url: str, route: str, task_id: str | None = None) -> str:
    """Keep the selected data mode when creating either native window."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query['host'] = 'desktop'
    if task_id is not None:
        query['task'] = task_id
        query['focus'] = str(time.time_ns())
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', urlencode(query), route))

class HostApi:
    def __init__(self, console_url: str) -> None:
        self.window: Any = None
        self.console: Any = None
        self.console_url = console_url
        self.exiting = False
        self.timer = None
        self.tray = None
        self.started = False
        self.results: dict = {}
        self.applied: set = set()

    def set_always_on_top(self, enabled: bool) -> bool:
        if self.window is None:
            return False
        self.window.on_top = bool(enabled)
        return True

    def resize(self, width: int, height: int) -> bool:
        if self.window is None:
            return False
        self.window.resize(int(width), int(height))
        return True

    def open_console(self, task_id: str | None = None) -> bool:
        url = desktop_url(self.console_url, '/console', task_id)
        if self.console is None:
            import webview
            self.console = webview.create_window(
                'AutoDispatch Console', url, width=CONSOLE_SIZE[0], height=CONSOLE_SIZE[1],
                x=CONSOLE_ORIGIN[0], y=CONSOLE_ORIGIN[1], min_size=(720, 540),
                frameless=True, easy_drag=False, transparent=False, background_color='#191e28',
                js_api=WindowBridge(self, 'console'))
            self.console.events.closing += lambda: self.close_window(self.console)
            _material(self.console)
            _fit_work_area(self.console, beside=self.window)
        elif task_id:
            from System import Uri
            self.console.native.webview.Source = Uri(url)
        from System.Windows.Forms import FormWindowState
        self.console.native.Show()
        self.console.native.WindowState = FormWindowState.Normal
        self.console.native.Activate()
        return True

    def close_window(self, window):
        if not self.exiting:
            _on_ui_thread(window.native, window.native.Hide)
            return False

    def apply(self, request: dict) -> None:
        target, action = request['target'], request['action']
        window = self.window if target == 'widget' else self.console
        if window is None:
            raise RuntimeError('Window unavailable')
        if action == 'size':
            from System.Drawing import Size
            form = window.native
            scale = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(form.Handle.ToInt64())) / 96
            form.ClientSize = Size(round(int(request['width']) * scale), round(int(request['height']) * scale))
        elif action == 'pin':
            window.native.TopMost = bool(request['value'])
        elif action == 'console':
            if not self.open_console(request.get('task_id')):
                raise RuntimeError('Console unavailable')
        elif action == 'hide':
            window.native.Hide()
        elif action == 'show':
            from System.Windows.Forms import FormWindowState
            window.native.Show()
            window.native.WindowState = FormWindowState.Normal
            window.native.Activate()
        elif action == 'minimize':
            from System.Windows.Forms import FormWindowState
            window.native.WindowState = FormWindowState.Minimized
        else:
            raise ValueError('Unknown window action')


class WindowBridge:
    """A window-local native drag command, without per-mousemove RPC calls."""
    def __init__(self, api: HostApi, target: str):
        if target not in {'widget', 'console'}:
            raise ValueError('Invalid native window target')
        self._api = api
        self._target = target

    def begin_drag(self) -> dict:
        window = self._api.window if self._target == 'widget' else self._api.console
        if window is None:
            raise RuntimeError('Window unavailable')

        def drag():
            from ctypes import wintypes
            user = ctypes.windll.user32
            user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
            user.SendMessageW.restype = ctypes.c_ssize_t
            cursor = wintypes.POINT()
            user.GetCursorPos(ctypes.byref(cursor))
            position = (cursor.x & 0xffff) | ((cursor.y & 0xffff) << 16)
            user.ReleaseCapture()
            user.SendMessageW(window.native.Handle.ToInt64(), 0xA1, 2, position)

        _on_ui_thread(window.native, drag)
        return {'queued': True}


class WidgetBridge(WindowBridge):
    """Only the widget's bounded resize is exposed directly to its WebView."""
    def __init__(self, api: HostApi):
        super().__init__(api, 'widget')

    def resize_widget(self, width: int, height: int) -> dict:
        if type(width) is not int or type(height) is not int or not (52 <= width <= 4096 and 52 <= height <= 4096):
            raise ValueError('Invalid widget size')
        if self._api.window is None:
            raise RuntimeError('Widget unavailable')
        from System import Action
        self._api.window.native.Invoke(Action(lambda: self._api.apply(
            {'action': 'size', 'target': 'widget', 'width': width, 'height': height})))
        return {'width': width, 'height': height}


def process_requests(api: HostApi, data: dict, now: float | None = None) -> None:
    """Ordered, deduplicated commands; expired requests never replay."""
    now = time.time() if now is None else now
    for request in sorted(data.get('commands', []), key=lambda r: r['seq']):
        key = str(request['seq'])
        if key in api.applied:
            continue
        api.applied.add(key)
        try:
            if now - request.get('at', 0) > 15:
                raise ValueError('Window request expired')
            api.apply(request)
            api.results[key] = {'ok': True}
        except Exception as exc:
            api.results[key] = {'ok': False, 'error': str(exc)}
    keys = list(api.results)[-128:]
    api.results = {key: api.results[key] for key in keys}
    api.applied = set(keys)


def _on_ui_thread(form: Any, fn) -> None:
    from System import Action
    form.BeginInvoke(Action(fn))


def transparency_enabled() -> bool:
    """Respect the user's Windows appearance preference without changing it."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
            return bool(winreg.QueryValueEx(key, 'EnableTransparency')[0])
    except (ImportError, OSError):
        return True


def configure_backdrop(hwnd, dwm, *, transparency: bool = True) -> dict:
    """Report real DWM results; a failed request must not advertise Acrylic."""
    def attribute(key, value):
        data = ctypes.c_int(value)
        return int(dwm.DwmSetWindowAttribute(hwnd, key, ctypes.byref(data), ctypes.sizeof(data)))

    if not transparency:
        attribute(38, 1)
        return {'mode': 'fallback', 'reason': 'transparency-disabled'}

    # Dark theme and rounded corners are optional, independent of the material.
    attribute(20, 1)  # DWMWA_USE_IMMERSIVE_DARK_MODE
    attribute(33, 2)  # DWMWA_WINDOW_CORNER_PREFERENCE: DWMWCP_ROUND
    attribute(34, -2)  # DWMWA_BORDER_COLOR: no extra system highlight around CSS border.
    result = attribute(38, 3)  # DWMWA_SYSTEMBACKDROP_TYPE: DWMSBT_TRANSIENTWINDOW
    if result:
        return {'mode': 'fallback', 'reason': 'Windows Acrylic unavailable', 'hresult': result}
    margins = (ctypes.c_int * 4)(-1, -1, -1, -1)
    result = int(dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)))
    if result:
        attribute(38, 1)  # DWMSBT_NONE: leave a readable solid fallback.
        return {'mode': 'fallback', 'reason': 'Window composition unavailable', 'hresult': result}
    return {'mode': 'acrylic'}


def _keep_glass_appearance(window, hwnd) -> bool:
    """Keep NC material rendering active, without changing keyboard activation.

    WM_NCACTIVATE controls non-client painting only. WM_ACTIVATE, input focus,
    foreground selection and all other messages continue unmodified.
    """
    if getattr(window, '_glass_subclass', None):
        return True
    controls = ctypes.WinDLL('comctl32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_size_t)
    controls.DefSubclassProc.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    controls.DefSubclassProc.restype = ctypes.c_ssize_t
    controls.SetWindowSubclass.argtypes = [ctypes.c_void_p, callback_type, ctypes.c_size_t, ctypes.c_size_t]
    controls.SetWindowSubclass.restype = ctypes.c_int
    controls.RemoveWindowSubclass.argtypes = [ctypes.c_void_p, callback_type, ctypes.c_size_t]
    controls.RemoveWindowSubclass.restype = ctypes.c_int
    is_minimized = ctypes.windll.user32.IsIconic
    is_minimized.argtypes = [ctypes.c_void_p]
    is_minimized.restype = ctypes.c_int

    @callback_type
    def paint(handle, message, wparam, lparam, subclass_id, reference):
        preserve = (message == 0x86 and not wparam and not is_minimized(handle)
                    and getattr(window, 'desktop_material', {}).get('mode') == 'acrylic')
        if preserve:
            wparam = 1  # WM_NCACTIVATE: retain the active glass appearance.
        if message == 0x82:  # WM_NCDESTROY: detach while the callback is still alive.
            controls.RemoveWindowSubclass(handle, paint, subclass_id)
        result = controls.DefSubclassProc(handle, message, wparam, lparam)
        # TRUE on the original deactivation message lets focus transfer normally.
        return 1 if preserve else result

    if not controls.SetWindowSubclass(hwnd, paint, 1, 0):
        return False
    window._glass_subclass = (paint, controls)  # retain the callback for the HWND lifetime
    send = ctypes.windll.user32.SendMessageW
    send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    send.restype = ctypes.c_ssize_t
    send(hwnd, 0x86, 1, 0)
    return True


def _material(window: Any) -> None:
    """DWM blurs the real desktop; WebView2 supplies only content and smoke tint."""
    from System.Drawing import Color
    form = window.native
    hwnd = ctypes.c_void_p(form.Handle.ToInt64())
    try:
        material = configure_backdrop(hwnd, ctypes.windll.dwmapi, transparency=transparency_enabled())
    except (OSError, AttributeError) as exc:
        material = {'mode': 'fallback', 'reason': type(exc).__name__}
    window.desktop_material = material
    acrylic = material['mode'] == 'acrylic'
    form.BackColor = Color.Black if acrylic else Color.FromArgb(25, 30, 40)
    form.webview.DefaultBackgroundColor = Color.Transparent if acrylic else Color.FromArgb(25, 30, 40)
    if acrylic:
        material['persistent'] = _keep_glass_appearance(window, hwnd)
    print(f'[host] material={material}', flush=True)


def _state(window: Any) -> dict:
    if window is None:
        return {'visible': False, 'created': False}
    f = window.native
    return {'visible': bool(f.Visible), 'minimized': str(f.WindowState) == 'Minimized',
            'hwnd': int(f.Handle.ToInt64()),
            'pinned': bool(f.TopMost), 'x': int(f.Left), 'y': int(f.Top),
            'width': int(f.Width), 'height': int(f.Height),
            'material': getattr(window, 'desktop_material', {'mode': 'fallback'})}


def _fit_work_area(window, beside=None):
    from System.Drawing import Point, Size
    from ctypes import wintypes
    class MonitorInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('monitor', wintypes.RECT),
                    ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
    f = window.native
    scale = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(f.Handle.ToInt64())) / 96
    user = ctypes.windll.user32
    user.MonitorFromWindow.restype = wintypes.HANDLE
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        info = MonitorInfo(); info.cbSize = ctypes.sizeof(info)
        monitor = user.MonitorFromWindow(ctypes.c_void_p(f.Handle.ToInt64()), 2)
        if not user.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            raise ctypes.WinError()
        area = info.work
    finally:
        user.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
    width = min(round(window.initial_width * scale), area.right - area.left - 40)
    height = min(round(window.initial_height * scale), area.bottom - area.top - 40)
    left = f.Left
    if beside is not None:
        next_left = beside.native.Right + 24
        available = area.right - next_left - 16
        if available >= round(720 * scale):
            width = min(width, available)
            left = next_left
    f.ClientSize = Size(width, height)
    f.Location = Point(max(area.left + 16, min(left, area.right - width - 16)),
                       max(area.top + 16, min(f.Top, area.bottom - height - 16)))
    print(f'[host] fit dpi={f.DeviceDpi}, work={area.right-area.left}x{area.bottom-area.top}, size={width}x{height}', flush=True)


def _setup(api: HostApi, root: Path) -> None:
    from System.Drawing import SystemIcons, Point
    from System.Windows.Forms import Timer, NotifyIcon, ContextMenuStrip, Screen, Application
    from System import EventHandler
    def thread_error(sender, event):
        print("[host] UI exception: " + str(event.Exception), flush=True)
        Application.ExitThread()
    Application.ThreadException += thread_error
    if api.started:
        return
    api.started = True
    root.mkdir(parents=True, exist_ok=True)
    _material(api.window)
    _fit_work_area(api.window)
    tray = NotifyIcon()
    tray.Icon = SystemIcons.Application
    tray.Text = 'AutoDispatch · 双击恢复悬浮窗'
    menu = ContextMenuStrip()
    menu.Items.Add('显示悬浮窗', None, EventHandler(lambda s, e: api.apply({'target': 'widget', 'action': 'show'})))
    menu.Items.Add('打开详情', None, EventHandler(lambda s, e: api.open_console()))
    menu.Items.Add('隐藏悬浮窗', None, EventHandler(lambda s, e: api.window.native.Hide()))

    def exit_ui(s, e):
        api.exiting = True
        api.timer.Stop()
        tray.Visible = False
        tray.Dispose()
        if api.console:
            api.console.destroy()
        api.window.destroy()

    menu.Items.Add('退出界面（任务继续运行）', None, EventHandler(exit_ui))
    tray.ContextMenuStrip = menu
    tray.DoubleClick += lambda s, e: api.apply({'target': 'widget', 'action': 'show'})
    tray.Visible = True
    api.tray = tray
    timer = Timer()
    api.timer = timer
    timer.Interval = TICK_MS
    appearance = {'enabled': transparency_enabled(), 'checked': time.monotonic()}

    def tick(s, e):
        try:
            if time.monotonic() - appearance['checked'] >= 1:
                enabled = transparency_enabled()
                if enabled != appearance['enabled']:
                    for window in (api.window, api.console):
                        if window is not None:
                            _material(window)
                    appearance['enabled'] = enabled
                appearance['checked'] = time.monotonic()
            path = root / UI_HOST_FILE
            if path.exists():
                process_requests(api, json.loads(path.read_text(encoding='utf-8')))
            atomic_json(root / UI_RESULT_FILE, {'pid': os.getpid(), 'at': time.time(),
                        'results': api.results, 'windows': {'widget': _state(api.window), 'console': _state(api.console)}})
            (root / UI_ALIVE_FILE).write_text(str(time.time()), encoding='utf-8')
        except Exception as exc:
            print(f'[host] {exc!r}', flush=True)

    timer.Tick += tick
    timer.Start()
    print(f'[host] ready pid={os.getpid()}, tray and native windows active', flush=True)


def run_widget(url: str, state_dir: str | None = None, *, show_console: bool = False,
               profile_dir: Path | None = None) -> None:
    try:
        import webview
    except ImportError as exc:
        raise Blocked('Install the host extra: pip install -e .[host]') from exc
    root = Path(state_dir) if state_dir else Path(os.environ.get('LOCALAPPDATA', '.')) / 'AutoDispatch'
    with CoordinatorLock(root / 'desktop-host-lock'):
        api = HostApi(desktop_url(url, '/console'))
        options = dict(frameless=True, easy_drag=False, transparent=False, background_color='#191e28')
        api.window = webview.create_window('AutoDispatch', desktop_url(url, '/widget'), width=EXPANDED[0],
                         height=EXPANDED[1], x=ORIGIN[0], y=ORIGIN[1], resizable=False,
                         min_size=(360, 130), js_api=WidgetBridge(api), **options)
        api.window.events.closing += lambda: api.close_window(api.window)

        api.window.events.before_show += lambda: _setup(api, root)

        if show_console:
            def initial_console():
                if api.console is None:
                    _on_ui_thread(api.window.native, api.open_console)
            api.window.events.loaded += initial_console

        storage = profile_dir or Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AutoDispatch' / 'widget-profile'
        storage.mkdir(parents=True, exist_ok=True)
        try:
            webview.start(storage_path=str(storage), private_mode=False, gui='edgechromium')
        finally:
            if api.tray:
                api.tray.Visible = False
                api.tray.Dispose()
            (root / UI_ALIVE_FILE).unlink(missing_ok=True)
