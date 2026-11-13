import base64, ctypes, ctypes.util, json, os, queue, re, socket, sys, threading, time, tkinter as tk, tkinter.font as tkfont, urllib.request

# ---- settings ----
# Page/row/column and Companion address are edited from the button's right-click
# menu (Settings...) and saved to settings.json next to this script, along with
# where the button was last dragged to.
DEFAULTS = {"host": "127.0.0.1", "page": 9, "row": 0, "col": 1, "x": 60, "y": 60, "only_over_propresenter": True}
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
DEFAULT_FACE = "#2b2b2e"
DEFAULT_TEXT = "#ffffff"
ACCENT = "#e8a33a"
W, H, R = 150, 64, 12
MAX_FONT, MIN_FONT = 15, 8


def load_settings():
    cfg = dict(DEFAULTS)
    path = SETTINGS_FILE if os.path.exists(SETTINGS_FILE) else LEGACY_SETTINGS_FILE
    try:
        with open(path) as f:
            saved = json.load(f)
        for k in DEFAULTS:
            if k in saved and type(saved[k]) is type(DEFAULTS[k]):
                cfg[k] = saved[k]
    except (OSError, ValueError):
        pass
    return cfg


def save_settings():
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


cfg = load_settings()


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
                     ("kCGWindowOwnerName", "kCGWindowOwnerPID", "kCGWindowLayer", "kCGWindowBounds")}

    def _str(self, v):
        buf = ctypes.create_string_buffer(256)
        return buf.value.decode(errors="replace") if v and self.cf.CFStringGetCString(v, buf, 256, 0x08000100) else ""

    def _int(self, v):
        out = ctypes.c_int64(0)
        if v:
            self.cf.CFNumberGetValue(v, 4, ctypes.byref(out))   # kCFNumberSInt64Type
        return out.value

    def windows(self):
        """[(owner, pid, layer, x, y, w, h)] front to back, on-screen only, desktop excluded."""
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
                            self._int(g("kCGWindowLayer")), r.x, r.y, r.w, r.h))
        finally:
            self.cf.CFRelease(arr)
        return out


class _CGRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("w", ctypes.c_double), ("h", ctypes.c_double)]


def target_app_on_top(winlist, x, y, w, h):
    """True if the front-most normal app window overlapping the rect belongs to TARGET_APP."""
    me = os.getpid()
    for owner, pid, layer, wx, wy, ww, wh in winlist.windows():
        if pid == me or layer != 0:      # skip our own windows, menu bar, Dock, overlays
            continue
        if wx < x + w and x < wx + ww and wy < y + h and y < wy + wh:
            return owner == TARGET_APP
    return False


