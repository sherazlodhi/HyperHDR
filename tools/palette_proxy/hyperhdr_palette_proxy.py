#!/usr/bin/env python3
"""
HyperHDR DDP Palette Proxy (Unified TV Ambilight & Music Visualizer Engine)
=============================================================================
Features:
1. TV / Movie Mode:
   - Full natural unquantized RGB ambilight sampled directly from the TV bezel
     (or optional Primary Color Mode toggled via /primary or Home Assistant).
   - Restores the calibrated perspective trapezoid LED layout in HyperHDR.
   - Rate-limited to max 20 FPS on active scene changes and 1.5s keepalive when
     static to keep Wi-Fi & ESP32 load minimal.

2. Music Mode:
   - Dynamically switches HyperHDR's LED layout to Full Perimeter (0.0 to 1.0).
   - All 359 LEDs around the TV (Right 64, Top 115, Left 65, Bottom 115)
     overlap the outer 8% visualizer trigger zones.
   - Direct audio-reactive DDP packet passthrough to WLED.

3. Screensaver Mode (AerialViews - Ultra-Low Overhead):
   - Sets HyperHDR smoothing to 5 Hz (90% CPU reduction vs 50 Hz).
   - Stops continuous UDP DDP streaming so WLED exits live mode and uses its
     native ESP32 hardware renderer (Solid effect + 1.5s hardware transition).
   - Samples the TV dominant color once per second and only sends a single
     lightweight WLED JSON state update when the scene color actually changes
     (0 packets/sec when steady).

4. Dynamic State & Hardware Protection:
   - Seamlessly switches HyperHDR layout & smoothing via JSON-RPC socket (port 19444).
   - Locks camera white balance once at startup (5200K, AWB off) with no periodic
     v4l2-ctl resets mid-stream.

5. Control Interface:
   - HTTP listener on port 5049 (/screensaver, /tv, /music, /idle, /primary, /status, /brightness, /balance, /mic, /save_tv_boundary, /restore)
   - Fallback to /dev/shm/hyperhdr_mode
"""

import socket
import time
import colorsys
import numpy as np
import logging
import threading
import json
import os
import sqlite3
import subprocess
import copy
import urllib.request
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
import shutil
import base64
import struct
from io import BytesIO

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

LISTEN_IP = "127.0.0.1"
LISTEN_PORT = 5048

DEST_IP = "192.168.87.114"
DEST_PORT = 4048

HTTP_PORT = 5049
MODE_FILE = "/dev/shm/hyperhdr_mode"

HYPERHDR_JSONRPC_PORT = 19444
HYPERHDR_WS_PORT = 8090
DB_PATH = "/home/pi/.config/HyperHDR/db/hyperhdr.db"
TV_LAYOUT_FILE = "/home/pi/.hyperhdr_tv_layout.json"
TV_CONFIG_FILE = "/home/pi/.hyperhdr_tv_config.json"
MUSIC_LAYOUT_FILE = "/home/pi/.hyperhdr_music_layout.json"

AUTO_CROP_FILE = "/home/pi/.hyperhdr_auto_crop"
AUTO_CROP_PREVIEW_PATH = "/tmp/hyperhdr_auto_crop_latest.jpg"
AUTO_CROP_ENABLED = True
AUTO_CROP_INTERVAL_SEC = 10.0
AUTO_CROP_SHIFT_THRESHOLD_PX = 4.5
AUTO_CROP_LOCK = threading.Lock()
AUTO_CROP_STATE = {
    'enabled': True,
    'interval_sec': 10.0,
    'threshold_px': 4.5,
    'status': 'init',
    'last_check_ts': 0.0,
    'last_applied_ts': 0.0,
    'last_shift_px': 0.0,
    'corners_px': {},
    'corners_pct': {},
    'inliers': {},
    'preview_url': '/auto_crop_preview'
}

# Global operating mode: 'screensaver', 'tv', 'music', 'idle'
CURRENT_MODE = 'tv'
ACTIVE_LAYOUT = 'tv'

BRIGHTNESS_FILE = "/home/pi/.hyperhdr_brightness"
CURRENT_BRIGHTNESS = 100

DEV_DDP_CONFIG = {
    "colorOrder": "rgb",
    "hardwareLedCount": 359,
    "host": "127.0.0.1",
    "port": 5048,
    "refreshTime": 0,
    "type": "ddp"
}

SMOOTHING_TV_MUSIC = {
    "antiFlickeringFilter": True,
    "continuousOutput": True,
    "enable": True,
    "smoothingFactor": 0.5,
    "time_ms": 150,
    "type": "RgbInterpolator",
    "updateFrequency": 20
}

SMOOTHING_SCREENSAVER = {
    "antiFlickeringFilter": True,
    "continuousOutput": True,
    "enable": True,
    "smoothingFactor": 0.5,
    "time_ms": 400,
    "type": "RgbInterpolator",
    "updateFrequency": 5
}


def load_saved_brightness():
    global CURRENT_BRIGHTNESS
    if os.path.exists(BRIGHTNESS_FILE):
        try:
            with open(BRIGHTNESS_FILE, 'r') as bf:
                val = int(float(bf.read().strip()))
                CURRENT_BRIGHTNESS = max(5, min(100, val))
                logging.info(f"Loaded saved screensaver brightness: {CURRENT_BRIGHTNESS}%")
        except Exception as e:
            logging.warning(f"Could not load saved brightness: {e}")


def save_brightness(val):
    global CURRENT_BRIGHTNESS
    try:
        CURRENT_BRIGHTNESS = max(5, min(100, int(float(val))))
        with open(BRIGHTNESS_FILE, 'w') as bf:
            bf.write(f"{CURRENT_BRIGHTNESS}\n")
    except Exception as e:
        logging.warning(f"Could not save brightness: {e}")


PRIMARY_MODE_FILE = "/home/pi/.hyperhdr_primary_mode"
PRIMARY_COLOR_MODE = False

SCREENSAVER_BORDER_FILE = "/home/pi/.hyperhdr_screensaver_border"
SCREENSAVER_BORDER_MODE = False


def load_saved_primary_mode():
    global PRIMARY_COLOR_MODE
    if os.path.exists(PRIMARY_MODE_FILE):
        try:
            with open(PRIMARY_MODE_FILE, 'r') as f:
                val = f.read().strip().lower()
                PRIMARY_COLOR_MODE = (val in ('1', 'true', 'on', 'yes'))
                logging.info(f"Loaded saved primary color mode: {PRIMARY_COLOR_MODE}")
        except Exception as e:
            logging.warning(f"Could not load saved primary mode: {e}")


def save_primary_mode(val):
    global PRIMARY_COLOR_MODE
    try:
        PRIMARY_COLOR_MODE = bool(val)
        with open(PRIMARY_MODE_FILE, 'w') as f:
            f.write("1\n" if PRIMARY_COLOR_MODE else "0\n")
        logging.info(f"Saved primary color mode: {PRIMARY_COLOR_MODE}")
    except Exception as e:
        logging.warning(f"Could not save primary mode: {e}")


def load_saved_screensaver_border_mode():
    global SCREENSAVER_BORDER_MODE
    if os.path.exists(SCREENSAVER_BORDER_FILE):
        try:
            with open(SCREENSAVER_BORDER_FILE, 'r') as f:
                val = f.read().strip().lower()
                SCREENSAVER_BORDER_MODE = (val in ('1', 'true', 'on', 'yes'))
                logging.info(f"Loaded saved screensaver border mode: {SCREENSAVER_BORDER_MODE}")
        except Exception as e:
            logging.warning(f"Could not load saved screensaver border mode: {e}")


def save_screensaver_border_mode(val):
    global SCREENSAVER_BORDER_MODE
    try:
        SCREENSAVER_BORDER_MODE = bool(val)
        with open(SCREENSAVER_BORDER_FILE, 'w') as f:
            f.write("1\n" if SCREENSAVER_BORDER_MODE else "0\n")
        logging.info(f"Saved screensaver border mode: {SCREENSAVER_BORDER_MODE}")
    except Exception as e:
        logging.warning(f"Could not save screensaver border mode: {e}")


BALANCE_FILE = "/home/pi/.hyperhdr_edge_balance"
EDGE_BALANCE = {
    'top': 1.0,
    'sides': 1.0,
    'bottom': 1.0
}


def compute_edge_scale(num_leds=359, s_top=1.0, s_sides=1.0, s_bottom=1.0):
    raw = np.zeros(num_leds, dtype=np.float32)
    for i in range(num_leds):
        if 64 <= i <= 178:
            raw[i] = s_top
        elif 179 <= i <= 243:
            raw[i] = s_sides
        elif 244 <= i <= 357:
            raw[i] = s_bottom
        else:  # 0..63 and 358 (Right edge)
            raw[i] = s_sides

    pad_size = 4
    padded = np.pad(raw, pad_size, mode='wrap')
    kernel = np.ones(pad_size * 2 + 1, dtype=np.float32) / (pad_size * 2 + 1)
    smoothed = np.convolve(padded, kernel, mode='same')[pad_size:-pad_size]
    return smoothed


EDGE_SCALE_ARRAY = compute_edge_scale(359, EDGE_BALANCE['top'], EDGE_BALANCE['sides'], EDGE_BALANCE['bottom'])


def load_saved_balance():
    global EDGE_BALANCE, EDGE_SCALE_ARRAY
    if os.path.exists(BALANCE_FILE):
        try:
            with open(BALANCE_FILE, 'r') as bf:
                data = json.load(bf)
                t = float(data.get('top', 1.0))
                s = float(data.get('sides', 1.0))
                b = float(data.get('bottom', 1.0))
                EDGE_BALANCE = {
                    'top': max(0.2, min(1.0, t)),
                    'sides': max(0.2, min(1.0, s)),
                    'bottom': max(0.2, min(1.0, b))
                }
                EDGE_SCALE_ARRAY = compute_edge_scale(359, EDGE_BALANCE['top'], EDGE_BALANCE['sides'], EDGE_BALANCE['bottom'])
                logging.info(f"Loaded edge balance: {EDGE_BALANCE}")
        except Exception as e:
            logging.warning(f"Could not load edge balance: {e}")


def save_balance(top=None, sides=None, bottom=None):
    global EDGE_BALANCE, EDGE_SCALE_ARRAY
    try:
        if top is not None:
            EDGE_BALANCE['top'] = max(0.2, min(1.0, float(top)))
        if sides is not None:
            EDGE_BALANCE['sides'] = max(0.2, min(1.0, float(sides)))
        if bottom is not None:
            EDGE_BALANCE['bottom'] = max(0.2, min(1.0, float(bottom)))
        EDGE_SCALE_ARRAY = compute_edge_scale(359, EDGE_BALANCE['top'], EDGE_BALANCE['sides'], EDGE_BALANCE['bottom'])
        with open(BALANCE_FILE, 'w') as bf:
            json.dump(EDGE_BALANCE, bf)
        logging.info(f"Saved edge balance: {EDGE_BALANCE}")
    except Exception as e:
        logging.warning(f"Could not save edge balance: {e}")


COLOR_NAMES = ['BLACK', 'WHITE', 'RED', 'ORANGE', 'YELLOW', 'GREEN', 'CYAN', 'BLUE', 'MAGENTA']

PALETTE_RGB = {
    'BLACK':   (0, 0, 0),
    'WHITE':   (255, 255, 255),
    'RED':     (255, 0, 0),
    'ORANGE':  (255, 90, 0),
    'YELLOW':  (255, 220, 0),
    'GREEN':   (0, 255, 0),
    'CYAN':    (0, 220, 255),
    'BLUE':    (0, 40, 255),
    'MAGENTA': (255, 0, 220)
}

PALETTE_ID = {name: i for i, name in enumerate(COLOR_NAMES)}

LAT_STATE = {
    'latched_color': 'BLACK',
    'candidate_color': 'BLACK',
    'candidate_count': 0,
    'output_rgb': [0, 0, 0],
    'brightness': 100,
    'primary_mode': False,
    'balance': EDGE_BALANCE,
    'debug': {},
    'music_debug': {},
    'tv_debug': {},
    'fps': {}
}

# Track last WLED JSON state sent in screensaver mode so we only send on change
LAST_WLED_SCREENSAVER_RGB = None


