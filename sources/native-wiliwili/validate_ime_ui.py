#!/usr/bin/env python3
"""Verify real Fcitx candidate windows through XTest, including fullscreen."""
import argparse
import ctypes as C
import json
from pathlib import Path
import subprocess
import tempfile
import time
import validate_ui as ui

ROOT = Path(__file__).resolve().parent


class WindowAttributes(C.Structure):
    _fields_ = [(n, C.c_int) for n in ('x', 'y', 'width', 'height', 'border_width', 'depth')] + [
        ('visual', C.c_void_p), ('root', C.c_ulong), ('window_class', C.c_int),
        ('bit_gravity', C.c_int), ('win_gravity', C.c_int), ('backing_store', C.c_int),
        ('backing_planes', C.c_ulong), ('backing_pixel', C.c_ulong), ('save_under', C.c_int),
        ('colormap', C.c_ulong), ('map_installed', C.c_int), ('map_state', C.c_int),
        ('all_event_masks', C.c_long), ('your_event_mask', C.c_long),
        ('do_not_propagate_mask', C.c_long), ('override_redirect', C.c_int), ('screen', C.c_void_p)]


class Keyboard:
    def __init__(self):
        self.x = C.CDLL('libX11.so.6')
        self.xt = C.CDLL('libXtst.so.6')
        self.x.XOpenDisplay.argtypes = [C.c_char_p]
        self.x.XOpenDisplay.restype = C.c_void_p
        self.x.XStringToKeysym.argtypes = [C.c_char_p]
        self.x.XStringToKeysym.restype = C.c_ulong
        self.x.XKeysymToKeycode.argtypes = [C.c_void_p, C.c_ulong]
        self.x.XKeysymToKeycode.restype = C.c_uint
        self.x.XFlush.argtypes = [C.c_void_p]
        self.x.XCloseDisplay.argtypes = [C.c_void_p]
        self.x.XDefaultRootWindow.argtypes = [C.c_void_p]
        self.x.XDefaultRootWindow.restype = C.c_ulong
        self.x.XQueryTree.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(C.c_ulong),
            C.POINTER(C.c_ulong), C.POINTER(C.POINTER(C.c_ulong)), C.POINTER(C.c_uint)]
        self.x.XFetchName.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(C.c_char_p)]
        self.x.XGetWindowAttributes.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(WindowAttributes)]
        self.x.XGetInputFocus.argtypes = [C.c_void_p, C.POINTER(C.c_ulong), C.POINTER(C.c_int)]
        self.x.XTranslateCoordinates.argtypes = [C.c_void_p, C.c_ulong, C.c_ulong,
            C.c_int, C.c_int, C.POINTER(C.c_int), C.POINTER(C.c_int), C.POINTER(C.c_ulong)]
        self.x.XFree.argtypes = [C.c_void_p]
        self.xt.XTestFakeKeyEvent.argtypes = [C.c_void_p, C.c_uint, C.c_int, C.c_ulong]
        self.display = self.x.XOpenDisplay(None)
        assert self.display, 'XWayland display is required'

    def focus_origin(self):
        focus, revert, child = C.c_ulong(), C.c_int(), C.c_ulong()
        x, y = C.c_int(), C.c_int()
        self.x.XGetInputFocus(self.display, C.byref(focus), C.byref(revert))
        assert focus.value > 1, 'An X11 application must have focus'
        assert self.x.XTranslateCoordinates(self.display, focus,
            self.x.XDefaultRootWindow(self.display), 0, 0, C.byref(x), C.byref(y), C.byref(child))
        return x.value, y.value

    def tap(self, name, mods=''):
        active = json.loads(subprocess.check_output(['hyprctl', 'activewindow', '-j']))
        assert ui.SELECTOR == f"pid:{active['pid']}", 'Focus must stay on the owned test window'
        held = [{'CTRL': 'Control_L', 'SHIFT': 'Shift_L'}[m] for m in mods.split()]
        def event(key, down):
            code = self.x.XKeysymToKeycode(self.display, self.x.XStringToKeysym(key.encode()))
            assert code, key
            self.xt.XTestFakeKeyEvent(self.display, code, int(down), 0)
            self.x.XFlush(self.display)
            time.sleep(.045)
        for modifier in held: event(modifier, True)
        event(name, True); event(name, False)
        for modifier in reversed(held): event(modifier, False)
        time.sleep(.2)

    def candidates(self):
        root = self.x.XDefaultRootWindow(self.display)
        root_return, parent, count = C.c_ulong(), C.c_ulong(), C.c_uint()
        children = C.POINTER(C.c_ulong)()
        assert self.x.XQueryTree(self.display, root, C.byref(root_return), C.byref(parent),
            C.byref(children), C.byref(count))
        result = []
        try:
            for i in range(count.value):
                name, attrs = C.c_char_p(), WindowAttributes()
                if self.x.XFetchName(self.display, children[i], C.byref(name)) and name:
                    try:
                        if b'Fcitx' in name.value and self.x.XGetWindowAttributes(
                                self.display, children[i], C.byref(attrs)) and attrs.map_state == 2:
                            result.append(dict(x=attrs.x, y=attrs.y, width=attrs.width, height=attrs.height))
                    finally: self.x.XFree(name)
        finally: self.x.XFree(children)
        return result


