# Sony A6300 Camera Control from Linux

## Overview

The Sony A6300 (ILCE-6300) can be remotely controlled from Linux via its
built-in WiFi API while simultaneously outputting clean HDMI video. This
enables zoom and refocus control during video calls without touching the
camera.

The camera connects to the computer via two independent paths:
- **HDMI** (via USB capture adapter) for the video feed
- **WiFi** (camera creates its own AP) for remote control

## What works

| Feature              | Status  | Notes                                      |
|----------------------|---------|--------------------------------------------|
| Zoom in/out          | Works   | Power zoom lens only (E PZ 16-50mm)        |
| Continuous zoom      | Works   | start/stop for smooth movement             |
| Refocus (AF-C nudge) | Works   | Zoom in+out triggers autofocus re-acquire   |
| Camera status        | Works   | Zoom position, focus mode, shoot mode, etc. |
| Exposure comp        | Works   | Available via API, not in script yet        |
| HDMI + WiFi coexist  | Works   | Clean high-res HDMI stays up during control |

## What doesn't work

| Feature                | Why                                              |
|------------------------|--------------------------------------------------|
| Focus mode switching   | Requires Smart Remote Control (full app), not available |
| Autofocus trigger      | Same — needs full app, not Smart Remote Embedded |
| Touch-to-focus         | Same                                             |
| Manual focus stepping  | Not in any Sony API for this camera generation   |
| ISO / aperture / shutter | Locked out on Smart Remote Embedded             |
| Zoom with manual lens  | No motor in manual zoom lenses — physical only   |

### Why focus control is locked

The A6300 ships with **Smart Remote Embedded**, which exposes a limited API
surface: zoom, capture, exposure comp, and status. The full **Smart Remote
Control** app unlocks focus, ISO, aperture, and shutter speed control, but
Sony discontinued the PlayMemories Camera Apps store where it was distributed.
If Smart Remote Control wasn't installed before the store shut down, there's
no official way to add it.

The workaround for focus: keep the camera in AF-C (continuous autofocus) mode
and use the `refocus` command, which nudges the zoom in then out by one step.
This causes AF-C to re-evaluate and reacquire focus on the subject.

## How it works

### Sony Camera Remote API

The camera runs a JSON-RPC HTTP server when Smart Remote Embedded is active.
The protocol is simple:

- Camera creates a WiFi AP (SSID: `DIRECT-xxxx:ILCE-6300`)
- Camera assigns itself `192.168.122.1`, clients get DHCP addresses
- API endpoint: `http://192.168.122.1:8080/sony/camera`
- All calls are HTTP POST with JSON payloads:

```json
{"method": "actZoom", "params": ["in", "1shot"], "id": 1, "version": "1.0"}
```

The API requires calling `startRecMode` first to unlock the full method set.
Without it, most methods return "Not Available Now."

### Discovery (SSDP)

The camera can be discovered via UPnP/SSDP multicast, but in practice
hardcoding the endpoint works fine since the camera always uses the same IP.
The script tries SSDP first and falls back to the default endpoint.

### Zoom positions

The zoom range is 0-100, where 0 = fully wide (16mm) and 100 = fully tele
(50mm). The `1shot` movement gives steps of roughly 9 positions. The
`start`/`stop` movement gives smooth continuous zoom.

## Camera mode requirements

The camera's physical mode dial affects what the API exposes:

| Mode dial | HDMI output   | Shoot mode | Zoom | Focus APIs |
|-----------|---------------|------------|------|------------|
| Movie     | High-res clean| `movie`    | Yes  | No (empty candidates) |
| P/A/S/M   | Low-res LCD mirror | `still` | Yes | No (needs full app) |

**Use Movie mode** for the teleprompter setup. It gives clean high-resolution
HDMI output and zoom control. Still modes output a low-resolution mirror of
the rear LCD, which is unusable for video calls.

Switching the mode dial while Smart Remote is running exits the app and drops
the WiFi connection. You must relaunch Smart Remote Embedded from the camera
menu after changing modes.

## Setup

### First-time setup

1. Connect the camera's HDMI output to the USB capture adapter
2. Plug in the dedicated USB WiFi adapter (MT7601U) — it appears as `wlan0`
3. Set the camera mode dial to **Movie**
4. On the camera: MENU -> Application -> Application List -> Smart Remote
   Embedded
5. `wlan0` auto-connects to the camera via the `Camera-A6300` NM profile
6. Test: `./camera-control.py zoom`

Create the `Camera-A6300` NetworkManager profile (first time only):

```bash
nmcli connection add type wifi con-name Camera-A6300 \
  ssid "DIRECT-xxxx:ILCE-6300" \
  ifname wlan0 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "your-camera-password" \
  ipv4.never-default yes \
  connection.autoconnect yes \
  connection.autoconnect-priority -100
```

