#!/bin/bash
# Ensure the MT7601U WiFi adapter's driver binds after USB enumeration.
# Called by teleprompter-wifi-rebind.service (triggered via udev).
#
# After KVM switches or port changes, the mt7601u driver sometimes fails to
# claim the USB interface despite successful enumeration. This script waits
# for the normal driver probe, then forces a USB re-probe if wlan0 is missing.
set -euo pipefail

TAG="teleprompter-wifi"
VENDOR="148f"
PRODUCT="7601"
MAX_RETRIES=3
PROBE_WAIT=5

# Find the MT7601U sysfs device node (excludes interface nodes containing ':')
find_device() {
    local d vid pid
    for d in /sys/bus/usb/devices/*/; do
        [ -e "$d/idVendor" ] || continue
        vid=$(cat "$d/idVendor")
        pid=$(cat "$d/idProduct")
        if [ "$vid" = "$VENDOR" ] && [ "$pid" = "$PRODUCT" ]; then
            echo "${d%/}"
            return 0
        fi
    done
    return 1
}

fix_autosuspend() {
    local dev
    dev=$(find_device) || return
    local ctrl="$dev/power/control"
    if [ -e "$ctrl" ] && [ "$(cat "$ctrl")" != "on" ]; then
        echo "on" > "$ctrl"
        logger -t "$TAG" "Disabled USB autosuspend on MT7601U ($dev)"
    fi
}

sleep "$PROBE_WAIT"

if ip link show wlan0 &>/dev/null; then
    logger -t "$TAG" "wlan0 present — no recovery needed"
    fix_autosuspend
    exit 0
fi

logger -t "$TAG" "wlan0 missing after MT7601U enumeration — attempting recovery"

for attempt in $(seq 1 "$MAX_RETRIES"); do
    dev=$(find_device) || true

    if [ -z "$dev" ]; then
        logger -t "$TAG" "MT7601U not found in sysfs (EPROTO?) — physical replug required"
        exit 1
    fi

    logger -t "$TAG" "Re-probing $dev (attempt $attempt/$MAX_RETRIES)"
    echo 0 > "$dev/authorized"
    sleep 1
    echo 1 > "$dev/authorized"
    sleep "$PROBE_WAIT"

    if ip link show wlan0 &>/dev/null; then
        logger -t "$TAG" "wlan0 recovered after re-probe (attempt $attempt)"
        fix_autosuspend
        exit 0
    fi
done

logger -t "$TAG" "wlan0 failed to appear after $MAX_RETRIES re-probe attempts"
exit 1
