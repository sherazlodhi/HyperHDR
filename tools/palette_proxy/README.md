# HyperHDR DDP Palette Proxy, Mode Isolation & Automatic TV Boundary Auto-Crop

A real-time UDP DDP proxy and computer-vision auto-calibration daemon (`hyperhdr_palette_proxy.py`) for **HyperHDR + WLED Ambilight** setups on Raspberry Pi 5, paired with isolated Home Assistant automations and a custom HyperHDR Web UI trigger.

---

## Key Features

1. **Automatic 4-Corner TV Boundary Detection & Perspective Auto-Crop (`/auto_crop`)**:
   - Grabs a raw `640x480` camera frame directly from HyperHDR's WebSocket (`ws://127.0.0.1:8090`) with zero `v4l2loopback` virtual camera overhead.
   - Scans **inward from the lighter wall toward the black TV bezel** on all 4 sides using **RANSAC line fitting + longest contiguous inlier cluster filtering** (`_longest_contiguous_cluster`). Works whether the TV is **ON** (playing bright or dark content) or **OFF**.
   - Generates all **359 LED sampling boxes** via bilinear perspective interpolation of the 4 detected corners (`TL`, `TR`, `BL`, `BR`) and clamps every axis-aligned box strictly inside the 4 sloped bezel line equations so tilted corners never sample the wall.
   - **Continuous Background Monitoring with Hysteresis**: Checks for camera movement every `10–15s` in `tv` / `screensaver` mode and automatically recalibrates HyperHDR only when a real shift `>= 4.5 px` is confirmed across 2 consecutive samples (`1.5s` apart).
   - **Annotated Live Preview (`/auto_crop_preview`)**: Serves `/tmp/hyperhdr_auto_crop_latest.jpg` showing the green detected TV quadrilateral and all 359 LED boxes overlaid on the camera frame.

2. **Strict Ambilight Mode Isolation (`tv`, `music`, `screensaver`, `idle`)**:
   - **TV / Movie Mode (`/tv`)**: Calibrated perspective TV layout (`20 Hz` smoothing), true black-level noise gate (`[0, 0, 0]` for dark scenes), white blue-cast neutralization, and optional primary color quantization (`/primary?value=on|off`).
   - **Music Mode (`/music`)**: Full-perimeter (`0.0..1.0`) layout (`20 Hz` smoothing) with direct DDP passthrough for HyperHDR audio spectrum effects and live microphone sensitivity control (`/mic?value=10..100`).
   - **Screensaver Mode (`/screensaver`)**: Supports two isolated sub-modes via `/screensaver_border?value=on|off`:
     - **Dominant Color Wash (`off`, default)**: Samples the dominant non-black screen color once per second and transitions WLED smoothly over HTTP (`0.0 FPS` UDP traffic).
     - **Border + Dominant Bottom (`on`)**: Top/Left/Right borders track the video edges while the Bottom border smoothly holds the dominant screen color.
   - **Idle Mode (`/idle`)**: Stops HyperHDR capture/LED device and sets WLED to a warm default yellow (`[255, 200, 0]`).

3. **Ultra-Low-Latency 3D LUT Color Quantization**:
   - Precomputed `64x64x64` NumPy LUT processes 359 LEDs in `< 0.02 ms`.

---

## Repository Contents (`tools/palette_proxy/`)

| File / Folder | Description |
| :--- | :--- |
| [`hyperhdr_palette_proxy.py`](./hyperhdr_palette_proxy.py) | Main UDP DDP proxy, HTTP control server (port `5049`), and RANSAC TV auto-crop daemon |
| [`hyperhdr-palette-proxy.service`](./hyperhdr-palette-proxy.service) | `systemd` service unit for `/home/pi/hyperhdr_palette_proxy.py` |
| [`AUTO_CROP_AND_CHANGELOG.md`](./AUTO_CROP_AND_CHANGELOG.md) | Detailed documentation of the Auto-Crop algorithm, API endpoints, and changelog |
| [`AMBILIGHT_MODES_ARCHITECTURE.md`](./AMBILIGHT_MODES_ARCHITECTURE.md) | Architecture & mode isolation reference for `tv`, `music`, `screensaver`, and `idle` |
| [`homeassistant/configuration_hyperhdr.yaml`](./homeassistant/configuration_hyperhdr.yaml) | Isolated Home Assistant `rest_command`, `input_boolean`, `input_number`, `input_select`, and `sensor` definitions |
| [`homeassistant/scripts_hyperhdr.yaml`](./homeassistant/scripts_hyperhdr.yaml) | Isolated Home Assistant scripts (`set_tv_video_mode`, `set_music_mode`, `set_aerialviews_ambient_mode`, `auto_calibrate_tv_boundary`, etc.) |
| [`homeassistant/automations_hyperhdr.yaml`](./homeassistant/automations_hyperhdr.yaml) | Isolated Home Assistant automations for TV/Music/Screensaver state transitions and live dashboard controls |

---

## Installation & Service Setup

1. Copy `hyperhdr_palette_proxy.py` to `/home/pi/`:
   ```bash
   cp hyperhdr_palette_proxy.py /home/pi/
   chmod +x /home/pi/hyperhdr_palette_proxy.py
   ```

2. Install and enable the `systemd` service:
   ```bash
   sudo cp hyperhdr-palette-proxy.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now hyperhdr-palette-proxy.service
   ```

3. Configure HyperHDR (`Settings -> LED Hardware`):
   - **Controller type**: `ddp`
   - **Target IP**: `127.0.0.1`
   - **Port**: `5048`

4. Trigger or preview Auto-Crop at any time:
   ```bash
   curl -X POST "http://127.0.0.1:5049/auto_crop?force=1"
   # View annotated JPEG preview in browser:
   # http://<raspberry-pi-ip>:5049/auto_crop_preview
   ```