Replace the SSID and password with the values shown on the camera's Smart
Remote Embedded screen. The profile settings:
- `ifname wlan0` — only uses the USB adapter, never the main WiFi
- `ipv4.never-default yes` — never steals the default route
- `connection.autoconnect yes` — connects when the camera's AP is visible
- `connection.autoconnect-priority -100` — lowest priority

If the camera is reset and gets a new SSID/password:

```bash
nmcli connection modify Camera-A6300 \
  802-11-wireless.ssid "DIRECT-newSSID:ILCE-6300" \
  wifi-sec.psk "new-password"
```

### Subnet collision with libvirt

The camera hardcodes `192.168.122.0/24` for its WiFi AP. This is the same
subnet libvirt uses by default for `virbr0`. When both are present, traffic
to `192.168.122.1` hits the bridge instead of the camera.

Fix: move libvirt's default network to a different subnet:

```bash
sudo virsh net-destroy default
sudo virsh net-edit default   # change 192.168.122 → 192.168.124
sudo virsh net-start default
```

This persists across reboots. VMs need a DHCP renew or reboot after
the change.

### USB control alternative (gphoto2)

The A6300 is recognized by libgphoto2 over USB (PTP protocol), but with
severe limitations for this use case:

- **Focus drive** (stepping the motor) is not implemented for any Sony camera.
  Sony uses proprietary PTP opcodes that haven't been reverse-engineered.
  Nikon cameras have this via dedicated `MfDrive`/`AfDrive` opcodes.
- **Zoom** is not implemented over USB either.
- Basic capture, ISO, aperture, and shutter speed work over USB/PTP.

A Windows-only project ([SonyAlphaUSB](https://github.com/pixeltris/SonyAlphaUSB))
has reverse-engineered more of Sony's USB protocol including focus distance,
but it uses the Windows WIA API.

The WiFi API is the only viable path for zoom and refocus on Linux.

### Sony Camera Remote SDK (newer cameras)

Sony's newer Camera Remote SDK supports absolute zoom positioning and focus
position control, but the A6300 is not in the compatible models list. That
SDK targets A7 IV, A6700, and newer bodies. The A6300 only supports the
older Camera Remote API (JSON-RPC over WiFi), which is what `camera-control.py`
uses.

## Lens compatibility

Zoom control requires a **power zoom (PZ) lens** with a built-in motor. The
camera body has no zoom motor — `actZoom` sends commands through the body to
the lens. Compatible kit lens: **E PZ 16-50mm f/3.5-5.6 OSS** (SELP1650).

Manual zoom lenses (those without "PZ" in the name) cannot be zoomed via the
API. The `actZoom` method will be absent from `getAvailableApiList` when a
non-PZ lens is attached.

## Available API methods (Movie mode, Smart Remote Embedded)

Confirmed working on the A6300 with firmware as of 2026.

### Useful for teleprompter setup

| Method | What it does | Notes |
|--------|-------------|-------|
| `actZoom` | Zoom in/out | 1shot (stepped) or start/stop (smooth). Power zoom lens only |
| `setExposureCompensation` | Adjust brightness | -6 to +6 EV in 1/3 stops. Useful if auto ISO isn't adapting fast enough |
| `startMovieRec` / `stopMovieRec` | Record to SD card | XAVC S, higher quality than screen recording. Good for archiving calls |
| `actTakePicture` | Capture a still photo | Works even in movie mode. Higher resolution than video frame grab |
| `startLiveview` | WiFi MJPEG preview | Low-res stream, separate from HDMI. Could be used for frame analysis |
| `getEvent` | Poll camera state | Zoom position, focus mode, exposure, battery, etc. |

### Query/config methods

```
getApplicationInfo      getAvailableShootMode    getSupportedShootMode
getAvailableApiList     getExposureCompensation  getSupportedExposureCompensation
getAvailableExposureCompensation  getMethodTypes getSupportedFlashMode
getAvailableSelfTimer   getSelfTimer             getSupportedSelfTimer
getShootMode            getVersions              setShootMode
setSelfTimer            stopRecMode
```

### Locked by Smart Remote Embedded (would need Smart Remote Control)

These exist in the protocol (`getMethodTypes`) but are absent from
`getAvailableApiList`. Sony discontinued the PlayMemories store where
Smart Remote Control was distributed.

```
actHalfPressShutter     setFNumber               setShutterSpeed
cancelHalfPressShutter  setFlashMode             setTouchAFPosition
cancelTouchAFPosition   setFocusMode             setWhiteBalance
setExposureMode         setIsoSpeedRate          setProgramShift
```
