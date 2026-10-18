# HyperHDR + WLED Ambilight Mode Isolation & Architecture Guide

> **IMPORTANT FOR FUTURE AGENTS**:
> The Ambilight system on **Raspberry Pi 5 (`192.168.87.2`)** and **Home Assistant (`192.168.87.112`)** has **three strictly isolated active modes** (`tv`, `music`, `screensaver`) plus `idle`.
> **Never mix or share mode overrides across modes.** Changes to Screensaver behavior must ONLY execute inside `CURRENT_MODE == 'screensaver'`, changes to TV behavior must ONLY execute inside `CURRENT_MODE == 'tv'`, and changes to Music behavior must ONLY execute inside `CURRENT_MODE == 'music'`.

---

## 1. Hardware & Network Topology
- **Raspberry Pi 5 (`192.168.87.2`)**:
  - Runs `hyperhdr@pi.service` (USB camera `/dev/video0` + ALSA audio capture on card 2).
  - HyperHDR outputs UDP DDP packets to `127.0.0.1:5048`.
  - Runs `hyperhdr-palette-proxy.service` (`/home/pi/hyperhdr_palette_proxy.py`, Golden Backup at `/home/pi/golden_backup/hyperhdr_palette_proxy.py`), which listens on UDP `127.0.0.1:5048`, processes frames according to the active mode, and forwards to WLED (`192.168.87.114:4048` UDP or `/json/state` HTTP).
  - Control HTTP API on port `5049`.
- **WLED Controller (`192.168.87.114`)**:
  - 359 LEDs around the TV perimeter:
    - **Right border**: `0..63` (64 LEDs)
    - **Top border**: `64..178` (115 LEDs)
    - **Left border**: `179..243` (65 LEDs)
    - **Bottom border**: `244..358` (115 LEDs)
- **Home Assistant (`192.168.87.112`)**:
  - Docker container `homeassistant` with config mounted at `/homeassistant_data/`.
  - Note: When editing `/homeassistant_data/.storage/lovelace.dashboard_l`, always `docker stop homeassistant` before copying the file, then `docker start homeassistant`.

---

## 2. Camera Settings (Locked Once at Startup / Stream Wake)
- **Function**: `tune_camera()` in `hyperhdr_palette_proxy.py`.
- **Settings**:
  - `auto_exposure=1` (Manual mode) and `white_balance_automatic=0` (must be applied 0.15s before `gain=64` due to UVC firmware quirk on `046d:081b`).
  - `exposure_time_absolute=166`
  - `gain=64`
  - `saturation=65`
  - `white_balance_temperature=6200`
- **Do NOT** run a periodic 30s `v4l2-ctl` reset loop during active streaming.

---

## 3. Strict Mode Isolation

### Mode A: TV / Movie Mode (`CURRENT_MODE == 'tv'`)
- **Endpoint**: `POST http://192.168.87.2:5049/tv`
- **Home Assistant Script**: `script.set_tv_video_mode`
- **Layout & Smoothing**: Calibrated TV trapezoid (`ACTIVE_LAYOUT = 'tv'`), `20 Hz` (`150 ms`) smoothing.
- **Behavior**:
  - **All 4 edges (`0..358`: Right, Top, Left, and Bottom)** track the TV screen borders naturally.
  - **Black-Level Noise Gate**: Dark pixels (`max_c < 45` or `max_c < 65` with `max_c - min_c < 20`) are gated to `[0, 0, 0]` so black TV screens turn the LEDs completely off instead of glowing dim blue.
  - **White Neutralization**: Pixels classified as `WHITE` in the LUT have their slight camera blue cast neutralized (`[v, v, 0.92*v]`).
  - **Optional Primary Color Mode**: Controlled ONLY by `/primary?value=on|off` (`input_boolean.tv_primary_color_mode`, default `off`).
  - **Isolation Rule**: Never override the bottom edge (`244..358`) with a solid dominant color in `tv` mode, and never route `set_tv_video_mode` to `screensaver`.

### Mode B: Music Mode (`CURRENT_MODE == 'music'`)
- **Endpoint**: `POST http://192.168.87.2:5049/music`
- **Home Assistant Script**: `script.set_music_mode`
- **Layout & Smoothing**: Full Perimeter (`0.0..1.0`, `ACTIVE_LAYOUT = 'music'`), `20 Hz` (`150 ms`) smoothing.
- **Behavior**:
  - Direct DDP passthrough from HyperHDR audio visualizer effects to WLED (rate-limited to 20 FPS).
  - Controlled by `input_number.music_mic_sensitivity` (`/mic?value=...`) and `input_select.music_visualizer_style`.
  - **Isolation Rule**: No camera color gating, no LUT quantization, and no screensaver overrides ever touch `music` mode.

### Mode C: Screensaver Mode (`CURRENT_MODE == 'screensaver'`)
- **Endpoint**: `POST http://192.168.87.2:5049/screensaver`
- **Home Assistant Script**: `script.set_aerialviews_ambient_mode`
- **Sub-mode Toggle**: `POST http://192.168.87.2:5049/screensaver_border?value=on|off` (linked to `input_boolean.ambilight_border_mode`, labeled **"Screensaver Border Mode (Off = Dominant)"**).
  - Toggling `/screensaver_border` saves `SCREENSAVER_BORDER_MODE` (`/home/pi/.hyperhdr_screensaver_border`) and **only** updates live HyperHDR/WLED state if `CURRENT_MODE == 'screensaver'` (never disturbs `tv` or `music` mode).
