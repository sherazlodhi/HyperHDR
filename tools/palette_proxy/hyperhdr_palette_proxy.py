#!/usr/bin/env python3
"""
HyperHDR DDP Palette Proxy (Option B: Strict Pencil Palette & Pure Black)
- Intercepts DDP frames from HyperHDR on 127.0.0.1:5048
- Quantizes every LED to strict primary/pencil colors (Red, Green, Blue, Yellow, Orange, Brown, Pink, Purple, Sky Blue, White)
- Maps dark/blackish signals (camera sensor noise) to TRUE BLACK (0, 0, 0)
- Forwards processed DDP packets to WLED/ESP32 at 192.168.87.114:4048
"""

import socket
import time
import colorsys
import numpy as np
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

LISTEN_IP = "127.0.0.1"
LISTEN_PORT = 5048

DEST_IP = "192.168.87.114"
DEST_PORT = 4048

PALETTE = {
    'WHITE':  (255, 255, 255),
    'RED':    (255, 0, 0),
    'ORANGE': (255, 100, 0),
    'BROWN':  (135, 60, 20),
    'YELLOW': (255, 220, 0),
    'GREEN':  (0, 255, 0),
    'CYAN':   (0, 220, 255),
    'BLUE':   (0, 30, 255),
    'PURPLE': (130, 0, 255),
    'PINK':   (255, 20, 147)
}

def generate_lut():
    logging.info("Building 64x64x64 palette quantization LUT...")
    t0 = time.time()
    lut = np.zeros((64, 64, 64, 3), dtype=np.uint8)

    for ri in range(64):
        r = ri * 4 + 2
        for gi in range(64):
            g = gi * 4 + 2
            for bi in range(64):
                b = bi * 4 + 2
                max_c = max(r, g, b)
                min_c = min(r, g, b)

                # 1. Pure Black: Drop darks, sensor noise, murky blueish shadows
                if max_c < 45 or (max_c < 65 and (max_c - min_c) < 25):
                    lut[ri, gi, bi] = (0, 0, 0)
                    continue

                h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                hue = h * 360.0

                # 2. White / Neutral
                if s < 0.16 and v > 0.65:
                    base = PALETTE['WHITE']
                # 3. Chromatic Palette
                elif hue >= 345 or hue < 15:
                    base = PALETTE['RED']
                elif 15 <= hue < 42:
                    if v < 0.60 or s < 0.75:
                        base = PALETTE['BROWN']
                    else:
                        base = PALETTE['ORANGE']
                elif 42 <= hue < 70:
                    base = PALETTE['YELLOW']
                elif 70 <= hue < 165:
                    base = PALETTE['GREEN']
                elif 165 <= hue < 205:
                    base = PALETTE['CYAN']
                elif 205 <= hue < 265:
                    base = PALETTE['BLUE']
                elif 265 <= hue < 305:
                    base = PALETTE['PURPLE']
                else: # 305 <= hue < 345
                    base = PALETTE['PINK']

                # Smoothly preserve scene brightness level
                factor = v ** 0.85
                lut[ri, gi, bi] = (
                    int(base[0] * factor),
                    int(base[1] * factor),
                    int(base[2] * factor)
                )

    logging.info(f"LUT generated in {time.time() - t0:.2f}s ({lut.nbytes / 1024:.1f} KB).")
    return lut

def main():
    lut = generate_lut()

    rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx_sock.bind((LISTEN_IP, LISTEN_PORT))

    tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest_addr = (DEST_IP, DEST_PORT)

    logging.info(f"DDP Palette Proxy active: listening on {LISTEN_IP}:{LISTEN_PORT}, forwarding to {DEST_IP}:{DEST_PORT}")

    packet_count = 0
    t_last = time.time()

    while True:
        try:
            data, _ = rx_sock.recvfrom(4096)
            if len(data) > 10:
                header = data[:10]
                payload = data[10:]
                num_leds = len(payload) // 3

                # Fast vectorized LUT lookup
                rgb = np.frombuffer(payload[:num_leds * 3], dtype=np.uint8).reshape((num_leds, 3))
                idx = rgb >> 2
                quantized = lut[idx[:, 0], idx[:, 1], idx[:, 2]]

                out_packet = header + quantized.tobytes()
                tx_sock.sendto(out_packet, dest_addr)
            else:
                tx_sock.sendto(data, dest_addr)

            packet_count += 1
            now = time.time()
            if now - t_last >= 10.0:
                fps = packet_count / (now - t_last)
                logging.info(f"Proxy forwarding smoothly at {fps:.1f} FPS (last packet: {len(data)} bytes).")
                packet_count = 0
                t_last = now
        except Exception as e:
            logging.error(f"Proxy error: {e}")
            time.sleep(0.01)

if __name__ == '__main__':
    main()