def send_wled_json_state(payload, timeout=1.5):
    """
    Sends a lightweight HTTP POST to WLED /json/state in a background thread or inline.
    Used in screensaver mode (and on mode transition) so WLED uses its native hardware
    transitions with zero continuous UDP traffic.
    """
    def _worker():
        try:
            url = f"http://{DEST_IP}/json/state"
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=timeout) as _:
                pass
        except Exception as e:
            logging.debug(f"WLED JSON state update error: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def ensure_wled_live_ready():
    """
    Ensures WLED master brightness is 255 and live override is 0 when entering TV/Music mode.
    """
    global LAST_WLED_SCREENSAVER_RGB
    LAST_WLED_SCREENSAVER_RGB = None
    send_wled_json_state({"on": True, "bri": 255, "lor": 0})


def generate_full_perimeter_leds(depth=0.08):
    leds = []
    # 1. Right: 64 LEDs (0..63), bottom (v=1.0) to top (v=0.0)
    for i in range(64):
        vmax = round(1.0 - i / 64.0, 4)
        vmin = round(1.0 - (i + 1) / 64.0, 4)
        leds.append({
            'group': 0,
            'hmin': round(1.0 - depth, 4),
            'hmax': 1.0,
            'vmin': vmin,
            'vmax': vmax
        })

    # 2. Top: 115 LEDs (64..178), right (h=1.0) to left (h=0.0)
    for i in range(115):
        hmax = round(1.0 - i / 115.0, 4)
        hmin = round(1.0 - (i + 1) / 115.0, 4)
        leds.append({
            'group': 0,
            'hmin': hmin,
            'hmax': hmax,
            'vmin': 0.0,
            'vmax': round(depth, 4)
        })

    # 3. Left: 65 LEDs (179..243), top (v=0.0) to bottom (v=1.0)
    for i in range(65):
        vmin = round(i / 65.0, 4)
        vmax = round((i + 1) / 65.0, 4)
        leds.append({
            'group': 0,
            'hmin': 0.0,
            'hmax': round(depth, 4),
            'vmin': vmin,
            'vmax': vmax
        })

    # 4. Bottom: 115 LEDs (244..358), left (h=0.0) to right (h=1.0)
    for i in range(115):
        hmin = round(i / 115.0, 4)
        hmax = round((i + 1) / 115.0, 4)
        leds.append({
            'group': 0,
            'hmin': hmin,
            'hmax': hmax,
            'vmin': round(1.0 - depth, 4),
            'vmax': 1.0
        })

    return leds


def load_music_layout():
    if os.path.exists(MUSIC_LAYOUT_FILE):
        try:
            with open(MUSIC_LAYOUT_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) == 359:
                    return data
        except Exception:
            pass
    music_leds = generate_full_perimeter_leds(0.08)
    try:
        with open(MUSIC_LAYOUT_FILE, 'w') as f:
            json.dump(music_leds, f, indent=2)
    except Exception:
        pass
    return music_leds


DEFAULT_TV_LED_CONFIG = {
    "classic": {
        "top": 115,
        "bottom": 115,
        "left": 65,
        "right": 64,
        "position": 180,
        "reverse": True,
        "ptlh": 5, "ptlv": 18,
        "ptrh": 78, "ptrv": 26,
        "pblh": 6, "pblv": 70,
        "pbrh": 73, "pbrv": 87,
        "hdepth": 6,
        "vdepth": 5,
        "edgegap": 0, "glength": 0, "gpos": 0, "groupX": 0, "groupY": 0, "overlap": 0
    },
    "matrix": {
        "cabling": "snake", "ledshoriz": 1, "ledsvert": 1, "start": "top-left"
    }
}

MUSIC_LED_CONFIG = {
    "classic": {
        "top": 115,
        "bottom": 115,
        "left": 65,
        "right": 64,
        "position": 180,
        "reverse": True,
        "ptlh": 0, "ptlv": 0,
        "ptrh": 100, "ptrv": 0,
        "pblh": 0, "pblv": 100,
        "pbrh": 100, "pbrv": 100,
        "hdepth": 8,
        "vdepth": 8,
        "edgegap": 0, "glength": 0, "gpos": 0, "groupX": 0, "groupY": 0, "overlap": 0
    },
    "matrix": {
        "cabling": "snake", "ledshoriz": 1, "ledsvert": 1, "start": "top-left"
    }
}


def generate_tv_leds_from_config(classic):
    """
    Generates 359 LEDs mapped strictly inside the 4-corner TV trapezoid using
    bilinear interpolation and sloped-edge clamping so no LED sampling box ever
    pokes outside the black TV bezel into the wall.
    Physical WLED index mapping (359 LEDs):
      - Right border:  0..63   (64 LEDs, bottom to top)
      - Top border:    64..178 (115 LEDs, right to left)
      - Left border:   179..243 (65 LEDs, top to bottom)
      - Bottom border: 244..358 (115 LEDs, left to right)
    """
    cn = classic.get('corners_norm') if isinstance(classic, dict) else None
    if isinstance(cn, dict) and all(k in cn for k in ('tl', 'tr', 'bl', 'br')):
        tl = np.array(cn['tl'], dtype=np.float64)
        tr = np.array(cn['tr'], dtype=np.float64)
        bl = np.array(cn['bl'], dtype=np.float64)
        br = np.array(cn['br'], dtype=np.float64)
    else:
        tl = np.array([classic.get('ptlh', 30) / 100.0, classic.get('ptlv', 21) / 100.0], dtype=np.float64)
        tr = np.array([classic.get('ptrh', 100) / 100.0, classic.get('ptrv', 27) / 100.0], dtype=np.float64)
        bl = np.array([classic.get('pblh', 30) / 100.0, classic.get('pblv', 71) / 100.0], dtype=np.float64)
        br = np.array([classic.get('pbrh', 99) / 100.0, classic.get('pbrv', 93) / 100.0], dtype=np.float64)

    hdepth = max(0.04, min(0.12, classic.get('hdepth', 7) / 100.0))
    vdepth = max(0.04, min(0.12, classic.get('vdepth', 7) / 100.0))
    inset = 0.015

    def quad_pt(u, v):
        return (1.0 - u) * (1.0 - v) * tl + u * (1.0 - v) * tr + (1.0 - u) * v * bl + u * v * br

    def y_top_at_x(x):
        t = (x - tl[0]) / max(1e-6, tr[0] - tl[0])
        return tl[1] + t * (tr[1] - tl[1]) + inset * 0.6

    def y_bot_at_x(x):
        t = (x - bl[0]) / max(1e-6, br[0] - bl[0])
        return bl[1] + t * (br[1] - bl[1]) - inset * 0.6

    def x_left_at_y(y):
        t = (y - tl[1]) / max(1e-6, bl[1] - tl[1])
        return tl[0] + t * (bl[0] - tl[0]) + inset * 0.6

    def x_right_at_y(y):
        t = (y - tr[1]) / max(1e-6, br[1] - tr[1])
        return tr[0] + t * (br[0] - tr[0]) - inset * 0.6

    leds = []

    def add_led(u0, u1, v0, v1, edge):
        um = 0.5 * (u0 + u1)
        vm = 0.5 * (v0 + v1)
        if edge in ('top', 'bottom'):
            p0 = quad_pt(u0, vm)
            p1 = quad_pt(u1, vm)
            pa = quad_pt(um, v0)
            pb = quad_pt(um, v1)
            hmin, hmax = min(p0[0], p1[0]), max(p0[0], p1[0])
            vmin, vmax = min(pa[1], pb[1]), max(pa[1], pb[1])
        else:
            p0 = quad_pt(um, v0)
            p1 = quad_pt(um, v1)
            pa = quad_pt(u0, vm)
            pb = quad_pt(u1, vm)
            hmin, hmax = min(pa[0], pb[0]), max(pa[0], pb[0])
            vmin, vmax = min(p0[1], p1[1]), max(p0[1], p1[1])

        yt_lim = max(y_top_at_x(hmin), y_top_at_x(hmax))
        yb_lim = min(y_bot_at_x(hmin), y_bot_at_x(hmax))
        xl_lim = max(x_left_at_y(vmin), x_left_at_y(vmax))
        xr_lim = min(x_right_at_y(vmin), x_right_at_y(vmax))

        vmin = max(vmin, yt_lim)
        vmax = min(vmax, yb_lim)
        if vmax <= vmin + 0.004:
            if edge == 'top':
                vmax = vmin + 0.015
            else:
                vmin = vmax - 0.015

        hmin = max(hmin, xl_lim)
        hmax = min(hmax, xr_lim)
        if hmax <= hmin + 0.004:
            if edge == 'left':
                hmax = hmin + 0.015
            else:
                hmin = hmax - 0.015

        hmin = float(np.clip(hmin, 0.0, 0.998))
        hmax = float(np.clip(hmax, hmin + 0.002, 1.0))
        vmin = float(np.clip(vmin, 0.0, 0.998))
        vmax = float(np.clip(vmax, vmin + 0.002, 1.0))
        leds.append({
            'group': 0,
            'hmin': round(hmin, 4),
            'hmax': round(hmax, 4),
            'vmin': round(vmin, 4),
            'vmax': round(vmax, 4)
        })

    span_v = 1.0 - 2.0 * inset
    span_u = 1.0 - 2.0 * inset

    # 1. Right: 64 LEDs (0..63), bottom to top
    for i in range(64):
        v1 = (1.0 - inset) - (i / 64.0) * span_v
        v0 = (1.0 - inset) - ((i + 1) / 64.0) * span_v
        add_led(1.0 - inset - hdepth, 1.0 - inset, v0, v1, 'right')

    # 2. Top: 115 LEDs (64..178), right to left
    for i in range(115):
        u1 = (1.0 - inset) - (i / 115.0) * span_u
        u0 = (1.0 - inset) - ((i + 1) / 115.0) * span_u
        add_led(u0, u1, inset, inset + vdepth, 'top')

    # 3. Left: 65 LEDs (179..243), top to bottom
    for i in range(65):
        v0 = inset + (i / 65.0) * span_v
        v1 = inset + ((i + 1) / 65.0) * span_v
        add_led(inset, inset + hdepth, v0, v1, 'left')

    # 4. Bottom: 115 LEDs (244..358), left to right
    for i in range(115):
        u0 = inset + (i / 115.0) * span_u
        u1 = inset + ((i + 1) / 115.0) * span_u
        add_led(u0, u1, 1.0 - inset - vdepth, 1.0 - inset, 'bottom')

    return leds


def _is_full_perimeter_leds(leds_list):
    if not isinstance(leds_list, list) or len(leds_list) != 359:
        return True
    h0 = min(l['hmin'] for l in leds_list)
    h1 = max(l['hmax'] for l in leds_list)
    v0 = min(l['vmin'] for l in leds_list)
    v1 = max(l['vmax'] for l in leds_list)
    return (h0 <= 0.01 and h1 >= 0.99 and v0 <= 0.01 and v1 >= 0.99)


def load_tv_layout():
    tv_leds = None
    tv_cfg = None

    # Prefer TV_CONFIG_FILE first so auto-calibrated corners_norm and layout are preserved across mode switches
    if os.path.exists(TV_CONFIG_FILE):
        try:
            with open(TV_CONFIG_FILE, 'r') as f:
                saved = json.load(f)
                cand_cfg = saved.get('ledConfig')
                cand_leds = saved.get('leds')
                if isinstance(cand_cfg, dict):
                    classic = cand_cfg.get('classic', {})
                    is_full_rect = (classic.get('ptlh', 0) == 0 and classic.get('ptlv', 0) == 0 and
                                    classic.get('ptrh', 100) == 100 and classic.get('pbrv', 100) == 100 and
                                    classic.get('pbrh', 100) == 100 and classic.get('pblv', 100) == 100)
                    if not is_full_rect:
                        tv_cfg = cand_cfg
                if isinstance(cand_leds, list) and len(cand_leds) == 359 and not _is_full_perimeter_leds(cand_leds):
                    tv_leds = cand_leds
        except Exception as e:
            logging.warning(f"Could not load TV config file: {e}")

    if (tv_cfg is None or tv_leds is None) and os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT config FROM settings WHERE type='ledConfig'")
            cfg_row = c.fetchone()
            c.execute("SELECT config FROM settings WHERE type='leds'")
            leds_row = c.fetchone()
            conn.close()

            if tv_cfg is None and cfg_row:
                parsed_cfg = json.loads(cfg_row[0])
                classic = parsed_cfg.get('classic', {})
                if classic.get('top', 0) >= 10:
                    is_full_rect = (classic.get('ptlh', 0) == 0 and classic.get('ptlv', 0) == 0 and
                                    classic.get('ptrh', 100) == 100 and classic.get('pbrv', 100) == 100 and
                                    classic.get('pbrh', 100) == 100 and classic.get('pblv', 100) == 100)
                    if not is_full_rect:
                        tv_cfg = parsed_cfg

            if tv_leds is None and leds_row:
                parsed_leds = json.loads(leds_row[0])
                if isinstance(parsed_leds, list) and len(parsed_leds) == 359 and not _is_full_perimeter_leds(parsed_leds):
                    tv_leds = parsed_leds
        except Exception as e:
            logging.warning(f"Could not load TV layout from DB: {e}")

    if tv_cfg is None:
        tv_cfg = copy.deepcopy(DEFAULT_TV_LED_CONFIG)
    if tv_leds is None:
        if os.path.exists(TV_LAYOUT_FILE):
            try:
                with open(TV_LAYOUT_FILE, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, list) and len(data) == 359 and not _is_full_perimeter_leds(data):
                        tv_leds = data
            except Exception:
                pass
        if tv_leds is None or len(tv_leds) != 359:
            tv_leds = generate_tv_leds_from_config(tv_cfg['classic'])

    if tv_cfg is not None and tv_leds is not None:
        classic = tv_cfg.get('classic', {})
        is_full_rect = (classic.get('ptlh', 0) == 0 and classic.get('ptlv', 0) == 0 and
                        classic.get('ptrh', 100) == 100 and classic.get('pbrv', 100) == 100 and
                        classic.get('pbrh', 100) == 100 and classic.get('pblv', 100) == 100)
        if not is_full_rect:
            try:
                with open(TV_CONFIG_FILE, 'w') as f:
                    json.dump({'ledConfig': tv_cfg, 'leds': tv_leds}, f, indent=2)
            except Exception:
                pass

    return tv_leds, tv_cfg


def save_tv_layout(leds_data):
    try:
        if not isinstance(leds_data, list) or len(leds_data) != 359:
            logging.error(f"Cannot save TV layout: expected 359 LEDs, got {len(leds_data) if isinstance(leds_data, list) else type(leds_data)}")
            return False

        if _is_full_perimeter_leds(leds_data):
            logging.warning("Refusing to save TV boundary: layout appears to be full perimeter, not TV trapezoid")
            return False

        with open(TV_LAYOUT_FILE, 'w') as f:
            json.dump(leds_data, f, indent=2)
        logging.info(f"Saved calibrated TV LED boundary layout ({len(leds_data)} LEDs) to {TV_LAYOUT_FILE}")
        return True
    except Exception as e:
        logging.error(f"Error saving TV layout: {e}")
        return False


def load_saved_auto_crop():
    global AUTO_CROP_ENABLED
    if os.path.exists(AUTO_CROP_FILE):
        try:
            with open(AUTO_CROP_FILE, 'r') as f:
                val = f.read().strip().lower()
                AUTO_CROP_ENABLED = (val in ('1', 'true', 'on', 'yes'))
        except Exception as e:
            logging.warning(f"Could not load saved auto-crop state: {e}")
    AUTO_CROP_STATE['enabled'] = AUTO_CROP_ENABLED
    if os.path.exists(TV_CONFIG_FILE):
        try:
            with open(TV_CONFIG_FILE, 'r') as f:
                saved = json.load(f)
                classic = saved.get('ledConfig', {}).get('classic', {})
                AUTO_CROP_STATE['corners_pct'] = {
                    k: classic.get(k) for k in ('ptlh', 'ptlv', 'ptrh', 'ptrv', 'pblh', 'pblv', 'pbrh', 'pbrv')
                }
                if 'corners_px' in classic:
                    AUTO_CROP_STATE['corners_px'] = classic['corners_px']
        except Exception:
            pass


def save_auto_crop_enabled(val):
    global AUTO_CROP_ENABLED
    try:
        AUTO_CROP_ENABLED = bool(val)
        AUTO_CROP_STATE['enabled'] = AUTO_CROP_ENABLED
        with open(AUTO_CROP_FILE, 'w') as f:
            f.write("1\n" if AUTO_CROP_ENABLED else "0\n")
        logging.info(f"Saved auto-crop enabled state: {AUTO_CROP_ENABLED}")
    except Exception as e:
        logging.warning(f"Could not save auto-crop state: {e}")


def _ws_send_text(sock, text):
    payload = text.encode('utf-8')
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack('>H', n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack('>Q', n))
    header.extend(mask)
    masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + masked)


