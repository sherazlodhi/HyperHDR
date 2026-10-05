# HyperHDR DDP Palette Proxy (Option B)

A high-performance, real-time UDP proxy for HyperHDR DDP output streams that quantizes LED colors into a strict primary/pencil color palette and enforces true black for dark scenes.

## Features

- **Pencil Color Palette Snapping:** Eliminates pale, washed-out, or strange tints (like murky gray-blue or sickly yellow-green) by snapping every LED to distinct, saturated pencil colors:
  - **Red** `(255, 0, 0)`
  - **Leaf Green** `(0, 255, 0)`
  - **Royal Blue** `(0, 30, 255)`
  - **Canary Yellow** `(255, 220, 0)`
  - **Vibrant Orange** `(255, 100, 0)`
  - **Rich Brown** `(135, 60, 20)`
  - **Hot Pink** `(255, 20, 147)`
  - **Deep Purple** `(130, 0, 255)`
  - **Sky Blue / Cyan** `(0, 220, 255)`
  - **Pure White** `(255, 255, 255)`
- **True Black Floor:** Automatically detects camera sensor dark noise and dark blueish glow and outputs **true black `(0, 0, 0)`**, keeping LEDs completely off during dark scenes or letterbox bars.
- **Ultra Low Latency:** Uses a precomputed 64×64×64 3D NumPy LUT to process a 359-LED frame in under **16 microseconds (0.016 ms)**.
- **Plug & Play:** HyperHDR sends DDP packets to `127.0.0.1:5048`; the proxy processes and forwards them directly to the hardware WLED/ESP32 controller at `192.168.87.114:4048`.

## Installation & Service Setup

1. Copy `hyperhdr_palette_proxy.py` to `/home/pi/`:
   ```bash
   cp hyperhdr_palette_proxy.py /home/pi/
   chmod +x /home/pi/hyperhdr_palette_proxy.py
   ```

2. Install systemd service:
   ```bash
   sudo cp hyperhdr-palette-proxy.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now hyperhdr-palette-proxy.service
   ```

3. In HyperHDR Settings $\rightarrow$ LED Hardware:
   - Host: `127.0.0.1`
   - Port: `5048`