- **Sub-mode C1: Screensaver Dominant Color Mode (`SCREENSAVER_BORDER_MODE == False`, Default)**
  - HyperHDR smoothing lowered to `5 Hz` (`400 ms`).
  - Stops continuous UDP DDP streaming (`0.0 FPS` UDP traffic).
  - Samples the dominant screen color once per second (`vote_mask` excludes right-edge `0..63` reflection) and **ignores `BLACK` votes** (`counts_no_black[0] = 0`) whenever `colored_votes >= 15`.
  - Sends a single HTTP `POST http://192.168.87.114/json/state` (`transition: 15`, `fx: 0`) to WLED only when the latched dominant color changes.
- **Sub-mode C2: Screensaver Border Mode (`SCREENSAVER_BORDER_MODE == True`)**
  - Uses TV trapezoid layout at `20 Hz` smoothing and streams DDP (max 20 FPS on change, 1.5s keepalive when static).
  - **Top, Left, and Right borders (`0..243`)**: Track the image's border colors around the screen.
  - **Bottom border (`244..358`)**: Smoothly stays on the **dominant color of the screen** (ignoring `BLACK` votes).

---

## 4. On-Demand Camera Color Diagnostics in Home Assistant
- `sensor.camera_primary_colors` and `sensor.camera_dominant_color` read from `http://192.168.87.2:5049/status`.
- To prevent Home Assistant SQLite database bloat, `scan_interval` is set to `86400` (24h) and both entities are excluded from `recorder`.
- Use `script.refresh_camera_colors` (**"Refresh Camera Colors (On-Demand)"** on the dashboard) to poll camera color votes on demand.

---

## 5. Automatic TV Boundary Detection & Perspective Auto-Crop
- **Goal**: Automatically detect the physical black-bezel TV boundary against the white/off-white wall—even if the camera shifts slightly or if the TV is playing bright/dark content—and dynamically update HyperHDR's 359-LED perspective sampling layout without virtual-camera overhead.
- **How It Works (`detect_tv_quad` & `generate_tv_leds_from_config` in `hyperhdr_palette_proxy.py`)**:
  - Connects to HyperHDR's WebSocket (`ws://127.0.0.1:8090`) to grab a single raw `640x480` camera frame via `{"command":"ledcolors","subcommand":"imagestream-start"}` and immediately stops the stream.
  - Scans **inward from the outer wall toward the TV** along all 4 sides (Top, Bottom, Left, Right) to find the first sustained transition from the bright/neutral wall to the dark TV bezel. Because scanning stops at the outer bezel, internal TV screen content (whether TV is ON or OFF) does not confuse the boundary.
  - Fits 4 lines using **RANSAC with longest contiguous inlier cluster filtering** (`_longest_contiguous_cluster`) and geometric slope constraints so wall art on the far left or ceiling ornaments on the top are rejected.
  - Computes the 4 perspective corner intersections (`TL`, `TR`, `BL`, `BR`) and generates all 359 axis-aligned LED sampling boxes (`generate_tv_leds_from_config`), clamping every box strictly inside the 4 sloped bezel line equations (`inset=0.015`, `hdepth=0.07`, `vdepth=0.07`) so tilted corners never poke outside into the wall.
- **Continuous Background Monitoring & Hysteresis (`auto_crop_monitor_loop`)**:
  - Checks the camera frame every `15s` when `CURRENT_MODE` is `tv` or `screensaver` (never wakes the camera in `idle` mode, and never overwrites `music` mode's full-perimeter layout).
  - Applies a **hysteresis threshold (`>= 4.5 px` corner shift)** confirmed across **2 consecutive samples (`1.5s` apart)** before updating HyperHDR, preventing jitter while reacting within seconds to real camera movement.
  - Saves updated coordinates to `/home/pi/.hyperhdr_tv_config.json`, `/home/pi/.hyperhdr_tv_leds.json`, and `/home/pi/golden_backup/.hyperhdr_tv_config.json`.
- **HTTP Endpoints & UI Controls**:
  - `POST http://192.168.87.2:5049/auto_crop` (or `?force=1`): Immediately runs TV boundary detection and applies the new 359-LED layout.
  - `POST http://192.168.87.2:5049/auto_crop?enable=on|off|toggle`: Enables or disables continuous background monitoring.
  - `GET http://192.168.87.2:5049/auto_crop_preview`: Returns an annotated JPEG (`/tmp/hyperhdr_auto_crop_latest.jpg`) showing the green detected TV quadrilateral, corner coordinates, and all 359 LED sampling boxes.
  - **Home Assistant**: `script.auto_calibrate_tv_boundary` (**"Auto-Calibrate TV Crop"** button on dashboard) calls `rest_command.proxy_auto_crop`.
  - **HyperHDR Web UI**: **"Auto-Crop TV"** button (`#leds_prev_auto_crop` in `light_source.html` / `light_source.js`).

