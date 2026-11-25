"""
Harness Camera Station
----------------------
1-3 (or more) cameras above a harness work table. Operator scans the
barcode -> one photo from every camera is taken, placed side by side in a
fixed order, and saved as <barcode>.jpg in the configured folder.

Camera order is NOT based on USB index numbers (those change on reboot).
Each slot (Left / Middle / Right ...) is bound to the camera's hardware ID:
  * Windows: DirectShow device path (contains serial number or USB port)
  * Linux:   /dev/v4l/by-path/... (USB port) link
  * LAN:     the camera's RTSP/HTTP URL (always stable)

Camera reset without rebooting the PC:
  * Reconnect  - release + reopen the camera (no admin needed)
  * USB restart - restart the USB device in Windows (like unplug/replug).
                 Windows asks for an admin login (UAC) each time.

Requirements:  pip install opencv-python pillow cv2-enumerate-cameras
"""

import os

# must be set before cv2 is imported – faster camera opening on Windows
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

import datetime as dt  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import tkinter as tk  # noqa: E402
import uuid  # noqa: E402
import base64  # noqa: E402
import tempfile  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW
STATION = socket.gethostname()   # station ID = computer name
LOG_MAX_LINES = 1000
_LOG_LOCK = threading.Lock()

SLOT_NAMES = ["Left", "Middle", "Right", "Slot 4", "Slot 5", "Slot 6"]

DEFAULT_CONFIG = {
    "save_folder": os.path.join(os.path.expanduser("~"), "HarnessPhotos"),
    "log_folder": os.path.join(os.path.expanduser("~"), "HarnessCamLogs"),   # scan_log.txt goes here
    "date_subfolders": False,        # save into YYYY-MM-DD subfolders
    "save_single_images": False,     # also save <barcode>_cam1.jpg, ...
    "on_duplicate": "suffix",        # "suffix" -> BARCODE_2.jpg, "overwrite", "block"
    "jpeg_quality": 92,
    "barcode_min_length": 3,
    "barcode_regex": "",             # optional, e.g. "^[A-Z0-9-]+$"
    "settings_pin": "",              # optional PIN to protect settings
    "width": 1920,
    "height": 1080,
    "use_mjpg": True,                # needed for several HD USB cams on one PC
    "max_frame_age_s": 2.0,          # refuse to save if a camera frame is older
    "hard_reset_enabled": True,      # allow USB device restart (Windows, asks for admin login)
    "auto_reset_s": 30,              # auto reconnect a camera that is down this long (0 = off)
    "popup_seconds": 1.5,            # how long the success popup stays (0 = no popup)
    "cameras": [
        # {"type": "usb", "id": "<device path>", "label": "...", "rotate": 0, "crop_left": 0, "crop_right": 0}
        # {"type": "ip",  "id": "rtsp://user:pass@192.168.1.50:554/stream1", ...}
        # {"type": "test","id": "test-1", ...}   (simulated camera, no hardware)
    ],
}


# --------------------------------------------------------------------------- config
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Config", f"Could not read config.json:\n{e}\nUsing defaults.")
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------- camera discovery
def list_usb_cameras():
    """Return list of {id, name, index, backend}. 'id' is stable across reboots."""
    found = []
    if IS_WINDOWS:
        try:
            from cv2_enumerate_cameras import enumerate_cameras
            for c in enumerate_cameras(cv2.CAP_DSHOW):
                found.append({"id": c.path or f"{c.name}#{c.index}", "name": c.name,
                              "index": c.index, "backend": cv2.CAP_DSHOW})
        except Exception as e:  # noqa: BLE001
            print("Camera enumeration failed, falling back to indexes:", e)
    elif IS_LINUX:
        # by-path links are tied to the physical USB port
        for link in sorted(glob.glob("/dev/v4l/by-path/*-video-index0")):
            real = os.path.realpath(link)
            name = os.path.basename(link)
            try:
                with open(f"/sys/class/video4linux/{os.path.basename(real)}/name") as f:
                    name = f.read().strip()
            except OSError:
                pass
            found.append({"id": link, "name": name, "index": real, "backend": cv2.CAP_V4L2})
    seen = {}
    for c in found:
        n = seen.get(c["id"], 0)
        seen[c["id"]] = n + 1
        if n:  # identical ID reported twice -> keep them apart (order-based, warn in UI)
            c["id"] = f"{c['id']}|dup{n}"
            c["duplicate"] = True
    if not found and not IS_WINDOWS:
        # generic fallback: probe indexes (order NOT guaranteed stable)
        for i in range(4):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                found.append({"id": f"index:{i}", "name": f"Camera index {i}", "index": i,
                              "backend": cv2.CAP_ANY})
            cap.release()
    return found


# One shared, cached device list: all camera threads use it, so the (slow-ish)
# enumeration runs once at startup instead of once per camera.
_USB = {"t": 0.0, "list": None}
_USB_LOCK = threading.Lock()


def usb_cameras(max_age=10.0):
    with _USB_LOCK:
        if _USB["list"] is None or time.time() - _USB["t"] > max_age:
            _USB["list"] = list_usb_cameras()
            _USB["t"] = time.time()
        return list(_USB["list"])


def invalidate_usb_cache():
    with _USB_LOCK:
        _USB["list"] = None


def short_id(cam_id, n=28):
    r"""Readable, UNIQUE part of a camera ID.
    Windows path \\?\usb#vid_046d&pid_0825&mi_00#7&2a3b4c&0&0000#{guid}\global
    -> 'port/serial 7&2a3b4c&0&0000'  (the end of the path is the same for every camera)"""
    p = cam_id or ""
    if p.startswith("\\\\?\\") and p.count("#") >= 2:
        return "port/serial " + p.split("#")[2]
    if p.startswith("/dev/v4l/by-path/"):
        return "port " + p[len("/dev/v4l/by-path/"):].replace("-video-index0", "")
    return _short_tail(p, n)


def _short_tail(cam_id, n=28):
    return cam_id if len(cam_id) <= n else "…" + cam_id[-n:]


