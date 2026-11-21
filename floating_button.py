import base64, ctypes, ctypes.util, json, math, os, queue, re, socket, sys, threading, time, tkinter as tk, tkinter.font as tkfont, urllib.request

# ---- settings ----
# Page/row/column and Companion address are edited from the button's right-click
# menu (Settings...) and saved to settings.json next to this script, along with
# where the button was last dragged to.
DEFAULTS = {"host": "127.0.0.1", "page": 9, "row": 0, "col": 1, "x": 60, "y": 60, "only_over_propresenter": True,
            "launches": 0, "only_over_user_set": False,
            "only_in_modes": True,   # macOS: hide unless ProPresenter is in Show, Reflow or Bible
            "locked": False,         # True: dragging can't move the button
            # Follow ProPresenter: offset of the button's top-left from a corner of ProPresenter's
            # main window ("tl", "tr", "bl", "br"); "" = not over ProPresenter, stays put.
            # anchor_w/h = size of the window it follows, to find that window again after a relaunch.
            "anchor": "", "anchor_dx": 0, "anchor_dy": 0, "anchor_w": 0, "anchor_h": 0,
            # Appearance: text_override "" = show Companion's text. Shapes: see SHAPES.
            "text_override": "", "width": 130, "height": 32, "shape": "rounded", "radius": 3}
SHAPES = [("rounded", "Rounded rectangle"), ("pill", "Pill"), ("oval", "Oval"), ("circle", "Circle")]
SIZE_LIMITS = {"width": (40, 600), "height": (20, 300), "radius": (0, 150)}
APP_NAME = "Companion Floating Button"


def settings_dir():
    # Installed apps can't write inside their own folder, so use the per-user app-data spot.
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


SETTINGS_FILE = os.path.join(settings_dir(), "settings.json")
LEGACY_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
HTTP_PORT = 8000          # Companion web/HTTP API (used for presses when the Satellite link is down)
SATELLITE_PORT = 16622    # Companion Satellite TCP API (live text/colour + presses)
FALLBACK_LABEL = "CLEAR"
SUB_ID = "floating-button"

# ---- look ----
BG_TRANSPARENT = "#010101"
# Styled after ProPresenter's old "Clear To Logo" button: flat, dark, barely rounded,
# dim regular-weight text. Companion's colours are shown muted so they sit in that style.
DEFAULT_FACE = "#3a3a3c"  # not linked to Companion: flat grey
DEFAULT_TEXT = "#ffffff"
FACE_BASE, FACE_MIX = "#262626", 0.80  # Companion colour mixed 80% into dark grey (#ff0000 -> #511e1e)
TEXT_DIM = 0.45           # text colour blended this far toward the face (white -> #ae9898 on maroon)
MAX_FONT, MIN_FONT = 12, 7   # MAX_FONT is for the default 32px height; it scales with the height


def load_settings():
    cfg = dict(DEFAULTS)
    path = SETTINGS_FILE if os.path.exists(SETTINGS_FILE) else LEGACY_SETTINGS_FILE
    try:
        with open(path) as f:
            saved = json.load(f)
        for k in DEFAULTS:
            if k in saved and type(saved[k]) is type(DEFAULTS[k]):
                cfg[k] = saved[k]
        if "launches" not in saved:
            cfg["launches"] = 2      # settings from before launch counting: already past the first runs
    except (OSError, ValueError):
        pass
    if cfg["shape"] not in dict(SHAPES):
        cfg["shape"] = DEFAULTS["shape"]
    for k, (lo, hi) in SIZE_LIMITS.items():
        cfg[k] = min(max(cfg[k], lo), hi)
    return cfg


def save_settings():
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


cfg = load_settings()


def shape_size(shape, width, height):
    """(W, H) actually drawn: a circle uses the width for both."""
    return (width, width) if shape == "circle" else (width, height)


W, H = shape_size(cfg["shape"], cfg["width"], cfg["height"])   # current button size (changed by apply_look)

# First launch: show the button everywhere so a new user can find it. Second launch:
# switch on "Only show over ProPresenter". Either way, a choice the user made wins.
cfg["launches"] = min(cfg["launches"] + 1, 1000)
if not cfg["only_over_user_set"] and cfg["launches"] <= 2:
    cfg["only_over_propresenter"] = cfg["launches"] == 2
try:
    save_settings()
except OSError as _ex:
    print("Couldn't save settings:", _ex)


# ---------------------------------------------------------------- what's on screen (macOS)
TARGET_APP = "ProPresenter"   # app the button should sit on top of


class WindowList:
    """Front-to-back list of on-screen app windows via CoreGraphics (no extra installs).

    Only owner names and bounds are read, which macOS provides without Screen Recording permission.
    """

    def __init__(self):
        vp = ctypes.c_void_p
        self.cg = cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        self.cf = cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cg.CGWindowListCopyWindowInfo.restype = vp
        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
        cg.CGRectMakeWithDictionaryRepresentation.argtypes = [vp, ctypes.POINTER(_CGRect)]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [vp]
        cf.CFArrayGetValueAtIndex.restype = vp
        cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
        cf.CFDictionaryGetValue.restype = vp
        cf.CFDictionaryGetValue.argtypes = [vp, vp]
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [vp, ctypes.c_int, vp]
        cf.CFRelease.argtypes = [vp]
        self.keys = {k: cf.CFStringCreateWithCString(None, k.encode(), 0x08000100) for k in
                     ("kCGWindowOwnerName", "kCGWindowOwnerPID", "kCGWindowLayer", "kCGWindowBounds",
                      "kCGWindowName", "kCGWindowNumber")}

    def _str(self, v):
        buf = ctypes.create_string_buffer(256)
        return buf.value.decode(errors="replace") if v and self.cf.CFStringGetCString(v, buf, 256, 0x08000100) else ""

    def _int(self, v):
        out = ctypes.c_int64(0)
        if v:
            self.cf.CFNumberGetValue(v, 4, ctypes.byref(out))   # kCFNumberSInt64Type
        return out.value

    def windows(self):
        """[(owner, pid, layer, x, y, w, h, title, window id)] front to back, on-screen only, desktop excluded.

        title is "" unless macOS shares it (it can need Screen Recording permission)."""
        arr = self.cg.CGWindowListCopyWindowInfo(1 | 16, 0)
        if not arr:
            return []
        out = []
        try:
            for n in range(self.cf.CFArrayGetCount(arr)):
                d = self.cf.CFArrayGetValueAtIndex(arr, n)
                g = lambda k: self.cf.CFDictionaryGetValue(d, self.keys[k])
                r = _CGRect()
                self.cg.CGRectMakeWithDictionaryRepresentation(g("kCGWindowBounds"), ctypes.byref(r))
                out.append((self._str(g("kCGWindowOwnerName")), self._int(g("kCGWindowOwnerPID")),
                            self._int(g("kCGWindowLayer")), r.x, r.y, r.w, r.h, self._str(g("kCGWindowName")),
                            self._int(g("kCGWindowNumber"))))
        finally:
            self.cf.CFRelease(arr)
        return out


