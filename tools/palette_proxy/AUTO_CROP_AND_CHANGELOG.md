# HyperHDR Automatic TV Boundary Detection & Perspective Auto-Crop — Change Notes & Usage Guide

## Overview
This document summarizes the changes made to add **automatic 4-corner TV boundary detection and dynamic perspective calibration (`auto_crop`)** to the HyperHDR + WLED Ambilight system on Raspberry Pi 5, along with strict mode isolation (`tv`, `music`, `screensaver`, `idle`).

It allows a USB camera pointed at a TV (even at an angled perspective) to automatically locate the TV's black physical bezel against a lighter wall (white/off-white/warm ambient lighting) and dynamically adjust all 359 LED sampling regions in HyperHDR—both **on-demand** and **continuously in the background** if the camera moves slightly.

---

## 1. Summary of Changes Made

### A. Core Proxy & Computer Vision Engine (`hyperhdr_palette_proxy.py`)
1. **Zero-Overhead Frame Capture (`capture_hyperhdr_frame`)**:
   - Connects to HyperHDR's local WebSocket (`ws://127.0.0.1:8090`) and sends `{"command": "ledcolors", "subcommand": "imagestream-start"}` to grab a single raw `640x480` camera frame, then immediately stops the stream (`imagestream-stop`).
   - Avoids needing `v4l2loopback` or a second video processing pipeline, keeping CPU usage near zero during normal operation.
2. **Inward Wall-to-Bezel RANSAC Quad Detector (`detect_tv_quad`)**:
   - Scans **inward from the outer wall toward the TV** along all 4 sides (`Top`, `Bottom`, `Left`, `Right`) to find the first sustained transition from the lighter wall to the dark TV bezel.
   - Because scanning stops at the outer black bezel, bright or dark content playing **inside** the TV screen (when the TV is **ON**) or a black screen (when the TV is **OFF**) never confuses the edge detector.
   - Uses **RANSAC line fitting** combined with **longest contiguous inlier cluster filtering (`_longest_contiguous_cluster`)** and slope constraints (`|m| <= 0.35` top, `0.45` bottom, `0.25` left/right) to reject false edges caused by wall art, lamps, or ceiling ornaments.
   - Intersects the 4 fitted bezel lines to compute exact pixel and percentage coordinates (`ptlh`, `ptlv`, `ptrh`, `ptrv`, `pblh`, `pblv`, `pbrh`, `pbrv`) for the 4 corners (`TL`, `TR`, `BL`, `BR`).
3. **Perspective-Clamped 359-LED Layout Generator (`generate_tv_leds_from_config`)**:
   - Uses bilinear perspective interpolation across the 4 detected corners to generate all 359 LED sampling boxes (`64` Right, `115` Top, `65` Left, `115` Bottom).
   - Clamps every axis-aligned (`hmin`/`hmax`/`vmin`/`vmax`) LED box strictly inside the 4 sloped line equations of the detected quadrilateral (`inset=0.015`, `hdepth=0.07`, `vdepth=0.07`) so tilted corners never poke outside the bezel into the wall.
   - Fixed `_is_full_perimeter_leds` so that a TV whose right edge touches `hmax=1.0` (`ptrh=100`, `pbrh=99`) is properly saved and loaded as a calibrated `tv` layout rather than mistaken for a `music` full-perimeter layout.