# --------------------------------------------------------------------------- hard reset (Windows)
def to_instance_id(device_path):
    r"""\\?\usb#vid_046d&pid_0825&mi_00#7&2a3b&0&0000#{guid}\global -> USB\VID_046D&PID_0825&MI_00\7&2A3B&0&0000"""
    p = device_path or ""
    if p.startswith("\\\\?\\"):
        p = p[4:]
    parts = p.split("#")
    if len(parts) < 3 or parts[0].lower() != "usb":
        return None
    return "\\".join(parts[:3]).upper()


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# PowerShell run elevated (UAC). Only devices of class Camera/Image are touched.
PS_RESET = r"""
$ids   = @(__IDS__)
$done  = '__DONE__'
$results = @()
foreach ($id in $ids) {
    $dev = Get-PnpDevice -InstanceId $id -ErrorAction SilentlyContinue
    if (-not $dev -or $dev.Class -notin @('Camera', 'Image')) {
        $results += [pscustomobject]@{ id = $id; ok = $false; output = 'not a camera / not present' }
        continue
    }
    $target = $id
    if ($id -match '&MI_[0-9A-F]{2}') {
        try {
            $p = (Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_Parent' -ErrorAction Stop).Data
            if ($p) { $target = $p }
        } catch {}
    }
    $out = (& pnputil /restart-device "$target" 2>&1 | Out-String)
    $ok = ($LASTEXITCODE -eq 0)
    if (-not $ok) {
        try {
            Disable-PnpDevice -InstanceId $target -Confirm:$false -ErrorAction Stop
            Start-Sleep -Milliseconds 1500
            Enable-PnpDevice -InstanceId $target -Confirm:$false -ErrorAction Stop
            $ok = $true
        } catch { $out += ' | ' + $_.Exception.Message }
    }
    $results += [pscustomobject]@{ id = $id; ok = $ok; output = $out.Trim() }
}
& pnputil /scan-devices | Out-Null
ConvertTo-Json -InputObject @{ token = '__TOKEN__'; results = @($results) } -Depth 4 | Set-Content -LiteralPath $done -Encoding UTF8
"""


def build_reset_command(instance_ids, done_path, token):
    q = lambda t: "'" + t.replace("'", "''") + "'"  # noqa: E731  PowerShell single-quoted string
    script = (PS_RESET.replace("__IDS__", ", ".join(q(i) for i in instance_ids))
              .replace("'__DONE__'", q(done_path)).replace("__TOKEN__", token))
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    return script, ["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", enc]


def hard_reset_usb(instance_ids):
    """Restart USB camera devices. Shows the Windows admin prompt (UAC) unless already admin.
    Returns (ok, message)."""
    token = uuid.uuid4().hex
    done = os.path.join(tempfile.gettempdir(), f"harnesscam_reset_{token}.json")
    _script, args = build_reset_command(instance_ids, done, token)
    try:
        if is_admin():
            subprocess.run(["powershell.exe", *args], creationflags=NO_WINDOW, timeout=60)
        else:
            import ctypes
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                                     subprocess.list2cmdline(args), None, 0)
            if rc <= 32:
                return False, "admin login cancelled"
    except Exception as e:  # noqa: BLE001
        return False, f"USB restart failed to start: {e}"

    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            with open(done, "r", encoding="utf-8-sig") as f:
                res = json.load(f)
            try:
                os.remove(done)
            except OSError:
                pass
            results = res.get("results") or []
            if isinstance(results, dict):
                results = [results]
            ok = bool(results) and all(r.get("ok") for r in results)
            return ok, "USB restart done" if ok else "USB restart failed: " + "; ".join(
                str(r.get("output", ""))[:80] for r in results if not r.get("ok"))
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    return False, "USB restart timed out (admin login not given?)"


# --------------------------------------------------------------------------- camera worker
class CameraWorker(threading.Thread):
    """Keeps a camera open and always holds the newest frame (fast, fresh capture)."""

    def __init__(self, slot, cam_cfg, app_cfg):
        super().__init__(daemon=True)
        self.slot = slot
        self.cfg = cam_cfg
        self.app_cfg = app_cfg
        self.lock = threading.Lock()
        self.frame = None
        self.frame_time = 0.0
        self.status = "starting"
        self._halt = threading.Event()
        self._paused = threading.Event()
        self._reopen = threading.Event()
        self.cap = None
        self.first_try = True

    # -- opening
    def _open(self):
        t, cid = self.cfg.get("type", "usb"), self.cfg.get("id", "")
        self.status = "connecting"
        if t == "test":
            return "test"
        if t == "ip":
            cap = cv2.VideoCapture(cid, cv2.CAP_FFMPEG)
        else:
            cams = usb_cameras(max_age=10.0 if self.first_try else 2.0)
            self.first_try = False
            match = next((c for c in cams if c["id"] == cid), None)
            if match is None:
                self.status = "NOT CONNECTED"
                return None
            cap = cv2.VideoCapture(match["index"], match["backend"])
            if self.app_cfg.get("use_mjpg", True):
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.app_cfg.get("width", 1920)))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.app_cfg.get("height", 1080)))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            cap.release()
            self.status = "OPEN FAILED"
            return None
        return cap

    def _release(self):
        if self.cap not in (None, "test"):
            try:
                self.cap.release()
            except Exception:  # noqa: BLE001
                pass
        self.cap = None

    def _test_frame(self):
        w, h = 1280, 720
        img = np.full((h, w, 3), (40 + 60 * self.slot, 90, 140), np.uint8)
        cv2.putText(img, f"TEST CAMERA {self.slot + 1}", (60, 200), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 6)
        cv2.putText(img, dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], (60, 400),
                    cv2.FONT_HERSHEY_SIMPLEX, 2.5, (255, 255, 255), 5)
        time.sleep(0.05)
        return True, img

    def run(self):
        while not self._halt.is_set():
            if self._paused.is_set():
                self._release()
                with self.lock:
                    self.frame = None
                self.status = "RESETTING"
                self._halt.wait(0.2)
                continue
            if self._reopen.is_set():
                self._reopen.clear()
                self._release()
            if self.cap is None:
                self.cap = self._open()
                if self.cap is None:
                    for _ in range(15):          # retry every 3 s, but react fast to pause/reconnect
                        if self._halt.is_set() or self._paused.is_set() or self._reopen.is_set():
                            break
                        self._halt.wait(0.2)
                    continue
            ok, img = self._test_frame() if self.cap == "test" else self.cap.read()
            if not ok or img is None:
                self.status = "NO SIGNAL"
                self._release()
                self._halt.wait(1)
                continue
            with self.lock:
                self.frame, self.frame_time = img, time.time()
            self.status = "OK"
        self._release()

    def latest(self):
        with self.lock:
            return (None if self.frame is None else self.frame.copy()), self.frame_time

    def reconnect(self):
        self._reopen.set()

    def pause(self, timeout=4.0):
        """Release the camera (needed before the USB device is restarted)."""
        self._paused.set()
        end = time.time() + timeout
        while self.cap is not None and time.time() < end:
            time.sleep(0.05)

    def resume(self):
        self.first_try = False
        self._paused.clear()

    def stop(self):
        self._halt.set()


# --------------------------------------------------------------------------- image ops
def prepare(img, cam_cfg):
    rot = int(cam_cfg.get("rotate", 0)) % 360
    if rot == 90:
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif rot == 180:
        img = cv2.rotate(img, cv2.ROTATE_180)
    elif rot == 270:
        img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    w = img.shape[1]
    cl = max(0, int(cam_cfg.get("crop_left", 0)))
    cr = max(0, int(cam_cfg.get("crop_right", 0)))
    if cl + cr < w - 10:
        img = img[:, cl:w - cr]
    return img