def _ws_recv_frame(sock):
    hdr = sock.recv(2)
    if len(hdr) < 2:
        return True, 8, b''
    b1, b2 = hdr[0], hdr[1]
    fin = (b1 & 0x80) != 0
    opcode = b1 & 0x0F
    length = b2 & 0x7F
    if length == 126:
        ext = sock.recv(2)
        length = struct.unpack('>H', ext)[0]
    elif length == 127:
        ext = sock.recv(8)
        length = struct.unpack('>Q', ext)[0]
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return fin, opcode, bytes(data)


def capture_hyperhdr_frame(force_wake=False, timeout=4.0):
    """
    Captures a single live uncropped camera frame from HyperHDR's WebSocket API
    without conflicting with HyperHDR's V4L2 lock on /dev/video0.
    If force_wake=True and VIDEOGRABBER is currently off (e.g. in idle mode),
    temporarily enables VIDEOGRABBER to capture the frame and restores it after.
    """
    if Image is None:
        logging.warning("PIL is not installed; cannot capture frame for auto-crop.")
        return None

    woke_grabber = False
    s = None
    try:
        # Check if VIDEOGRABBER is enabled via JSON-RPC
        rpc = socket.create_connection(('127.0.0.1', HYPERHDR_JSONRPC_PORT), timeout=2.0)
        rpc.sendall(json.dumps({'command': 'serverinfo', 'tan': 1}).encode('utf-8') + b'\n')
        buf = b''
        while b'\n' not in buf:
            chunk = rpc.recv(65536)
            if not chunk:
                break
            buf += chunk
        rpc.close()
        info = json.loads(buf.decode('utf-8')).get('info', {})
        comps = {c.get('name'): c.get('enabled') for c in info.get('components', [])}
        vg_enabled = bool(comps.get('VIDEOGRABBER', False))

        if not vg_enabled:
            if not force_wake:
                return None
            woke_grabber = True
            rpc2 = socket.create_connection(('127.0.0.1', HYPERHDR_JSONRPC_PORT), timeout=2.0)
            rpc2.sendall(json.dumps({
                'command': 'componentstate',
                'componentstate': {'component': 'VIDEOGRABBER', 'state': True},
                'tan': 2
            }).encode('utf-8') + b'\n')
            rpc2.recv(4096)
            rpc2.close()
            time.sleep(1.1)
            tune_camera()
            time.sleep(0.4)

        s = socket.create_connection(('127.0.0.1', HYPERHDR_WS_PORT), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        req = (
            f"GET /json-rpc HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{HYPERHDR_WS_PORT}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        s.sendall(req.encode('utf-8'))
        s.recv(4096)

        _ws_send_text(s, json.dumps({'command': 'authorize', 'subcommand': 'login', 'password': 'hyperhdr', 'tan': 1}))
        _ws_send_text(s, json.dumps({'command': 'ledcolors', 'subcommand': 'imagestream-start', 'tan': 2}))

        jpeg_buf = bytearray()
        t0 = time.time()
        while time.time() - t0 < timeout:
            fin, op, data = _ws_recv_frame(s)
            if op == 2 or (op == 0 and len(jpeg_buf) > 0):
                jpeg_buf.extend(data)
                if fin:
                    break
            elif op == 8:
                break

        _ws_send_text(s, json.dumps({'command': 'ledcolors', 'subcommand': 'imagestream-stop', 'tan': 3}))
        s.close()
        s = None

        if len(jpeg_buf) > 500:
            return Image.open(BytesIO(bytes(jpeg_buf))).convert('RGB')
    except Exception as e:
        logging.debug(f"Auto-crop frame capture error: {e}")
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        if woke_grabber and CURRENT_MODE in ('idle', 'off', 'music'):
            try:
                rpc3 = socket.create_connection(('127.0.0.1', HYPERHDR_JSONRPC_PORT), timeout=2.0)
                rpc3.sendall(json.dumps({
                    'command': 'componentstate',
                    'componentstate': {'component': 'VIDEOGRABBER', 'state': False},
                    'tan': 4
                }).encode('utf-8') + b'\n')
                rpc3.recv(4096)
                rpc3.close()
            except Exception:
                pass
    return None


def _longest_contiguous_cluster(xs, max_gap=40.0):
    if len(xs) == 0:
        return xs
    xs_sorted = np.sort(xs)
    splits = np.where(np.diff(xs_sorted) > max_gap)[0] + 1
    clusters = np.split(xs_sorted, splits)
    return max(clusters, key=lambda c: (float(c[-1] - c[0]), len(c)))


def detect_tv_quad(img_pil):
    """
    Detects the 4 corners of the black-bezel TV against the white/off-white wall
    whether the TV is ON (playing bright/dark video) or OFF.
    Scans inward from the wall on all 4 sides for the outer wall->bezel boundary
    and fits collinear edge lines with contiguous-cluster RANSAC.
    """
    arr = np.array(img_pil, dtype=np.float32)
    H, W, _ = arr.shape
    luma = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]

    # Refuse calibration if room + wall are completely dark
    if float(np.percentile(luma, 85)) < 42.0:
        return None

    gy = np.zeros_like(luma)
    gy[2:-2, :] = luma[4:, :] - luma[:-4, :]
    gx = np.zeros_like(luma)
    gx[:, 2:-2] = luma[:, 4:] - luma[:, :-4]

    def fit_line_ransac(xs, ys, weights=None, tol=2.5, min_inliers=8, max_slope=0.45):
        xs = np.asarray(xs, dtype=np.float64)
        ys = np.asarray(ys, dtype=np.float64)
        if len(xs) < min_inliers:
            return None
        best_inliers = None
        best_score = -1.0
        n = len(xs)
        w = np.ones(n) if weights is None else np.asarray(weights, dtype=np.float64)
        idx_pool = np.linspace(0, n - 1, min(n, 55)).astype(int)
        for i in range(len(idx_pool)):
            for j in range(i + 2, len(idx_pool)):
                i1, i2 = idx_pool[i], idx_pool[j]
                dx = xs[i2] - xs[i1]
                if abs(dx) < 12:
                    continue
                m = (ys[i2] - ys[i1]) / dx
                if abs(m) > max_slope:
                    continue
                b = ys[i1] - m * xs[i1]
                resid = np.abs(ys - (m * xs + b))
                inliers = resid <= tol
                cnt = int(np.sum(inliers))
                if cnt < min_inliers:
                    continue
                score = float(np.sum(w[inliers] * (1.0 - resid[inliers] / (tol * 2.0))))
                if score > best_score:
                    best_score = score
                    best_inliers = inliers
        if best_inliers is None:
            return None
        in_xs = xs[best_inliers]
        cluster = _longest_contiguous_cluster(in_xs, max_gap=40.0)
        best_inliers = best_inliers & (xs >= cluster[0]) & (xs <= cluster[-1])
        if int(np.sum(best_inliers)) < min_inliers:
            return None
        xi, yi, wi = xs[best_inliers], ys[best_inliers], w[best_inliers]
        A = np.vstack([xi, np.ones_like(xi)]).T
        m, b = np.linalg.lstsq(A * wi[:, None], yi * wi, rcond=None)[0]
        resid = np.abs(ys - (m * xs + b))
        inliers2 = (resid <= tol) & (xs >= cluster[0] - 10) & (xs <= cluster[-1] + 10)
        if int(np.sum(inliers2)) >= min_inliers:
            xi, yi, wi = xs[inliers2], ys[inliers2], w[inliers2]
            A = np.vstack([xi, np.ones_like(xi)]).T
            m, b = np.linalg.lstsq(A * wi[:, None], yi * wi, rcond=None)[0]
            best_inliers = inliers2
        return float(m), float(b), int(np.sum(best_inliers)), xs[best_inliers]

    # 1. TOP EDGE: scan downward from wall into top bezel
    y_top_min, y_top_max = int(0.05 * H), int(0.48 * H)
    top_x, top_y, top_w = [], [], []
    for x in range(int(0.06 * W), int(0.99 * W), 2):
        found_in_col = 0
        for y in range(y_top_min, y_top_max):
            g = -gy[y, x]
            if g < 24 or g < -gy[y - 1, x] or g < -gy[y + 1, x]:
                continue
            wall_strip = luma[max(0, y - 14):max(1, y - 2), x]
            bezel_strip = luma[min(H - 1, y + 1):min(H, y + 4), x]
            wall_v = float(np.mean(wall_strip))
            wall_std = float(np.std(wall_strip))
            bezel_v = float(np.min(bezel_strip))
            if wall_v > 48 and wall_std < 24 and bezel_v < min(wall_v * 0.68, wall_v - 18):
                bonus = 1.35 if found_in_col == 0 else 0.85
                top_x.append(x)
                top_y.append(y)
                top_w.append(g * bonus)
                found_in_col += 1
                if found_in_col >= 2:
                    break

    res_t = fit_line_ransac(top_x, top_y, top_w, tol=2.5, min_inliers=20, max_slope=0.35)
    if not res_t:
        return None
    m_t, b_t, in_t, inlier_tx = res_t

    # 2. BOTTOM EDGE: scan upward from lower wall into bottom bezel within horizontal span of top edge
    x_span_min = max(int(0.06 * W), int(np.min(inlier_tx) - 25))
    x_span_max = min(int(0.99 * W), int(np.max(inlier_tx) + 25))
    y_bot_min, y_bot_max = int(0.50 * H), int(0.97 * H)
    bot_x, bot_y, bot_w = [], [], []
    for x in range(x_span_min, x_span_max, 2):
        found_in_col = 0
        for y in range(y_bot_max, y_bot_min, -1):
            g = gy[y, x]
            if g < 22 or g < gy[y - 1, x] or g < gy[y + 1, x]:
                continue
            wall_strip = luma[min(H - 1, y + 2):min(H, y + 14), x]
            bezel_strip = luma[max(0, y - 4):max(1, y - 1), x]
            wall_v = float(np.mean(wall_strip))
            wall_std = float(np.std(wall_strip))
            bezel_v = float(np.min(bezel_strip))
            if wall_v > 42 and wall_std < 25 and bezel_v < min(wall_v * 0.68, wall_v - 18):
                bonus = 1.35 if found_in_col == 0 else 0.85
                bot_x.append(x)
                bot_y.append(y)
                bot_w.append(g * bonus)
                found_in_col += 1
                if found_in_col >= 2:
                    break

    res_b = fit_line_ransac(bot_x, bot_y, bot_w, tol=2.5, min_inliers=20, max_slope=0.45)
    if not res_b:
        return None
    m_b, b_b, in_b, inlier_bx = res_b

    approx_x_left = float(min(np.min(inlier_tx), np.min(inlier_bx)))
    approx_x_right = float(max(np.max(inlier_tx), np.max(inlier_bx)))

    # 3. LEFT EDGE: scan rightward from left wall into left bezel
    x_l_min = max(int(0.04 * W), int(approx_x_left - 35))
    x_l_max = min(int(0.65 * W), int(approx_x_left + 35))
    left_y, left_x, left_w = [], [], []
    for y in range(int(0.05 * H), int(0.95 * H), 2):
        for x in range(x_l_min, x_l_max):
            yt = m_t * x + b_t
            yb = m_b * x + b_b
            if not (yt + 4 <= y <= yb - 4):
                continue
            g = -gx[y, x]
            if g < 22 or g < -gx[y, x - 1] or g < -gx[y, x + 1]:
                continue
            wall_strip = luma[y, max(0, x - 14):max(1, x - 2)]
            bezel_strip = luma[y, min(W - 1, x + 1):min(W, x + 4)]
            wall_v = float(np.mean(wall_strip))
            wall_std = float(np.std(wall_strip))
            bezel_v = float(np.min(bezel_strip))
            if wall_v > 48 and wall_std < 24 and bezel_v < min(wall_v * 0.68, wall_v - 18):
                left_y.append(y)
                left_x.append(x)
                left_w.append(g)
                break

    res_l = fit_line_ransac(left_y, left_x, left_w, tol=2.5, min_inliers=10, max_slope=0.25)
    if not res_l:
        return None
    m_l, b_l, in_l, _ = res_l

    # 4. RIGHT EDGE: scan leftward from right wall into right bezel
    x_r_min = max(int(0.45 * W), int(approx_x_right - 40))
    right_y, right_x, right_w = [], [], []
    for y in range(int(0.05 * H), int(0.96 * H), 2):
        for x in range(W - 3, x_r_min, -1):
            yt = m_t * x + b_t
            yb = m_b * x + b_b
            if not (yt + 3 <= y <= yb - 3):
                continue
            g = gx[y, x]
            if g < 22 or g < gx[y, x - 1] or g < gx[y, x + 1]:
                continue
            wall_strip = luma[y, min(W - 1, x + 2):min(W, x + 14)]
            bezel_strip = luma[y, max(0, x - 4):max(1, x - 1)]
            wall_v = float(np.mean(wall_strip))
            wall_min = float(np.min(wall_strip))
            wall_std = float(np.std(wall_strip)) if len(wall_strip) > 1 else 0.0
            bezel_v = float(np.min(bezel_strip))
            if wall_v > 45 and wall_min > 38 and wall_std < 25 and bezel_v < min(wall_v * 0.68, wall_v - 18):
                right_y.append(y)
                right_x.append(x)
                right_w.append(g)
                break

    res_r = fit_line_ransac(right_y, right_x, right_w, tol=2.5, min_inliers=5, max_slope=0.25)

    def intersect_yx_xy(m_y, b_y, m_x, b_x):
        denom = 1.0 - m_x * m_y
        x = (m_x * b_y + b_x) / denom
        y = m_y * x + b_y
        return x, y

    tl_x, tl_y = intersect_yx_xy(m_t, b_t, m_l, b_l)
    bl_x, bl_y = intersect_yx_xy(m_b, b_b, m_l, b_l)
    if res_r:
        m_r, b_r, in_r, _ = res_r
        tr_x, tr_y = intersect_yx_xy(m_t, b_t, m_r, b_r)
        br_x, br_y = intersect_yx_xy(m_b, b_b, m_r, b_r)
    else:
        in_r = 0
        tr_x = float(np.max(inlier_tx))
        tr_y = m_t * tr_x + b_t
        br_x = float(np.max(inlier_bx))
        br_y = m_b * br_x + b_b

    tl_x_c = float(np.clip(tl_x, 0.0, W - 1.0)); tl_y_c = float(np.clip(m_t * tl_x_c + b_t, 0.0, H - 1.0))
    tr_x_c = float(np.clip(tr_x, 0.0, W - 1.0)); tr_y_c = float(np.clip(m_t * tr_x_c + b_t, 0.0, H - 1.0))
    bl_x_c = float(np.clip(bl_x, 0.0, W - 1.0)); bl_y_c = float(np.clip(m_b * bl_x_c + b_b, 0.0, H - 1.0))
    br_x_c = float(np.clip(br_x, 0.0, W - 1.0)); br_y_c = float(np.clip(m_b * br_x_c + b_b, 0.0, H - 1.0))

    top_w_px = tr_x_c - tl_x_c
    bot_w_px = br_x_c - bl_x_c
    left_h_px = bl_y_c - tl_y_c
    right_h_px = br_y_c - tr_y_c
    if top_w_px < 0.35 * W or bot_w_px < 0.35 * W or left_h_px < 0.25 * H or right_h_px < 0.25 * H:
        return None

    return {
        'resolution': [W, H],
        'corners_px': {
            'tl': [round(tl_x_c, 1), round(tl_y_c, 1)],
            'tr': [round(tr_x_c, 1), round(tr_y_c, 1)],
            'bl': [round(bl_x_c, 1), round(bl_y_c, 1)],
            'br': [round(br_x_c, 1), round(br_y_c, 1)],
        },
        'corners_norm': {
            'tl': [round(tl_x_c / W, 4), round(tl_y_c / H, 4)],
            'tr': [round(tr_x_c / W, 4), round(tr_y_c / H, 4)],
            'bl': [round(bl_x_c / W, 4), round(bl_y_c / H, 4)],
            'br': [round(br_x_c / W, 4), round(br_y_c / H, 4)],
        },
        'corners_pct': {
            'ptlh': int(round(tl_x_c / W * 100)),
            'ptlv': int(round(tl_y_c / H * 100)),
            'ptrh': int(round(tr_x_c / W * 100)),
            'ptrv': int(round(tr_y_c / H * 100)),
            'pblh': int(round(bl_x_c / W * 100)),
            'pblv': int(round(bl_y_c / H * 100)),
            'pbrh': int(round(br_x_c / W * 100)),
            'pbrv': int(round(br_y_c / H * 100)),
        },
        'inliers': {
            'top': in_t,
            'bottom': in_b,
            'left': in_l,
            'right': in_r
        }
    }


def _compute_corner_shift_px(c1_px, c2_px):
    if not c1_px or not c2_px:
        return 999.0
    shifts = []
    for k in ('tl', 'tr', 'bl', 'br'):
        if k not in c1_px or k not in c2_px:
            return 999.0
        p1, p2 = c1_px[k], c2_px[k]
        shifts.append(float(np.hypot(p1[0] - p2[0], p1[1] - p2[1])))
    return max(shifts) if shifts else 999.0


def save_auto_crop_preview(img_pil, quad_info, leds):
    if ImageDraw is None or img_pil is None or not quad_info:
        return
    try:
        vis = img_pil.copy()
        W, H = vis.size
        draw = ImageDraw.Draw(vis)
        cpx = quad_info['corners_px']
        pts = [
            tuple(cpx['tl']),
            tuple(cpx['tr']),
            tuple(cpx['br']),
            tuple(cpx['bl']),
            tuple(cpx['tl'])
        ]
        draw.line(pts, fill=(0, 255, 0), width=2)
        for cx, cy in pts[:4]:
            draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), outline=(255, 40, 40), width=2)
        if isinstance(leds, list):
            for idx, l in enumerate(leds):
                x0, x1 = l['hmin'] * W, l['hmax'] * W
                y0, y1 = l['vmin'] * H, l['vmax'] * H
                color = (
                    (0, 220, 255) if idx < 64 else
                    (255, 220, 0) if idx < 179 else
                    (255, 80, 200) if idx < 244 else
                    (80, 255, 120)
                )
                draw.rectangle([x0, y0, x1, y1], outline=color, width=1)
        vis.save(AUTO_CROP_PREVIEW_PATH, quality=88)
    except Exception as e:
        logging.debug(f"Could not save auto-crop preview image: {e}")