class WinWindowList:
    """Windows equivalent: top-level windows front to back via user32 (no extra installs).

    Returns the same tuples as WindowList; owner is the program name without ".exe",
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
            out.append((self._exe(pid.value), pid.value, layer, r.left, r.top, r.right - r.left, r.bottom - r.top))
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


def rrect(x1, y1, x2, y2, r, **kw):
    pts = [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2,
           x2-r,y2, x1+r,y2, x1,y2, x1,y2-r, x1,y1+r, x1,y1]
    return c.create_polygon(pts, smooth=True, **kw)


body = rrect(2, 2, W-2, H-2, R, fill=DEFAULT_FACE, outline="#000000", width=2)
bar = c.create_rectangle(14, H-12, W-14, H-8, fill=ACCENT, width=0)
font = tkfont.Font(family="Helvetica", size=MAX_FONT, weight="bold")
label = c.create_text(W/2, H/2-4, text=FALLBACK_LABEL, fill=DEFAULT_TEXT, font=font,
                      justify="center", width=W-16)

ui = queue.Queue()   # background threads hand UI updates to the Tk thread here
# (Tk 9: root.after() called from a background thread silently never runs.)


def pump():
    while True:
        try:
            ui.get_nowait()()
        except queue.Empty:
            break
    root.after(50, pump)


look = {"face": DEFAULT_FACE, "hover": False, "down": False, "pushed": False}


def repaint():
    face = look["face"]
    if look["down"] or look["pushed"]:
        face = shade(face, -0.35)
    elif look["hover"]:
        face = shade(face, 0.15)
    c.itemconfig(body, fill=face)


def set_text(text):
    text = (text or FALLBACK_LABEL).replace("\\n", "\n")
    c.itemconfig(label, text=text)
    # Shrink the font until the text fits the button.
    for size in range(MAX_FONT, MIN_FONT - 1, -1):
        font.configure(size=size)
        x1, y1, x2, y2 = c.bbox(label)
        if (y2 - y1) <= H - 20 and (x2 - x1) <= W - 12:
            break


def on_state(text, color, textcolor, pushed):
    def apply():
        look["face"] = color or DEFAULT_FACE
        look["pushed"] = pushed
        c.itemconfig(label, fill=textcolor or DEFAULT_TEXT)
        set_text(text)
        repaint()
    ui.put(apply)


def on_status(connected):
    def apply():
        c.itemconfig(bar, fill=ACCENT if connected else "#666666")
        if not connected:
            look["face"], look["pushed"] = DEFAULT_FACE, False
            c.itemconfig(label, fill=DEFAULT_TEXT)
            set_text(FALLBACK_LABEL)
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

    tk.Label(frm, text="Row and column start at 0 (top-left button is row 0, column 0).",
             fg="#888888", font=("Helvetica", 11)).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
    error = tk.Label(frm, text="", fg="#d33", font=("Helvetica", 11))
    error.grid(row=6, column=0, columnspan=2, sticky="w")

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
        cfg.update(host=host, page=page, row=row, col=col)
        try:
            save_settings()
        except OSError as ex:
            error.config(text=f"Couldn't save settings: {ex}")
            return
        sat.reconnect()
        close()

    def close(_e=None):
        global settings_win
        settings_win = None
        win.destroy()

    btns = tk.Frame(frm)
    btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
    tk.Button(btns, text="Cancel", command=close).pack(side="left", padx=(0, 6))
    tk.Button(btns, text="Save", command=save, default="active").pack(side="left")
    win.bind("<Return>", save)
    win.bind("<Escape>", close)
    win.protocol("WM_DELETE_WINDOW", close)

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
    try:
        save_settings()
    except OSError as ex:
        print("Couldn't save setting:", ex)


menu.add_checkbutton(label="Only show over ProPresenter", variable=only_over_var, command=toggle_only_over)
menu.add_command(label="Move to top-left", command=lambda: move_to(DEFAULTS["x"], DEFAULTS["y"]))
menu.add_separator()
menu.add_command(label="Quit", command=root.destroy)


def show_menu(e):
    look["down"] = False
    repaint()
    menu.tk_popup(e.x_root, e.y_root)


# ---------------------------------------------------------------- mouse
drag = {"x": 0, "y": 0, "moved": False, "ignore": False}
CONTROL_MASK = 0x4


def down(e):
    # Control-click is the Mac "right-click": open the menu, don't press.
    drag.update(x=e.x, y=e.y, moved=False, ignore=bool(e.state & CONTROL_MASK))
    if drag["ignore"]:
        show_menu(e)
        return
    look["down"] = True
    repaint()


def move(e):
    if drag["ignore"]:
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
    else:
        threading.Thread(target=press, daemon=True).start()


def remember_position():
    cfg.update(x=root.winfo_x(), y=root.winfo_y())
    try:
        save_settings()
    except OSError as ex:
        print("Couldn't save position:", ex)


def move_to(x, y):
    root.geometry(f"+{x}+{y}")
    root.update_idletasks()
    remember_position()


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
    # Also hide the button unless ProPresenter's window is what's directly under it.
    busy = look["down"] or (settings_win is not None and settings_win.winfo_exists())
    want = True
    if winlist is not None and cfg["only_over_propresenter"] and not busy:
        try:
            want = target_app_on_top(winlist, cfg["x"], cfg["y"], W, H)
        except Exception as ex:   # never let a window-list hiccup kill the button
            print("Window check failed:", ex)
    if want and not shown["visible"]:
        root.deiconify()
        shown["visible"] = True
    elif not want and shown["visible"]:
        root.withdraw()
        shown["visible"] = False
    if shown["visible"]:
        root.attributes("-topmost", True)
        root.lift()
    root.after(500, keep_on_top)


on_status(False)
root.after(50, root.deiconify)
root.after(50, pump)
root.after(500, keep_on_top)
root.mainloop()
