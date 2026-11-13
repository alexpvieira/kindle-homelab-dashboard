#!/usr/bin/env python3
# Kindle homelab dashboard, server side.
# A jailbroken Kindle is a dumb touch screen: it asks GET /k/screen for a PNG (its framebuffer size, greyscale)
# and sends raw touch coordinates as ?x=&y=. All layout, state and actions live here.
#   GET /k/screen?t=TOKEN[&v=<md5 of the PNG it shows>][&x=&y=][&b=<battery %>&c=<1 charging>]
#       -> 200 PNG, or 204 when unchanged
#       response header X-Action: exit (Kindle goes back to its UI) / flash (full e-ink refresh)
#   GET /preview.png?t=TOKEN[&page=status|containers|actions]     -> current screen, for a browser
#   GET /health                                                    -> "ok"
# Pages: Status (verdict, CPU/memory/temperature gauges, storage, network, UPS), Containers (tap one to
# restart), Actions (container shortcuts + configurable actions). Every action asks for a confirming tap.
# Settings: config.toml (see config.example.toml). DEMO=1 renders made-up data and runs no actions.
# Icons are Font Awesome Free (solid), downloaded into the image by the Dockerfile.
import hashlib, http.client, io, json, os, random, socket, threading, time, tomllib, traceback, urllib.parse, urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PIL import Image, ImageDraw, ImageFont

CONFIG_FILE = os.environ.get("CONFIG", "/config/config.toml")
try:
    with open(CONFIG_FILE, "rb") as f:
        CFG = tomllib.load(f)
except FileNotFoundError:
    CFG = {}
DEMO = os.environ.get("DEMO", "0") == "1" or CFG.get("demo", False)

TOKEN = os.environ.get("KINDLE_DASH_TOKEN") or CFG.get("token") or ("demo" if DEMO else None)
if not TOKEN:
    raise SystemExit("set token in config.toml or KINDLE_DASH_TOKEN")
PORT = int(os.environ.get("PORT") or CFG.get("port", 8100))
HOSTNAME = CFG.get("name") or ("homelab" if DEMO else socket.gethostname())

# The Kindle's framebuffer (portrait). Pages are laid out for a 1072 px wide portrait screen (Paperwhite 3/4);
# other sizes get a canvas with the same short side and their own aspect ratio, scaled when sent.
FB_W, FB_H = CFG.get("screen", [1072, 1448])
LAYOUT_SHORT = 1072
LW, LH = LAYOUT_SHORT, round(LAYOUT_SHORT * FB_H / FB_W)        # logical portrait canvas

# How the Kindle hangs: portrait, or landscape with its bottom edge (USB port) on the right or the left.
# Landscape pages are drawn at LH x LW and rotated into the portrait framebuffer; taps are rotated back.
# The Rotate button (Actions) cycles through ORIENTATIONS; the choice is kept in ORIENTATION_FILE.
ORIENTATIONS = ["portrait", "landscape-port-right", "landscape-port-left"]
ORIENTATION_LABELS = {"portrait": "portrait", "landscape-port-right": "landscape, port right",
                      "landscape-port-left": "landscape, port left"}
ORIENTATION_FILE = os.environ.get("ORIENTATION_FILE", "/state/orientation")
ORIENTATION = LANDSCAPE = W = H = CL = CR = CB = None    # set by set_orientation()

GATUS = (CFG.get("gatus") or {}).get("url")
UPS_CFG = CFG.get("ups")
NUT = (UPS_CFG.get("host", "127.0.0.1"), int(UPS_CFG.get("port", 3493)), UPS_CFG.get("name", "ups")) if UPS_CFG is not None else None
CHECKS = CFG.get("checks", [])               # [{name, url, icon}] plain HTTP checks
NET_IF = CFG.get("net_interface") or None    # None: the interface of the default route
INTERNET = CFG.get("internet_check", "1.1.1.1:443")
SELF = CFG.get("self_container", "kindle-dash")
# Storage rows (paths as mounted in this container; slow/hung network mounts are handled).
STORAGE = [(s["label"], s.get("detail", ""), s["path"]) for s in CFG.get("storage", [{"label": "System", "path": "/rootfs"}])]
SHORTCUTS = CFG.get("shortcuts", [])[:4]     # containers with a restart button on the Actions page
# Touch transform: raw Kindle coordinates -> framebuffer pixels. The defaults fit the Paperwhite 4.
TOUCH = CFG.get("touch", {})
TOUCH_SWAP = TOUCH.get("swap", False)
TOUCH_FLIP_X = TOUCH.get("flip_x", False)
TOUCH_FLIP_Y = TOUCH.get("flip_y", False)
TOUCH_MAX_X = TOUCH.get("max_x", FB_W - 1)
TOUCH_MAX_Y = TOUCH.get("max_y", FB_H - 1)
CONFIRM_SECONDS = 30
CONFIRM_GUARD = 1.5         # taps on "do" this soon after the confirm screen opened are ignored (double taps)
IDLE_SECONDS = 120          # back to Status after this long without a tap
FLASH_EVERY = 8             # full e-ink refresh after this many partial redraws
KINDLE_LOW = CFG.get("kindle_battery_warning", 20)   # % at which an unplugged Kindle shows up as a problem

# Font Awesome 6 solid code points. Config files may use these names, or a hex code point such as "f1eb".
I = {"server": "", "microchip": "", "memory": "", "temp": "", "drive": "",
     "database": "", "network": "", "down": "", "up": "", "plug": "", "bolt": "",
     "globe": "", "wifi": "", "pulse": "", "cubes": "", "check": "", "xmark": "",
     "warn": "", "hourglass": "", "restart": "", "power": "", "film": "",
     "house": "", "shield": "", "download": "", "wrench": "", "refresh": "",
     "exit": "", "gauge": "", "sliders": "", "left": "", "right": "", "play": "",
     "fiber": "", "kindle": "", "rotate": "", "battery": "", "cube": "", "clock": "",
     "music": "", "camera": "", "book": "", "cloud": "", "lock": "", "key": "",
     "lightbulb": "", "router": "", "bell": "", "terminal": "", "code": "",
     "envelope": "", "image": "", "video": "", "headphones": "", "gamepad": "",
     "print": "", "fan": "", "robot": "", "chart": "", "folder": "", "trash": ""}
SERVICE_ICONS = {"plex": "film", "jellyfin": "film", "emby": "film", "homeassistant": "house",
                 "home-assistant": "house", "gluetun": "shield", "wireguard": "shield", "qbittorrent": "download",
                 "transmission": "download", "sabnzbd": "download", "nzbget": "download", "immich-server": "image",
                 "immich": "image", "photoprism": "image", "adguard": "shield", "pihole": "shield",
                 "nextcloud": "cloud", "vaultwarden": "lock", "navidrome": "music", "audiobookshelf": "headphones",
                 "calibre-web": "book", "frigate": "video", "mosquitto": "bell", "grafana": "chart"}