def apply_detected_tv_quad(quad_info, img_pil=None):
    """
    Persists the detected 4-corner TV boundary into TV_CONFIG_FILE, TV_LAYOUT_FILE,
    and hyperhdr.db, and live-updates HyperHDR via JSON-RPC if not in music mode.
    """
    _, tv_cfg = load_tv_layout()
    if tv_cfg is None:
        tv_cfg = copy.deepcopy(DEFAULT_TV_LED_CONFIG)
    classic = tv_cfg.setdefault('classic', {})
    classic.update(quad_info['corners_pct'])
    classic['corners_norm'] = quad_info['corners_norm']
    classic['corners_px'] = quad_info['corners_px']
    classic['top'] = 115
    classic['bottom'] = 115
    classic['left'] = 65
    classic['right'] = 64
    classic['hdepth'] = 7
    classic['vdepth'] = 7
    classic['position'] = 180
    classic['reverse'] = True

    tv_leds = generate_tv_leds_from_config(classic)
    try:
        with open(TV_CONFIG_FILE, 'w') as f:
            json.dump({'ledConfig': tv_cfg, 'leds': tv_leds}, f, indent=2)
        with open(TV_LAYOUT_FILE, 'w') as f:
            json.dump(tv_leds, f, indent=2)
    except Exception as e:
        logging.warning(f"Error writing calibrated TV config files: {e}")

    if os.path.exists(DB_PATH) and CURRENT_MODE != 'music':
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE settings SET config=? WHERE type='ledConfig'", (json.dumps(tv_cfg),))
            c.execute("UPDATE settings SET config=? WHERE type='leds'", (json.dumps(tv_leds),))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.debug(f"Could not update hyperhdr.db directly: {e}")

    if img_pil is not None:
        save_auto_crop_preview(img_pil, quad_info, tv_leds)

    # Strict mode isolation: never overwrite Music mode's full-perimeter layout
    if CURRENT_MODE != 'music':
        apply_hyperhdr_layout(CURRENT_MODE)

    now = time.time()
    AUTO_CROP_STATE['last_applied_ts'] = round(now, 1)
    AUTO_CROP_STATE['corners_px'] = quad_info['corners_px']
    AUTO_CROP_STATE['corners_pct'] = quad_info['corners_pct']
    AUTO_CROP_STATE['inliers'] = quad_info['inliers']
    return tv_leds, tv_cfg