class _CGRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("w", ctypes.c_double), ("h", ctypes.c_double)]


def target_app_on_top(winlist, x, y, w, h):
    """True if the front-most normal app window under the button's centre belongs to TARGET_APP.

    Uses the centre point, not the whole rect: near a screen edge, a window on the next
    monitor can touch a sliver of the button and would otherwise hide it.
    """
    me = os.getpid()
    cx, cy = x + w / 2, y + h / 2
    for owner, pid, layer, wx, wy, ww, wh, *_ in winlist.windows():
        if pid == me or layer != 0:      # skip our own windows, menu bar, Dock, overlays
            continue
        if wx <= cx < wx + ww and wy <= cy < wy + wh:
            return owner == TARGET_APP
    return False


class WinWindowList:
    """Windows equivalent: top-level windows front to back via user32 (no extra installs).

    Returns the same tuples as WindowList (title = window title, id = HWND); owner is the program name without ".exe",
    layer is 0 for normal windows and 1 for always-on-top ones (taskbar, overlays).
    """

    def __init__(self):
        from ctypes import wintypes
        self.wt = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi")
        self.EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u, k = self.user32, self.kernel32
        u.EnumWindows.argtypes = [self.EnumProc, wintypes.LPARAM]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p,
                                                      wintypes.DWORD]
        self.names = {}

    def _exe(self, pid):
        if pid in self.names:
            return self.names[pid]
        name = ""
        h = self.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = self.wt.DWORD(1024)
            if self.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
                if name.lower().endswith(".exe"):
                    name = name[:-4]
            self.kernel32.CloseHandle(h)
        if len(self.names) > 500:
            self.names.clear()
        self.names[pid] = name
        return name

    def windows(self):
        wt, u = self.wt, self.user32
        out = []

        def cb(hwnd, _):
            if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
                return True
            cloaked = wt.DWORD(0)   # hidden UWP apps / other virtual desktops
            if self.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
                return True
            ex = u.GetWindowLongW(hwnd, -20)    # GWL_EXSTYLE
            if ex & 0x80:                       # WS_EX_TOOLWINDOW (tooltips, palettes)
                return True
            r = wt.RECT()
            if not u.GetWindowRect(hwnd, ctypes.byref(r)) or r.right <= r.left or r.bottom <= r.top:
                return True
            pid = wt.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            layer = 1 if ex & 0x8 else 0        # WS_EX_TOPMOST
            title = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, title, 256)
            out.append((self._exe(pid.value), pid.value, layer, r.left, r.top, r.right - r.left, r.bottom - r.top,
                        title.value, hwnd or 0))
            return True

        u.EnumWindows(self.EnumProc(cb), 0)    # EnumWindows walks top-level windows in z-order
        return out


try:
    if sys.platform == "darwin":
        winlist = WindowList()
    elif sys.platform == "win32":
        winlist = WinWindowList()
    else:
        winlist = None
except (OSError, AttributeError):
    winlist = None


# ---------------------------------------------------------------- ProPresenter's mode (macOS)
SHOW_MODES = {"presentation", "reflow", "bible"}   # toolbar segments the button belongs with
MODE_PREFIX = "MainWindowToolbar.mainView."        # + presentation / editor / reflow / bible / more


