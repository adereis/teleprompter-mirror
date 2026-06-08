#!/bin/bash
# Install system hooks and desktop entry for Teleprompter Mirror.
# Run with sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_USER="${SUDO_USER:?Run with sudo, not as root directly}"
TARGET_HOME="$(eval echo ~"$TARGET_USER")"

echo "Installing for user: $TARGET_USER"

echo "Installing udev rules..."
cp "$SCRIPT_DIR/99-teleprompter-tablet.rules" /etc/udev/rules.d/
cp "$SCRIPT_DIR/99-teleprompter-wifi.rules" /etc/udev/rules.d/
udevadm control --reload-rules

echo "Installing systemd services..."
sed "s/__USER__/$TARGET_USER/g" "$SCRIPT_DIR/teleprompter-tether-prompt.service" \
    > /etc/systemd/system/teleprompter-tether-prompt.service
cp "$SCRIPT_DIR/teleprompter-wifi-rebind.service" \
    /etc/systemd/system/teleprompter-wifi-rebind.service
install -Dm755 "$SCRIPT_DIR/wifi-rebind.sh" /usr/local/libexec/teleprompter-wifi-rebind.sh
systemctl daemon-reload

echo "Installing NetworkManager dispatchers..."
sed "s/__USER__/$TARGET_USER/g" "$SCRIPT_DIR/99-teleprompter" \
    > /etc/NetworkManager/dispatcher.d/99-teleprompter
chmod +x /etc/NetworkManager/dispatcher.d/99-teleprompter

sed -e "s/__USER__/$TARGET_USER/g" -e "s|__PROJECT_DIR__|$SCRIPT_DIR|g" \
    "$SCRIPT_DIR/99-teleprompter-camera" \
    > /etc/NetworkManager/dispatcher.d/99-teleprompter-camera
chmod +x /etc/NetworkManager/dispatcher.d/99-teleprompter-camera

echo "Installing desktop entry..."
DESKTOP_DIR="$TARGET_HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
sed "s|__PROJECT_DIR__|$SCRIPT_DIR|g" "$SCRIPT_DIR/teleprompter-mirror.desktop" \
    > "$DESKTOP_DIR/teleprompter-mirror.desktop"
chown "$TARGET_USER:$TARGET_USER" "$DESKTOP_DIR/teleprompter-mirror.desktop"
runuser -u "$TARGET_USER" -- update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

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
echo ""
echo "To uninstall: sudo $SCRIPT_DIR/uninstall.sh"
