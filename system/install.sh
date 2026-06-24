#!/bin/bash
# Install system hooks and desktop entry for Teleprompter Mirror.
# Run with sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"   # the system/ directory (source files)
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"        # repo root (baked into __PROJECT_DIR__)
TARGET_USER="${SUDO_USER:?Run with sudo, not as root directly}"
TARGET_HOME="$(eval echo ~"$TARGET_USER")"

echo "Installing for user: $TARGET_USER"

# Resolve environment-specific values that must be baked into system files
# (NetworkManager dispatchers can't read the user's config at runtime). The
# runtime tools read ~/.config/teleprompter-mirror/config.env directly, so only
# the values install.sh hard-substitutes need to be looked up here.
CONFIG_FILE="$TARGET_HOME/.config/teleprompter-mirror/config.env"
read_config() {
    local key="$1" default="$2" val=""
    if [ -f "$CONFIG_FILE" ]; then
        val=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$key=" "$CONFIG_FILE" \
              | tail -1 | cut -d= -f2-)
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%\"}"; val="${val#\"}"
        val="${val%\'}"; val="${val#\'}"
    fi
    echo "${val:-$default}"
}
CAMERA_CONNECTION="$(read_config TELEPROMPTER_CAMERA_CONNECTION Camera-A6300)"
CAMERA_BSSID="$(read_config TELEPROMPTER_CAMERA_BSSID "")"
echo "Camera WiFi connection: $CAMERA_CONNECTION"

echo "Installing udev rules..."
cp "$SCRIPT_DIR/udev/99-teleprompter-tablet.rules" /etc/udev/rules.d/
cp "$SCRIPT_DIR/udev/99-teleprompter-wifi.rules" /etc/udev/rules.d/
udevadm control --reload-rules

echo "Installing systemd services..."
sed "s/__USER__/$TARGET_USER/g" "$SCRIPT_DIR/systemd/teleprompter-tether-prompt.service" \
    > /etc/systemd/system/teleprompter-tether-prompt.service
cp "$SCRIPT_DIR/systemd/teleprompter-wifi-rebind.service" \
    /etc/systemd/system/teleprompter-wifi-rebind.service
install -Dm755 "$SCRIPT_DIR/wifi-rebind.sh" /usr/local/libexec/teleprompter-wifi-rebind.sh
systemctl daemon-reload

echo "Installing NetworkManager dispatchers..."
sed "s/__USER__/$TARGET_USER/g" "$SCRIPT_DIR/networkmanager/99-teleprompter" \
    > /etc/NetworkManager/dispatcher.d/99-teleprompter
chmod +x /etc/NetworkManager/dispatcher.d/99-teleprompter

sed -e "s/__USER__/$TARGET_USER/g" -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__CAMERA_CONNECTION__|$CAMERA_CONNECTION|g" \
    "$SCRIPT_DIR/networkmanager/99-teleprompter-camera" \
    > /etc/NetworkManager/dispatcher.d/99-teleprompter-camera
chmod +x /etc/NetworkManager/dispatcher.d/99-teleprompter-camera

echo "Disabling WiFi power save on camera connection"
nmcli connection modify "$CAMERA_CONNECTION" 802-11-wireless.powersave 2

if [ -n "$CAMERA_BSSID" ]; then
    echo "Locking camera WiFi to BSSID: $CAMERA_BSSID"
    nmcli connection modify "$CAMERA_CONNECTION" 802-11-wireless.bssid "$CAMERA_BSSID"
else
    echo "No TELEPROMPTER_CAMERA_BSSID set — NM background scanning remains enabled"
fi

echo "Installing desktop entry..."
DESKTOP_DIR="$TARGET_HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$SCRIPT_DIR/teleprompter-mirror.desktop" \
    > "$DESKTOP_DIR/teleprompter-mirror.desktop"
chown "$TARGET_USER:$TARGET_USER" "$DESKTOP_DIR/teleprompter-mirror.desktop"
runuser -u "$TARGET_USER" -- update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "Installing systemd user service..."
USER_SERVICE_DIR="$TARGET_HOME/.config/systemd/user"
mkdir -p "$USER_SERVICE_DIR"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$SCRIPT_DIR/systemd/teleprompter-mirror.service" \
    > "$USER_SERVICE_DIR/teleprompter-mirror.service"
chown "$TARGET_USER:$TARGET_USER" "$USER_SERVICE_DIR/teleprompter-mirror.service"
XDG_DIR="/run/user/$(id -u "$TARGET_USER")"
runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="$XDG_DIR" \
    systemctl --user daemon-reload
runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="$XDG_DIR" \
    systemctl --user enable teleprompter-mirror.service

echo ""
echo "Installed:"
echo "  udev:    /etc/udev/rules.d/99-teleprompter-tablet.rules"
echo "  udev:    /etc/udev/rules.d/99-teleprompter-wifi.rules"
echo "  systemd: /etc/systemd/system/teleprompter-tether-prompt.service"
echo "  systemd: /etc/systemd/system/teleprompter-wifi-rebind.service"
echo "  script:  /usr/local/libexec/teleprompter-wifi-rebind.sh"
echo "  NM:      /etc/NetworkManager/dispatcher.d/99-teleprompter"
echo "  NM:      /etc/NetworkManager/dispatcher.d/99-teleprompter-camera"
echo "  desktop: $DESKTOP_DIR/teleprompter-mirror.desktop"
echo "  systemd: $USER_SERVICE_DIR/teleprompter-mirror.service (user)"
echo ""
echo "To uninstall: sudo $SCRIPT_DIR/uninstall.sh"