# ---- free layout -----------------------------------------------------------
# Every camera has a position in "layout pixels" (= full-resolution pixels):
#   x, y   top-left corner       scale  size factor       z  drawing order (higher = on top)
# The saved photo is drawn with exactly the same function as the screen preview,
# so the file always looks like what the operator sees.
LAYOUT_KEYS = ("x", "y", "scale", "z")


def cam_rect(cam, size):
    sc = float(cam.get("scale", 1) or 1)
    return (int(round(cam.get("x", 0))), int(round(cam.get("y", 0))),
            max(1, int(round(size[0] * sc))), max(1, int(round(size[1] * sc))))


def auto_layout(cams, sizes):
    """Side by side, all the same height (the default layout)."""
    hmin = min(h for _w, h in sizes)
    x = 0
    for i, (c, (w, h)) in enumerate(zip(cams, sizes)):
        sc = hmin / h
        c.update(x=x, y=0, scale=round(sc, 4), z=i)
        x += int(round(w * sc))


def ensure_layout(cams, sizes):
    """Give cameras that have no position yet a default spot. sizes[i] = (w, h) or None.
    Returns True if anything changed."""
    placed = [i for i, c in enumerate(cams) if "x" in c and "y" in c]
    if not placed:
        if any(sz is None for sz in sizes):
            return False                      # wait until every camera delivered a frame
        auto_layout(cams, sizes)
        return True
    changed = False
    for i, c in enumerate(cams):
        if i in placed or sizes[i] is None:
            continue
        rects = [cam_rect(cams[j], sizes[j]) for j in placed if sizes[j] is not None]
        ref_h = rects[0][3] if rects else sizes[i][1]
        c["scale"] = round(ref_h / sizes[i][1], 4)
        c["x"] = max((r[0] + r[2] for r in rects), default=0)   # new camera goes to the right
        c["y"] = min((r[1] for r in rects), default=0)
        c["z"] = max((int(cc.get("z", 0)) for cc in cams), default=0) + 1
        placed.append(i)
        changed = True
    return changed


def compose(frames, cams, f=1.0, origin=None, canvas=None, bg=(0, 0, 0)):
    """Draw the camera images at their layout positions.
    f      = zoom (1.0 = full resolution, used when saving)
    origin = layout point shown at the top-left of the output (default: top-left of all cameras)
    canvas = (W, H) of the output (default: just big enough for all cameras)"""
    sizes = [(fr.shape[1], fr.shape[0]) for fr in frames]
    rects = [cam_rect(c, sz) for c, sz in zip(cams, sizes)]
    if origin is None:
        origin = (min(r[0] for r in rects), min(r[1] for r in rects))
    if canvas is None:
        canvas = (max(1, int(round((max(r[0] + r[2] for r in rects) - origin[0]) * f))),
                  max(1, int(round((max(r[1] + r[3] for r in rects) - origin[1]) * f))))
    W, H = canvas
    out = np.empty((H, W, 3), np.uint8)
    out[:] = bg
    for i in sorted(range(len(frames)), key=lambda k: cams[k].get("z", k)):
        x, y, w, h = rects[i]
        X = int(round((x - origin[0]) * f))
        Y = int(round((y - origin[1]) * f))
        wd, hd = max(1, int(round(w * f))), max(1, int(round(h * f)))
        img = frames[i]
        if (wd, hd) != sizes[i]:
            img = cv2.resize(img, (wd, hd), interpolation=cv2.INTER_AREA if wd < sizes[i][0] else cv2.INTER_LINEAR)
        x0, y0, x1, y1 = max(X, 0), max(Y, 0), min(X + wd, W), min(Y + hd, H)
        if x1 > x0 and y1 > y0:
            out[y0:y1, x0:x1] = img[y0 - Y:y1 - Y, x0 - X:x1 - X]
    return out


def write_jpg(path, img, quality):
    # imencode + tofile works with non-ASCII paths on Windows (cv2.imwrite does not)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise IOError("JPEG encoding failed")
    tmp = path + ".part"
    buf.tofile(tmp)
    os.replace(tmp, path)


def safe_name(text):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")