def run_auto_crop_check(force=False):
    """
    Runs a single TV boundary detection check.
    - If force=True (on-demand via /auto_crop): wakes camera if needed and applies immediately.
    - If force=False (background monitor): only applies if camera moved by >= AUTO_CROP_SHIFT_THRESHOLD_PX
      and a second confirmation frame 1.5s later agrees within 3.5px.
    """
    if not AUTO_CROP_LOCK.acquire(blocking=force):
        return {'status': 'busy', 'applied': False, 'state': AUTO_CROP_STATE}

    try:
        now = time.time()
        AUTO_CROP_STATE['last_check_ts'] = round(now, 1)

        img1 = capture_hyperhdr_frame(force_wake=force)
        if img1 is None:
            AUTO_CROP_STATE['status'] = 'grabber_idle'
            return {'status': 'grabber_idle', 'applied': False, 'state': AUTO_CROP_STATE}

        quad1 = detect_tv_quad(img1)
        if quad1 is None:
            AUTO_CROP_STATE['status'] = 'low_contrast_or_dark'
            return {'status': 'low_contrast_or_dark', 'applied': False, 'state': AUTO_CROP_STATE}

        # Determine active corners in pixels for shift calculation
        cur_px = AUTO_CROP_STATE.get('corners_px')
        if not cur_px:
            _, tv_cfg = load_tv_layout()
            classic = tv_cfg.get('classic', {}) if tv_cfg else {}
            W, H = quad1['resolution']
            if 'corners_px' in classic:
                cur_px = classic['corners_px']
            else:
                cur_px = {
                    'tl': [classic.get('ptlh', 30) * W / 100.0, classic.get('ptlv', 21) * H / 100.0],
                    'tr': [classic.get('ptrh', 100) * W / 100.0, classic.get('ptrv', 27) * H / 100.0],
                    'bl': [classic.get('pblh', 30) * W / 100.0, classic.get('pblv', 71) * H / 100.0],
                    'br': [classic.get('pbrh', 99) * W / 100.0, classic.get('pbrv', 93) * H / 100.0],
                }

        shift_px = _compute_corner_shift_px(cur_px, quad1['corners_px'])
        AUTO_CROP_STATE['last_shift_px'] = round(shift_px, 2)
        AUTO_CROP_STATE['inliers'] = quad1['inliers']

        if force:
            leds, _ = apply_detected_tv_quad(quad1, img_pil=img1)
            AUTO_CROP_STATE['status'] = 'calibrated_forced'
            logging.info(
                f"Auto-crop (on-demand): calibrated TV boundary -> {quad1['corners_pct']} "
                f"(px={quad1['corners_px']}, shift={shift_px:.1f}px, inliers={quad1['inliers']})"
            )
            return {
                'status': 'ok',
                'applied': True,
                'shift_px': round(shift_px, 2),
                'corners_px': quad1['corners_px'],
                'corners_pct': quad1['corners_pct'],
                'inliers': quad1['inliers'],
                'led_count': len(leds),
                'preview_url': '/auto_crop_preview'
            }

        if shift_px < AUTO_CROP_SHIFT_THRESHOLD_PX:
            AUTO_CROP_STATE['status'] = 'locked'
            if not os.path.exists(AUTO_CROP_PREVIEW_PATH):
                tv_leds, _ = load_tv_layout()
                save_auto_crop_preview(img1, quad1, tv_leds)
            return {
                'status': 'locked',
                'applied': False,
                'shift_px': round(shift_px, 2),
                'corners_px': AUTO_CROP_STATE.get('corners_px', quad1['corners_px']),
                'corners_pct': AUTO_CROP_STATE.get('corners_pct', quad1['corners_pct']),
                'inliers': quad1['inliers']
            }

        # Camera movement >= threshold detected! Wait 1.5s and verify stability with a 2nd frame
        logging.info(f"Auto-crop: potential camera shift of {shift_px:.1f}px detected; verifying stability...")
        time.sleep(1.5)
        img2 = capture_hyperhdr_frame(force_wake=False)
        if img2 is None:
            return {'status': 'verify_grab_failed', 'applied': False, 'state': AUTO_CROP_STATE}
        quad2 = detect_tv_quad(img2)
        if quad2 is None:
            return {'status': 'verify_detect_failed', 'applied': False, 'state': AUTO_CROP_STATE}

        consistency_px = _compute_corner_shift_px(quad1['corners_px'], quad2['corners_px'])
        shift2_px = _compute_corner_shift_px(cur_px, quad2['corners_px'])
        if consistency_px <= 3.5 and shift2_px >= AUTO_CROP_SHIFT_THRESHOLD_PX:
            leds, _ = apply_detected_tv_quad(quad2, img_pil=img2)
            AUTO_CROP_STATE['status'] = 'recalibrated_on_move'
            AUTO_CROP_STATE['last_shift_px'] = round(shift2_px, 2)
            logging.info(
                f"Auto-crop: confirmed camera movement ({shift2_px:.1f}px, consistency={consistency_px:.1f}px) -> "
                f"updated TV boundary to {quad2['corners_pct']} (px={quad2['corners_px']})"
            )
            return {
                'status': 'recalibrated',
                'applied': True,
                'shift_px': round(shift2_px, 2),
                'corners_px': quad2['corners_px'],
                'corners_pct': quad2['corners_pct'],
                'inliers': quad2['inliers'],
                'led_count': len(leds)
            }
        else:
            AUTO_CROP_STATE['status'] = 'transient_ignored'
            logging.info(f"Auto-crop: ignored transient movement (consistency={consistency_px:.1f}px, shift2={shift2_px:.1f}px)")
            return {'status': 'transient_ignored', 'applied': False, 'state': AUTO_CROP_STATE}
    finally:
        AUTO_CROP_LOCK.release()


def auto_crop_monitor_loop():
    """
    Background daemon thread that continuously monitors the TV boundary every
    AUTO_CROP_INTERVAL_SEC seconds when in TV or Screensaver mode and automatically
    re-crops if the camera moves.
    """
    time.sleep(2.5)
    while True:
        try:
            if AUTO_CROP_ENABLED and CURRENT_MODE in ('tv', 'screensaver'):
                run_auto_crop_check(force=False)
        except Exception as e:
            logging.debug(f"Auto-crop monitor loop error: {e}")
        time.sleep(AUTO_CROP_INTERVAL_SEC)


def apply_hyperhdr_layout(target):
    """
    Applies layout, smoothing, and hardware settings to HyperHDR dynamically via JSON-RPC.
    - For 'screensaver' (when SCREENSAVER_BORDER_MODE is False): sets 5 Hz smoothing to reduce HyperHDR CPU & UDP rate by 90%.
    - For 'tv', 'music', or 'screensaver' with SCREENSAVER_BORDER_MODE=True: sets 20 Hz smoothing (150ms).
    """
    global ACTIVE_LAYOUT
    is_screensaver_solid = (
        (target == 'screensaver' or CURRENT_MODE == 'screensaver')
        and not SCREENSAVER_BORDER_MODE
    )
    config_update = {
        'device': DEV_DDP_CONFIG,
        'smoothing': SMOOTHING_SCREENSAVER if is_screensaver_solid else SMOOTHING_TV_MUSIC
    }

    if target == 'music':
        leds = load_music_layout()
        config_update['leds'] = leds
        config_update['ledConfig'] = MUSIC_LED_CONFIG
        layout_name = 'music'
    else:
        leds, tv_cfg = load_tv_layout()
        config_update['leds'] = leds
        config_update['ledConfig'] = tv_cfg
        layout_name = 'tv'

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(('127.0.0.1', HYPERHDR_JSONRPC_PORT))

        auth_cmd = json.dumps({'command': 'authorize', 'subcommand': 'login', 'password': 'hyperhdr', 'tan': 1})
        s.sendall(auth_cmd.encode('utf-8') + b'\n')
        resp1 = json.loads(s.recv(4096).decode())
        if not resp1.get('success'):
            logging.warning(f"HyperHDR login failed: {resp1}")
            s.close()
            return False

        cfg_cmd = json.dumps({'command': 'config', 'subcommand': 'setconfig', 'config': config_update, 'tan': 2})
        s.sendall(cfg_cmd.encode('utf-8') + b'\n')
        data = b''
        while True:
            chunk = s.recv(4096)
            data += chunk
            if b'\n' in chunk or len(chunk) == 0:
                break
        resp2 = json.loads(data.decode())

        try:
            if target == 'music':
                eff_cmd = json.dumps({'command': 'effect', 'effect': {'name': 'Music: quatro for LED strip (MULTI COLOR)'}, 'priority': 100, 'origin': 'Proxy', 'tan': 3})
                s.sendall(eff_cmd.encode('utf-8') + b'\n')
                s.recv(4096)
            else:
                clear_cmd = json.dumps({'command': 'clear', 'priority': 100, 'tan': 3})
                s.sendall(clear_cmd.encode('utf-8') + b'\n')
                s.recv(4096)
        except Exception as e_eff:
            logging.warning(f"HyperHDR effect trigger/clear error: {e_eff}")

        s.close()

        if resp2.get('success'):
            ACTIVE_LAYOUT = layout_name
            hz = config_update['smoothing']['updateFrequency']
            logging.info(f"HyperHDR layout switched to '{layout_name.upper()}' ({len(leds)} LEDs, {hz}Hz smoothing). DDP active.")
            return True
        else:
            logging.warning(f"HyperHDR setconfig returned error: {resp2}")
    except Exception as e:
        logging.warning(f"Could not update HyperHDR layout to {target}: {e}")
    return False


def get_mic_sensitivity():
    try:
        res = subprocess.run(["amixer", "-c", "2", "sget", "Mic"], capture_output=True, text=True, check=True)
        import re
        m = re.search(r'\[(\d+)%\]', res.stdout)
        if m:
            return int(m.group(1))
    except Exception as e:
        logging.warning(f"Could not read mic sensitivity: {e}")
    return 100


