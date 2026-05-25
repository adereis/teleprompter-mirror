#!/bin/bash
# Virtual display management for teleprompter setup
# Uses GNOME Remote Desktop (headless RDP) to create a virtual monitor

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CERT="$SCRIPT_DIR/rdp-tls.crt"
KEY="$SCRIPT_DIR/rdp-tls.key"
CRED_FILE="$SCRIPT_DIR/.rdp-credentials"
USERNAME="teleprompter"

cmd_status() {
    echo "=== GNOME Remote Desktop (headless) ==="
    grdctl --headless status 2>&1 | grep -v "TPM\|tcti"
    echo ""
    echo "=== Port 3389 ==="
    ss -tlnp 2>/dev/null | grep 3389 || echo "Not listening"
    echo ""
    echo "=== Local IP ==="
    hostname -I | awk '{print $1}'
}

cmd_setup() {
    if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
        echo "Generating TLS certificate..."
        openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
            -days 730 -nodes -x509 -subj "/CN=gnome-remote-desktop" \
            -keyout "$KEY" -out "$CERT" 2>/dev/null
        chmod 600 "$KEY"
    fi

    if [ ! -f "$CRED_FILE" ]; then
        PASSWORD=$(openssl rand -base64 16)
        echo "$PASSWORD" > "$CRED_FILE"
        chmod 600 "$CRED_FILE"
        echo "Generated new RDP password."
    else
        PASSWORD=$(cat "$CRED_FILE")
    fi

    echo "Configuring headless RDP..."
    grdctl --headless rdp set-tls-cert "$CERT" 2>/dev/null
    grdctl --headless rdp set-tls-key "$KEY" 2>/dev/null
    grdctl --headless rdp set-credentials "$USERNAME" "$PASSWORD" 2>/dev/null
    grdctl --headless rdp disable-view-only 2>/dev/null
    grdctl --headless rdp enable 2>/dev/null
    # RDP should only be accessible from USB-tethered tablet (trusted zone),
    # not from WiFi or ethernet. Remove from all other zones.
    for zone in $(firewall-cmd --get-zones); do
        [ "$zone" = "trusted" ] && continue
        firewall-cmd --zone="$zone" --remove-port=3389/tcp 2>/dev/null || true
    done
    echo "Done. RDP is enabled on port 3389 (trusted zone / USB only)."
    echo "Username: $USERNAME / Password: $PASSWORD"
}

cmd_mirror() {
    local output="${1:-Meta-0}"
    echo "Launching local preview of virtual monitor ($output)..."
    echo "Close the window or Ctrl+C to stop."
    exec wl-mirror "$output"
}

cmd_stop() {
    echo "Disabling headless RDP..."
    grdctl --headless rdp disable 2>/dev/null
    echo "Done."
}

case "${1:-help}" in
    status)  cmd_status ;;
    setup)   cmd_setup ;;
    mirror)  cmd_mirror "${2:-}" ;;
    stop)    cmd_stop ;;
    *)
        echo "Usage: $0 {setup|status|mirror|stop}"
        echo "  setup      - Generate certs and enable headless RDP"
        echo "  status     - Show current state"
        echo "  mirror     - Open local preview window of the virtual monitor"
        echo "  stop       - Disable headless RDP"
        ;;
esac