class ProPresenterMode:
    """Which of ProPresenter's Show / Edit / Reflow / Bible / More toolbar segments is selected.

    Read through the macOS Accessibility API, which needs this app ticked in System Settings >
    Privacy & Security > Accessibility. selected() returns None whenever it can't tell (no
    permission, ProPresenter not running, toolbar not found), and the caller then shows the button.
    """

    def __init__(self):
        vp, i32 = ctypes.c_void_p, ctypes.c_int32
        self.ax = ax = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        self.cf = cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        ax.AXIsProcessTrustedWithOptions.argtypes = [vp]
        ax.AXUIElementCreateApplication.restype = vp
        ax.AXUIElementCreateApplication.argtypes = [i32]
        ax.AXUIElementCopyAttributeValue.restype = i32
        ax.AXUIElementCopyAttributeValue.argtypes = [vp, vp, ctypes.POINTER(vp)]
        ax.AXUIElementSetMessagingTimeout.restype = i32
        ax.AXUIElementSetMessagingTimeout.argtypes = [vp, ctypes.c_float]
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [vp]
        cf.CFArrayGetValueAtIndex.restype = vp
        cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [vp]
        cf.CFRetain.restype = vp
        cf.CFRetain.argtypes = [vp]
        cf.CFRelease.argtypes = [vp]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]
        self.attr = {n: cf.CFStringCreateWithCString(None, n.encode(), 0x08000100) for n in
                     ("AXWindows", "AXChildren", "AXRole", "AXIdentifier", "AXSelected")}
        self.pid = None
        self.app = None
        self.segments = []      # [(name, retained AXUIElement)] for the mode buttons, found once per pid

    def trusted(self, prompt=False):
        """True if macOS lets us read other apps' UI. prompt=True shows macOS's permission dialog."""
        if not prompt:
            return self.ax.AXIsProcessTrusted()
        vp = ctypes.c_void_p
        key = vp.in_dll(self.ax, "kAXTrustedCheckOptionPrompt")
        true = vp.in_dll(self.cf, "kCFBooleanTrue")
        keys, vals = (vp * 1)(key.value), (vp * 1)(true.value)
        opts = self.cf.CFDictionaryCreate(None, keys, vals, 1,
                                          ctypes.addressof(ctypes.c_char.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks")),
                                          ctypes.addressof(ctypes.c_char.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks")))
        try:
            return self.ax.AXIsProcessTrustedWithOptions(opts)
        finally:
            self.cf.CFRelease(opts)

    def _get(self, el, name):
        """Copied attribute value (caller releases), or None."""
        out = ctypes.c_void_p()
        if self.ax.AXUIElementCopyAttributeValue(el, self.attr[name], ctypes.byref(out)) != 0:
            return None
        return out.value

    def _str(self, el, name):
        v = self._get(el, name)
        if not v:
            return ""
        try:
            buf = ctypes.create_string_buffer(256)
            return buf.value.decode(errors="replace") if self.cf.CFStringGetCString(v, buf, 256, 0x08000100) else ""
        finally:
            self.cf.CFRelease(v)

    def _children(self, el):
        """[retained child] — caller releases each."""
        arr = self._get(el, "AXChildren")
        if not arr:
            return []
        try:
            return [self.cf.CFRetain(self.cf.CFArrayGetValueAtIndex(arr, i))
                    for i in range(self.cf.CFArrayGetCount(arr))]
        finally:
            self.cf.CFRelease(arr)

    def _forget(self):
        for _, el in self.segments:
            self.cf.CFRelease(el)
        self.segments = []
        if self.app:
            self.cf.CFRelease(self.app)
        self.app = None

    def _find_segments(self):
        """Window > AXToolbar > group whose children carry MainWindowToolbar.mainView.* ids."""
        wins = self._get(self.app, "AXWindows")
        if not wins:
            return
        try:
            for i in range(self.cf.CFArrayGetCount(wins)):
                for bar in self._children(self.cf.CFArrayGetValueAtIndex(wins, i)):
                    if not self.segments and self._str(bar, "AXRole") == "AXToolbar":
                        for grp in self._children(bar):
                            if not self.segments:
                                for seg in self._children(grp):
                                    ident = self._str(seg, "AXIdentifier")
                                    if ident.startswith(MODE_PREFIX):
                                        self.segments.append((ident[len(MODE_PREFIX):], seg))
                                    else:
                                        self.cf.CFRelease(seg)
                            self.cf.CFRelease(grp)
                    self.cf.CFRelease(bar)
                if self.segments:
                    return
        finally:
            self.cf.CFRelease(wins)

    def selected(self, pid):
        """Set of selected mode names (e.g. {"presentation"}), or None if unknown."""
        if not pid or not self.trusted():
            return None
        if pid != self.pid:
            self._forget()
            self.pid = pid
            self.app = self.ax.AXUIElementCreateApplication(pid)
            self.ax.AXUIElementSetMessagingTimeout(self.app, 0.25)   # a busy ProPresenter can't freeze us
        for attempt in (1, 2):
            if not self.segments:
                self._find_segments()
                if not self.segments:
                    return None
            out, ok = set(), True
            for name, el in self.segments:
                v = self._get(el, "AXSelected")
                if v is None:
                    ok = False        # element went stale (window rebuilt): look again
                    break
                if self.cf.CFBooleanGetValue(v):
                    out.add(name)
                self.cf.CFRelease(v)
            if ok:
                return out
            for _, el in self.segments:
                self.cf.CFRelease(el)
            self.segments = []
        return None


try:
    pp_mode = ProPresenterMode() if sys.platform == "darwin" else None
except (OSError, AttributeError, ValueError) as _ex:
    print("ProPresenter mode check unavailable:", _ex)
    pp_mode = None


def propresenter_windows():
    """[(x, y, w, h, title, id)] of ProPresenter's normal on-screen windows, front to back.
    (Output windows sit at higher layers, so they're left out.)"""
    if winlist is None:
        return []
    me = os.getpid()
    return [(int(w[3]), int(w[4]), int(w[5]), int(w[6]), w[7], w[8]) for w in winlist.windows()
            if w[0] == TARGET_APP and w[1] != me and w[2] == 0 and w[5] > 0 and w[6] > 0]


def propresenter_pid():
    if winlist is None:
        return None
    for owner, pid, layer, *_ in winlist.windows():
        if owner == TARGET_APP and layer == 0:
            return pid
    return None


# ---------------------------------------------------------------- displays
class _NSRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("w", ctypes.c_double), ("h", ctypes.c_double)]


class ObjC:
    """Just enough of the Objective-C runtime (via ctypes) for screen names and the Dock menu."""

    def __init__(self):
        self.lib = lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("AppKit"))   # makes NSScreen/NSMenu available
        vp = ctypes.c_void_p
        lib.objc_getClass.restype = vp
        lib.objc_getClass.argtypes = [ctypes.c_char_p]
        lib.sel_registerName.restype = vp
        lib.sel_registerName.argtypes = [ctypes.c_char_p]
        lib.class_addMethod.restype = ctypes.c_bool
        lib.class_addMethod.argtypes = [vp, vp, vp, ctypes.c_char_p]
        self.addr = ctypes.cast(lib.objc_msgSend, vp).value
        self.protos = {}

    def cls(self, name):
        return self.lib.objc_getClass(name.encode())

    def sel(self, name):
        return self.lib.sel_registerName(name.encode())

    def send(self, obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
        # arm64 objc_msgSend must be called through a prototype matching the method exactly.
        key = (restype, tuple(argtypes))
        if key not in self.protos:
            self.protos[key] = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(self.addr)
        return self.protos[key](obj, self.sel(sel), *args)

    def nsstring(self, text):
        return self.send(self.cls("NSString"), "stringWithUTF8String:", text.encode(), argtypes=[ctypes.c_char_p])

    def pystring(self, ns):
        if not ns:
            return ""
        raw = self.send(ns, "UTF8String", restype=ctypes.c_char_p)
        return raw.decode(errors="replace") if raw else ""


try:
    objc = ObjC() if sys.platform == "darwin" else None
except (OSError, AttributeError):
    objc = None


def _mac_displays():
    screens = objc.send(objc.cls("NSScreen"), "screens")
    out = []
    main_h = None
    for i in range(objc.send(screens, "count", restype=ctypes.c_ulong)):
        sc = objc.send(screens, "objectAtIndex:", i, argtypes=[ctypes.c_ulong])
        f = objc.send(sc, "frame", restype=_NSRect)
        if main_h is None:
            main_h = f.h                  # screens[0] holds the menu bar; Tk's 0,0 is its top-left
        name = objc.pystring(objc.send(sc, "localizedName")) or f"Display {i + 1}"
        out.append((name, int(f.x), int(main_h - (f.y + f.h)), int(f.w), int(f.h)))
    return out


def _win_displays():
    from ctypes import wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    user32 = ctypes.WinDLL("user32")
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                              wintypes.LPARAM)
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), proc, wintypes.LPARAM]
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    found = []

    def cb(hmon, _hdc, _rect, _data):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            found.append((bool(mi.dwFlags & 1), r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    found.sort(key=lambda d: (not d[0], d[1], d[2]))   # primary first, then left to right
    return [((f"Display {i + 1}" + (" (main)" if d[0] else "")),) + d[1:] for i, d in enumerate(found)]


def displays():
    """[(name, x, y, w, h)] for each connected display, in the button's screen coordinates."""
    try:
        if objc is not None:
            ds = _mac_displays()
        elif sys.platform == "win32":
            ds = _win_displays()
        else:
            ds = []
    except Exception as ex:   # never let a display query break the menus
        print("Display list failed:", ex)
        ds = []
    if not ds:
        ds = [("Display 1", 0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]
    seen = {}
    named = []
    for name, x, y, w, h in ds:   # two identical monitors get "(2)" so they can be told apart
        seen[name] = seen.get(name, 0) + 1
        named.append((name if seen[name] == 1 else f"{name} ({seen[name]})", x, y, w, h))
    return named


def move_to_display(index):
    ds = displays()
    if not 0 <= index < len(ds):
        return
    cx, cy = cfg["x"] + W / 2, cfg["y"] + H / 2
    cur = next((d for d in ds if d[1] <= cx < d[1] + d[3] and d[2] <= cy < d[2] + d[4]), ds[0])
    _, nx, ny, nw, nh = ds[index]
    # Keep the same relative spot (e.g. near the right edge stays near the right edge).
    fx = (cfg["x"] - cur[1]) / max(cur[3] - W, 1)
    fy = (cfg["y"] - cur[2]) / max(cur[4] - H, 1)
    fx, fy = min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0)
    move_to(nx + round(fx * (nw - W)), ny + round(fy * (nh - H)))


def fill_display_menu(m):
    """(Re)build a Tk menu with one "Move to <display>" item per display."""
    m.delete(0, "end")
    for i, d in enumerate(displays()):
        m.add_command(label=f"Move to {d[0]}", command=lambda i=i: move_to_display(i))


# ---------------------------------------------------------------- Satellite link
class Satellite:
    """Subscribes to one Companion button via the Satellite API (Companion 4.1+ / API 1.10+).

    Requires Settings > Protocols > Satellite > "Enable Button Subscriptions API" in Companion.
    Without it, the button still works (HTTP press) but shows FALLBACK_LABEL.
    """

    def __init__(self, on_state, on_status):
        self.on_state, self.on_status = on_state, on_status
        self.sock = None
        self.subscribed = False
        self.reconnect_now = False
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._pinger, daemon=True).start()

    def _send(self, line):
        with self.lock:
            if self.sock:
                try:
                    self.sock.sendall((line + "\n").encode())
                    return True
                except OSError:
                    pass
        return False

    def press(self):
        if not self.subscribed:
            return False
        ok = self._send(f"SUB-PRESS SUBID={SUB_ID} PRESSED=1")
        time.sleep(0.05)
        return self._send(f"SUB-PRESS SUBID={SUB_ID} PRESSED=0") and ok

    def reconnect(self):
        """Drop the connection and reconnect straight away using the current cfg."""
        self.reconnect_now = True
        with self.lock:
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _pinger(self):
        # Companion drops idle Satellite sockets after 5s.
        while True:
            time.sleep(2)
            self._send("PING floating-button")

    def _run(self):
        while True:
            try:
                s = socket.create_connection((cfg["host"], SATELLITE_PORT), timeout=5)
                s.settimeout(None)
                with self.lock:
                    self.sock = s
                buf = b""
                while True:
                    data = s.recv(65536)
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._handle(line.decode(errors="replace").strip())
            except OSError:
                pass
            with self.lock:
                if self.sock:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                self.sock = None
            self.subscribed = False
            self.on_status(False)
            if self.reconnect_now:
                self.reconnect_now = False
            else:
                time.sleep(3)

    def _handle(self, line):
        cmd, _, rest = line.partition(" ")
        p = parse_params(rest)
        if cmd == "CAPS":
            if p.get("SUBSCRIPTIONS") == "1":
                self._send(f"ADD-SUB SUBID={SUB_ID} LOCATION={cfg['page']}/{cfg['row']}/{cfg['col']} TEXT=1 COLORS=hex")
            else:
                print("Companion: enable Settings > Protocols > 'Enable Button Subscriptions API' for live labels.")
                self.on_status(False)
        elif cmd == "ADD-SUB":
            self.subscribed = rest.startswith("OK")
            self.on_status(self.subscribed)
            if not self.subscribed:
                print("Subscribe failed:", rest)
        elif cmd == "SUB-STATE" and p.get("SUBID") == SUB_ID:
            text = ""
            if "TEXT" in p:
                try:
                    text = base64.b64decode(p["TEXT"]).decode(errors="replace")
                except ValueError:
                    pass
            self.on_state(text, p.get("COLOR"), p.get("TEXTCOLOR"), p.get("PRESSED") == "1")
        elif cmd == "PING":
            self._send("PONG " + rest)


def parse_params(s):
    return {k: (q if q or v == "" else v) for k, q, v in re.findall(r'(\w+)=(?:"([^"]*)"|(\S*))', s)}


def http_press():
    url = f"http://{cfg['host']}:{HTTP_PORT}/api/location/{cfg['page']}/{cfg['row']}/{cfg['col']}/press"
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=2)
    except Exception as e:
        print("Press failed:", e)