def run(installed=False):
    private = Path(tempfile.mkdtemp(prefix='wiliwili-ime-'))
    private.chmod(0o700)
    ui.STATE = private/'state.json'
    ui.REPORT = ROOT/'validation'/('ime-installed-results.json' if installed else 'ime-ui-results.json')
    ui.results = []
    ui.EVIDENCE = 'real_fcitx_rime_candidates_xtest_and_native_libmpv'
    previous = json.loads(subprocess.check_output(['hyprctl', 'activeworkspace', '-j']))['id']
    focus = json.loads(subprocess.check_output(['hyprctl', 'activewindow', '-j'])).get('address')
    used = {w['id'] for w in json.loads(subprocess.check_output(['hyprctl', 'workspaces', '-j']))}
    workspace = next(i for i in range(9900, 10000) if i not in used)
    launch = [str(Path.home()/'.local/bin/wiliwili-ai')] if installed else ['python3', str(ROOT/'launch.py')]
    keyboard = Keyboard()
    ui.key = keyboard.tap
    log = open(private/'client.log', 'w')
    process = subprocess.Popen([*launch, '--ai-test-media', str(ROOT/'validation/clip.mp4'),
        '--fixture', str(ROOT/'validation/fixture.json'), '--diagnostics', str(ui.STATE)], stdout=log, stderr=log)
    def window():
        return next(c for c in json.loads(subprocess.check_output(['hyprctl', 'clients', '-j']))
            if ui.SELECTOR == f"pid:{c['pid']}")
    def capture(label):
        time.sleep(.5)
        candidates = keyboard.candidates()
        assert candidates, 'Fcitx candidate window must be mapped'
        client = window(); desktop = ui.state()['desktop']; rect = desktop['input_rect']
        # XWayland's root origin can differ from Hyprland's multi-monitor origin.
        scale = desktop['window_scale']; x, y = keyboard.focus_origin()
        left, top = x+rect['x']*scale, y+rect['y']*scale
        assert any(left-10 <= p['x'] <= left+rect['width']*scale and
            top-10 <= p['y'] <= top+rect['height']*scale+80 for p in candidates), (client, rect, candidates)
        ui.screenshot('ime-'+label+'.png')
        ui.record(label, dict(desktop=desktop, xwayland=client['xwayland'], candidates=candidates,
            screenshot='ime-'+label+'.png'))
    try:
        ui.bind_test_process(process)
        ui.wait(lambda d: d.get('ready') and not d.get('stopped'), 20)
        ui.dispatch('hl.dsp.window.move({window="class:^wiliwili$",workspace='+str(workspace)+'})')
        ui.dispatch('hl.dsp.focus({workspace='+str(workspace)+'})')
        ui.dispatch('hl.dsp.focus({window="class:^wiliwili$"})')
        assert window()['xwayland'], 'The default Hyprland compatibility backend must be selected'
        ui.key('space'); ui.wait(lambda d: d.get('paused'), 10)
        ui.key('slash'); ui.wait(lambda d: d.get('desktop', {}).get('editing'), 10)
        subprocess.run(['fcitx5-remote', '-o'], check=True)
        assert subprocess.check_output(['fcitx5-remote', '-n'], text=True).strip() == 'rime'
        for c in 'ni': ui.key(c)
        capture('tiled-window-candidates')
        ui.dispatch('hl.dsp.window.float({window="class:^wiliwili$",action="enable"})')
        ui.dispatch('hl.dsp.window.resize({window="class:^wiliwili$",x=1259,y=676,relative=false})')
        ui.dispatch('hl.dsp.window.center({window="class:^wiliwili$"})')
        capture('normal-window-candidates')
        ui.dispatch('hl.dsp.window.resize({window="class:^wiliwili$",x=624,y=676,relative=false})')
        capture('resized-window-candidates')
        ui.dispatch('hl.dsp.window.fullscreen({window="class:^wiliwili$",action="toggle"})')
        time.sleep(.7)
        ui.key('BackSpace'); ui.key('i')
        capture('fullscreen-candidates')
        ui.key('Return')
        ui.record('Enter commits composition without submitting search', ui.wait(lambda d:
            d.get('desktop', {}).get('editing') and d['desktop'].get('input_text') == 'ni', 10))
        ui.key('a', 'CTRL'); ui.key('BackSpace')
        for c in 'ni': ui.key(c)
        ui.key('2')
        ui.record('A numbered candidate commits Chinese into the search field', ui.wait(lambda d:
            d.get('desktop', {}).get('editing') and any('\u4e00' <= c <= '\u9fff'
                for c in d['desktop'].get('input_text', '')), 10))
        ui.key('Return')
        ui.record('Enter submits the committed search', ui.wait(lambda d:
            not d.get('desktop', {}).get('editing'), 10))
        ui.key('slash'); ui.wait(lambda d: d.get('desktop', {}).get('editing'), 10)
        for c in 'ni': ui.key(c)
        capture('reopened-search-candidates')
        # Clear preedit in the IME, then cancel the application editor.
        ui.key('Escape'); ui.key('Escape')
        ui.wait(lambda d: not d.get('desktop', {}).get('editing'), 10)
        ui.key('slash'); ui.wait(lambda d: d.get('desktop', {}).get('editing'), 10)
        subprocess.run(['fcitx5-remote', '-c'], check=True)
        for c in 'fhjklv': ui.key(c)
        ui.record('ASCII input retains text focus', ui.wait(lambda d:
            d.get('desktop', {}).get('input_text') == 'fhjklv' and d.get('speed') == 1, 10))
        ui.key('a', 'CTRL'); ui.key('x'); ui.key('Home'); ui.key('h'); ui.key('End'); ui.key('l')
        ui.record('Select all and caret navigation still work', ui.wait(lambda d:
            d.get('desktop', {}).get('input_text') == 'hxl', 10))
        ui.key('Escape')
        ui.record('Escape cancels text input', ui.wait(lambda d: not d.get('desktop', {}).get('editing'), 10))
    except Exception as error:
        ui.results.append(dict(check='interrupted', result='fail', error=str(error)))
        ui.REPORT.write_text(json.dumps(ui.results, ensure_ascii=False, indent=2)+'\n')
        raise
    finally:
        if ui.SELECTOR:
            ui.dispatch('hl.dsp.window.close({window="class:^wiliwili$"})')
        ui.dispatch('hl.dsp.focus({workspace='+str(previous)+'})')
        if focus:
            ui.dispatch('hl.dsp.focus({window='+json.dumps('address:'+focus)+'})')
        try: process.wait(10)
        except subprocess.TimeoutExpired: process.terminate(); process.wait(5)
        keyboard.x.XCloseDisplay(keyboard.display)
        log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    run(parser.parse_args().installed)