def icon_char(name):
    if name in I:
        return I[name]
    try:
        return chr(int(name, 16))
    except (TypeError, ValueError):
        return I["cube"]

# ---------------------------------------------------------------- data sources

def docker(method, path, body=None, timeout=30):
    c = http.client.HTTPConnection("localhost", timeout=timeout)
    c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.sock.settimeout(timeout)
    c.sock.connect("/var/run/docker.sock")
    data = json.dumps(body).encode() if body is not None else None
    c.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse()
    raw = r.read()
    c.close()
    if r.status >= 300:
        raise RuntimeError(f"docker {method} {path}: {r.status} {raw[:200]!r}")
    return json.loads(raw) if raw and raw[:1] in b"[{" else raw


def containers():
    out = []
    for c in docker("GET", "/containers/json?all=1"):
        name = c["Names"][0].lstrip("/")
        st = c.get("Status", "")
        if c["State"] != "running" or "unhealthy" in st:
            level = 2
        elif "health: starting" in st or "Restarting" in st:
            level = 1
        else:
            level = 0
        out.append({"id": c["Id"], "name": name, "state": c["State"], "status": st, "level": level})
    return sorted(out, key=lambda c: (-c["level"], c["name"]))


def get_json(url, timeout=4):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def gatus():
    eps = get_json(f"{GATUS.rstrip('/')}/api/v1/endpoints/statuses")
    down = [e["name"] for e in eps if e.get("results") and not e["results"][-1]["success"]]
    return len(eps), down


def http_checks():
    """[(check, ok)] for the [[checks]] in the config: ok = any HTTP answer below 400 within 4 s."""
    out = []
    for c in CHECKS:
        try:
            with urllib.request.urlopen(c["url"], timeout=4) as r:
                ok = r.status < 400
        except Exception:
            ok = False
        out.append((c, ok))
    return out


def ups():
    s = socket.create_connection(NUT[:2], timeout=4)
    s.sendall(f"LIST VAR {NUT[2]}\n".encode())
    buf = b""
    while b"END LIST VAR" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    v = {}
    for line in buf.decode(errors="replace").splitlines():
        if line.startswith("VAR "):
            _, _, key, val = line.split(" ", 3)
            v[key] = val.strip('"')
    return v


def cpu_times():
    with open("/proc/stat") as f:
        p = [int(x) for x in f.readline().split()[1:]]
    return sum(p), p[3] + p[4]


def default_interface():
    with open("/proc/net/route") as f:
        for line in f.readlines()[1:]:
            p = line.split()
            if p[1] == "00000000":
                return p[0]
    return None


def net_bytes():
    iface = NET_IF or default_interface()
    with open("/proc/net/dev") as f:
        for line in f:
            if iface and line.strip().startswith(iface + ":"):
                p = line.split(":", 1)[1].split()
                return int(p[0]), int(p[8])
    return 0, 0


def cpu_temp():
    """First CPU sensor found: Intel coretemp, AMD k10temp/zenpower, Raspberry Pi/ARM thermal zones."""
    base = "/sys/class/hwmon"
    names = {}
    for h in os.listdir(base):
        try:
            names[open(f"{base}/{h}/name").read().strip()] = h
        except OSError:
            pass
    for n in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "acpitz"):
        if n in names:
            return int(open(f"{base}/{names[n]}/temp1_input").read()) / 1000
    try:
        return int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except OSError:
        return None


def meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            m[k] = int(v.split()[0]) * 1024
    return m["MemTotal"], m["MemTotal"] - m["MemAvailable"]


def internet_ms():
    host, _, port = INTERNET.rpartition(":")
    t = time.time()
    try:
        socket.create_connection((host, int(port)), timeout=3).close()
        return (time.time() - t) * 1000
    except OSError:
        return None


class StorageWatch(threading.Thread):
    """statvfs of every storage path every 60 s in its own thread: a hung NFS mount must not block drawing."""
    def __init__(self):
        super().__init__(daemon=True)
        self.result = {}          # path -> (total, free) or None when it failed
        self.started = {}         # path -> time the current check started (stays set while it hangs)

    def check(self, path):
        try:
            st = os.statvfs(path)
            self.result[path] = (st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize)
        except OSError:
            self.result[path] = None
        self.started.pop(path, None)

    def run(self):
        while True:
            for _, _, path in STORAGE:
                if path not in self.started:
                    self.started[path] = time.time()
                    threading.Thread(target=self.check, args=(path,), daemon=True).start()
            time.sleep(60)

    def get(self, path):
        """(total, free), or 'hung' / 'error' / None (not checked yet)."""
        if path in self.started and time.time() - self.started[path] > 20:
            return "hung"
        if path in self.result:
            return self.result[path] or "error"
        return None


class Sampler(threading.Thread):
    """Samples host CPU/network every 15 s for the 1-hour sparklines."""
    def __init__(self):
        super().__init__(daemon=True)
        self.cpu = deque(maxlen=240)
        self.rx = deque(maxlen=240)
        self.tx = deque(maxlen=240)

    def run(self):
        prev_c, prev_n, prev_t = cpu_times(), net_bytes(), time.time()
        while True:
            time.sleep(15)
            c, n, t = cpu_times(), net_bytes(), time.time()
            dt = c[0] - prev_c[0]
            self.cpu.append(100 * (1 - (c[1] - prev_c[1]) / dt) if dt else 0)
            self.rx.append((n[0] - prev_n[0]) * 8 / (t - prev_t) / 1e6)
            self.tx.append((n[1] - prev_n[1]) * 8 / (t - prev_t) / 1e6)
            prev_c, prev_n, prev_t = c, n, t


SAMPLER = Sampler()
STORAGE_WATCH = StorageWatch()


def collect():
    if DEMO:
        return demo_data()
    d = {"errors": []}
    def safe(key, fn):
        try:
            d[key] = fn()
        except Exception as e:
            d[key] = None
            d["errors"].append(f"{key}: {e}")
    safe("containers", containers)
    if GATUS:
        safe("gatus", gatus)
    if NUT:
        safe("ups", ups)
    d["checks"] = http_checks()
    safe("mem", meminfo)
    safe("temp", cpu_temp)
    safe("ping", internet_ms)
    d["storage"] = [(label, what, STORAGE_WATCH.get(path)) for label, what, path in STORAGE]
    with open("/proc/uptime") as f:
        d["uptime"] = float(f.read().split()[0])
    return d


DEMO_NAMES = ["adguard", "audiobookshelf", "bazarr", "gluetun", "grafana", "homeassistant", "immich-server",
              "jellyfin", "mosquitto", "navidrome", "nextcloud", "paperless", "plex", "prowlarr", "qbittorrent",
              "radarr", "sonarr", "syncthing", "uptime-kuma", "vaultwarden", "watchtower", "zigbee2mqtt"]