# ---------------------------------------------------------------- colours
def shade(hex_color, factor):
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, TypeError):
        return hex_color
    f = lambda c: max(0, min(255, int(c + (255 - c) * factor if factor > 0 else c * (1 + factor))))
    return f"#{f(r):02x}{f(g):02x}{f(b):02x}"


def blend(hex_a, hex_b, t):
    """Mix hex_a toward hex_b by t (0 = all a, 1 = all b)."""
    try:
        a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, TypeError):
        return hex_a
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


# ---------------------------------------------------------------- window
root = tk.Tk()
root.withdraw()                  # hide until borderless, or macOS adds a title bar
root.update_idletasks()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.geometry(f"{W}x{H}+{cfg['x']}+{cfg['y']}")
root.configure(bg=BG_TRANSPARENT)
try:
    root.wm_attributes("-transparentcolor", BG_TRANSPARENT)  # Windows
except tk.TclError:
    root.wm_attributes("-transparent", True)                 # macOS
    root.configure(bg="systemTransparent")

c = tk.Canvas(root, width=W, height=H, highlightthickness=0, bg=root["bg"])
c.pack()


def round_rect_points(x1, y1, x2, y2, r):
    """Outline of a rectangle with true circular corners of radius r (0 = square corners)."""
    steps = max(2, min(16, int(r / 2)))
    pts = []
    for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
        for i in range(steps + 1):
            a = math.radians(a0 + 90 * i / steps)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


def draw_body(shape, w, h, r, fill):
    if shape in ("oval", "circle"):
        return c.create_oval(0, 0, w, h, fill=fill, outline="")
    r = min(w, h) / 2 if shape == "pill" else min(r, min(w, h) / 2)
    return c.create_polygon(round_rect_points(0, 0, w, h, r), fill=fill, outline="")


def mask_window(shape, w, h, r):
    """macOS: clip the button's window to its shape.

    Tk 9 on macOS paints "transparent" canvas pixels black (the window and its layer are
    already non-opaque; Tk's own bitmap has black in it), so the corners round the shape
    would show as black. A Core Animation mask on the window's content layer trims them,
    with smooth antialiased edges. Windows uses -transparentcolor instead.
    """
    if objc is None:
        return
    try:
        vp = ctypes.c_void_p
        if mask_window.cg is None:
            ctypes.cdll.LoadLibrary(ctypes.util.find_library("QuartzCore"))   # CAShapeLayer
            cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
            cg.CGPathCreateWithEllipseInRect.restype = vp
            cg.CGPathCreateWithEllipseInRect.argtypes = [_NSRect, vp]
            cg.CGPathCreateWithRoundedRect.restype = vp
            cg.CGPathCreateWithRoundedRect.argtypes = [_NSRect, ctypes.c_double, ctypes.c_double, vp]
            cg.CGPathRelease.argtypes = [vp]
            objc.lib.object_getClass.restype, objc.lib.object_getClass.argtypes = vp, [vp]
            objc.lib.class_getName.restype, objc.lib.class_getName.argtypes = ctypes.c_char_p, [vp]
            mask_window.cg = cg
        cg = mask_window.cg
        rect = _NSRect(0, 0, w, h)
        if shape in ("oval", "circle"):
            path = cg.CGPathCreateWithEllipseInRect(rect, None)
        else:
            r = min(w, h) / 2 if shape == "pill" else min(r, min(w, h) / 2)
            path = cg.CGPathCreateWithRoundedRect(rect, r, r, None)
        try:
            wins = objc.send(objc.send(objc.cls("NSApplication"), "sharedApplication"), "windows")
            for i in range(objc.send(wins, "count", restype=ctypes.c_ulong)):
                win = objc.send(wins, "objectAtIndex:", i, argtypes=[ctypes.c_ulong])
                name = objc.lib.class_getName(objc.lib.object_getClass(win)) or b""
                # The button is Tk's only untitled window (Settings has a title bar).
                if not name.startswith(b"TK") or objc.send(win, "styleMask", restype=ctypes.c_ulong) & 1:
                    continue
                view = objc.send(win, "contentView")
                objc.send(view, "setWantsLayer:", True, argtypes=[ctypes.c_bool])
                layer = objc.send(view, "layer")
                if not layer:
                    continue
                mask = objc.send(objc.cls("CAShapeLayer"), "layer")
                objc.send(mask, "setFrame:", rect, argtypes=[_NSRect])
                objc.send(mask, "setPath:", path, argtypes=[vp])
                objc.send(layer, "setMask:", mask, argtypes=[vp])
                objc.send(win, "invalidateShadow")    # shadow follows the new outline
        finally:
            cg.CGPathRelease(path)
    except Exception as ex:   # never let a cosmetic step break the button
        print("Shape mask failed:", ex)


