# Conference Setup

Tools for mirroring a laptop window to a tablet, designed for a teleprompter-style
video calling setup (eye contact with the camera by placing the tablet nearby).

## WebRTC Window Mirror

Captures a specific window on the laptop and streams it to a tablet browser via
WebRTC. Ideal for video calls on a large/5K monitor where full-screen RDP mirroring
would be impractical.

### Usage

Start the server:

```bash
./mirror-server.py               # auto-detects all local IPs
./mirror-server.py --ip 10.0.0.5 # use a specific IP for mDNS rewrite
```

Then open:
- **Laptop**: http://localhost:8080/cast — click "Share Window" and pick a window
- **Tablet**: http://\<laptop-ip\>:8080/view — auto-connects and displays the stream

Tap the tablet screen to toggle fullscreen.

### USB tethering (lower latency)

For a direct wired link instead of Wi-Fi:

1. Connect the tablet via USB with ADB/debug enabled
2. Enable **USB tethering** on the tablet (Settings → Connections → Tethering)
3. Fix the default route so the laptop keeps its internet:
   ```bash
   nmcli connection modify "Wired connection 1" ipv4.never-default yes ipv6.never-default yes
   nmcli connection up "Wired connection 1"
   ```
4. On the tablet, open `http://<usb-ip>:8080/view` (check `ip addr show enp*` for the IP)

Optionally disable Wi-Fi on the tablet to force all traffic over USB.

### How it works

- `cast.html` uses `getDisplayMedia()` to capture a window, then sends it via WebRTC
- `view.html` receives the WebRTC stream and displays it fullscreen
- `mirror-server.py` handles signaling (SDP offer/answer exchange) and rewrites
  Chrome's mDNS ICE candidates to real LAN IPs so the tablet can connect

### Tuning

The cast page is configured for video call mirroring:
- VP8 preferred (Chrome's libvpx encoder is optimized for real-time WebRTC)
- 8 Mbps max bitrate (high quality on LAN)
- 60 fps capture, `contentHint = 'motion'` (smooth faces over sharp text)
- `degradationPreference = 'maintain-resolution'`

## RDP Virtual Display (alternative)

For simpler setups (laptop with 1080p screen), GNOME Remote Desktop can mirror the
primary display to the tablet via RDP — no window selection needed.

```bash
./virtual-display.sh setup    # configure headless RDP
./virtual-display.sh status   # check state
./virtual-display.sh mirror   # open local preview with wl-mirror
./virtual-display.sh stop     # disable
```

Connect from the tablet with an RDP client (e.g., Windows App) to `<laptop-ip>:3389`,
username/password: generated on first `./virtual-display.sh setup`.

## Requirements

- Fedora Linux with GNOME/Wayland
- `gnome-remote-desktop` (installed by default)
- `wl-mirror` (`dnf install wl-mirror`) — for local preview of virtual display
- Python 3 (standard library only) — for the WebRTC mirror server
- Chrome or Chromium on both laptop and tablet