# --------------------------------------------------------------------------- main app
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.workers = []
        self.badges = []
        self.busy = False
        self.resetting = False
        self.bad_since = {}
        self.last_auto_reset = {}
        self._popup = None
        self.editing = False          # layout edit mode
        self.drag = None
        self.view = None              # (zoom, origin, rects) of the current preview
        self.last_sizes = {}          # slot -> (w, h) of the prepared full-res image
        self.layout_backup = None

        root.title("Harness Camera Station")
        root.geometry("1280x800")
        root.configure(bg="#1e1e1e")
        self._build_ui()
        # show the window first, open cameras right after -> window appears instantly
        self.root.after(10, self.start_workers)
        self.root.after(200, self.refresh_preview)
        self.root.after(300, lambda: self.entry.focus_force())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if not self.cfg["cameras"]:
            self.root.after(800, self.open_settings)

    # ---------------- UI
    def _build_ui(self):
        top = tk.Frame(self.root, bg="#1e1e1e")
        top.pack(fill="x", padx=10, pady=(10, 0))
        self.title_lbl = tk.Label(top, text=f"Station: {STATION}", fg="white", bg="#1e1e1e",
                                  font=("Segoe UI", 18, "bold"))
        self.title_lbl.pack(side="left")
        tk.Button(top, text="⚙ Settings", command=self.open_settings, font=("Segoe UI", 11)).pack(side="right")
        tk.Button(top, text="📁 Open folder", command=self.open_folder, font=("Segoe UI", 11)).pack(side="right", padx=6)
        tk.Button(top, text="✥ Layout", command=self.start_layout_edit, font=("Segoe UI", 11)).pack(side="right", padx=(0, 6))
        tk.Button(top, text="↻ Restart cameras", command=lambda: self.reset_cameras(None, interactive=True),
                  font=("Segoe UI", 11)).pack(side="right", padx=(0, 6))
        self.cam_status = tk.Frame(top, bg="#1e1e1e")
        self.cam_status.pack(side="right", padx=20)

        # Bottom area is packed FIRST, so it always keeps its space – the camera
        # preview only gets what is left (it can never cover the scan field).
        self.status = tk.Label(self.root, text="Ready", fg="white", bg="#444", font=("Segoe UI", 16, "bold"),
                               padx=12, pady=6, anchor="center")
        self.status.pack(side="bottom", fill="x", padx=10, pady=(0, 10))

        bottom = tk.Frame(self.root, bg="#1e1e1e")
        bottom.pack(side="bottom", fill="x", padx=10, pady=(0, 8))
        center = tk.Frame(bottom, bg="#1e1e1e")
        center.pack(anchor="center")          # scan field centred horizontally
        tk.Label(center, text="Scan barcode:", fg="white", bg="#1e1e1e", font=("Segoe UI", 16)).pack(side="left")
        self.entry = tk.Entry(center, font=("Consolas", 22), width=30, justify="center")
        self.entry.pack(side="left", padx=10)
        self.entry.bind("<Return>", self.on_scan)
        self.entry.bind("<KP_Enter>", self.on_scan)

        # preview box with fixed size from the layout (image is scaled to fit inside it)
        self.preview_box = tk.Frame(self.root, bg="black")
        self.preview_box.pack(side="top", fill="both", expand=True, padx=10, pady=10)
        self.preview_box.pack_propagate(False)
        self.preview = tk.Canvas(self.preview_box, bg="black", highlightthickness=0)
        self.preview.pack(fill="both", expand=True)
        self.preview_img = self.preview.create_image(0, 0, anchor="nw")
        self.preview.bind("<ButtonPress-1>", self._drag_start)
        self.preview.bind("<B1-Motion>", self._drag_move)
        self.preview.bind("<ButtonRelease-1>", self._drag_end)
        self.preview.bind("<MouseWheel>", lambda e: self._wheel(e, 1 if e.delta > 0 else -1))   # Windows
        self.preview.bind("<Button-4>", lambda e: self._wheel(e, 1))                             # Linux
        self.preview.bind("<Button-5>", lambda e: self._wheel(e, -1))

        # layout edit toolbar (only visible while editing)
        self.layout_bar = tk.Frame(self.root, bg="#f9a825")
        tk.Button(self.layout_bar, text="↶ Cancel", command=self.cancel_layout_edit,
                  font=("Segoe UI", 11)).pack(side="right", padx=4, pady=4)
        tk.Button(self.layout_bar, text="⇔ Side by side", command=self.layout_side_by_side,
                  font=("Segoe UI", 11)).pack(side="right", padx=4, pady=4)
        tk.Button(self.layout_bar, text="💾 Save layout", command=self.save_layout_edit,
                  font=("Segoe UI", 11, "bold")).pack(side="right", padx=4, pady=4)
        # text last, so the buttons always keep their space on small screens
        tk.Label(self.layout_bar, text="LAYOUT EDIT:  drag = move   •   mouse wheel = size   •   edges snap",
                 bg="#f9a825", fg="black", font=("Segoe UI", 12, "bold"), anchor="w").pack(
            side="left", fill="x", expand=True, padx=10, pady=6)

        # F11 = full screen on/off, Esc = leave full screen
        self.root.bind("<F11>", lambda e: self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen")))
        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))

        # keep focus in barcode field (operators click around)
        self.root.bind("<FocusIn>", lambda e: self.entry.focus_set() if e.widget == self.root else None)

    def set_status(self, text, color):
        self.status.config(text=text, bg=color)

    def render_cam_badges(self):
        for w in self.cam_status.winfo_children():
            w.destroy()
        self.badges = []
        for i, _c in enumerate(self.cfg["cameras"]):
            b = tk.Label(self.cam_status, text=f"{SLOT_NAMES[i]}: …", font=("Segoe UI", 10, "bold"),
                         fg="white", bg="#666", padx=6, pady=2, cursor="hand2")
            b.pack(side="left", padx=3)
            b.bind("<Button-1>", lambda e, i=i: self.reset_cameras([i], interactive=True))
            self.badges.append(b)

    # ---------------- workers
    def start_workers(self):
        self.stop_workers()
        self.bad_since = {}
        for i, c in enumerate(self.cfg["cameras"]):
            w = CameraWorker(i, c, self.cfg)
            w.start()
            self.workers.append(w)
        self.render_cam_badges()

    def stop_workers(self):
        for w in self.workers:
            w.stop()
        for w in self.workers:
            w.join(timeout=3)
        self.workers = []

    def refresh_preview(self):
        try:
            imgs, now = [], time.time()
            cams = self.cfg["cameras"]
            for i, w in enumerate(self.workers):
                frame, t = w.latest()
                fresh = frame is not None and now - t < self.cfg["max_frame_age_s"]
                if fresh:
                    self.bad_since.pop(i, None)
                else:
                    self.bad_since.setdefault(i, now)
                if i < len(self.badges):
                    txt = "OK" if fresh else f"{w.status} – click to reset"
                    self.badges[i].config(text=f"{SLOT_NAMES[i]}: {txt}", bg="#2e7d32" if fresh else "#c62828")
                if frame is not None:
                    frame = prepare(frame, w.cfg)
                    self.last_sizes[i] = (frame.shape[1], frame.shape[0])
                else:  # placeholder with the camera's last known size
                    pw_, ph_ = self.last_sizes.get(i, (1280, 720))
                    frame = np.full((ph_, pw_, 3), 40, np.uint8)
                    cv2.putText(frame, f"{SLOT_NAMES[i]}: {w.status}", (40, ph_ // 2),
                                cv2.FONT_HERSHEY_SIMPLEX, max(1.0, pw_ / 700), (0, 0, 255), max(2, pw_ // 300))
                imgs.append(frame)
            if imgs:
                sizes = [self.last_sizes.get(i) for i in range(len(imgs))]
                if ensure_layout(cams, sizes):
                    save_config(self.cfg)      # remember the default layout, so it never changes by itself
                if all("x" in c for c in cams[:len(imgs)]):
                    self._draw_preview(imgs, cams[:len(imgs)])
            self._auto_reset(now)
        except Exception as e:  # noqa: BLE001
            print("preview error:", e)
        self.root.after(150, self.refresh_preview)

    def _draw_preview(self, imgs, cams):
        pw, ph = max(self.preview.winfo_width(), 100), max(self.preview.winfo_height(), 100)
        rects = [cam_rect(c, (im.shape[1], im.shape[0])) for c, im in zip(cams, imgs)]
        if self.drag is not None:
            f, origin = self.drag["view"]          # keep the view still while dragging
        else:
            minx, miny = min(r[0] for r in rects), min(r[1] for r in rects)
            bw = max(r[0] + r[2] for r in rects) - minx
            bh = max(r[1] + r[3] for r in rects) - miny
            pad = 0.92 if self.editing else 1.0    # a little free space around while editing
            f = min(pw / bw, ph / bh) * pad
            origin = (minx - (pw / f - bw) / 2, miny - (ph / f - bh) / 2)   # centred
        self.view = (f, origin, rects)
        img = compose(imgs, cams, f=f, origin=origin, canvas=(pw, ph))
        photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
        self.preview.itemconfig(self.preview_img, image=photo)
        self.preview.image = photo
        self.preview.delete("overlay")
        if self.editing:
            for i, (x, y, w, h) in enumerate(rects):
                sx, sy = (x - origin[0]) * f, (y - origin[1]) * f
                active = self.drag is not None and self.drag["i"] == i
                self.preview.create_rectangle(sx, sy, sx + w * f, sy + h * f, outline="#ffeb3b" if active else "#f9a825",
                                              width=4 if active else 2, tags="overlay")
                self.preview.create_text(sx + 10, sy + 10, anchor="nw", text=f"{i + 1}. {SLOT_NAMES[i]}",
                                         fill="#ffeb3b", font=("Segoe UI", 16, "bold"), tags="overlay")

    # ---------------- layout editing
    def start_layout_edit(self):
        if self.editing or not self.cfg["cameras"]:
            return
        pin = self.cfg.get("settings_pin", "")
        if pin:
            from tkinter import simpledialog
            if simpledialog.askstring("Layout", "PIN:", show="*", parent=self.root) != pin:
                return
        self.layout_backup = [{k: c[k] for k in LAYOUT_KEYS if k in c} for c in self.cfg["cameras"]]
        self.editing = True
        self.layout_bar.pack(side="top", fill="x", padx=10, pady=(8, 0), before=self.preview_box)
        self.preview.config(cursor="fleur")
        self.set_status("Layout editing – scanning is paused until you Save or Cancel", "#f9a825")

    def _end_layout_edit(self):
        self.editing = False
        self.drag = None
        self.layout_bar.pack_forget()
        self.preview.config(cursor="")
        self.preview.delete("overlay")
        self.entry.focus_set()

    def save_layout_edit(self):
        cams = self.cfg["cameras"]
        if self.view:   # move everything so the top-left camera starts at 0,0 (tidy numbers)
            rects = self.view[2]
            minx, miny = min(r[0] for r in rects), min(r[1] for r in rects)
            for c in cams:
                c["x"] = int(round(c.get("x", 0) - minx))
                c["y"] = int(round(c.get("y", 0) - miny))
        zs = sorted(range(len(cams)), key=lambda i: cams[i].get("z", i))
        for rank, i in enumerate(zs):
            cams[i]["z"] = rank
        save_config(self.cfg)
        self._end_layout_edit()
        self.ok("✔ Layout saved – photos are saved exactly like this from now on")

    def cancel_layout_edit(self):
        for c, b in zip(self.cfg["cameras"], self.layout_backup or []):
            for k in LAYOUT_KEYS:
                c.pop(k, None)
            c.update(b)
        self._end_layout_edit()
        self.set_status("Layout not changed", "#444")

    def layout_side_by_side(self):
        sizes = [self.last_sizes.get(i) for i in range(len(self.cfg["cameras"]))]
        if any(sz is None for sz in sizes):
            return self.fail("All cameras must be connected for 'Side by side'")
        auto_layout(self.cfg["cameras"], sizes)

    def _hit(self, ex, ey):
        if not self.view:
            return None
        f, (ox, oy), rects = self.view
        lx, ly = ox + ex / f, oy + ey / f
        cams = self.cfg["cameras"]
        for i in sorted(range(len(rects)), key=lambda k: cams[k].get("z", k), reverse=True):
            x, y, w, h = rects[i]
            if x <= lx < x + w and y <= ly < y + h:
                return i
        return None

    def _drag_start(self, e):
        if not self.editing:
            return
        i = self._hit(e.x, e.y)
        if i is None:
            return
        cams = self.cfg["cameras"]
        cams[i]["z"] = max(int(c.get("z", 0)) for c in cams) + 1      # bring to front
        f, origin, _r = self.view
        self.drag = {"i": i, "sx": e.x, "sy": e.y, "x0": cams[i].get("x", 0), "y0": cams[i].get("y", 0),
                     "view": (f, origin)}

    def _drag_move(self, e):
        if not self.drag or not self.view:
            return
        d = self.drag
        f = d["view"][0]
        i = d["i"]
        rects = self.view[2]
        w, h = rects[i][2], rects[i][3]
        nx = d["x0"] + (e.x - d["sx"]) / f
        ny = d["y0"] + (e.y - d["sy"]) / f
        # snap edges to the other cameras' edges (within 12 screen pixels)
        th = 12 / f
        xs = [v for j, r in enumerate(rects) if j != i for v in (r[0], r[0] + r[2])]
        ys = [v for j, r in enumerate(rects) if j != i for v in (r[1], r[1] + r[3])]
        nx = self._snap(nx, w, xs, th)
        ny = self._snap(ny, h, ys, th)
        cam = self.cfg["cameras"][i]
        cam["x"], cam["y"] = int(round(nx)), int(round(ny))

    @staticmethod
    def _snap(pos, size, targets, th):
        best, best_d = pos, th
        for t in targets:
            for cand in (t, t - size):          # my left/top edge on t, or my right/bottom edge on t
                dd = abs(pos - cand)
                if dd < best_d:
                    best, best_d = cand, dd
        return best

    def _drag_end(self, _e):
        self.drag = None

    def _wheel(self, e, direction):
        if not self.editing or self.drag:
            return
        i = self._hit(e.x, e.y)
        if i is None:
            return
        cam = self.cfg["cameras"][i]
        size = self.last_sizes.get(i)
        x, y, w, h = cam_rect(cam, size) if size else self.view[2][i]   # current size, not the last drawn one
        k = 1.05 if direction > 0 else 1 / 1.05
        new_scale = min(4.0, max(0.1, float(cam.get("scale", 1)) * k))
        k = new_scale / float(cam.get("scale", 1))
        cx, cy = x + w / 2, y + h / 2                # keep the camera centred under the mouse
        cam["scale"] = round(new_scale, 4)
        cam["x"], cam["y"] = int(round(cx - w * k / 2)), int(round(cy - h * k / 2))

    def _auto_reset(self, now):
        limit = float(self.cfg.get("auto_reset_s", 0) or 0)
        if limit <= 0 or self.resetting:
            return
        due = [i for i, t in self.bad_since.items()
               if now - t > limit and now - self.last_auto_reset.get(i, 0) > 180]  # max once / 3 min
        if due:
            for i in due:
                self.last_auto_reset[i] = now
            self.reset_cameras(due, interactive=False)

    # ---------------- camera reset
    def reset_cameras(self, slots=None, interactive=True):
        """slots=None -> all cameras. Step 1 reconnect, step 2 USB restart if still down."""
        if self.resetting or not self.workers:
            return
        self.resetting = True
        targets = list(range(len(self.workers))) if slots is None else slots
        names = ", ".join(SLOT_NAMES[i] for i in targets)
        self.set_status(f"Restarting camera(s): {names} …", "#f9a825")
        threading.Thread(target=self._reset_worker, args=(targets, interactive), daemon=True).start()

    def _reset_worker(self, targets, interactive):
        msg = ""
        try:
            workers = [self.workers[i] for i in targets]
            t_start = time.time()
            # step 1: soft reconnect
            invalidate_usb_cache()
            for w in workers:
                w.reconnect()
            if self._wait_ok(workers, 6, t_start):
                msg = "reconnected"
                return
            # step 2: hard USB restart for USB cameras that are still down (Windows)
            down = [w for w in workers if not self._is_ok(w, t_start)]
            usb_down = [w for w in down if w.cfg.get("type") == "usb"]
            ids = [i for i in (to_instance_id(w.cfg.get("id")) for w in usb_down) if i]
            if IS_WINDOWS and ids and self.cfg.get("hard_reset_enabled", True) and (interactive or is_admin()):
                self.root.after(0, self.set_status, "Restarting USB device(s) – admin login required …", "#f9a825")
                for w in usb_down:
                    w.pause()
                ok, msg = hard_reset_usb(ids)
                time.sleep(2)  # give Windows time to re-create the device
                invalidate_usb_cache()
                t_hard = time.time()
                for w in usb_down:
                    w.resume()
                if self._wait_ok(usb_down, 10, t_hard):
                    msg = "USB restart OK"
                    return
            still = [SLOT_NAMES[w.slot] for w in workers if not self._is_ok(w)]
            msg = (f"still not working: {', '.join(still)}" + (f" ({msg})" if msg else "") +
                   " – check cable / USB port")
        finally:
            self.resetting = False
            good = msg in ("reconnected", "USB restart OK")
            names = ", ".join(SLOT_NAMES[i] for i in targets)
            self._log("CAMERA RESET", "", f"{names}: {msg}" + ("" if interactive else " (auto)"))
            self.root.after(0, (self.ok if good else self.fail),
                            f"{'✔' if good else '✖'} Camera reset: {msg}")

    def _is_ok(self, w, since=0.0):
        _f, t = w.latest()
        return t > since and time.time() - t < self.cfg["max_frame_age_s"]

    def _wait_ok(self, workers, timeout, since=0.0):
        end = time.time() + timeout
        while time.time() < end:
            if all(self._is_ok(w, since) for w in workers):
                return True
            time.sleep(0.25)
        return False

    # ---------------- capture
    def on_scan(self, _event=None):
        code = self.entry.get().strip()
        self.entry.delete(0, "end")
        if self.busy or not code:
            return
        if self.resetting:
            return self.fail("Camera reset in progress – scan again in a few seconds")
        if self.editing:
            return self.fail("Layout editing is open – press Save layout or Cancel first. Photo NOT saved.")
        if len(code) < int(self.cfg.get("barcode_min_length", 0)):
            return self.fail(f"Barcode too short: {code}")
        rx = self.cfg.get("barcode_regex", "")
        if rx and not re.fullmatch(rx, code):
            return self.fail(f"Invalid barcode format: {code}")
        if not self.workers:
            return self.fail("No cameras configured – open Settings")

        frames, now = [], time.time()
        for i, w in enumerate(self.workers):
            f, t = w.latest()
            if f is None or now - t > self.cfg["max_frame_age_s"]:
                return self.fail(f"{SLOT_NAMES[i]} camera not ready ({w.status}) – photo NOT saved. "
                                 f"Click the red badge to reset it.")
            frames.append(prepare(f, w.cfg))

        cams = [dict(c) for c in self.cfg["cameras"][:len(frames)]]
        if ensure_layout(cams, [(f.shape[1], f.shape[0]) for f in frames]):
            pass  # first scan before the preview set a layout – default side by side
        self.busy = True
        self.set_status(f"Saving {code} …", "#f9a825")
        threading.Thread(target=self._save, args=(code, frames, cams), daemon=True).start()

    def _save(self, code, frames, cams):
        try:
            folder = self.cfg["save_folder"]
            if self.cfg.get("date_subfolders"):
                folder = os.path.join(folder, dt.date.today().isoformat())
            os.makedirs(folder, exist_ok=True)
            base = safe_name(code)
            path = os.path.join(folder, base + ".jpg")
            mode = self.cfg.get("on_duplicate", "suffix")
            if os.path.exists(path):
                if mode == "block":
                    raise FileExistsError(f"{base}.jpg already exists – NOT saved")
                if mode == "suffix":
                    n = 2
                    while os.path.exists(os.path.join(folder, f"{base}_{n}.jpg")):
                        n += 1
                    base = f"{base}_{n}"
                    path = os.path.join(folder, base + ".jpg")
            q = self.cfg.get("jpeg_quality", 92)
            write_jpg(path, compose(frames, cams), q)      # same drawing as the screen preview
            if self.cfg.get("save_single_images") and len(frames) > 1:
                for i, f in enumerate(frames):
                    write_jpg(os.path.join(folder, f"{base}_cam{i + 1}.jpg"), f, q)
            self._log(code, path, "OK")
            self.root.after(0, self.ok, f"✔ Saved {os.path.basename(path)}")
            self.root.after(0, self.popup, code, os.path.basename(path))
        except Exception as e:  # noqa: BLE001
            self._log(code, "", f"ERROR: {e}")
            self.root.after(0, self.fail, f"✖ {e}")
        finally:
            self.busy = False

    def _log(self, code, path, result):
        """scan_log.txt: one line per scan, newest at the bottom, max 1000 lines (oldest removed)."""
        line = (f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} | {STATION} | {code} | {result}"
                + (f" | {path}" if path else "") + "\n")
        try:
            folder = self.cfg.get("log_folder") or self.cfg["save_folder"]
            os.makedirs(folder, exist_ok=True)
            log = os.path.join(folder, "scan_log.txt")
            with _LOG_LOCK:
                lines = []
                if os.path.exists(log):
                    with open(log, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                lines.append(line)
                if len(lines) > LOG_MAX_LINES:
                    lines = lines[-LOG_MAX_LINES:]
                tmp = log + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                os.replace(tmp, log)
        except Exception as e:  # noqa: BLE001
            print("log error:", e)

    def popup(self, code, filename):
        """Big green 'SCAN OK' popup. Closes by itself and never takes focus,
        so the operator can scan the next harness immediately."""
        secs = float(self.cfg.get("popup_seconds", 1.5) or 0)
        if secs <= 0:
            return
        if getattr(self, "_popup", None) is not None:
            try:
                self._popup.destroy()
            except tk.TclError:
                pass
        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)          # no title bar
        pop.attributes("-topmost", True)
        pop.configure(bg="#2e7d32")
        tk.Label(pop, text="✔  SCAN OK", fg="white", bg="#2e7d32", font=("Segoe UI", 44, "bold")).pack(padx=60, pady=(30, 5))
        tk.Label(pop, text=code, fg="white", bg="#2e7d32", font=("Consolas", 26)).pack(padx=60)
        tk.Label(pop, text=f"saved as {filename}", fg="#c8e6c9", bg="#2e7d32", font=("Segoe UI", 14)).pack(padx=60, pady=(5, 30))
        pop.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - pop.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - pop.winfo_height()) // 2
        pop.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self._popup = pop
        # give keyboard focus straight back to the barcode field (scanner = keyboard)
        pop.bind("<FocusIn>", lambda e: self.root.after(1, self._refocus))
        pop.after(30, self._refocus)

        def close():
            try:
                pop.destroy()
            except tk.TclError:
                pass
            if self._popup is pop:
                self._popup = None
        pop.after(int(secs * 1000), close)
        pop.bind("<Button-1>", lambda e: close())

    def _refocus(self):
        try:
            self.root.focus_force()
            self.entry.focus_force()
        except tk.TclError:
            pass

    def ok(self, msg):
        self.set_status(msg, "#2e7d32")
        self.root.bell()
        self.entry.focus_set()

    def fail(self, msg):
        self.set_status(msg, "#c62828")
        for _ in range(3):
            self.root.bell()
        self.entry.focus_set()

    # ---------------- misc
    def open_folder(self):
        p = self.cfg["save_folder"]
        os.makedirs(p, exist_ok=True)
        if IS_WINDOWS:
            os.startfile(p)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", p])

    def open_settings(self):
        pin = self.cfg.get("settings_pin", "")
        if pin:
            from tkinter import simpledialog
            if simpledialog.askstring("Settings", "PIN:", show="*", parent=self.root) != pin:
                return
        SettingsWindow(self)

    def apply_config(self, cfg):
        self.cfg = cfg
        save_config(cfg)
        self.start_workers()
        self.set_status("Settings saved – ready", "#444")
        self.entry.focus_set()

    def on_close(self):
        self.stop_workers()
        self.root.destroy()


# --------------------------------------------------------------------------- settings
class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.cfg = json.loads(json.dumps(app.cfg))
        self.win = tk.Toplevel(app.root)
        self.win.title("Settings")
        self.win.geometry("1000x720")
        self.win.transient(app.root)
        self.win.grab_set()
        self.detected = []
        self.rows = []

        nb = ttk.Notebook(self.win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.cam_tab = ttk.Frame(nb)
        gen = ttk.Frame(nb)
        nb.add(self.cam_tab, text="Cameras")
        nb.add(gen, text="General")
        self._build_general(gen)
        self._build_cameras()

        bar = ttk.Frame(self.win)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="Save & apply", command=self.save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.win.destroy).pack(side="right", padx=6)

    # ---- general tab
    def _build_general(self, f):
        self.vars = {}

        def row(r, label, key, kind="entry", values=None):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=5)
            val = self.cfg.get(key)
            if kind == "check":
                v = tk.BooleanVar(value=bool(val))
                ttk.Checkbutton(f, variable=v).grid(row=r, column=1, sticky="w")
            elif kind == "combo":
                v = tk.StringVar(value=str(val))
                ttk.Combobox(f, textvariable=v, values=values, state="readonly", width=20).grid(row=r, column=1, sticky="w")
            else:
                v = tk.StringVar(value=str(val))
                ttk.Entry(f, textvariable=v, width=60).grid(row=r, column=1, sticky="w")
            self.vars[key] = v

        ttk.Label(f, text=f"Station ID (computer name): {STATION}").grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=5)
        row(1, "Photo folder (local or \\\\server\\share)", "save_folder")
        ttk.Button(f, text="Browse…", command=lambda: self._browse("save_folder")).grid(row=1, column=2, padx=4)
        row(2, "Log folder (scan_log.txt)", "log_folder")
        ttk.Button(f, text="Browse…", command=lambda: self._browse("log_folder")).grid(row=2, column=2, padx=4)
        row(3, "Date subfolders for photos (YYYY-MM-DD)", "date_subfolders", "check")
        row(4, "Also save each camera separately", "save_single_images", "check")
        row(5, "If barcode already exists", "on_duplicate", "combo", ["suffix", "overwrite", "block"])
        row(6, "JPEG quality (50-100)", "jpeg_quality")
        row(7, "Barcode minimum length", "barcode_min_length")
        row(8, "Barcode regex (optional)", "barcode_regex")
        row(9, "USB capture width", "width")
        row(10, "USB capture height", "height")
        row(11, "Use MJPG (recommended for multiple USB cams)", "use_mjpg", "check")
        row(12, "Success popup seconds (0 = off)", "popup_seconds")
        row(13, "Settings PIN (empty = none)", "settings_pin")

        ttk.Separator(f).grid(row=14, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(f, text="Camera reset (without PC reboot)", font=("Segoe UI", 10, "bold")).grid(
            row=15, column=0, sticky="w", padx=8)
        row(16, "Auto-reconnect a camera that is down for N seconds (0 = off)", "auto_reset_s")
        row(17, "Allow USB restart on manual reset (asks for admin login)", "hard_reset_enabled", "check")

    def _browse(self, key):
        d = filedialog.askdirectory(parent=self.win, initialdir=self.vars[key].get())
        if d:
            self.vars[key].set(os.path.normpath(d))

    # ---- cameras tab
    def _build_cameras(self):
        for w in self.cam_tab.winfo_children():
            w.destroy()
        top = ttk.Frame(self.cam_tab)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Slots are saved left → right. Each slot is locked to the camera's hardware ID, "
                            "so order survives restarts. Use ▲ ▼ to swap positions.", wraplength=900).pack(anchor="w")
        btns = ttk.Frame(self.cam_tab)
        btns.pack(fill="x", padx=8)
        ttk.Button(btns, text="🔍 Detect USB cameras", command=self.detect).pack(side="left")
        ttk.Button(btns, text="👁 Identify (show detected cameras)", command=self.identify).pack(side="left", padx=6)
        ttk.Button(btns, text="+ Add slot", command=self.add_slot).pack(side="left", padx=6)
        self.det_lbl = ttk.Label(btns, text="")
        self.det_lbl.pack(side="left", padx=10)

        self.slot_frame = ttk.Frame(self.cam_tab)
        self.slot_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.draw_slots()

    def detect(self):
        self.app.stop_workers()  # release cameras so they can be probed (restarted on save/cancel)
        self.win.bind("<Destroy>", lambda e: self.app.start_workers()
                      if e.widget == self.win and not self.app.workers else None)
        invalidate_usb_cache()
        self.detected = usb_cameras(0)
        self.det_lbl.config(text=f"{len(self.detected)} USB camera(s) found")
        self.draw_slots()

    def cam_label(self, c):
        # "#1", "#2"... = detection number, same as in the Identify window -> always unique
        k = next((i for i, d in enumerate(self.detected) if d["id"] == c["id"]), None)
        num = f"#{k + 1}  " if k is not None else ""
        return f"{num}{c['name']}  [{short_id(c['id'])}]"

    def draw_slots(self):
        for w in self.slot_frame.winfo_children():
            w.destroy()
        self.rows = []
        hdr = ["Slot", "Type", "Camera / URL", "Rotate", "Crop L px", "Crop R px", ""]
        for j, h in enumerate(hdr):
            ttk.Label(self.slot_frame, text=h, font=("Segoe UI", 9, "bold")).grid(row=0, column=j, sticky="w", padx=4)
        options = [self.cam_label(c) for c in self.detected]
        for i, cam in enumerate(self.cfg["cameras"]):
            r = i + 1
            ttk.Label(self.slot_frame, text=f"{i + 1}. {SLOT_NAMES[i]}").grid(row=r, column=0, sticky="w", padx=4, pady=4)
            tv = tk.StringVar(value=cam.get("type", "usb"))
            ttk.Combobox(self.slot_frame, textvariable=tv, values=["usb", "ip", "test"], state="readonly",
                         width=6).grid(row=r, column=1, padx=4)
            # camera selector: show stored camera even if not currently detected
            stored = next((self.cam_label(c) for c in self.detected if c["id"] == cam.get("id")), None)
            if stored is None and cam.get("id"):
                stored = (cam.get("label") or "saved camera") + f"  [{short_id(cam['id'])}]"
            idv = tk.StringVar(value=cam.get("id", "") if cam.get("type") in ("ip", "test") else (stored or ""))
            cb = ttk.Combobox(self.slot_frame, textvariable=idv, values=options, width=60)
            cb.grid(row=r, column=2, padx=4, sticky="w")
            rot = tk.StringVar(value=str(cam.get("rotate", 0)))
            ttk.Combobox(self.slot_frame, textvariable=rot, values=["0", "90", "180", "270"], width=5,
                         state="readonly").grid(row=r, column=3, padx=4)
            cl = tk.StringVar(value=str(cam.get("crop_left", 0)))
            cr = tk.StringVar(value=str(cam.get("crop_right", 0)))
            ttk.Entry(self.slot_frame, textvariable=cl, width=7).grid(row=r, column=4, padx=4)
            ttk.Entry(self.slot_frame, textvariable=cr, width=7).grid(row=r, column=5, padx=4)
            act = ttk.Frame(self.slot_frame)
            act.grid(row=r, column=6)
            ttk.Button(act, text="▲", width=3, command=lambda i=i: self.move(i, -1)).pack(side="left")
            ttk.Button(act, text="▼", width=3, command=lambda i=i: self.move(i, 1)).pack(side="left")
            ttk.Button(act, text="✕", width=3, command=lambda i=i: self.remove(i)).pack(side="left")
            self.rows.append({"type": tv, "id": idv, "rotate": rot, "crop_left": cl, "crop_right": cr,
                              "orig": cam})
        ttk.Label(self.slot_frame, foreground="#555", wraplength=900, justify="left", text=(
            "usb: pick a camera from the list (press Detect first).   "
            "ip: type the stream URL, e.g. rtsp://admin:pass@192.168.1.50:554/stream1   "
            "test: simulated camera.\n"
            "Crop L/R removes overlapping pixels at the edges (in full-resolution pixels) so the side-by-side "
            "image has no duplicated area.")).grid(row=len(self.rows) + 1, column=0, columnspan=7, sticky="w", pady=12)

    def collect_slots(self):
        cams = []
        label_to_cam = {self.cam_label(c): c for c in self.detected}
        for row in self.rows:
            t = row["type"].get()
            val = row["id"].get().strip()
            entry = {"type": t, "rotate": int(row["rotate"].get() or 0),
                     "crop_left": int(row["crop_left"].get() or 0), "crop_right": int(row["crop_right"].get() or 0)}
            if t == "usb":
                if val in label_to_cam:
                    entry["id"], entry["label"] = label_to_cam[val]["id"], label_to_cam[val]["name"]
                else:  # unchanged, camera currently not detected
                    entry["id"], entry["label"] = row["orig"].get("id", ""), row["orig"].get("label", "")
            else:
                entry["id"] = val
            if entry.get("id") == row["orig"].get("id"):          # same camera -> keep its layout position
                for k in LAYOUT_KEYS:
                    if k in row["orig"]:
                        entry[k] = row["orig"][k]
            cams.append(entry)
        return cams

    def add_slot(self):
        self.cfg["cameras"] = self.collect_slots()
        self.cfg["cameras"].append({"type": "usb", "id": "", "rotate": 0, "crop_left": 0, "crop_right": 0})
        self.draw_slots()

    def move(self, i, d):
        cams = self.collect_slots()
        j = i + d
        if 0 <= j < len(cams):
            cams[i], cams[j] = cams[j], cams[i]
            self.cfg["cameras"] = cams
            self.draw_slots()

    def remove(self, i):
        cams = self.collect_slots()
        cams.pop(i)
        self.cfg["cameras"] = cams
        self.draw_slots()

    def identify(self):
        """Show one snapshot per detected USB camera so you can see which physical camera is which."""
        if not self.detected:
            self.detect()
        if not self.detected:
            messagebox.showinfo("Identify", "No USB cameras detected.", parent=self.win)
            return
        top = tk.Toplevel(self.win)
        top.title("Detected cameras – wave your hand under each one")
        photos = []
        for k, c in enumerate(self.detected):
            cap = cv2.VideoCapture(c["index"], c["backend"])
            img = None
            for _ in range(10):  # let auto-exposure settle
                ok, f = cap.read()
                if ok:
                    img = f
            cap.release()
            if img is None:
                img = np.zeros((240, 320, 3), np.uint8)
            img = cv2.resize(img, (400, int(img.shape[0] * 400 / img.shape[1])))
            ph = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
            photos.append(ph)
            fr = ttk.Frame(top)
            fr.grid(row=k // 3, column=k % 3, padx=6, pady=6)
            tk.Label(fr, image=ph).pack()
            ttk.Label(fr, text=self.cam_label(c), wraplength=400).pack()
        top.photos = photos

    def save(self):
        try:
            cfg = self.cfg
            cfg["cameras"] = self.collect_slots()
            for k, v in self.vars.items():
                val = v.get()
                if k in ("jpeg_quality", "barcode_min_length", "width", "height"):
                    val = int(val)
                if k in ("auto_reset_s", "popup_seconds"):
                    val = float(val or 0)
                cfg[k] = val
            ids = [c["id"] for c in cfg["cameras"]]
            if any(not i for i in ids):
                raise ValueError("Every slot needs a camera (or remove the empty slot).")
            if len(set(ids)) != len(ids):
                raise ValueError("The same camera is assigned to two slots.")
            if cfg["barcode_regex"]:
                re.compile(cfg["barcode_regex"])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Settings", str(e), parent=self.win)
            return
        self.win.unbind("<Destroy>")
        self.win.destroy()
        self.app.apply_config(cfg)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if IS_WINDOWS else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