def demo_data():
    """Made-up but plausible numbers, for screenshots and trying the Kindle side without a server."""
    rnd = random.Random(int(time.time() // 60))
    if not SAMPLER.cpu:
        for i in range(240):
            SAMPLER.cpu.append(12 + 10 * abs((i % 60) - 30) / 30 + rnd.random() * 6)
            SAMPLER.rx.append(max(0, 40 + 35 * (1 if 80 < i < 140 else 0) + rnd.gauss(0, 8)))
            SAMPLER.tx.append(max(0, 6 + rnd.gauss(0, 2)))
    cs = [{"id": n, "name": n, "state": "running", "status": f"Up {rnd.choice([2, 5, 8, 12])} days (healthy)", "level": 0}
          for n in DEMO_NAMES]
    storage = [(label, what, (4e12, 4e12 * (0.2 + 0.6 * rnd.random()))) for label, what, _ in STORAGE]
    return {"errors": [], "containers": cs, "gatus": (24, []) if GATUS or not CFG else None,
            "ups": {"ups.status": "OL", "battery.charge": "100", "battery.runtime": "2700", "ups.load": "14"},
            "checks": [(c, True) for c in CHECKS], "mem": (32 * 2**30, 9.6 * 2**30), "temp": 46.0,
            "ping": 9.0, "storage": storage, "uptime": 12 * 86400 + 5 * 3600}

# ---------------------------------------------------------------- actions

def restart(name, t=20):
    docker("POST", f"/containers/{name}/restart?t={t}", timeout=t + 60)
    return f"{name} restarted"


def start(name):
    docker("POST", f"/containers/{name}/start", timeout=60)
    return f"{name} started"


def exec_in(container, cmd, timeout=120):
    e = docker("POST", f"/containers/{container}/exec", {"Cmd": cmd, "AttachStdout": True, "AttachStderr": True})
    out = docker("POST", f"/exec/{e['Id']}/start", {"Detach": False}, timeout=timeout)
    code = docker("GET", f"/exec/{e['Id']}/json").get("ExitCode")
    text = out.decode(errors="replace") if isinstance(out, bytes) else str(out)
    # The stream is multiplexed: keep only printable text of the last line.
    last = "".join(ch for ch in text.strip().splitlines()[-1] if ch.isprintable()) if text.strip() else ""
    if code:
        raise RuntimeError(last or f"exit {code}")
    return last or "done"


def http_call(url, method="POST", body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, method=method, data=body.encode() if body else None, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return f"HTTP {r.status}"


def reboot_host():
    # A short-lived privileged container enters the host namespaces and asks systemd to reboot.
    # Our own image has nsenter (util-linux).
    img = docker("GET", f"/containers/{SELF}/json")["Config"]["Image"]
    c = docker("POST", "/containers/create?name=kindle-dash-reboot", {
        "Image": img, "Entrypoint": ["sh", "-c", "sleep 8; nsenter -t 1 -m -u -i -n -p -- systemctl reboot"],
        "HostConfig": {"Privileged": True, "PidMode": "host", "AutoRemove": True}})
    docker("POST", f"/containers/{c['Id']}/start")
    return "server reboots in a few seconds"


def unhealthy_restart():
    bad = [c["name"] for c in containers() if c["level"] == 2 and c["state"] == "running"]
    for n in bad:
        restart(n)
    return f"restarted {len(bad)}: {', '.join(bad)}" if bad else "nothing unhealthy"


BUILTINS = {
    "restart_unhealthy": dict(label="Fix unhealthy", sub="restart failing ones", icon="wrench",
                              title="Restart unhealthy containers?", button="Restart them",
                              text="Restarts every running container whose health check fails.", fn=unhealthy_restart),
    "reboot_host": dict(label="Reboot server", sub="~2 min", icon="power", title=f"Reboot {HOSTNAME}?",
                        button="Reboot server", fn=reboot_host,
                        text="Every service stops. The server is back in a few minutes; this screen reconnects by itself."),
}


def make_action(a):
    """One [[actions]] entry -> dict(label, sub, icon, title, text, button, fn)."""
    base = dict(BUILTINS.get(a.get("builtin"), {}))
    if "exec" in a:
        e = a["exec"]
        cmd = e["cmd"] if isinstance(e["cmd"], list) else ["sh", "-c", e["cmd"]]
        base["fn"] = lambda: exec_in(e["container"], cmd, e.get("timeout", 120))
    elif "http" in a:
        h = a["http"]
        base["fn"] = lambda: http_call(h["url"], h.get("method", "POST"), h.get("body"), h.get("headers"))
    elif "restart" in a:
        base["fn"] = lambda: restart(a["restart"])
    if "fn" not in base:
        raise SystemExit(f"action {a.get('label')!r}: needs builtin, exec, http or restart")
    for k in ("label", "sub", "icon", "title", "text", "button"):
        if k in a:
            base[k] = a[k]
    base.setdefault("label", "Action")
    base.setdefault("sub", "")
    base.setdefault("icon", "bolt")
    base.setdefault("title", base["label"] + "?")
    base.setdefault("text", "")
    base.setdefault("button", base["label"])
    if DEMO:
        label = base["label"]
        base["fn"] = lambda: (time.sleep(2), f"demo: {label} (nothing ran)")[1]
    return base


ACTIONS_TITLE = CFG.get("actions_title", "Server")
ACTIONS = {f"a{i}": make_action(a) for i, a in enumerate(
    CFG.get("actions", [{"builtin": "restart_unhealthy"}, {"builtin": "reboot_host"}])[:4])}

# ---------------------------------------------------------------- drawing

def font_path(kind):
    cands = {"bold": ["/usr/share/fonts/opentype/inter/Inter-Bold.otf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
             "regular": ["/usr/share/fonts/opentype/inter/Inter-Regular.otf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
             "icons": ["/fonts/fa-solid-900.ttf"]}[kind]
    for p in cands:
        if os.path.exists(p):
            return p
    raise SystemExit(f"no {kind} font found")


_fonts = {}
def F(size, kind="regular"):
    k = (size, kind)
    if k not in _fonts:
        _fonts[k] = ImageFont.truetype(font_path(kind), size)
    return _fonts[k]


BLACK, DARK, MID, LIGHT, PALE, WHITE = 0, 0x44, 0x88, 0xCC, 0xEE, 0xFF
M = 40                       # side margin
GAP = 20
TAB_W, TAB_H = 112, 84        # page buttons in the header, between the hostname and the clock
TABS = [("status", "Status", "gauge"), ("containers", "Containers", "cubes"), ("actions", "Actions", "sliders")]
# content area: left/right edges (margins included) and bottom


def set_orientation(o, save=False):
    """Switches the canvas size and content area (CL/CR = content edges incl. margins, CB = bottom)."""
    global ORIENTATION, LANDSCAPE, W, H, CL, CR, CB
    ORIENTATION = o if o in ORIENTATIONS else "portrait"
    LANDSCAPE = ORIENTATION.startswith("landscape")
    W, H = (LH, LW) if LANDSCAPE else (LW, LH)
    CL = M
    CR = W - M
    CB = H
    if save:
        try:
            with open(ORIENTATION_FILE, "w") as f:
                f.write(ORIENTATION)
        except OSError as e:
            print(f"can't save orientation: {e}", flush=True)


def saved_orientation():
    try:
        return open(ORIENTATION_FILE).read().strip()
    except OSError:
        return os.environ.get("ORIENTATION", "portrait")


set_orientation(saved_orientation())


def human_size(b):
    t = b / 1e12
    return f"{t:.1f} TB" if t >= 1 else f"{b / 1e9:.0f} GB"


def human_uptime(s):
    d, h, m = int(s // 86400), int(s % 86400 // 3600), int(s % 3600 // 60)
    return f"{d} day{'s' if d != 1 else ''} {h} h" if d else f"{h} h {m} min"


class Canvas:
    def __init__(self):
        self.img = Image.new("L", (W, H), WHITE)
        self.d = ImageDraw.Draw(self.img)
        self.zones = []          # (x0, y0, x1, y1, action)

    def text(self, xy, s, size, bold=False, fill=BLACK, anchor="la"):
        self.d.text(xy, s, font=F(size, "bold" if bold else "regular"), fill=fill, anchor=anchor)

    def icon(self, xy, name, size, fill=BLACK, anchor="la"):
        self.d.text(xy, icon_char(name), font=F(size, "icons"), fill=fill, anchor=anchor)

    def width(self, s, size, bold=False):
        return self.d.textlength(s, font=F(size, "bold" if bold else "regular"))

    def fit(self, s, size, bold, maxw):
        if self.width(s, size, bold) <= maxw:
            return s
        while s and self.width(s + "…", size, bold) > maxw:
            s = s[:-1]
        return s + "…"

    def wrap(self, s, size, maxw):
        lines, line = [], ""
        for wd in s.split():
            if line and self.width(line + " " + wd, size) > maxw:
                lines.append(line)
                line = wd
            else:
                line = (line + " " + wd).strip()
        return lines + [line] if line else lines

    def rect(self, box, fill=None, outline=None, width=1, radius=0):
        if radius:
            self.d.rounded_rectangle(box, radius, fill=fill, outline=outline, width=width)
        else:
            self.d.rectangle(box, fill=fill, outline=outline, width=width)

    def card(self, box, title=None, icon=None):
        self.rect(box, outline=BLACK, width=3, radius=24)
        if title:
            x = box[0] + 26
            if icon:
                self.icon((x, box[1] + 26), icon, 30, DARK)
                x += 48
            self.text((x, box[1] + 26), title, 30, True, DARK)

    def zone(self, box, action):
        self.zones.append((*box, action))

    def button(self, box, label, action, icon=None, sub=None, primary=False, size=40):
        self.rect(box, fill=BLACK if primary else WHITE, outline=BLACK, width=4, radius=22)
        fg, fg2 = (WHITE, LIGHT) if primary else (BLACK, DARK)
        cy = (box[1] + box[3]) / 2
        if icon and sub and box[2] - box[0] < 340:
            # Narrow (landscape grid): icon on top, label and sub-label centred below
            cx = (box[0] + box[2]) / 2
            maxw = box[2] - box[0] - 24
            self.icon((cx, box[1] + 50), icon, 48, fg, "mm")
            self.text((cx, box[1] + 108), self.fit(label, size, True, maxw), size, True, fg, "mm")
            if sub:
                self.text((cx, box[1] + 146), self.fit(sub, 24, False, maxw), 24, False, fg2, "mm")
        elif icon and box[2] - box[0] < 600:
            # Icon on the left, text block next to it
            self.icon((box[0] + 34, cy), icon, 52, fg, "lm")
            tx = box[0] + 112
            maxw = box[2] - tx - 20
            if sub:
                self.text((tx, cy - 4), self.fit(label, size, True, maxw), size, True, fg, "ls")
                self.text((tx, cy + 10), self.fit(sub, 26, False, maxw), 26, False, fg2, "lt")
            else:
                self.text((tx, cy), self.fit(label, size, True, maxw), size, True, fg, "lm")
        else:
            cx = (box[0] + box[2]) / 2
            if icon:
                lw = self.width(label, size, True) + size + 30
                self.icon((cx - lw / 2, cy), icon, size, fg, "lm")
                self.text((cx - lw / 2 + size + 30, cy), label, size, True, fg, "lm")
            else:
                self.text((cx, cy), label, size, True, fg, "mm")
        self.zone(box, action)

    def bar(self, x, y, w, h, frac, fill=BLACK):
        self.rect((x, y, x + w, y + h), fill=PALE, outline=LIGHT, width=1, radius=h // 2)
        if frac > 0:
            self.rect((x, y, x + max(h, int(w * min(1, frac))), y + h), fill=fill, radius=h // 2)

    def gauge(self, cx, cy, r, frac, width=26):
        """270-degree ring gauge, open at the bottom."""
        box = (cx - r, cy - r, cx + r, cy + r)
        self.d.arc(box, 135, 405, fill=PALE, width=width)
        frac = max(0, min(1, frac))
        if frac > 0.005:
            self.d.arc(box, 135, 135 + 270 * frac, fill=BLACK, width=width)

    def spark(self, x, y, w, h, values, top=None):
        vals = list(values)
        self.rect((x, y + h, x + w, y + h + 2), fill=LIGHT)
        if len(vals) < 2:
            self.text((x + w / 2, y + h / 2), "collecting…", 24, False, MID, "mm")
            return
        top = top or max(max(vals), 1e-9)
        n = len(vals)
        pts = [(x + i * w / (n - 1), y + h - h * min(v, top) / top) for i, v in enumerate(vals)]
        self.d.polygon(pts + [(x + w, y + h), (x, y + h)], fill=LIGHT)
        self.d.line(pts, fill=BLACK, width=3, joint="curve")

    def battery(self, x, y, w, h, frac, charging):
        """Battery outline with a fill level and a bolt when charging."""
        self.rect((x, y, x + w, y + h), outline=BLACK, width=5, radius=12)
        self.rect((x + w, y + h * 0.3, x + w + 12, y + h * 0.7), fill=BLACK, radius=4)
        inner = w - 20
        if frac > 0:
            self.rect((x + 10, y + 10, x + 10 + max(8, int(inner * min(1, frac))), y + h - 10), fill=DARK, radius=6)
        if charging:
            self.icon((x + w / 2, y + h / 2), "bolt", int(h * 0.6), WHITE if frac > 0.55 else BLACK, "mm")


def draw_header(cv, now, subtitle, icon="server"):
    L, R = CL, CR
    cv.icon((L, 64), icon, 50, BLACK, "lm")
    cv.text((L + 76, 64), HOSTNAME, 58, True, anchor="lm")
    cv.text((L, 132), subtitle, 30, False, DARK, "lm")
    cv.text((R, 64), now.strftime("%H:%M"), 64, True, anchor="rm")
    # date, then the Kindle's own battery
    k = STATE.get("kindle")
    x = R
    if k:
        label = f"{k[0]}%"
        cv.text((x, 132), label, 30, not k[1], BLACK if not k[1] else DARK, "rm")
        x -= cv.width(label, 30, not k[1]) + 10
        cv.icon((x, 132), "bolt" if k[1] else "battery", 26, DARK if k[1] else BLACK, "rm")
        x -= 50
    cv.text((x, 132), now.strftime("%a %-d %b"), 30, False, DARK, "rm")
    cv.rect((L, 168, R, 171), fill=BLACK)


def draw_nav(cv, page):
    """Page buttons in the header's free middle: icons only, the current page filled black."""
    x0 = CL + 76 + cv.width(HOSTNAME, 58, True) + 30
    x1 = CR - cv.width("00:00", 64, True) - 30
    gap = 14
    total = len(TABS) * TAB_W + (len(TABS) - 1) * gap
    x = max(x0, (x0 + x1 - total) / 2)
    y0, y1 = 64 - TAB_H / 2, 64 + TAB_H / 2
    for key, label, icon in TABS:
        box = (x, y0, x + TAB_W, y1)
        if key == page:
            cv.rect(box, fill=BLACK, radius=18)
            cv.icon((x + TAB_W / 2, 64), icon, 44, WHITE, "mm")
        else:
            cv.rect(box, outline=BLACK, width=3, radius=18)
            cv.icon((x + TAB_W / 2, 64), icon, 44, BLACK, "mm")
        cv.zone((x - gap / 2, 0, x + TAB_W + gap / 2, y1 + 20), f"page:{key}")
        x += TAB_W + gap


def draw_toast(cv, toast):
    msg, ok = toast
    working = msg.startswith("working")
    y = CB - 124
    cv.rect((CL, y, CR, y + 100), fill=BLACK if ok else WHITE, outline=BLACK, width=4, radius=22)
    fg = WHITE if ok else BLACK
    cv.icon((CL + 36, y + 50), "hourglass" if working else ("check" if ok else "xmark"), 44, fg, "lm")
    cv.text((CL + 104, y + 50), cv.fit(("" if ok else "Failed: ") + msg, 34, True, CR - CL - 140), 34, True, fg, "lm")


def problems(d):
    out = []
    if d.get("containers") is not None:
        out += [f"{c['name']} is {'stopped' if c['state'] != 'running' else 'unhealthy'}"
                for c in d["containers"] if c["level"] == 2]
    if d.get("gatus"):
        out += [f"check down: {n}" for n in d["gatus"][1]]
    out += [f"{c['name']} is down" for c, ok in d.get("checks", []) if not ok]
    u = d.get("ups") or {}
    if u and "OL" not in u.get("ups.status", ""):
        out.append(f"UPS on battery ({u.get('battery.charge', '?')}%)")
    if d.get("ping") is None:
        out.append("internet unreachable")
    for label, _, v in d.get("storage", []):
        if v == "hung":
            out.append(f"{label} not responding")
        elif v == "error":
            out.append(f"{label} not mounted")
        elif v and 1 - v[1] / v[0] > 0.9:
            out.append(f"{label} over 90% full")
    out += [f"no data: {e.split(':')[0]}" for e in d["errors"]]
    k = STATE.get("kindle")
    if k and not k[1] and k[0] < KINDLE_LOW:      # unplugged is fine (header shows it); only nag when it's running out
        out.append(f"Kindle battery low ({k[0]}%)")
    return out


def summary_items(d):
    """(icon, text, ok) for the line under 'All systems OK'."""
    items = []
    ping = d.get("ping")
    items.append(("globe", f"{ping:.0f} ms" if ping is not None else "offline", ping is not None))
    for c, ok in d.get("checks", []):
        items.append((c.get("icon", "globe"), c["name"] if ok else f"{c['name']} down", ok))
    if d.get("gatus"):
        t, down = d["gatus"]
        items.append(("pulse", f"{t - len(down)}/{t}", not down))
    if d.get("containers") is not None:
        cs = d["containers"]
        items.append(("cubes", str(sum(c["state"] == "running" for c in cs)), not any(c["level"] == 2 for c in cs)))
    return items


def verdict(cv, d, y, cols=1, max_rows=3):
    """'All systems OK' or the problem list, full content width from y. Returns its height."""
    probs = problems(d)
    L, R = CL, CR
    if probs:
        per_col = max_rows
        cols = 1 if len(probs) <= per_col else cols     # a short list gets the full width
        shown = probs[:per_col * cols]
        rows = -(-len(shown) // cols)
        h = 112 + 48 * rows + (40 if len(probs) > len(shown) else 0)
        cv.rect((L, y, R, y + h), fill=BLACK, radius=24)
        cv.icon((L + 34, y + 58), "warn", 54, WHITE, "lm")
        cv.text((L + 114, y + 58), f"{len(probs)} problem{'s' if len(probs) > 1 else ''}", 50, True, WHITE, "lm")
        colw = (R - L - 114 - 34) / cols
        for i, p in enumerate(shown):
            cx = L + 114 + (i // rows) * colw
            cv.text((cx, y + 112 + (i % rows) * 48), "•  " + cv.fit(p, 32, False, colw - 30), 32, False, WHITE)
        if len(probs) > len(shown):
            cv.text((L + 114, y + 112 + rows * 48), f"+ {len(probs) - len(shown)} more", 30, True, LIGHT)
    else:
        h = 150
        cv.rect((L, y, R, y + h), outline=BLACK, width=4, radius=24)
        cv.icon((L + 34, y + 54), "check", 60, BLACK, "lm")
        cv.text((L + 118, y + 54), "All systems OK", 50, True, anchor="lm")
        x = L + 118
        for icon, txt, ok in summary_items(d):
            cv.icon((x, y + 112), icon, 26, BLACK, "lm")
            cv.text((x + 38, y + 112), txt, 30, not ok, DARK if ok else BLACK, "lm")
            x += 38 + cv.width(txt, 30, not ok) + 40
    cv.zone((L, y, R, y + h), "refresh")
    return h


def gauge_specs(d):
    cpu_now = SAMPLER.cpu[-1] if SAMPLER.cpu else 0
    mem = d.get("mem")
    temp = d.get("temp")
    return [("CPU", "microchip", cpu_now / 100, f"{cpu_now:.0f}%",
             f"1 h peak {max(SAMPLER.cpu):.0f}%" if SAMPLER.cpu else "1 h peak –"),
            ("Memory", "memory", mem[1] / mem[0] if mem else 0, f"{mem[1] / mem[0] * 100:.0f}%" if mem else "–",
             f"{mem[1] / 2**30:.1f} of {mem[0] / 2**30:.0f} GB" if mem else ""),
            ("CPU temp", "temp", (temp - 25) / 75 if temp else 0, f"{temp:.0f}°" if temp else "–", "°C, max 100")]


def gauge_tile(cv, box, spec):
    title, icon, frac, value, sub = spec
    x0, y0, x1, y1 = box
    cv.card(box, title, icon)
    cx, cy = (x0 + x1) / 2, y0 + 158
    cv.gauge(cx, cy, 96, frac, 20)
    cv.text((cx, cy + 4), value, 44, True, anchor="mm")
    cv.text((cx, y1 - 26), sub, 26, False, DARK, "mm")


def storage_card(cv, box, rows):
    x0, y0, x1, y1 = box
    cv.card(box, "Storage", "drive")
    pitch = min(78, (y1 - y0 - 84) / max(1, len(rows)))
    yy = y0 + 80
    for label, what, v in rows:
        icon = "drive" if "Server" in label else "database"
        cv.icon((x0 + 28, yy + 22), icon, 34, BLACK, "lm")
        cv.text((x0 + 80, yy + 22), label, 32, True, anchor="lm")
        room = x1 - x0 - 80 - cv.width(label, 32, True) - 14 - 28 - 190
        if room > 60:
            cv.text((x0 + 80 + cv.width(label, 32, True) + 14, yy + 22), cv.fit(what, 26, False, room), 26, False, DARK, "lm")
        if isinstance(v, tuple):
            used = 1 - v[1] / v[0]
            cv.text((x1 - 28, yy + 22), f"{human_size(v[1])} free", 30, used > 0.9, anchor="rm")
            cv.bar(x0 + 80, yy + 48, x1 - x0 - 108 - 90, 16, used)
            cv.text((x1 - 28, yy + 56), f"{used * 100:.0f}%", 28, True, anchor="rm")
        else:
            msg = {"hung": "NOT RESPONDING", "error": "NOT MOUNTED"}.get(v, "checking…")
            cv.text((x1 - 28, yy + 22), msg, 30, v is not None, anchor="rm")
        yy += pitch


def network_card(cv, box):
    x0, y0, x1, y1 = box
    cv.card(box, "Network", "network")
    rx = SAMPLER.rx[-1] if SAMPLER.rx else 0
    tx = SAMPLER.tx[-1] if SAMPLER.tx else 0
    mbps = lambda v: f"{v:.1f}" if v < 10 else f"{v:.0f}"
    cv.icon((x0 + 28, y0 + 100), "down", 30, BLACK, "lm")
    cv.text((x0 + 66, y0 + 100), mbps(rx), 46, True, anchor="lm")
    xx = x0 + 66 + cv.width(mbps(rx), 46, True) + 30
    cv.icon((xx, y0 + 100), "up", 30, BLACK, "lm")
    cv.text((xx + 36, y0 + 100), mbps(tx), 46, True, anchor="lm")
    cv.text((x1 - 26, y0 + 100), "Mb/s", 26, False, DARK, "rm")
    if y1 - y0 > 200:
        cv.spark(x0 + 28, y0 + 150, x1 - x0 - 56, y1 - y0 - 200, SAMPLER.rx, max(list(SAMPLER.rx) + [1]))
        cv.text((x0 + 28, y1 - 30), "download, last hour", 24, False, DARK, "lm")


def ups_card(cv, box, d, compact=False):
    """compact: narrow tile (landscape row of four) with the battery centred."""
    x0, y0, x1, y1 = box
    cv.card(box, "UPS", "plug")
    u = d.get("ups") or {}
    if not u:
        cv.text((x0 + 28, y0 + 110), "no data", 36, True, anchor="lm")
        return
    on_mains = "OL" in u.get("ups.status", "")
    charge = float(u.get("battery.charge", 0))
    charging = "CHRG" in u.get("ups.status", "")
    runtime = f"{int(float(u.get('battery.runtime', 0)) // 60)} min"
    if compact:
        cx = (x0 + x1) / 2
        cv.battery(cx - 66, y0 + 78, 120, 60, charge / 100, charging)
        cv.text((cx, y0 + 172), f"{charge:.0f}%", 44, True, anchor="mm")
        cv.text((cx, y1 - 26), f"{runtime} · " + ("mains" if on_mains else "BATTERY"), 26, not on_mains,
                DARK if on_mains else BLACK, "mm")
        cv.text((cx, y1 - 62), f"load {u.get('ups.load', '?')}%", 26, False, DARK, "mm")
        return
    cv.battery(x0 + 28, y0 + 76, 150, 70, charge / 100, charging)
    cv.text((x0 + 214, y0 + 111), f"{charge:.0f}%", 50, True, anchor="lm")
    cv.text((x0 + 28, y1 - 78), f"{runtime} runtime", 30, False, anchor="lm")
    cv.text((x0 + 28, y1 - 36), "on mains" if on_mains else "ON BATTERY", 30, not on_mains,
            DARK if on_mains else BLACK, "lm")
    cv.text((x1 - 26, y1 - 36), f"load {u.get('ups.load', '?')}%", 30, False, DARK, "rm")


def page_status(cv, d, now):
    draw_header(cv, now, f"up {human_uptime(d['uptime'])}")
    L, R, B = CL, CR, CB - GAP
    y = 194
    if LANDSCAPE:
        y += verdict(cv, d, y, cols=2, max_rows=2) + GAP
        # row of four: three gauges + UPS (three gauges without a UPS)
        n = 4 if NUT else 3
        tw = (R - L - (n - 1) * GAP) / n
        th = 286
        for i, spec in enumerate(gauge_specs(d)):
            x = L + i * (tw + GAP)
            gauge_tile(cv, (x, y, x + tw, y + th), spec)
        if NUT:
            ups_card(cv, (L + 3 * (tw + GAP), y, R, y + th), d, compact=True)
        y += th + GAP
        # storage (wide) + network
        sw = (R - L - GAP) * 0.62
        storage_card(cv, (L, y, L + sw, B), d.get("storage", []))
        network_card(cv, (L + sw + GAP, y, R, B))
        return
    y += verdict(cv, d, y) + GAP
    tw = (R - L - 2 * GAP) / 3
    th = 286
    for i, spec in enumerate(gauge_specs(d)):
        x = L + i * (tw + GAP)
        gauge_tile(cv, (x, y, x + tw, y + th), spec)
    y += th + GAP
    rows = d.get("storage", [])
    sh = 80 + 78 * len(rows)
    storage_card(cv, (L, y, R, y + sh), rows)
    y += sh + GAP
    if not NUT:
        network_card(cv, (L, y, R, B))
        return
    hw = (R - L - GAP) / 2
    network_card(cv, (L, y, L + hw, B))
    ups_card(cv, (L + hw + GAP, y, R, B), d)


ROW_H = 92
LIST_TOP = 194


def page_containers(cv, d, now, state):
    cs = d.get("containers") or []
    cols = 2 if LANDSCAPE else 1
    gap = 40
    colw = (CR - CL - (cols - 1) * gap) / cols
    rows = int((CB - 140 - LIST_TOP) // ROW_H)
    per = rows * cols
    pages = max(1, -(-len(cs) // per))
    p = min(state["cpage"], pages - 1)
    state["cpage"] = p
    bad = sum(c["level"] == 2 for c in cs)
    draw_header(cv, now, f"{len(cs)} containers" + (f" · {bad} need attention" if bad else " · all healthy"), "cubes")
    for i, c in enumerate(cs[p * per:(p + 1) * per]):
        x0 = CL + (i // rows) * (colw + gap)
        x1 = x0 + colw
        y = LIST_TOP + (i % rows) * ROW_H
        if c["level"] == 2:
            cv.rect((x0 - 10, y + 6, x1 + 10, y + ROW_H - 6), fill=BLACK, radius=16)
            fg, sub = WHITE, LIGHT
        else:
            fg, sub = BLACK, DARK
        icon = ("check", "hourglass", "xmark")[c["level"]]
        cv.icon((x0 + 10, y + ROW_H / 2), icon, 36, fg, "lm")
        st = c["status"] if c["state"] == "running" else c["state"]
        st = st.replace(" (healthy)", "").replace("Up ", "up ")
        if LANDSCAPE:
            st = st.replace("up ", "").replace("About a", "1").replace("hours", "h").replace("hour", "h") \
                   .replace("minutes", "min").replace("seconds", "s")
        stw = 150 if LANDSCAPE else 330
        nsize = 34 if LANDSCAPE else 38
        cv.text((x0 + 70, y + ROW_H / 2), cv.fit(c["name"], nsize, True, colw - 70 - stw - 70), nsize, True, fg, "lm")
        cv.text((x1 - 70, y + ROW_H / 2), cv.fit(st, 28, False, stw), 28, False, sub, "rm")
        cv.icon((x1 - 14, y + ROW_H / 2), "restart" if c["state"] == "running" else "play", 30,
                fg if c["level"] == 2 else LIGHT, "rm")
        if c["level"] != 2:
            cv.rect((x0, y + ROW_H - 1, x1, y + ROW_H), fill=PALE)
        cv.zone((x0, y, x1, y + ROW_H), f"ask:{'start' if c['state'] != 'running' else 'restart'}:{c['name']}")
    by = CB - 120
    bw = 250
    if p > 0:
        cv.button((CL, by, CL + bw, by + 96), "Prev", "cpage:-1", "left", size=36)
    if p < pages - 1:
        cv.button((CR - bw, by, CR, by + 96), "Next", "cpage:1", "right", size=36)
    dx = (CL + CR) / 2 - (pages - 1) * 15
    for i in range(pages):
        r = 9 if i == p else 6
        cv.d.ellipse((dx + i * 30 - r, by + 48 - r, dx + i * 30 + r, by + 48 + r),
                     fill=BLACK if i == p else WHITE, outline=BLACK, width=2)


def section(cv, y, title, icon):
    cv.icon((CL, y), icon, 28, DARK, "lm")
    cv.text((CL + 44, y), title, 30, True, DARK, "lm")
    cv.rect((CL + 60 + cv.width(title, 30, True), y, CR, y + 2), fill=LIGHT)


def page_actions(cv, d, now):
    draw_header(cv, now, "every action asks for confirmation", "sliders")
    def grid(y, items, cols=4 if LANDSCAPE else 2):
        bw = (CR - CL - (cols - 1) * GAP) / cols
        bh = 170 if bw < 340 else 140          # narrow buttons stack icon / label / sub-label
        for i, (label, action, icon, sub) in enumerate(items):
            x = CL + (i % cols) * (bw + GAP)
            yy = y + (i // cols) * (bh + GAP)
            cv.button((x, yy, x + bw, yy + bh), label, action, icon, sub, size=32 if bw < 340 else 40)
        return y + -(-len(items) // cols) * (bh + GAP)
    y = 220
    if SHORTCUTS:
        section(cv, y, "Restart a service", "restart")
        y = grid(y + 40, [(n, f"ask:restart:{n}", SERVICE_ICONS.get(n, "cube"), "restart container") for n in SHORTCUTS])
        y += 26
    if ACTIONS:
        section(cv, y, ACTIONS_TITLE, "server")
        y = grid(y + 40, [(a["label"], f"ask:{k}", a["icon"], a["sub"]) for k, a in ACTIONS.items()])
        y += 26
    section(cv, y, "This screen", "kindle")
    nxt = ORIENTATIONS[(ORIENTATIONS.index(ORIENTATION) + 1) % len(ORIENTATIONS)]
    grid(y + 40, [("Clean refresh", "flash", "refresh", "no ghosting"),
                  ("Rotate", "rotate", "rotate", "to " + ORIENTATION_LABELS[nxt].replace("landscape, ", "")),
                  ("Exit", "exit", "exit", "back to Kindle")], cols=4 if LANDSCAPE else 3)


def page_confirm(cv, now, state):
    kind, _, target = state["confirm"][0].partition(":")
    if kind in ("restart", "start"):
        icon = "restart" if kind == "restart" else "play"
        title = f"{kind.capitalize()} {target}?"
        text = ("Restarts the container. Its service is unavailable for a few seconds." if kind == "restart"
                else "The container is stopped. Start it again?")
        label = kind.capitalize()
    else:
        a = ACTIONS[kind]
        icon, title, text, label = a["icon"], a["title"], a["text"], a["button"]
    draw_header(cv, now, "confirm", "warn")
    if LANDSCAPE:
        # explanation on the left, buttons on the right
        lx1 = CL + (CR - CL) * 0.52
        cx = (CL + lx1) / 2
        cv.d.ellipse((cx - 90, 230, cx + 90, 410), fill=BLACK)
        cv.icon((cx, 320), icon, 90, WHITE, "mm")
        y = 450
        for line in cv.wrap(title, 50, lx1 - CL)[:2]:
            cv.text((cx, y), line, 50, True, anchor="mt")
            y += 64
        y += 16
        for line in cv.wrap(text, 32, lx1 - CL - 20):
            cv.text((cx, y), line, 32, False, DARK, "mt")
            y += 46
        bx0 = lx1 + 40
        cv.button((bx0, 220, CR, 560), "Cancel", "cancel", "xmark", size=48)
        cv.button((bx0, 610, CR, 820), label, "do", icon, primary=True, size=48)
        cv.icon(((bx0 + CR) / 2 - 170, 870), "clock", 26, DARK, "lm")
        cv.text(((bx0 + CR) / 2 - 130, 870), f"cancels itself after {CONFIRM_SECONDS} s", 28, False, DARK, "lm")
        return
    cx, cy = W / 2, 330
    cv.d.ellipse((cx - 100, cy - 100, cx + 100, cy + 100), fill=BLACK)
    cv.icon((cx, cy), icon, 100, WHITE, "mm")
    y = 470
    cv.text((W / 2, y), cv.fit(title, 54, True, W - 2 * M), 54, True, anchor="mt")
    y += 90
    for line in cv.wrap(text, 34, W - 2 * M - 60):
        cv.text((W / 2, y), line, 34, False, DARK, "mt")
        y += 50
    # Cancel sits where most buttons that open this screen are; "do" also ignores taps in the first CONFIRM_GUARD s.
    cv.button((M, 720, W - M, 1010), "Cancel", "cancel", "xmark", size=48)
    cv.button((M, 1060, W - M, 1230), label, "do", icon, primary=True, size=50)
    cv.icon((W / 2 - 170, 1275), "clock", 26, DARK, "lm")
    cv.text((W / 2 - 130, 1275), f"cancels itself after {CONFIRM_SECONDS} s", 28, False, DARK, "lm")

# ---------------------------------------------------------------- state machine

LOCK = threading.Lock()
STATE = {"page": "status", "cpage": 0, "confirm": None, "toast": None, "last_tap": 0, "draws": 0,
         "zones": [], "data": None, "data_t": 0, "kindle": None}


def run_async(desc, fn):
    def work():
        try:
            msg, ok = fn(), True
        except Exception as e:
            traceback.print_exc()
            msg, ok = str(e), False
        print(f"{datetime.now():%F %T} action {desc}: {'ok' if ok else 'FAILED'} {msg}", flush=True)
        with LOCK:
            STATE["toast"] = (msg, ok, time.time() + 90)
            STATE["data_t"] = 0
    threading.Thread(target=work, daemon=True).start()


def handle_tap(x, y):
    """Returns an extra action for the Kindle ('exit', 'flash') or None."""
    if TOUCH_SWAP:
        x, y = y, x
    x = x * (LW - 1) / TOUCH_MAX_X               # straight to the logical portrait canvas
    y = y * (LH - 1) / TOUCH_MAX_Y
    if TOUCH_FLIP_X:
        x = LW - 1 - x
    if TOUCH_FLIP_Y:
        y = LH - 1 - y
    if ORIENTATION == "landscape-port-right":      # Kindle turned counter-clockwise
        x, y = y, LW - 1 - x
    elif ORIENTATION == "landscape-port-left":     # Kindle turned clockwise
        x, y = LH - 1 - y, x
    STATE["last_tap"] = time.time()
    hit = next((z[4] for z in STATE["zones"] if z[0] <= x < z[2] and z[1] <= y < z[3]), None)
    print(f"{datetime.now():%F %T} tap {x:.0f},{y:.0f} -> {hit}", flush=True)
    if not hit:
        return None
    if hit.startswith("page:"):
        STATE["page"], STATE["confirm"] = hit[5:], None
        STATE["data_t"] = 0
    elif hit.startswith("cpage:"):
        STATE["cpage"] = max(0, STATE["cpage"] + int(hit[6:]))
    elif hit.startswith("ask:"):
        STATE["confirm"] = (hit[4:], time.time() + CONFIRM_SECONDS)
        STATE["confirm_opened"] = time.time()
    elif hit == "cancel":
        STATE["confirm"] = None
    elif hit == "do" and STATE["confirm"] and time.time() - STATE.get("confirm_opened", 0) >= CONFIRM_GUARD:
        what = STATE["confirm"][0]
        STATE["confirm"] = None
        kind, _, target = what.partition(":")
        if DEMO and kind in ("restart", "start"):
            run_async(what, lambda: (time.sleep(2), f"demo: {kind} {target} (nothing ran)")[1])
        elif kind == "restart":
            run_async(what, lambda: restart(target))
        elif kind == "start":
            run_async(what, lambda: start(target))
        else:
            run_async(what, ACTIONS[kind]["fn"])
        STATE["toast"] = (f"working: {ACTIONS[kind]['button'] if kind in ACTIONS else what.replace(':', ' ')}…", True,
                          time.time() + 600)
    elif hit == "refresh":
        STATE["data_t"] = 0
    elif hit == "flash":
        return "flash"
    elif hit == "rotate":
        set_orientation(ORIENTATIONS[(ORIENTATIONS.index(ORIENTATION) + 1) % len(ORIENTATIONS)], save=True)
        print(f"{datetime.now():%F %T} orientation -> {ORIENTATION}", flush=True)
        return "flash"
    elif hit == "exit":
        STATE["page"] = "status"
        return "exit"
    return None


def render(rotate=True):
    now = datetime.now()
    if STATE["confirm"] and STATE["confirm"][1] < time.time():
        STATE["confirm"] = None
    if STATE["page"] != "status" and not STATE["confirm"] and time.time() - STATE["last_tap"] > IDLE_SECONDS:
        STATE["page"] = "status"
    if STATE["toast"] and STATE["toast"][2] < time.time():
        STATE["toast"] = None
    if time.time() - STATE["data_t"] > 25 or STATE["data"] is None:
        STATE["data"], STATE["data_t"] = collect(), time.time()
    d = STATE["data"]
    cv = Canvas()
    if STATE["confirm"]:
        page_confirm(cv, now, STATE)
    else:
        {"status": lambda: page_status(cv, d, now),
         "containers": lambda: page_containers(cv, d, now, STATE),
         "actions": lambda: page_actions(cv, d, now)}[STATE["page"]]()
        draw_nav(cv, STATE["page"])
    if STATE["toast"]:
        draw_toast(cv, STATE["toast"][:2])
    STATE["zones"] = cv.zones
    img = cv.img
    if not rotate:
        return png_bytes(img)
    if ORIENTATION == "landscape-port-right":
        img = img.transpose(Image.Transpose.ROTATE_270)
    elif ORIENTATION == "landscape-port-left":
        img = img.transpose(Image.Transpose.ROTATE_90)
    if (LW, LH) != (FB_W, FB_H):
        img = img.resize((FB_W, FB_H), Image.Resampling.LANCZOS)
    return png_bytes(img)


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/health":
            return self.reply(200, b"ok", "text/plain")
        if q.get("t") != TOKEN:
            return self.reply(403, b"forbidden", "text/plain")
        try:
            with LOCK:
                action = None
                if u.path == "/k/screen" and q.get("b", "").isdigit():
                    STATE["kindle"] = (int(q["b"]), q.get("c") == "1")
                if u.path == "/k/screen" and "x" in q and "y" in q:
                    action = handle_tap(int(q["x"]), int(q["y"]))
                if u.path == "/preview.png" and q.get("page") in [t[0] for t in TABS]:
                    STATE["page"], STATE["last_tap"] = q["page"], time.time()
                if action == "exit":
                    return self.reply(200, b"", "text/plain", {"X-Action": "exit"})
                png = render(rotate=u.path == "/k/screen")
                headers = {}
                if u.path == "/k/screen":
                    if q.get("v") == hashlib.md5(png).hexdigest() and action != "flash":
                        return self.reply(204, b"", "text/plain")
                    STATE["draws"] += 1
                    if action == "flash" or STATE["draws"] % FLASH_EVERY == 0:
                        headers["X-Action"] = "flash"
            return self.reply(200, png, "image/png", headers)
        except Exception:
            traceback.print_exc()
            return self.reply(500, b"error", "text/plain")

    def reply(self, code, body, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    if not DEMO:
        SAMPLER.start()
        STORAGE_WATCH.start()
    print(f"kindle-dash on :{PORT}{' (demo data)' if DEMO else ''}, screen {FB_W}x{FB_H}, {ORIENTATION}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