mask_window.cg = None


def text_box(shape, w, h):
    """(width, height) the label must fit inside for this shape."""
    if shape in ("oval", "circle"):
        return w * 0.70, h * 0.70          # roughly the rectangle inscribed in the ellipse
    if shape == "pill":
        return w - h * 0.6, h - 2          # keep clear of the round ends
    return w - 10, h - 2


body = draw_body(cfg["shape"], W, H, cfg["radius"], DEFAULT_FACE)
font = tkfont.Font(family="Helvetica", size=MAX_FONT, weight="normal")
label = c.create_text(W/2, H/2, text=FALLBACK_LABEL, fill=blend(DEFAULT_TEXT, DEFAULT_FACE, TEXT_DIM),
                      font=font, justify="center", width=text_box(cfg["shape"], W, H)[0])

ui = queue.Queue()   # background threads hand UI updates to the Tk thread here
# (Tk 9: root.after() called from a background thread silently never runs.)


def pump():
    while True:
        try:
            ui.get_nowait()()
        except queue.Empty:
            break
    root.after(50, pump)


look = {"face": DEFAULT_FACE, "text": DEFAULT_TEXT, "hover": False, "down": False, "pushed": False,
        "companion_text": ""}   # last text Companion sent ("" = none / not linked)


def repaint():
    face = look["face"]
    if look["down"] or look["pushed"]:
        face = shade(face, -0.30)
    elif look["hover"]:
        face = shade(face, 0.10)
    c.itemconfig(body, fill=face)
    c.itemconfig(label, fill=blend(look["text"], face, TEXT_DIM))


def set_text(text):
    """Remember Companion's text and redraw the label."""
    look["companion_text"] = text or ""
    refresh_text()


def refresh_text(shape=None, override=None):
    # The user's own text wins over Companion's; "\n" typed in either makes a line break.
    shape = cfg["shape"] if shape is None else shape
    override = cfg["text_override"] if override is None else override
    text = (override.strip() or look["companion_text"] or FALLBACK_LABEL).replace("\\n", "\n")
    tw, th = text_box(shape, W, H)
    lines = text.split("\n")
    # Start at a size that suits the button's height, then shrink until the text fits.
    # First try without wrapping (so a word is never split), measured directly from the font.
    sizes = range(max(MAX_FONT, round(MAX_FONT * H / 32)), MIN_FONT - 1, -1)
    for size in sizes:
        font.configure(size=size)
        if max(font.measure(ln) for ln in lines) <= tw and len(lines) * font.metrics("linespace") <= th:
            c.itemconfig(label, text=text, width=0, font=font)
            return
    # Too long for one line: let Tk wrap it. Re-setting font= makes Tk re-lay the text now;
    # a named-font change on its own only applies at idle time, so bbox() would be stale.
    words = text.split() or [text]
    for size in sizes:
        font.configure(size=size)
        if max(font.measure(wd) for wd in words) > tw:
            continue                       # a word would be split mid-way at this size
        c.itemconfig(label, text=text, width=max(tw, 10), font=font)
        x1, y1, x2, y2 = c.bbox(label)
        if (y2 - y1) <= th and (x2 - x1) <= tw + 2:
            return
    font.configure(size=MIN_FONT)          # nothing fits: smallest size, no wrapping
    c.itemconfig(label, text=text, width=0, font=font)


def apply_look(shape, width, height, radius, override):
    """Resize and reshape the button (used by Settings' live preview and Save)."""
    global W, H, body
    W, H = shape_size(shape, width, height)
    root.geometry(f"{W}x{H}+{cfg['x']}+{cfg['y']}")
    c.configure(width=W, height=H)
    c.delete(body)
    body = draw_body(shape, W, H, radius, look["face"])
    c.tag_lower(body)
    c.coords(label, W / 2, H / 2)
    refresh_text(shape, override)
    repaint()
    root.update_idletasks()
    mask_window(shape, W, H, radius)


def on_state(text, color, textcolor, pushed):
    def apply():
        look["face"] = blend(color, FACE_BASE, FACE_MIX) if color else DEFAULT_FACE
        look["text"] = textcolor or DEFAULT_TEXT
        look["pushed"] = pushed
        set_text(text)
        repaint()
    ui.put(apply)


def on_status(connected):
    def apply():
        # Grey face = not linked; Companion's (muted) colour = linked.
        if not connected:
            look.update(face=DEFAULT_FACE, text=DEFAULT_TEXT, pushed=False)
            set_text("")
            repaint()
    ui.put(apply)


sat = Satellite(on_state, on_status)


def press():
    if not sat.press():
        http_press()


# ---------------------------------------------------------------- settings window
settings_win = None


