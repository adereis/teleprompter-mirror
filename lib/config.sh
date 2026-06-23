# Shared configuration loader for the Teleprompter Mirror bash scripts.
#
# Source this file (do not execute it). It exports the TELEPROMPTER_* variables
# so child processes — most importantly mirror-server.py — inherit them.
#
# Precedence (highest first):
#   1. variables already set in the environment (one-off overrides)
#   2. ~/.config/teleprompter-mirror/config.env  (the user's persistent config)
#   3. the built-in defaults below
#
# shellcheck shell=bash

_tp_config_file="${XDG_CONFIG_HOME:-$HOME/.config}/teleprompter-mirror/config.env"

# Overlay the user's config, but never clobber a variable already set in the
# environment — that is how a one-off `TELEPROMPTER_PORT=9000 ./start-mirror.sh`
# override keeps working.
if [ -f "$_tp_config_file" ]; then
    while IFS= read -r _tp_line || [ -n "$_tp_line" ]; do
        case "$_tp_line" in
            ''|\#*) continue ;;
        esac
        _tp_line="${_tp_line#export }"
        _tp_key="${_tp_line%%=*}"
        _tp_val="${_tp_line#*=}"
        # Trim surrounding whitespace and one layer of matching quotes.
        _tp_key="${_tp_key//[[:space:]]/}"
        _tp_val="${_tp_val#"${_tp_val%%[![:space:]]*}"}"
        _tp_val="${_tp_val%\"}"; _tp_val="${_tp_val#\"}"
        _tp_val="${_tp_val%\'}"; _tp_val="${_tp_val#\'}"
        case "$_tp_key" in
            TELEPROMPTER_*) ;;
            *) continue ;;
        esac
        if [ -z "${!_tp_key:-}" ]; then
            export "$_tp_key=$_tp_val"
        fi
    done < "$_tp_config_file"
fi

# Built-in defaults for anything still unset.
: "${TELEPROMPTER_PORT:=8047}"
: "${TELEPROMPTER_BIND:=127.0.0.1}"
: "${TELEPROMPTER_BROWSER:=google-chrome}"
: "${TELEPROMPTER_CAMERA_CONNECTION:=Camera-A6300}"
: "${TELEPROMPTER_CAMERA_BSSID:=}"
: "${TELEPROMPTER_CAMERA_ENDPOINT:=http://192.168.122.1:8080/sony}"

export TELEPROMPTER_PORT TELEPROMPTER_BIND TELEPROMPTER_BROWSER \
    TELEPROMPTER_CAMERA_CONNECTION TELEPROMPTER_CAMERA_BSSID \
    TELEPROMPTER_CAMERA_ENDPOINT

unset _tp_config_file _tp_line _tp_key _tp_val