4. **Continuous Background Monitoring with Hysteresis (`auto_crop_monitor_loop`)**:
   - Runs in a background daemon thread, checking a frame every `10–15s` when `CURRENT_MODE` is `tv` or `screensaver` (never wakes the camera in `idle` mode and never overwrites `music` mode's full-perimeter layout).
   - Requires a corner movement **`>= 4.5 px`** confirmed across **2 consecutive checks (`1.5s` apart)** before pushing a new LED layout to HyperHDR via JSON-RPC (`127.0.0.1:19444`). This prevents frame-to-frame noise/jitter while adapting within seconds when the camera is physically nudged.
   - Persists updated coordinates to `/home/pi/.hyperhdr_tv_config.json`, `/home/pi/.hyperhdr_tv_leds.json`, and `/home/pi/golden_backup/.hyperhdr_tv_config.json`.
5. **HTTP Control & Diagnostic Preview Endpoints (Port `5049`)**:
   - `POST /auto_crop` (or `/auto_crop?force=1`): Immediately runs detection and applies the calibrated 359-LED boundary to HyperHDR.
   - `POST /auto_crop?enable=on|off|toggle`: Enables or disables continuous background monitoring.
   - `GET /auto_crop_preview`: Returns an annotated JPEG (`/tmp/hyperhdr_auto_crop_latest.jpg`) showing the raw camera frame with the green detected TV boundary, red corner markers, and all 359 LED sampling boxes drawn on top.
   - `GET /status` & `GET /debug`: Includes the `auto_crop` status object (`corners_px`, `corners_pct`, `last_shift_px`, `inliers`, `status`, `preview_url`).

### B. HyperHDR Web UI (`www/content/light_source.html` & `www/js/light_source.js`)
- Added an **"Auto-Crop TV"** button (`#leds_prev_auto_crop`) in the LED Layout / Visualization toolbar that calls `POST http://<host>:5049/auto_crop?force=1` and reloads the 359-LED preview boxes in the browser.

### C. Home Assistant Integration (`ha_config/`)
- `ha_config/configuration.yaml`:
  - Added `rest_command.proxy_auto_crop` (`POST http://192.168.87.2:5049/auto_crop?force=1`).
  - Added `auto_crop` attribute to `sensor.camera_primary_colors` (excluded from `recorder` with 24h `scan_interval` to prevent SQLite database bloat).
- `ha_config/scripts.yaml`:
  - Added `script.auto_calibrate_tv_boundary` (**"Auto-Calibrate TV Boundary"**) which triggers `rest_command.proxy_auto_crop` and refreshes `sensor.camera_primary_colors`.
- `ha_config/lovelace.dashboard_l`:
  - Added the **"Auto-Calibrate TV Boundary (On-Demand)"** (`script.auto_calibrate_tv_boundary`) button and **"Refresh Camera Colors (On-Demand)"** (`script.refresh_camera_colors`) button to the dashboard.

---

## 2. Quick Reference & Commands

| Action | Command / URL |
| :--- | :--- |
| **Force Auto-Crop Now** | `curl -X POST "http://192.168.87.2:5049/auto_crop?force=1"` |
| **View Annotated Preview Image** | `http://192.168.87.2:5049/auto_crop_preview` |
| **Enable Background Auto-Crop** | `curl -X POST "http://192.168.87.2:5049/auto_crop?enable=on"` |
| **Disable Background Auto-Crop** | `curl -X POST "http://192.168.87.2:5049/auto_crop?enable=off"` |
| **Check Current Calibration JSON** | `curl -s "http://192.168.87.2:5049/status" \| jq .auto_crop` |

---

## 3. Adapting for Other Setups
If you are using `hyperhdr_palette_proxy.py` on your own Raspberry Pi + HyperHDR setup:
1. **LED Counts**: Update the per-side LED counts in `generate_tv_leds_from_config()` (default: Right `64`, Top `115`, Left `65`, Bottom `115`, total `359` LEDs).
2. **Ports & Addresses**:
   - HyperHDR JSON-RPC port: `19444` (`127.0.0.1`)
   - HyperHDR WebSocket port: `8090` (`127.0.0.1`)
   - Proxy UDP input from HyperHDR: `127.0.0.1:5048`
   - Proxy HTTP control port: `5049`
   - WLED target IP/port: `WLED_IP` (`192.168.87.114`), UDP port `4048`
3. **Sensitivity / Hysteresis**:
   - Adjust `AUTO_CROP_STATE["threshold_px"]` (default `4.5` pixels) and `AUTO_CROP_STATE["interval_sec"]` (default `10.0` seconds) in `hyperhdr_palette_proxy.py`.