def open_settings():
    global settings_win
    if settings_win is not None and settings_win.winfo_exists():
        settings_win.lift()
        settings_win.focus_force()
        return

    win = settings_win = tk.Toplevel(root)
    win.title("Floating Button Settings")
    win.attributes("-topmost", True)
    win.resizable(False, False)
    frm = tk.Frame(win, padx=16, pady=14)
    frm.pack()

    tk.Label(frm, text="Companion button to show and press", font=("Helvetica", 13, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

    fields = {}
    rows = [("host", "Companion address"), ("page", "Page"), ("row", "Row"), ("col", "Column")]
    for i, (key, text) in enumerate(rows, start=1):
        tk.Label(frm, text=text).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=3)
        e = tk.Entry(frm, width=16 if key == "host" else 6)
        e.insert(0, str(cfg[key]))
        e.grid(row=i, column=1, sticky="w", pady=3)
        fields[key] = e

    hint = ("#888888", ("Helvetica", 11))
    tk.Label(frm, text="Row and column start at 0 (top-left button is row 0, column 0).",
             fg=hint[0], font=hint[1]).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # ---- appearance (previewed live on the button; Cancel puts it back)
    tk.Label(frm, text="Appearance", font=("Helvetica", 13, "bold")).grid(
        row=6, column=0, columnspan=2, sticky="w", pady=(14, 6))
    look_vars = {k: tk.StringVar(value=str(cfg[k])) for k in ("text_override", "width", "height", "radius")}
    shape_names = dict(SHAPES)
    shape_var = tk.StringVar(value=shape_names[cfg["shape"]])

    tk.Label(frm, text="Button text").grid(row=7, column=0, sticky="w", padx=(0, 10), pady=3)
    tk.Entry(frm, width=22, textvariable=look_vars["text_override"]).grid(row=7, column=1, sticky="w", pady=3)
    tk.Label(frm, text="Leave blank to show Companion's text. Type \\n for a new line.",
             fg=hint[0], font=hint[1]).grid(row=8, column=0, columnspan=2, sticky="w")

    tk.Label(frm, text="Shape").grid(row=9, column=0, sticky="w", padx=(0, 10), pady=(8, 3))
    tk.OptionMenu(frm, shape_var, *shape_names.values()).grid(row=9, column=1, sticky="w", pady=(8, 3))

    size_entries = {}
    for i, (key, text) in enumerate([("width", "Width (px)"), ("height", "Height (px)"),
                                     ("radius", "Corner radius (px)")], start=10):
        tk.Label(frm, text=text).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=3)
        e = tk.Entry(frm, width=6, textvariable=look_vars[key])
        e.grid(row=i, column=1, sticky="w", pady=3)
        size_entries[key] = e
    size_note = tk.Label(frm, text="", fg=hint[0], font=hint[1])
    size_note.grid(row=13, column=0, columnspan=2, sticky="w")

    locked_var = tk.BooleanVar(value=cfg["locked"])
    tk.Checkbutton(frm, text="Lock position (dragging won't move it)", variable=locked_var).grid(
        row=14, column=0, columnspan=2, sticky="w", pady=(8, 0))
    tk.Label(frm, text="Either way, it follows ProPresenter's window when that moves.",
             fg=hint[0], font=hint[1]).grid(row=15, column=0, columnspan=2, sticky="w")

    error = tk.Label(frm, text="", fg="#d33", font=("Helvetica", 11))
    error.grid(row=16, column=0, columnspan=2, sticky="w")

    before = {k: cfg[k] for k in ("text_override", "width", "height", "radius", "shape")}

    def read_look():
        """The appearance fields as (values dict, error text or "")."""
        shape = next(k for k, v in SHAPES if v == shape_var.get())
        vals = {"shape": shape, "text_override": look_vars["text_override"].get()}
        for key, (lo, hi) in SIZE_LIMITS.items():
            if key == "height" and shape == "circle":
                vals[key] = cfg["height"]      # unused for a circle; keep what was saved
                continue
            if key == "radius" and shape != "rounded":
                vals[key] = cfg["radius"]
                continue
            try:
                n = int(look_vars[key].get().strip())
            except ValueError:
                return vals, f"{key.capitalize()} must be a whole number."
            if not lo <= n <= hi:
                return vals, f"{key.capitalize()} must be between {lo} and {hi}."
            vals[key] = n
        return vals, ""

    def preview(*_):
        shape = next(k for k, v in SHAPES if v == shape_var.get())
        size_entries["height"].config(state="disabled" if shape == "circle" else "normal")
        size_entries["radius"].config(state="normal" if shape == "rounded" else "disabled")
        size_note.config(text={"circle": "A circle uses the width for its height.",
                               "rounded": "Corner radius 0 = square corners."}.get(shape, ""))
        vals, problem = read_look()
        error.config(text=problem)
        if not problem:
            apply_look(vals["shape"], vals["width"], vals["height"], vals["radius"], vals["text_override"])

    for v in list(look_vars.values()) + [shape_var]:
        v.trace_add("write", preview)

    def save(_e=None):
        host = fields["host"].get().strip()
        try:
            page, row, col = (int(fields[k].get().strip()) for k in ("page", "row", "col"))
        except ValueError:
            error.config(text="Page, row and column must be whole numbers.")
            return
        if not host or " " in host:
            error.config(text="Enter a Companion address, e.g. 127.0.0.1")
            return
        if page < 1 or row < 0 or col < 0:
            error.config(text="Page must be 1 or more; row and column 0 or more.")
            return
        vals, problem = read_look()
        if problem:
            error.config(text=problem)
            return
        relink = (host, page, row, col) != (cfg["host"], cfg["page"], cfg["row"], cfg["col"])
        cfg.update(host=host, page=page, row=row, col=col, locked=locked_var.get(), **vals)
        try:
            save_settings()
        except OSError as ex:
            error.config(text=f"Couldn't save settings: {ex}")
            return
        apply_look(cfg["shape"], cfg["width"], cfg["height"], cfg["radius"], cfg["text_override"])
        if relink:
            sat.reconnect()
        close(keep=True)

    def close(_e=None, keep=False):
        global settings_win
        settings_win = None
        if not keep:   # Cancel / Escape / window close: undo the live preview
            apply_look(before["shape"], before["width"], before["height"], before["radius"],
                       before["text_override"])
        win.destroy()

    btns = tk.Frame(frm)
    btns.grid(row=17, column=0, columnspan=2, sticky="e", pady=(10, 0))
    tk.Button(btns, text="Cancel", command=close).pack(side="left", padx=(0, 6))
    tk.Button(btns, text="Save", command=save, default="active").pack(side="left")
    win.bind("<Return>", save)
    win.bind("<Escape>", close)
    win.protocol("WM_DELETE_WINDOW", close)
    preview()   # set which size fields apply to the current shape

    win.update_idletasks()
    x = min(root.winfo_x(), root.winfo_screenwidth() - win.winfo_width() - 20)
    y = root.winfo_y() + H + 10
    win.geometry(f"+{max(x, 0)}+{y}")
    win.focus_force()
    fields["page"].focus_set()


menu = tk.Menu(root, tearoff=0)
menu.add_command(label="Settings…", command=open_settings)
only_over_var = tk.BooleanVar(value=cfg["only_over_propresenter"])


def toggle_only_over():
    cfg["only_over_propresenter"] = only_over_var.get()
    cfg["only_over_user_set"] = True
    try:
        save_settings()
    except OSError as ex:
        print("Couldn't save setting:", ex)


menu.add_checkbutton(label="Only show over ProPresenter", variable=only_over_var, command=toggle_only_over)
only_modes_var = tk.BooleanVar(value=cfg["only_in_modes"])


def toggle_only_modes():
    cfg["only_in_modes"] = only_modes_var.get()
    if cfg["only_in_modes"]:
        pp_mode.trusted(prompt=True)   # macOS asks for Accessibility permission if not yet given
    try:
        save_settings()
    except OSError as ex:
        print("Couldn't save setting:", ex)


if pp_mode is not None:
    menu.add_checkbutton(label="Only show in Show, Reflow & Bible", variable=only_modes_var,
                         command=toggle_only_modes)
display_menu = tk.Menu(menu, tearoff=0)
display_menu.configure(postcommand=lambda: fill_display_menu(display_menu))
fill_display_menu(display_menu)
menu.add_cascade(label="Move to Display", menu=display_menu)
menu.add_command(label="Move to top-left", command=lambda: move_to(DEFAULTS["x"], DEFAULTS["y"]))
menu.add_separator()
menu.add_command(label="Quit", command=root.destroy)


