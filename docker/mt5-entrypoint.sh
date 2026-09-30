#!/bin/sh
# Fixed MT5 container entrypoint — single authoritative startup.
# 1. Cleans stale RPyC FIFO
# 2. Starts Xvfb, x11vnc, noVNC
# 3. Creates VNC password file
# 4. Launches MT5 terminal WITH auto-login from env vars
# 5. Starts RPyC server
# 6. Watches MT5 terminal only

set -e

echo "[entrypoint] ===== MT5 Container Starting ====="
echo "[entrypoint] MT5_LOGIN=${MT5_LOGIN:-NOT SET}"
echo "[entrypoint] MT5_PASSWORD=${MT5_PASSWORD:+SET}"
echo "[entrypoint] MT5_SERVER=${MT5_SERVER:-NOT SET}"
echo "[entrypoint] VNC_PASSWORD=${VNC_PASSWORD:+SET}"

# ---- 0. Clean stale RPyC FIFO ----
rm -f /opt/wineprefix/drive_c/server
echo "[entrypoint] Cleaned stale RPyC FIFO"

# ---- 1. Create VNC password file ----
mkdir -p /opt/wineprefix
if [ -n "${VNC_PASSWORD:-}" ]; then
    x11vnc -storepasswd "$VNC_PASSWORD" /opt/wineprefix/vnc_passwd
    echo "[entrypoint] VNC password file created"
else
    echo "[entrypoint] WARNING: VNC_PASSWORD not set — noVNC will be open"
fi

# ---- 2. Start Xvfb ----
echo "[entrypoint] Starting Xvfb on :0..."
Xvfb :0 -screen 0 1024x768x16 >/dev/null 2>&1 &
XVFB_PID=$!
sleep 2
echo "[entrypoint] Xvfb started (PID: $XVFB_PID)"

# ---- 3. Start x11vnc ----
echo "[entrypoint] Starting x11vnc on port 5900..."
x11vnc -display :0 -rfbport 5900 -rfbauth /opt/wineprefix/vnc_passwd -forever -shared -noxdamage -nowf -nowcr -noscr >/dev/null 2>&1 &
X11VNC_PID=$!
sleep 1
echo "[entrypoint] x11vnc started (PID: $X11VNC_PID)"

# ---- 4. Start noVNC (WebSocket proxy + HTTP) ----
echo "[entrypoint] Starting noVNC WebSocket proxy on 5901..."
/opt/websockify 5901 localhost:5900 >/dev/null 2>&1 &
WEBSOCKIFY_PID=$!

echo "[entrypoint] Starting noVNC HTTP server on 8080..."
cd /opt/noVNC && python3 -m http.server 8080 >/dev/null 2>&1 &
NOVNC_HTTP_PID=$!
sleep 1
echo "[entrypoint] noVNC started (WebSocket PID: $WEBSOCKIFY_PID, HTTP PID: $NOVNC_HTTP_PID)"

# ---- 5. Initialize Wine and launch MT5 terminal with auto-login ----
export WINEPREFIX=/opt/wineprefix
export DISPLAY=:0
export WINEDEBUG=-all

echo "[entrypoint] Initializing Wine..."
wine64 wineboot --init >/dev/null 2>&1
sleep 3

MT5_LOGIN="${MT5_LOGIN:-}"
MT5_PASSWORD="${MT5_PASSWORD:-}"
MT5_SERVER="${MT5_SERVER:-}"

if [ -n "$MT5_LOGIN" ] && [ -n "$MT5_PASSWORD" ] && [ -n "$MT5_SERVER" ]; then
    echo "[entrypoint] Launching MT5 terminal with auto-login..."
    echo "[entrypoint]   Login: $MT5_LOGIN"
    echo "[entrypoint]   Server: $MT5_SERVER"
    # Don't suppress output — we need to see login success/failure
    wine64 "C:/MT5/terminal64.exe" "/login:$MT5_LOGIN" "/password:$MT5_PASSWORD" "/server:$MT5_SERVER" &
else
    echo "[entrypoint] WARNING: No MT5 credentials — launching without auto-login"
    echo "[entrypoint] Log in manually via noVNC (http://localhost:8080, password: \$VNC_PASSWORD)"
    wine64 "C:/MT5/terminal64.exe" &
fi
MT5_PID=$!
sleep 8  # Give MT5 time to connect and log in
echo "[entrypoint] MT5 terminal launched (PID: $MT5_PID)"

# ---- 6. Start RPyC server ----
echo "[entrypoint] Starting RPyC server on 0.0.0.0:18812..."
wine64 /opt/wineprefix/drive_c/mt5server.exe -p 18812 >/dev/null 2>&1 &
RPYC_PID=$!
sleep 3
echo "[entrypoint] RPyC server started (PID: $RPYC_PID)"

# ---- 7. Verify MT5 is actually logged in ----
echo "[entrypoint] Verifying MT5 login via RPyC..."
for i in 1 2 3 4 5 6; do
    if python3 -c "
import sys
sys.path.insert(0, '/opt/wineprefix/drive_c')
import rpyc
try:
    c = rpyc.classic.connect('localhost', 18812, timeout=5)
    mt5 = c.modules.MetaTrader5
    if mt5.initialize():
        acct = mt5.account_info()
        if acct and acct.login == int('${MT5_LOGIN:-0}'):
            print('LOGIN_VERIFIED')
            sys.exit(0)
    sys.exit(1)
except Exception as e:
    sys.exit(1)
" 2>/dev/null; then
        echo "[entrypoint] MT5 login VERIFIED (account $MT5_LOGIN)"
        break
    fi
    echo "[entrypoint] Waiting for MT5 login... ($i/6)"
    sleep 5
done

if ! python3 -c "
import sys
sys.path.insert(0, '/opt/wineprefix/drive_c')
import rpyc
try:
    c = rpyc.classic.connect('localhost', 18812, timeout=5)
    mt5 = c.modules.MetaTrader5
    if mt5.initialize():
        acct = mt5.account_info()
        if acct and acct.login == int('${MT5_LOGIN:-0}'):
            sys.exit(0)
    sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null; then
    echo "[entrypoint] WARNING: MT5 login not verified after 30s — check noVNC"
fi

# ---- 8. Watchdog ----
echo "[entrypoint] All services started. Watching MT5 terminal (PID: $MT5_PID)..."
while true; do
    if ! kill -0 $MT5_PID 2>/dev/null; then
        echo "[watchdog] MT5 terminal died — restarting..."
        rm -f /opt/wineprefix/drive_c/server
        if [ -n "$MT5_LOGIN" ] && [ -n "$MT5_PASSWORD" ] && [ -n "$MT5_SERVER" ]; then
            wine64 "C:/MT5/terminal64.exe" "/login:$MT5_LOGIN" "/password:$MT5_PASSWORD" "/server:$MT5_SERVER" &
        else
            wine64 "C:/MT5/terminal64.exe" &
        fi
        MT5_PID=$!
        echo "[watchdog] MT5 terminal restarted (PID: $MT5_PID)"
        sleep 8
    fi
    sleep 10
done