def set_mic_sensitivity(val):
    try:
        pct = max(10, min(100, int(float(val))))
        subprocess.run(["amixer", "-c", "2", "sset", "Mic", f"{pct}%"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logging.info(f"Set C310 mic capture volume to {pct}%")
        return pct
    except Exception as e:
        logging.error(f"Could not set mic sensitivity: {e}")
        return 100


class ModeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global CURRENT_MODE, PRIMARY_COLOR_MODE, SCREENSAVER_BORDER_MODE, LAST_WLED_SCREENSAVER_RGB
        path = urlparse(self.path).path.strip('/').lower()
        if path == 'screensaver':
            CURRENT_MODE = 'screensaver'
            LAST_WLED_SCREENSAVER_RGB = None
            if SCREENSAVER_BORDER_MODE:
                ensure_wled_live_ready()
            apply_hyperhdr_layout('screensaver')
            threading.Timer(1.8, tune_camera).start()
            if AUTO_CROP_ENABLED:
                threading.Timer(2.5, lambda: run_auto_crop_check(force=False)).start()
            try:
                with open(MODE_FILE, 'w') as mf:
                    mf.write('screensaver\n')
            except Exception:
                pass
            logging.info(f"HTTP Command from {self.client_address}: Switched proxy mode to SCREENSAVER (screensaver_border={SCREENSAVER_BORDER_MODE})")
            self._reply(200, {
                "status": "ok",
                "mode": "screensaver",
                "screensaver_border_mode": SCREENSAVER_BORDER_MODE,
                "layout": ACTIVE_LAYOUT
            })
        elif path == 'tv':
            CURRENT_MODE = 'tv'
            ensure_wled_live_ready()
            apply_hyperhdr_layout('tv')
            threading.Timer(1.8, tune_camera).start()
            if AUTO_CROP_ENABLED:
                threading.Timer(2.5, lambda: run_auto_crop_check(force=False)).start()
            try:
                with open(MODE_FILE, 'w') as mf:
                    mf.write('tv\n')
            except Exception:
                pass
            logging.info(f"HTTP Command from {self.client_address}: Switched proxy mode to TV (All 4 Edges TV Boundary, primary_mode={PRIMARY_COLOR_MODE})")
            self._reply(200, {"status": "ok", "mode": "tv", "layout": ACTIVE_LAYOUT, "primary_mode": PRIMARY_COLOR_MODE})
        elif path == 'music':
            CURRENT_MODE = 'music'
            ensure_wled_live_ready()
            apply_hyperhdr_layout('music')
            try:
                with open(MODE_FILE, 'w') as mf:
                    mf.write('music\n')
            except Exception:
                pass
            logging.info(f"HTTP Command from {self.client_address}: Switched proxy mode to MUSIC (Full Perimeter)")
            self._reply(200, {"status": "ok", "mode": "music", "layout": ACTIVE_LAYOUT})
        elif path in ('idle', 'off'):
            CURRENT_MODE = 'idle'
            LAST_WLED_SCREENSAVER_RGB = None
            apply_hyperhdr_layout('tv')
            try:
                with open(MODE_FILE, 'w') as mf:
                    mf.write('idle\n')
            except Exception:
                pass
            logging.info(f"HTTP Command from {self.client_address}: Switched proxy mode to IDLE (Packets Dropped, WLED Free)")
            self._reply(200, {"status": "ok", "mode": "idle", "layout": ACTIVE_LAYOUT})
        elif path == 'auto_crop_preview':
            if not os.path.exists(AUTO_CROP_PREVIEW_PATH):
                run_auto_crop_check(force=True)
            if os.path.exists(AUTO_CROP_PREVIEW_PATH):
                try:
                    with open(AUTO_CROP_PREVIEW_PATH, 'rb') as pf:
                        img_bytes = pf.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(img_bytes)
                    return
                except Exception as e:
                    self._reply(500, {"error": str(e)})
                    return
            self._reply(404, {"error": "Preview image not available"})
        elif path.startswith('auto_crop') or path.startswith('autocrop'):
            qs = parse_qs(urlparse(self.path).query) if '?' in self.path else {}
            enable_val = None
            if 'enable' in qs:
                enable_val = qs['enable'][0].strip().lower()
            elif 'enabled' in qs:
                enable_val = qs['enabled'][0].strip().lower()

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                try:
                    body = json.loads(self.rfile.read(content_length).decode())
                    if isinstance(body, dict) and 'enable' in body:
                        enable_val = str(body['enable']).strip().lower()
                except Exception:
                    pass

            if enable_val is not None:
                if enable_val in ('off', 'false', '0', 'no'):
                    save_auto_crop_enabled(False)
                elif enable_val in ('on', 'true', '1', 'yes'):
                    save_auto_crop_enabled(True)
                elif enable_val == 'toggle':
                    save_auto_crop_enabled(not AUTO_CROP_ENABLED)
                if 'force' not in qs and 'run' not in qs:
                    self._reply(200, {"status": "ok", "auto_crop": AUTO_CROP_STATE})
                    return

            res = run_auto_crop_check(force=True)
            self._reply(200, res)
        elif path.startswith('screensaver_border'):
            val = None
            if '?' in self.path:
                qs = parse_qs(urlparse(self.path).query)
                if 'value' in qs:
                    val = qs['value'][0].strip().lower()
                elif 'val' in qs:
                    val = qs['val'][0].strip().lower()
            elif '/' in path:
                parts = path.split('/')
                if len(parts) > 1:
                    val = parts[1].strip().lower()

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                try:
                    body = json.loads(self.rfile.read(content_length).decode())
                    if isinstance(body, dict) and 'screensaver_border_mode' in body:
                        val = str(body['screensaver_border_mode']).strip().lower()
                    elif isinstance(body, dict) and 'value' in body:
                        val = str(body['value']).strip().lower()
                except Exception:
                    pass

            if val is not None:
                if val in ('off', 'false', '0', 'no'):
                    save_screensaver_border_mode(False)
                elif val in ('on', 'true', '1', 'yes'):
                    save_screensaver_border_mode(True)
                elif val == 'toggle':
                    save_screensaver_border_mode(not SCREENSAVER_BORDER_MODE)

                # Only apply live to WLED/HyperHDR if currently in screensaver mode (never disturb TV or Music mode!)
                if CURRENT_MODE == 'screensaver':
                    LAST_WLED_SCREENSAVER_RGB = None
                    if SCREENSAVER_BORDER_MODE:
                        ensure_wled_live_ready()
                    apply_hyperhdr_layout('screensaver')

                self._reply(200, {
                    "status": "ok",
                    "screensaver_border_mode": SCREENSAVER_BORDER_MODE,
                    "current_mode": CURRENT_MODE
                })
                return

            self._reply(200, {"screensaver_border_mode": SCREENSAVER_BORDER_MODE, "current_mode": CURRENT_MODE})
        elif path == 'save_tv_boundary':
            content_length = int(self.headers.get('Content-Length', 0))
            leds_data = None
            if content_length > 0:
                try:
                    leds_data = json.loads(self.rfile.read(content_length).decode())
                except Exception:
                    pass
            if not leds_data and os.path.exists(DB_PATH) and CURRENT_MODE != 'music':
                try:
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT config FROM settings WHERE type='leds'")
                    row = c.fetchone()
                    conn.close()
                    if row:
                        leds_data = json.loads(row[0])
                except Exception:
                    pass
            if leds_data and isinstance(leds_data, list) and len(leds_data) == 359:
                ok = save_tv_layout(leds_data)
                if ok:
                    if CURRENT_MODE != 'music':
                        apply_hyperhdr_layout(CURRENT_MODE)
                    self._reply(200, {"status": "ok", "message": "Saved TV boundary", "led_count": len(leds_data)})
                    return
            self._reply(400, {"error": "Could not extract valid TV trapezoid leds data"})
        elif path.startswith('brightness'):
            val = None
            if '?' in self.path:
                qs = parse_qs(urlparse(self.path).query)
                if 'value' in qs:
                    val = qs['value'][0]
                elif 'val' in qs:
                    val = qs['val'][0]
            elif '/' in path:
                parts = path.split('/')
                if len(parts) > 1:
                    val = parts[1]

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                try:
                    body = json.loads(self.rfile.read(content_length).decode())
                    if isinstance(body, dict) and 'brightness' in body:
                        val = body['brightness']
                    elif isinstance(body, (int, float)):
                        val = body
                except Exception:
                    pass

            if val is not None:
                save_brightness(val)
                LAST_WLED_SCREENSAVER_RGB = None
                logging.info(f"HTTP Command: Set screensaver brightness to {CURRENT_BRIGHTNESS}%")
                self._reply(200, {"status": "ok", "brightness": CURRENT_BRIGHTNESS})
                return

            self._reply(200, {"brightness": CURRENT_BRIGHTNESS})
        elif path.startswith('balance'):
            top = None
            sides = None
            bottom = None
            if '?' in self.path:
                qs = parse_qs(urlparse(self.path).query)
                if 'top' in qs:
                    top = qs['top'][0]
                if 'sides' in qs:
                    sides = qs['sides'][0]
                if 'bottom' in qs:
                    bottom = qs['bottom'][0]

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                try:
                    body = json.loads(self.rfile.read(content_length).decode())
                    if isinstance(body, dict):
                        top = body.get('top', top)
                        sides = body.get('sides', sides)
                        bottom = body.get('bottom', bottom)
                except Exception:
                    pass

            if top is not None or sides is not None or bottom is not None:
                save_balance(top=top, sides=sides, bottom=bottom)
                self._reply(200, {"status": "ok", "balance": EDGE_BALANCE})
                return

            self._reply(200, EDGE_BALANCE)
        elif path.startswith('mic') or path.startswith('sensitivity'):
            val = None
            if '?' in self.path:
                qs = parse_qs(urlparse(self.path).query)
                if 'value' in qs:
                    val = qs['value'][0]
                elif 'val' in qs:
                    val = qs['val'][0]
            elif '/' in path:
                parts = path.split('/')
                if len(parts) > 1:
                    val = parts[1]

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                try:
                    body = json.loads(self.rfile.read(content_length).decode())
                    if isinstance(body, dict) and 'value' in body:
                        val = body['value']
                    elif isinstance(body, (int, float)):
                        val = body
                except Exception:
                    pass

            if val is not None:
                new_pct = set_mic_sensitivity(val)
                self._reply(200, {"status": "ok", "mic_sensitivity": new_pct})
                return

            self._reply(200, {"mic_sensitivity": get_mic_sensitivity()})
        elif path.startswith('primary'):
            val = None
            if '?' in self.path:
                qs = parse_qs(urlparse(self.path).query)
                if 'value' in qs:
                    val = qs['value'][0].strip().lower()
                elif 'val' in qs:
                    val = qs['val'][0].strip().lower()
            elif '/' in path:
                parts = path.split('/')
                if len(parts) > 1:
                    val = parts[1].strip().lower()

            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                try:
                    body = json.loads(self.rfile.read(content_length).decode())
                    if isinstance(body, dict) and 'primary_mode' in body:
                        val = str(body['primary_mode']).strip().lower()
                    elif isinstance(body, dict) and 'value' in body:
                        val = str(body['value']).strip().lower()
                except Exception:
                    pass

            if val is not None:
                if val in ('off', 'false', '0', 'no'):
                    save_primary_mode(False)
                elif val in ('on', 'true', '1', 'yes'):
                    save_primary_mode(True)
                elif val == 'toggle':
                    save_primary_mode(not PRIMARY_COLOR_MODE)
                LAT_STATE['primary_mode'] = PRIMARY_COLOR_MODE
                self._reply(200, {"status": "ok", "primary_mode": PRIMARY_COLOR_MODE})
                return

            self._reply(200, {"primary_mode": PRIMARY_COLOR_MODE})
        elif path == 'restore':
            try:
                golden_cfg = "/home/pi/golden_backup/.hyperhdr_tv_config.json"
                if os.path.exists(golden_cfg):
                    shutil.copy2(golden_cfg, TV_CONFIG_FILE)
                save_primary_mode(False)
                LAT_STATE['primary_mode'] = False
                save_balance(top=1.0, sides=1.0, bottom=1.0)
                ensure_wled_live_ready()
                apply_hyperhdr_layout('tv')
                tune_camera()
                CURRENT_MODE = 'tv'
                try:
                    with open(MODE_FILE, 'w') as mf:
                        mf.write('tv\n')
                except Exception:
                    pass
                logging.info(f"HTTP Command from {self.client_address}: Golden state restored successfully")
                self._reply(200, {
                    "status": "ok",
                    "message": "Golden state restored: TV boundary, natural colors, camera white balance & exposure locked, and balance reset.",
                    "mode": CURRENT_MODE,
                    "primary_mode": PRIMARY_COLOR_MODE,
                    "balance": EDGE_BALANCE
                })
            except Exception as e:
                logging.error(f"Restore error: {e}")
                self._reply(500, {"error": str(e)})
        elif path == 'status':
            d_debug = LAT_STATE.get('debug', {})
            self._reply(200, {
                "mode": CURRENT_MODE,
                "screensaver_border_mode": SCREENSAVER_BORDER_MODE,
                "active_layout": ACTIVE_LAYOUT,
                "primary_mode": PRIMARY_COLOR_MODE,
                "latched_color": LAT_STATE.get('latched_color', 'NONE'),
                "dominant_camera_color": d_debug.get('dominant_camera_color', 'NONE'),
                "camera_votes_summary": d_debug.get('camera_votes_summary', 'None'),
                "votes": d_debug.get('votes', {}),
                "output_rgb": LAT_STATE.get('output_rgb', [0, 0, 0]),
                "brightness": CURRENT_BRIGHTNESS,
                "balance": EDGE_BALANCE,
                "mic_sensitivity": get_mic_sensitivity(),
                "auto_crop": AUTO_CROP_STATE,
                "music_debug": LAT_STATE.get('music_debug', {}),
                "tv_debug": LAT_STATE.get('tv_debug', {}),
                "fps": LAT_STATE.get('fps', {})
            })
        elif path == 'debug':
            d = LAT_STATE.get('debug', {})
            d['brightness'] = CURRENT_BRIGHTNESS
            d['balance'] = EDGE_BALANCE
            d['active_layout'] = ACTIVE_LAYOUT
            d['primary_mode'] = PRIMARY_COLOR_MODE
            d['screensaver_border_mode'] = SCREENSAVER_BORDER_MODE
            d['auto_crop'] = AUTO_CROP_STATE
            self._reply(200, d)
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        self.do_GET()

    def _reply(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def log_message(self, format, *args):
        pass


def start_http_server():
    try:
        server = HTTPServer(('0.0.0.0', HTTP_PORT), ModeHandler)
        logging.info(f"Mode control HTTP server running on port {HTTP_PORT}")
        server.serve_forever()
    except Exception as e:
        logging.error(f"HTTP server error: {e}")


def tune_camera():
    """
    Locks camera exposure, gain, and white balance once on camera init
    (Manual Exposure=1, exposure=166, gain=64, saturation=65, WB=6200K)
    so dark/black TV screens never boost gain to 192 and white TV screens
    are not tinted blue.
    Note: UVC firmware requires setting auto_exposure=1 before gain=64.
    """
    try:
        subprocess.run(
            [
                "v4l2-ctl", "-d", "/dev/video0",
                "-c", "auto_exposure=1",
                "-c", "white_balance_automatic=0"
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(0.15)
        subprocess.run(
            [
                "v4l2-ctl", "-d", "/dev/video0",
                "-c", "exposure_time_absolute=166",
                "-c", "gain=64",
                "-c", "saturation=65",
                "-c", "white_balance_temperature=6200"
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        logging.info("Camera locked: auto_exposure=1 (Manual), exposure=166, gain=64, saturation=65, WB=6200K.")
    except Exception as e:
        logging.warning(f"Could not lock camera settings: {e}")


def generate_luts():
    logging.info("Building 64x64x64 quantization LUTs...")
    t0 = time.time()
    lut_rgb = np.zeros((64, 64, 64, 3), dtype=np.uint8)
    lut_id = np.zeros((64, 64, 64), dtype=np.uint8)
    lut_base_rgb = np.zeros((64, 64, 64, 3), dtype=np.uint8)

    for ri in range(64):
        r = ri * 4 + 2
        for gi in range(64):
            g = gi * 4 + 2
            for bi in range(64):
                b = bi * 4 + 2
                max_c = max(r, g, b)
                min_c = min(r, g, b)

                if max_c < 45 or (max_c < 65 and (max_c - min_c) < 20):
                    lut_rgb[ri, gi, bi] = (0, 0, 0)
                    lut_id[ri, gi, bi] = PALETTE_ID['BLACK']
                    lut_base_rgb[ri, gi, bi] = (0, 0, 0)
                    continue

                h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                hue = h * 360.0

                # Recognize neutral, cool-white, and warm-white TV scenes as WHITE
                is_bright_white = (v > 0.50 and s < 0.34 and min_c >= 100)
                if s < 0.25 or is_bright_white:
                    if v > 0.38:
                        c_name = 'WHITE'
                    else:
                        lut_rgb[ri, gi, bi] = (0, 0, 0)
                        lut_id[ri, gi, bi] = PALETTE_ID['BLACK']
                        lut_base_rgb[ri, gi, bi] = (0, 0, 0)
                        continue
                elif 15 <= hue < 42:
                    c_name = 'ORANGE'
                elif 42 <= hue < 68:
                    c_name = 'YELLOW'
                elif 68 <= hue < 165:
                    c_name = 'GREEN'
                elif 165 <= hue < 205:
                    c_name = 'CYAN'
                elif 205 <= hue < 265:
                    c_name = 'BLUE'
                elif 265 <= hue < 340:
                    c_name = 'MAGENTA'
                else:
                    c_name = 'RED'

                base = PALETTE_RGB[c_name]
                factor = min(1.0, (v ** 0.8) * 1.15)
                lut_rgb[ri, gi, bi] = (
                    int(base[0] * factor),
                    int(base[1] * factor),
                    int(base[2] * factor)
                )
                lut_id[ri, gi, bi] = PALETTE_ID[c_name]
                lut_base_rgb[ri, gi, bi] = base

    elapsed = time.time() - t0
    logging.info(f"LUTs generated in {elapsed:.2f}s.")
    return lut_rgb, lut_id, lut_base_rgb


def load_screensaver_mask(num_leds=359):
    """
    Excludes right-edge LEDs (0..63) from dominant-color voting because the right
    camera angle (ptrh=83) catches WLED wall reflection, which otherwise creates
    a self-sustaining BLUE feedback loop. All 359 LEDs still display the output color.
    """
    mask = np.ones(num_leds, dtype=bool)
    mask[0:64] = False
    return mask


def update_live_camera_debug(rgb, idx, lut_id, vote_mask, latched_color, candidate_color, candidate_count):
    num_leds = len(rgb)
    all_ids = lut_id[idx[:, 0], idx[:, 1], idx[:, 2]]
    voter_ids = all_ids[vote_mask] if len(vote_mask) == num_leds else all_ids
    counts = np.bincount(voter_ids, minlength=len(COLOR_NAMES))
    votes_dict = {COLOR_NAMES[i]: int(counts[i]) for i in range(len(COLOR_NAMES))}

    # Dominant non-black camera color right now
    colored_votes = int(counts[1:].sum())
    if colored_votes < 15:
        dom_cam = 'BLACK'
    else:
        c_nb = counts.copy()
        c_nb[0] = 0
        dom_cam = COLOR_NAMES[int(np.argmax(c_nb))]

    # Build top 4 colors summary string for Home Assistant sensor
    sorted_colors = sorted(
        [(COLOR_NAMES[i], int(counts[i])) for i in range(len(COLOR_NAMES)) if counts[i] > 0],
        key=lambda x: x[1],
        reverse=True
    )
    summary_parts = [f"{name}: {cnt}" for name, cnt in sorted_colors[:4]]
    votes_summary = " | ".join(summary_parts) if summary_parts else "BLACK: 295"

    LAT_STATE['debug'] = {
        'latched_color': latched_color,
        'dominant_camera_color': dom_cam,
        'camera_votes_summary': votes_summary,
        'candidate_color': candidate_color,
        'candidate_count': candidate_count,
        'votes': votes_dict,
        'total_voters': int(len(voter_ids)),
        'avg_rgb': [int(x) for x in rgb.mean(axis=0)] if num_leds > 0 else [],
        'right_avg_rgb': [int(x) for x in rgb[0:64].mean(axis=0)] if num_leds >= 64 else [],
        'top_avg_rgb': [int(x) for x in rgb[64:179].mean(axis=0)] if num_leds >= 179 else [],
        'left_avg_rgb': [int(x) for x in rgb[179:244].mean(axis=0)] if num_leds >= 244 else [],
        'bottom_avg_rgb': [int(x) for x in rgb[244:359].mean(axis=0)] if num_leds >= 359 else [],
        'sample_leds': [
            {
                'idx': i,
                'rgb': [int(x) for x in rgb[i]],
                'color': COLOR_NAMES[all_ids[i]]
            } for i in [20, 50, 100, 130, 150, 170, 200, 230, 260, 300, 340] if i < num_leds
        ]
    }
    return counts, colored_votes


def main():
    global CURRENT_MODE, ACTIVE_LAYOUT, LAST_WLED_SCREENSAVER_RGB
    load_saved_brightness()
    load_saved_balance()
    load_saved_primary_mode()
    load_saved_screensaver_border_mode()
    load_saved_auto_crop()
    LAT_STATE['primary_mode'] = PRIMARY_COLOR_MODE
    tune_camera()

    if os.path.exists(MODE_FILE):
        try:
            with open(MODE_FILE, 'r') as mf:
                m = mf.read().strip().lower()
                if m in ('screensaver', 'tv', 'music', 'idle', 'off'):
                    CURRENT_MODE = 'idle' if m == 'off' else m
        except Exception:
            pass

    load_tv_layout()
    load_music_layout()

    apply_hyperhdr_layout(CURRENT_MODE)
    if CURRENT_MODE in ('tv', 'music') or (CURRENT_MODE == 'screensaver' and SCREENSAVER_BORDER_MODE):
        ensure_wled_live_ready()

    lut_rgb, lut_id, lut_base_rgb = generate_luts()
    vote_mask = load_screensaver_mask()
    logging.info(f"Screensaver voter active on all {vote_mask.sum()} LEDs across TV boundary.")

    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()

    t_crop = threading.Thread(target=auto_crop_monitor_loop, daemon=True)
    t_crop.start()

    rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx_sock.bind((LISTEN_IP, LISTEN_PORT))
    rx_sock.settimeout(1.0)

    tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest_addr = (DEST_IP, DEST_PORT)

    logging.info(f"DDP Palette Proxy active: listening on {LISTEN_IP}:{LISTEN_PORT}, forwarding to {DEST_IP}:{DEST_PORT}")

    latched_color = 'BLACK'
    latched_lut_rgb = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    candidate_color = 'BLACK'
    candidate_lut_rgb = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    candidate_count = 0

    t_last_mode_check = 0.0
    t_last_screensaver_sample = 0.0
    t_last_dom_sample = 0.0
    t_last_fps = time.time()
    t_last_rx = time.time()
    t_last_heal = 0.0
    packet_count = 0
    tx_packet_count = 0

    bottom_smooth_rgb = np.array([0.0, 0.0, 0.0], dtype=np.float32)

    # Rate limiting & WLED Wi-Fi traffic protection:
    # - 20 FPS max (50ms) during active TV/Music/Screensaver-Border changes
    # - 1.5s keepalive when scene is static (WLED live timeout is 2.5s)
    # - 0 DDP packets/sec in Screensaver Dominant mode (uses WLED native Solid + hardware transition)
    MIN_FRAME_INTERVAL = 0.050
    KEEPALIVE_INTERVAL = 1.500
    SCREENSAVER_SAMPLE_INTERVAL = 1.000
    DOM_SAMPLE_INTERVAL = 0.250
    t_last_tx = 0.0
    last_sent_leds = None

    def step_dominant_color(rgb_arr, idx_arr):
        nonlocal latched_color, latched_lut_rgb, candidate_color, candidate_lut_rgb, candidate_count
        counts, colored_votes = update_live_camera_debug(
            rgb_arr, idx_arr, lut_id, vote_mask, latched_color, candidate_color, candidate_count
        )
        if colored_votes < 15:
            target_color_name = 'BLACK'
            dominant_id = 0
            target_lut_rgb = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        else:
            counts_no_black = counts.copy()
            counts_no_black[0] = 0
            dominant_id = int(np.argmax(counts_no_black))
            target_color_name = COLOR_NAMES[dominant_id]
            target_lut_rgb = np.array(PALETTE_RGB[target_color_name], dtype=np.float32)

        current_id = PALETTE_ID.get(latched_color, 0)
        current_votes = int(counts[current_id]) if current_id > 0 else 0
        challenger_votes = int(counts[dominant_id]) if colored_votes >= 15 else 0

        is_significant_change = (
            (latched_color == 'BLACK' and colored_votes >= 20) or
            (target_color_name == 'BLACK' and colored_votes < 15) or
            (challenger_votes > int(current_votes * 1.15) + 5)
        )

        if target_color_name != latched_color and is_significant_change:
            if target_color_name == candidate_color:
                candidate_count += 1
                req_samples = 3 if target_color_name == 'BLACK' else 2
                if candidate_count >= req_samples:
                    latched_color = candidate_color
                    latched_lut_rgb = candidate_lut_rgb.copy()
                    candidate_count = 0
                    logging.info(f"Dominant color change -> {latched_color} (votes: {challenger_votes} vs {current_votes}, colored: {colored_votes})")
            else:
                candidate_color = target_color_name
                candidate_lut_rgb = target_lut_rgb.copy()
                candidate_count = 1
        else:
            candidate_count = 0

        LAT_STATE['latched_color'] = latched_color
        LAT_STATE['debug']['latched_color'] = latched_color
        LAT_STATE['debug']['candidate_color'] = candidate_color
        LAT_STATE['debug']['candidate_count'] = candidate_count

    while True:
        try:
            data, _ = rx_sock.recvfrom(4096)
            if len(data) > 10:
                header = data[:10]
                payload = data[10:]
                num_leds = len(payload) // 3

                rgb = np.frombuffer(payload[:num_leds * 3], dtype=np.uint8).reshape((num_leds, 3))
                idx = rgb >> 2

                now = time.time()
                # If camera stream just woke up after >2.5s idle, re-lock camera exposure/gain once
                if now - t_last_rx > 2.5:
                    threading.Thread(target=tune_camera, daemon=True).start()
                t_last_rx = now
                if now - t_last_mode_check > 1.0:
                    t_last_mode_check = now
                    if os.path.exists(MODE_FILE):
                        try:
                            with open(MODE_FILE, 'r') as mf:
                                m = mf.read().strip().lower()
                                if m in ('screensaver', 'tv', 'music', 'idle', 'off'):
                                    norm_m = 'idle' if m == 'off' else m
                                    if norm_m != CURRENT_MODE:
                                        CURRENT_MODE = norm_m
                                        if norm_m in ('tv', 'music') or (norm_m == 'screensaver' and SCREENSAVER_BORDER_MODE):
                                            ensure_wled_live_ready()
                                        else:
                                            LAST_WLED_SCREENSAVER_RGB = None
                                        apply_hyperhdr_layout(norm_m)
                        except Exception:
                            pass

                packet_count += 1
                if now - t_last_fps >= 5.0:
                    fps_rx = packet_count / (now - t_last_fps)
                    fps_tx = tx_packet_count / (now - t_last_fps)
                    packet_count = 0
                    tx_packet_count = 0
                    t_last_fps = now
                    LAT_STATE['fps'] = {'rx': round(fps_rx, 1), 'tx_wled': round(fps_tx, 1)}

                # ------------------------------------------------------------------
                # MODE 0: IDLE / OFF MODE — Drop packets immediately so WLED is free
                # ------------------------------------------------------------------
                if CURRENT_MODE in ('idle', 'off'):
                    continue

                # ------------------------------------------------------------------
                # MODE 1: MUSIC MODE — Isolated Full-Perimeter Audio Visualizer
                # ------------------------------------------------------------------
                if CURRENT_MODE == "music":
                    if packet_count % 15 == 0:
                        LAT_STATE['music_debug'] = {
                            'max_rgb': [int(x) for x in rgb.max(axis=0)],
                            'avg_rgb': [int(x) for x in rgb.mean(axis=0)],
                            'active_leds': int((rgb > 5).any(axis=1).sum()),
                        }
                    if now - t_last_tx >= MIN_FRAME_INTERVAL:
                        tx_sock.sendto(data, dest_addr)
                        t_last_tx = now
                        tx_packet_count += 1
                    continue

                # ------------------------------------------------------------------
                # MODE 2: SCREENSAVER MODE — Isolated AerialViews Screensaver
                #   Sub-mode A (SCREENSAVER_BORDER_MODE = False):
                #     Whole strip = Single Dominant Color (ignores BLACK), 0.0 FPS UDP
                #   Sub-mode B (SCREENSAVER_BORDER_MODE = True):
                #     Top/Left/Right (0..243) = Image border colors
                #     Bottom (244..358)       = Dominant Color of screen (ignores BLACK)
                # ------------------------------------------------------------------
                if CURRENT_MODE == 'screensaver':
                    if not SCREENSAVER_BORDER_MODE:
                        if now - t_last_screensaver_sample < SCREENSAVER_SAMPLE_INTERVAL:
                            continue
                        t_last_screensaver_sample = now

                        step_dominant_color(rgb, idx)

                        brightness_factor = CURRENT_BRIGHTNESS / 100.0
                        if latched_color == 'BLACK':
                            out_rgb_list = [0, 0, 0]
                        else:
                            scaled = np.clip(latched_lut_rgb * brightness_factor, 0, 255).astype(np.uint8)
                            out_rgb_list = [int(scaled[0]), int(scaled[1]), int(scaled[2])]

                        LAT_STATE['output_rgb'] = out_rgb_list
                        LAT_STATE['brightness'] = CURRENT_BRIGHTNESS

                        if LAST_WLED_SCREENSAVER_RGB != tuple(out_rgb_list):
                            LAST_WLED_SCREENSAVER_RGB = tuple(out_rgb_list)
                            wled_payload = {
                                "on": True,
                                "bri": 255,
                                "transition": 15,
                                "lor": 0,
                                "seg": [{
                                    "id": 0,
                                    "on": True,
                                    "bri": 255,
                                    "fx": 0,
                                    "col": [out_rgb_list, [0, 0, 0], [0, 0, 0]]
                                }]
                            }
                            send_wled_json_state(wled_payload)
                            tx_packet_count += 1
                        continue
                    else:
                        # Screensaver Border Mode: Top/Left/Right = image border colors, Bottom = dominant screen color (ignoring blacks)
                        if now - t_last_dom_sample >= DOM_SAMPLE_INTERVAL:
                            t_last_dom_sample = now
                            step_dominant_color(rgb, idx)

                        all_ids = lut_id[idx[:, 0], idx[:, 1], idx[:, 2]]
                        if PRIMARY_COLOR_MODE:
                            raw_target = lut_rgb[idx[:, 0], idx[:, 1], idx[:, 2]].copy()
                        else:
                            max_c = rgb.max(axis=1)
                            min_c = rgb.min(axis=1)
                            dark_mask = (max_c < 45) | ((max_c < 65) & ((max_c - min_c) < 20))
                            raw_target = rgb.copy()
                            raw_target[dark_mask] = 0
                            white_mask = (all_ids == PALETTE_ID['WHITE']) & (~dark_mask)
                            if np.any(white_mask):
                                w_val = max_c[white_mask]
                                raw_target[white_mask, 0] = w_val
                                raw_target[white_mask, 1] = w_val
                                raw_target[white_mask, 2] = (w_val.astype(np.float32) * 0.92).astype(np.uint8)

                        brightness_factor = CURRENT_BRIGHTNESS / 100.0
                        if latched_color == 'BLACK':
                            target_bottom_rgb = np.array([0.0, 0.0, 0.0], dtype=np.float32)
                        else:
                            target_bottom_rgb = latched_lut_rgb * brightness_factor
                        bottom_smooth_rgb = 0.85 * bottom_smooth_rgb + 0.15 * target_bottom_rgb
                        bottom_u8 = np.clip(bottom_smooth_rgb, 0, 255).astype(np.uint8)
                        if num_leds >= 359:
                            raw_target[244:359] = bottom_u8

                        if (EDGE_BALANCE.get('top', 1.0) == 1.0 and
                            EDGE_BALANCE.get('sides', 1.0) == 1.0 and
                            EDGE_BALANCE.get('bottom', 1.0) == 1.0):
                            final_leds = raw_target
                        else:
                            scale_weights = EDGE_SCALE_ARRAY if len(EDGE_SCALE_ARRAY) == num_leds else np.ones(num_leds, dtype=np.float32)
                            final_leds = np.clip(raw_target * scale_weights[:, None], 0, 255).astype(np.uint8)

                        if packet_count % 15 == 0:
                            LAT_STATE['output_rgb'] = [int(bottom_u8[0]), int(bottom_u8[1]), int(bottom_u8[2])]
                            LAT_STATE['tv_debug'] = {
                                'max_rgb': [int(x) for x in final_leds.max(axis=0)],
                                'avg_rgb': [int(x) for x in final_leds.mean(axis=0)],
                                'active_leds': int((final_leds > 5).any(axis=1).sum()),
                                'right_avg_rgb': [int(x) for x in final_leds[0:64].mean(axis=0)] if num_leds >= 64 else [],
                                'top_avg_rgb': [int(x) for x in final_leds[64:179].mean(axis=0)] if num_leds >= 179 else [],
                                'left_avg_rgb': [int(x) for x in final_leds[179:244].mean(axis=0)] if num_leds >= 244 else [],
                                'bottom_avg_rgb': [int(x) for x in final_leds[244:359].mean(axis=0)] if num_leds >= 359 else [],
                            }

                        diff = 255
                        if last_sent_leds is not None and getattr(last_sent_leds, 'shape', None) == final_leds.shape:
                            diff = int(np.max(np.abs(final_leds.astype(np.int16) - last_sent_leds.astype(np.int16))))

                        if diff >= 3:
                            if now - t_last_tx >= MIN_FRAME_INTERVAL:
                                out_packet = header + final_leds.tobytes()
                                tx_sock.sendto(out_packet, dest_addr)
                                t_last_tx = now
                                last_sent_leds = final_leds.copy()
                                tx_packet_count += 1
                        else:
                            if now - t_last_tx >= KEEPALIVE_INTERVAL:
                                out_packet = header + final_leds.tobytes()
                                tx_sock.sendto(out_packet, dest_addr)
                                t_last_tx = now
                                tx_packet_count += 1
                        continue

                # ------------------------------------------------------------------
                # MODE 3: TV / MOVIE MODE — Pure 4-Edge Ambilight (Right, Top, Left, Bottom)
                #   Completely isolated from Screensaver overrides.
                # ------------------------------------------------------------------
                all_ids = lut_id[idx[:, 0], idx[:, 1], idx[:, 2]]
                if PRIMARY_COLOR_MODE:
                    raw_target = lut_rgb[idx[:, 0], idx[:, 1], idx[:, 2]].copy()
                else:
                    max_c = rgb.max(axis=1)
                    min_c = rgb.min(axis=1)
                    dark_mask = (max_c < 45) | ((max_c < 65) & ((max_c - min_c) < 20))
                    raw_target = rgb.copy()
                    raw_target[dark_mask] = 0
                    white_mask = (all_ids == PALETTE_ID['WHITE']) & (~dark_mask)
                    if np.any(white_mask):
                        w_val = max_c[white_mask]
                        raw_target[white_mask, 0] = w_val
                        raw_target[white_mask, 1] = w_val
                        raw_target[white_mask, 2] = (w_val.astype(np.float32) * 0.92).astype(np.uint8)

                if (EDGE_BALANCE.get('top', 1.0) == 1.0 and
                    EDGE_BALANCE.get('sides', 1.0) == 1.0 and
                    EDGE_BALANCE.get('bottom', 1.0) == 1.0):
                    final_leds = raw_target
                else:
                    scale_weights = EDGE_SCALE_ARRAY if len(EDGE_SCALE_ARRAY) == num_leds else np.ones(num_leds, dtype=np.float32)
                    final_leds = np.clip(raw_target * scale_weights[:, None], 0, 255).astype(np.uint8)

                if packet_count % 15 == 0:
                    step_dominant_color(rgb, idx)
                    LAT_STATE['tv_debug'] = {
                        'max_rgb': [int(x) for x in final_leds.max(axis=0)],
                        'avg_rgb': [int(x) for x in final_leds.mean(axis=0)],
                        'active_leds': int((final_leds > 5).any(axis=1).sum()),
                        'right_avg_rgb': [int(x) for x in final_leds[0:64].mean(axis=0)] if num_leds >= 64 else [],
                        'top_avg_rgb': [int(x) for x in final_leds[64:179].mean(axis=0)] if num_leds >= 179 else [],
                        'left_avg_rgb': [int(x) for x in final_leds[179:244].mean(axis=0)] if num_leds >= 244 else [],
                        'bottom_avg_rgb': [int(x) for x in final_leds[244:359].mean(axis=0)] if num_leds >= 359 else [],
                        'sample_leds': [
                            {
                                'idx': i,
                                'rgb': [int(x) for x in final_leds[i]],
                                'color': COLOR_NAMES[all_ids[i]]
                            } for i in [20, 50, 100, 130, 150, 170, 200, 230, 260, 300, 340] if i < num_leds
                        ]
                    }

                diff = 255
                if last_sent_leds is not None and getattr(last_sent_leds, 'shape', None) == final_leds.shape:
                    diff = int(np.max(np.abs(final_leds.astype(np.int16) - last_sent_leds.astype(np.int16))))

                if diff >= 3:
                    # Dynamic changing scene: stream at max 20 FPS
                    if now - t_last_tx >= MIN_FRAME_INTERVAL:
                        out_packet = header + final_leds.tobytes()
                        tx_sock.sendto(out_packet, dest_addr)
                        t_last_tx = now
                        last_sent_leds = final_leds.copy()
                        tx_packet_count += 1
                else:
                    # Static video / paused scene: 1 packet every 1.5s to maintain WLED live mode (2.5s timeout)
                    if now - t_last_tx >= KEEPALIVE_INTERVAL:
                        out_packet = header + final_leds.tobytes()
                        tx_sock.sendto(out_packet, dest_addr)
                        t_last_tx = now
                        tx_packet_count += 1

        except socket.timeout:
            now = time.time()
            if now - t_last_fps >= 5.0:
                fps_tx = tx_packet_count / (now - t_last_fps)
                packet_count = 0
                tx_packet_count = 0
                t_last_fps = now
                LAT_STATE['fps'] = {'rx': 0.0, 'tx_wled': round(fps_tx, 1)}

            if CURRENT_MODE not in ('idle', 'off') and (now - t_last_rx > 4.0) and (now - t_last_heal > 8.0):
                t_last_heal = now
                logging.warning(f"No DDP packets received from HyperHDR for >4s in {CURRENT_MODE} mode. Auto-healing DDP output...")
                apply_hyperhdr_layout(CURRENT_MODE)
            continue
        except Exception as e:
            logging.error(f"Proxy error: {e}")
            time.sleep(0.01)


if __name__ == '__main__':
    main()