# ---------------------------------------------------------------- macOS menu bar + Dock menu
def fill_window_menu(m):
    fill_display_menu(m)
    m.add_command(label="Move to top-left", command=lambda: move_to(DEFAULTS["x"], DEFAULTS["y"]))
    m.add_separator()     # macOS appends its own window items below


if sys.platform == "darwin":
    menubar = tk.Menu(root, name="menubar")
    window_menu = tk.Menu(menubar, name="window", tearoff=0)   # ".menubar.window" = the app's Window menu
    window_menu.configure(postcommand=lambda: fill_window_menu(window_menu))
    fill_window_menu(window_menu)
    menubar.add_cascade(label="Window", menu=window_menu)
    root.configure(menu=menubar)


class DockMenu:
    """Right-click menu on the Dock icon. Tk has no Dock menu, so this adds
    applicationDockMenu: to Tk's application delegate through the Objective-C runtime."""

    TOP_LEFT = 1000

    def __init__(self):
        vp = ctypes.c_void_p
        self.app = objc.send(objc.cls("NSApplication"), "sharedApplication")
        delegate = objc.send(self.app, "delegate")
        if not delegate:
            raise RuntimeError("no application delegate")
        self.menu = objc.send(objc.send(objc.cls("NSMenu"), "alloc"), "initWithTitle:", objc.nsstring(""),
                              argtypes=[vp])
        # Keep the callbacks alive for the life of the app.
        self.dock_imp = ctypes.CFUNCTYPE(vp, vp, vp, vp)(self._dock_menu)
        self.action_imp = ctypes.CFUNCTYPE(None, vp, vp, vp)(self._action)
        cls = objc.cls("TKApplication") or objc.cls("NSApplication")
        objc.lib.class_addMethod(cls, objc.sel("cfbDockAction:"), ctypes.cast(self.action_imp, vp), b"v@:@")
        dcls = self._class_of(delegate)
        objc.lib.class_addMethod(dcls, objc.sel("applicationDockMenu:"), ctypes.cast(self.dock_imp, vp), b"@@:@")
        # NSApplication caches which delegate methods exist; setting the delegate again refreshes that.
        objc.send(self.app, "setDelegate:", None, argtypes=[vp])
        objc.send(self.app, "setDelegate:", delegate, argtypes=[vp])

    @staticmethod
    def _class_of(obj):
        f = objc.lib.object_getClass
        f.restype, f.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
        return f(obj)

    def _item(self, title, tag):
        vp = ctypes.c_void_p
        item = objc.send(objc.send(objc.cls("NSMenuItem"), "alloc"), "initWithTitle:action:keyEquivalent:",
                         objc.nsstring(title), objc.sel("cfbDockAction:"), objc.nsstring(""),
                         argtypes=[vp, vp, vp])
        objc.send(item, "setTarget:", self.app, argtypes=[vp])
        objc.send(item, "setTag:", tag, argtypes=[ctypes.c_long])
        objc.send(self.menu, "addItem:", item, argtypes=[vp])
        objc.send(item, "release")

    def _dock_menu(self, _self, _cmd, _sender):
        try:
            objc.send(self.menu, "removeAllItems")
            for i, d in enumerate(displays()):
                self._item(f"Move to {d[0]}", i)
            self._item("Move to top-left", self.TOP_LEFT)
        except Exception as ex:
            print("Dock menu failed:", ex)
        return self.menu

    def _action(self, _self, _cmd, sender):
        tag = objc.send(sender, "tag", restype=ctypes.c_long)
        if tag == self.TOP_LEFT:
            ui.put(lambda: move_to(DEFAULTS["x"], DEFAULTS["y"]))
        else:
            ui.put(lambda: move_to_display(tag))


dock_menu = None


def install_dock_menu():
    global dock_menu
    if objc is None:
        return
    try:
        dock_menu = DockMenu()
    except Exception as ex:
        print("Dock menu unavailable:", ex)


def show_menu(e):
    look["down"] = False
    repaint()
    menu.tk_popup(e.x_root, e.y_root)


# ---------------------------------------------------------------- mouse
drag = {"x": 0, "y": 0, "moved": False, "ignore": False, "cancel": False}
LOCKED_SLOP = 4   # px a locked button may be pulled before the click is cancelled
CONTROL_MASK = 0x4


def down(e):
    # Control-click is the Mac "right-click": open the menu, don't press.
    drag.update(x=e.x, y=e.y, moved=False, ignore=bool(e.state & CONTROL_MASK), cancel=False)
    if drag["ignore"]:
        show_menu(e)
        return
    look["down"] = True
    repaint()


def move(e):
    if drag["ignore"]:
        return
    if cfg["locked"]:
        # Locked: don't move. Pulling it more than a few px cancels the click, so an
        # attempted drag never fires the Companion button by accident.
        if abs(e.x - drag["x"]) > LOCKED_SLOP or abs(e.y - drag["y"]) > LOCKED_SLOP:
            drag["cancel"] = True
        return
    drag["moved"] = True
    root.geometry(f"+{root.winfo_x()+e.x-drag['x']}+{root.winfo_y()+e.y-drag['y']}")


def up(e):
    if drag["ignore"]:
        return
    look["down"] = False
    repaint()
    if drag["moved"]:
        remember_position()
    elif not drag["cancel"]:
        threading.Thread(target=press, daemon=True).start()


def remember_position():
    cfg.update(x=root.winfo_x(), y=root.winfo_y())
    update_anchor()
    try:
        save_settings()
    except OSError as ex:
        print("Couldn't save position:", ex)


def move_to(x, y):
    # Save the target directly: while hidden (not over ProPresenter) winfo_x/y can be stale.
    root.geometry(f"+{x}+{y}")
    root.update_idletasks()
    cfg.update(x=int(x), y=int(y))
    update_anchor()
    try:
        save_settings()
    except OSError as ex:
        print("Couldn't save position:", ex)


# ---------------------------------------------------------------- follow ProPresenter's window
# It follows one window, identified by its window id: if that window is minimised or closed,
# the button stays put rather than jumping to another ProPresenter window (Preferences, the
# preview panel...). After a relaunch the id changes, so it re-finds the window by size.
SIZE_MATCH = 40   # px either way that counts as "the same window" when re-finding it


def update_anchor():
    """After the button is placed: tie it to the nearest corner of the ProPresenter window
    it sits on (the largest one, if several), otherwise let it float free. (Caller saves.)"""
    try:
        wins = propresenter_windows()
    except Exception as ex:
        print("Window check failed:", ex)
        wins = []
    cx, cy = cfg["x"] + W / 2, cfg["y"] + H / 2
    under = [w for w in wins if w[0] <= cx < w[0] + w[2] and w[1] <= cy < w[1] + w[3]]
    if not under:
        follow_state["wid"] = None
        cfg.update(anchor="", anchor_dx=0, anchor_dy=0, anchor_w=0, anchor_h=0)
        return
    rx, ry, rw, rh, _title, wid = max(under, key=lambda w: w[2] * w[3])
    corner = ("t" if cy < ry + rh / 2 else "b") + ("l" if cx < rx + rw / 2 else "r")
    bx, by = anchor_corner((rx, ry, rw, rh), corner)
    follow_state["wid"] = wid
    cfg.update(anchor=corner, anchor_dx=int(cfg["x"] - bx), anchor_dy=int(cfg["y"] - by), anchor_w=rw, anchor_h=rh)


