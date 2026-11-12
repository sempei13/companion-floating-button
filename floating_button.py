import json, threading, tkinter as tk, urllib.request

# ---- settings ----
COMPANION = "http://127.0.0.1:8000"
PAGE, ROW, COL = 1, 0, 0
FALLBACK_LABEL = "CLEAR"
POLL_SECONDS = 2

# ---- look (adjust to match your reference) ----
BG_TRANSPARENT = "#010101"
FACE, FACE_HOVER, FACE_DOWN = "#2b2b2e", "#3a3a3e", "#1c1c1e"
ACCENT = "#e8a33a"
TEXT = "#ffffff"
W, H, R = 150, 64, 12

loc = f"{COMPANION}/api/location/{PAGE}/{ROW}/{COL}"

def press():
    try:
        urllib.request.urlopen(urllib.request.Request(loc + "/press", method="POST"), timeout=2)
    except Exception as e:
        print("Press failed:", e)

def fetch_label():
    # NOTE: unverified endpoint - Companion may not expose button text over HTTP.
    try:
        with urllib.request.urlopen(loc + "/style", timeout=2) as r:
            return json.load(r).get("text") or FALLBACK_LABEL
    except Exception:
        return None

root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.geometry(f"{W}x{H}+60+60")
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

body = rrect(2, 2, W-2, H-2, R, fill=FACE, outline="#000000", width=2)
bar = c.create_rectangle(14, H-12, W-14, H-8, fill=ACCENT, width=0)
label = c.create_text(W/2, H/2-4, text=FALLBACK_LABEL, fill=TEXT,
                      font=("Helvetica", 15, "bold"))

drag = {"x": 0, "y": 0, "moved": False}
def down(e):
    drag.update(x=e.x, y=e.y, moved=False); c.itemconfig(body, fill=FACE_DOWN)
def move(e):
    drag["moved"] = True
    root.geometry(f"+{root.winfo_x()+e.x-drag['x']}+{root.winfo_y()+e.y-drag['y']}")
def up(e):
    c.itemconfig(body, fill=FACE_HOVER)
    if not drag["moved"]:
        threading.Thread(target=press, daemon=True).start()

c.bind("<ButtonPress-1>", down)
c.bind("<B1-Motion>", move)
c.bind("<ButtonRelease-1>", up)
c.bind("<Enter>", lambda e: c.itemconfig(body, fill=FACE_HOVER))
c.bind("<Leave>", lambda e: c.itemconfig(body, fill=FACE))
c.bind("<Button-3>", lambda e: root.destroy())   # right-click to quit
c.bind("<Button-2>", lambda e: root.destroy())   # mac right-click

def refresh():
    def work():
        t = fetch_label()
        if t: root.after(0, lambda: c.itemconfig(label, text=t.replace("\\n", "\n")))
    threading.Thread(target=work, daemon=True).start()
    root.after(POLL_SECONDS * 1000, refresh)

refresh()
root.mainloop()
