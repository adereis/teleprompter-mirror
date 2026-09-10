#!/bin/bash
# Start the WebRTC window mirror with optional USB tethering setup.
#
# Usage:
#   ./start-mirror.sh              # start server only
#   ./start-mirror.sh usb          # enable USB tethering + start server
#   ./start-mirror.sh reconnect    # re-enable USB after KVM switch (no server restart)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=../lib/config.sh
. "$PROJECT_DIR/lib/config.sh"
# shellcheck source=../lib/usb.sh
. "$PROJECT_DIR/lib/usb.sh"
PORT="$TELEPROMPTER_PORT"

setup_usb() {
    if ! adb devices 2>/dev/null | grep -q "device$"; then
        echo "No ADB device found. Connect tablet with USB debugging enabled."
        return 1
    fi

    echo "Enabling USB tethering via ADB..."
    adb shell svc usb setFunctions rndis,adb 2>/dev/null || true
    sleep 5

    USB_IF=$(find_tablet_interface)

    if [ -z "$USB_IF" ]; then
        echo "ADB USB function switch didn't work — opening tethering settings on tablet..."
        adb shell am start -a android.settings.TETHER_SETTINGS 2>/dev/null
        echo "Enable USB tethering on the tablet, then press Enter."
        read -r
    fi

    echo "Waiting for USB network interface..."
    for _ in $(seq 1 15); do
        USB_IF=$(find_tablet_interface)
        if [ -n "$USB_IF" ]; then
            break
        fi
        sleep 1
    done

    if [ -z "${USB_IF:-}" ]; then
        echo "USB network interface did not appear."
        return 1
    fi

    echo "USB interface: $USB_IF"

    # Ensure the NM profile won't steal the default route
    local conn conn_uuid
    for _ in $(seq 1 15); do
        conn_uuid=$(nmcli -g GENERAL.CON-UUID device show "$USB_IF")
        if [ -n "$conn_uuid" ] && [ "$conn_uuid" != "--" ]; then break; fi
        sleep 1
    done
    if [ -z "$conn_uuid" ] || [ "$conn_uuid" = "--" ]; then
        echo "NetworkManager did not activate the tablet connection." >&2
        return 1
    fi
    conn=$(nmcli -e no -g connection.id connection show uuid "$conn_uuid")
    case "$conn" in
        "Wired connection"*) ;;
        *) echo "Refusing to change named connection '$conn'." >&2; return 1 ;;
    esac
    nmcli connection modify uuid "$conn_uuid" ipv4.never-default yes ipv6.never-default yes
    echo "Set never-default on '$conn'"

    # Trust USB interface for WebRTC media (UDP)
    sudo firewall-cmd --zone=trusted --change-interface="$USB_IF"
    echo "Firewall: $USB_IF → trusted zone"

    # Wait for IP assignment
    for _ in $(seq 1 5); do
        USB_IP=$(ip -4 addr show "$USB_IF" 2>/dev/null | grep -oP '(?<=inet )\S+(?=/)') || true
        if [ -n "$USB_IP" ]; then break; fi
        sleep 1
    done
    echo "USB IP: ${USB_IP:-not assigned yet}"

    # Set up ADB reverse for signaling fallback
    adb reverse "tcp:$PORT" "tcp:$PORT"
    echo "ADB reverse: localhost:$PORT → laptop"
}

case "${1:-}" in
    usb)
        setup_usb
        echo ""
        echo "Starting mirror server on port $PORT..."
        exec python3 "$PROJECT_DIR/app/mirror-server.py" -p "$PORT"
        ;;
    reconnect)
        setup_usb
        echo ""
        echo "USB reconnected. Reload http://localhost:$PORT/view on the tablet."
        ;;
    *)
        echo "Starting mirror server on port $PORT..."
        exec python3 "$PROJECT_DIR/app/mirror-server.py" -p "$PORT"
        ;;
esac