def followed_window():
    """(x, y, w, h) of the window the button follows, or None if it isn't on screen."""
    wins = propresenter_windows()
    for w in wins:
        if w[5] == follow_state["wid"]:
            return w[:4]
    # Not seen yet this run (or ProPresenter was relaunched): look for a window the same size,
    # preferring the one titled "ProPresenter". Old settings without a size take the largest.
    aw, ah = cfg["anchor_w"], cfg["anchor_h"]
    if aw and ah:
        same = [w for w in wins if abs(w[2] - aw) <= SIZE_MATCH and abs(w[3] - ah) <= SIZE_MATCH]
    else:
        same = sorted(wins, key=lambda w: w[2] * w[3], reverse=True)[:1]
    same.sort(key=lambda w: w[4] != TARGET_APP)
    if not same:
        return None
    follow_state["wid"] = same[0][5]
    return same[0][:4]


def anchor_corner(r, corner):
    rx, ry, rw, rh = r
    return (rx if corner[1] == "l" else rx + rw), (ry if corner[0] == "t" else ry + rh)


follow_state = {"save": None, "first_look": True, "wid": None, "new_size": None, "seen": 0,
                "last_rect": None, "fast_until": 0.0}
FOLLOW_IDLE_MS, FOLLOW_FAST_MS = 50, 16    # check 20x/s when still; as fast as Tk allows (~37/s) while it moves
FOLLOW_FAST_HOLD = 0.5                     # seconds to stay fast after the window last moved
SIZE_SETTLE = 3   # ticks (~0.3 s) a new window size must hold before the button follows it


def follow_propresenter():
    # Runs 20x a second, faster while ProPresenter's window is moving: keep the button at the
    # same offset from that window's corner. Works whether or not the button is locked.
    try:
        if follow_state["first_look"] and not cfg["anchor"] and not look["down"]:
            # Settings from before this feature (or a button left on ProPresenter while it was
            # closed): the first time ProPresenter's window is seen, tie the button to it.
            if propresenter_windows():
                follow_state["first_look"] = False
                update_anchor()
                if cfg["anchor"]:
                    save_quietly()
        elif cfg["anchor"] in ("tl", "tr", "bl", "br") and not look["down"]:
            r = followed_window()   # None while it's minimised/closed: stay put
            if r and r != follow_state["last_rect"]:
                if follow_state["last_rect"] is not None:   # it moved: speed up for a moment
                    follow_state["fast_until"] = time.monotonic() + FOLLOW_FAST_HOLD
                follow_state["last_rect"] = r
            if r and (r[2], r[3]) != (cfg["anchor_w"], cfg["anchor_h"]):
                # Size changed. Wait until it holds still: the minimise/restore animation shrinks
                # the window toward the Dock for a moment, and the button mustn't chase that.
                if follow_state["new_size"] == (r[2], r[3]):
                    follow_state["seen"] += 1
                else:
                    follow_state.update(new_size=(r[2], r[3]), seen=1)
                if follow_state["seen"] < SIZE_SETTLE:
                    r = None
            if r:
                follow_state["new_size"] = None
                bx, by = anchor_corner(r, cfg["anchor"])
                x, y = bx + cfg["anchor_dx"], by + cfg["anchor_dy"]
                if (x, y, r[2], r[3]) != (cfg["x"], cfg["y"], cfg["anchor_w"], cfg["anchor_h"]):
                    if (x, y) != (cfg["x"], cfg["y"]):
                        root.geometry(f"+{x}+{y}")
                    cfg.update(x=x, y=y, anchor_w=r[2], anchor_h=r[3])
                    if follow_state["save"]:          # save once ProPresenter stops moving
                        root.after_cancel(follow_state["save"])
                    follow_state["save"] = root.after(1000, save_quietly)
    except Exception as ex:   # never let a window-list hiccup kill the button
        print("Follow failed:", ex)
    fast = time.monotonic() < follow_state["fast_until"]
    root.after(FOLLOW_FAST_MS if fast else FOLLOW_IDLE_MS, follow_propresenter)


def save_quietly():
    follow_state["save"] = None
    try:
        save_settings()
    except OSError as ex:
        print("Couldn't save position:", ex)


def hover(on):
    look["hover"] = on
    repaint()


c.bind("<ButtonPress-1>", down)
c.bind("<B1-Motion>", move)
c.bind("<ButtonRelease-1>", up)
c.bind("<Enter>", lambda e: hover(True))
c.bind("<Leave>", lambda e: hover(False))
c.bind("<Button-3>", show_menu)   # right-click (Windows, and macOS on Tk 9)
c.bind("<Button-2>", show_menu)   # right-click on macOS with older Tk


# ---------------------------------------------------------------- stay on top
shown = {"visible": True}


def keep_on_top():
    # macOS lets other apps cover a "topmost" borderless window; re-assert it.
    # Also hide the button unless ProPresenter's window is what's directly under it, and
    # (macOS) unless ProPresenter is in Show, Reflow or Bible mode.
    busy = look["down"] or (settings_win is not None and settings_win.winfo_exists())
    want = True
    if winlist is not None and cfg["only_over_propresenter"] and not busy:
        try:
            want = target_app_on_top(winlist, cfg["x"], cfg["y"], W, H)
        except Exception as ex:   # never let a window-list hiccup kill the button
            print("Window check failed:", ex)
    if want and pp_mode is not None and cfg["only_in_modes"] and not busy:
        try:
            modes = pp_mode.selected(propresenter_pid())
            if modes is not None:      # None = can't tell (no permission etc.): stay visible
                want = bool(modes & SHOW_MODES)
        except Exception as ex:
            print("Mode check failed:", ex)
    if want and not shown["visible"]:
        root.deiconify()
        shown["visible"] = True
        root.update_idletasks()
        mask_window(cfg["shape"], W, H, cfg["radius"])
    elif not want and shown["visible"]:
        root.withdraw()
        shown["visible"] = False
    if shown["visible"]:
        root.attributes("-topmost", True)
        root.lift()
    root.after(500, keep_on_top)


on_status(False)


def first_show():
    root.deiconify()
    root.update_idletasks()
    mask_window(cfg["shape"], W, H, cfg["radius"])
    if pp_mode is not None and cfg["only_in_modes"] and not pp_mode.trusted():
        pp_mode.trusted(prompt=True)   # first run: macOS shows its Accessibility permission dialog


root.after(50, first_show)
root.after(50, pump)
root.after(500, keep_on_top)
root.after(300, follow_propresenter)
root.after(200, install_dock_menu)
root.mainloop()
