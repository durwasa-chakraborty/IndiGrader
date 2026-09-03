#!/bin/bash
# ==============================================================================
# IndiGrader Startup Script
# ==============================================================================

echo -e "\033[1;36m[*] Starting IndiGrader Server...\033[0m"

# Pre-flight Checks
echo -e "\033[1;34m[*] Running pre-flight checks...\033[0m"
if ! command -v jq >/dev/null 2>&1; then
    echo -e "\033[0;31m[-] ERROR: jq is not installed, and this script needs it to read config.json.\033[0m"
    echo -e "\033[1;33m    sudo apt install jq        (or: brew install jq)\033[0m"
    exit 1
fi

if ! jq empty config.json 2>/dev/null; then
    echo -e "\033[0;31m[-] ERROR: config.json is missing or contains invalid JSON.\033[0m"
    exit 1
fi

# Grading runs inside firejail. Without it every submission fails its testcases,
# which looks like the students' code being wrong rather than the server being
# misconfigured, so say so up front.
if ! command -v firejail >/dev/null 2>&1; then
    echo -e "\033[1;33m[!] WARNING: firejail is not installed. grade.sh --sandbox will fail and\033[0m"
    echo -e "\033[1;33m    every submission will score zero. Install it before the lab:\033[0m"
    echo -e "\033[1;33m      sudo apt install firejail\033[0m"
fi

if ! ls statics/*.zip 1> /dev/null 2>&1; then
    echo -e "\033[0;31m[-] ERROR: No starter kit .zip file found in statics/ folder.\033[0m"
    exit 1
fi
echo -e "\033[1;32m[+] Pre-flight checks passed.\033[0m"

# Create logs directory if it doesn't exist
mkdir -p logs

# 1. Broker. Not necessarily Redis, and not necessarily on this machine:
#    IG_BROKER_URL wins, then broker_url in config.json, then the local default.
BROKER_URL="${IG_BROKER_URL:-$(jq -r '.broker_url // empty' config.json 2>/dev/null)}"
if [ -z "$BROKER_URL" ]; then BROKER_URL="redis://localhost:6379"; fi

# Ask kombu (which Celery uses anyway) whether it can actually connect. This is
# broker-agnostic, so it works for a remote Redis, a container, or AMQP.
broker_reachable() {
    python3 - "$BROKER_URL" <<'PYEOF' >/dev/null 2>&1
import sys
from kombu import Connection
try:
    conn = Connection(sys.argv[1])
    conn.ensure_connection(max_retries=1, timeout=3)
    conn.release()
except Exception:
    sys.exit(1)
PYEOF
}

if broker_reachable; then
    echo -e "\033[1;33m[*] Broker already reachable at $BROKER_URL\033[0m"
else
    # Only try to boot a local Redis when that is what we were pointed at.
    case "$BROKER_URL" in
        redis://localhost*|redis://127.0.0.1*)
            PORT=$(echo "$BROKER_URL" | sed -n 's#.*:\([0-9][0-9]*\).*#\1#p')
            if [ -z "$PORT" ]; then PORT=6379; fi

            # Prefer a system Redis, but fall back to the one redislite ships
            # inside the virtualenv, so the lab server needs no apt and no sudo.
            REDIS_BIN=""
            if command -v redis-server >/dev/null 2>&1; then
                REDIS_BIN="redis-server"
            else
                REDIS_BIN=$(python3 - <<'PYEOF' 2>/dev/null
import inspect, os
try:
    import redislite
    path = os.path.join(os.path.dirname(inspect.getfile(redislite)), "bin", "redis-server")
    print(path if os.path.exists(path) else "")
except Exception:
    print("")
PYEOF
)
                if [ -n "$REDIS_BIN" ]; then
                    echo -e "\033[1;33m[*] No system redis-server; using the one bundled with redislite in your virtualenv.\033[0m"
                fi
            fi

            if [ -n "$REDIS_BIN" ]; then
                echo -e "\033[1;32m[+] Starting Redis Server on port $PORT...\033[0m"
                mkdir -p logs
                "$REDIS_BIN" --port "$PORT" --daemonize yes --dir "$(pwd)/logs" --logfile "$(pwd)/logs/redis.log"
                sleep 1
            else
                echo -e "\033[0;31m[-] No redis-server found, on PATH or in the virtualenv.\033[0m"
            fi
            ;;
    esac

    if ! broker_reachable; then
        echo -e "\033[0;31m[-] ERROR: cannot reach the broker at $BROKER_URL\033[0m"
        echo -e "\033[0;31m    Nothing was started. Without a broker every submission fails\033[0m"
        echo -e "\033[0;31m    after a ~20s hang, so this refuses to start rather than pretend.\033[0m"
        echo -e "\033[1;33m    Fix it with one of:\033[0m"
        echo -e "\033[1;33m      pip install redislite                # no sudo; bundles redis in your venv\033[0m"
        echo -e "\033[1;33m      sudo apt install redis-server        # system-wide install\033[0m"
        echo -e "\033[1;33m      export IG_BROKER_URL=redis://<host>:6379   # use another machine\033[0m"
        echo -e "\033[1;33m      set \"broker_url\" in config.json\033[0m"
        exit 1
    fi
    echo -e "\033[1;32m[+] Broker reachable at $BROKER_URL\033[0m"
fi

# 2. Start Celery Worker in the background
echo -e "\033[1;32m[+] Starting Celery Worker...\033[0m"
celery -A task.capp worker --loglevel=info > logs/celery.log 2>&1 &
echo -e "\033[1;30m   (Celery logs available at: logs/celery.log)\033[0m"

# 3. Start FastAPI Server in the background
echo -e "\033[1;32m[+] Starting FastAPI Server...\033[0m"
fastapi run main.py > logs/fastapi.log 2>&1 &
echo -e "\033[1;30m   (FastAPI logs available at: logs/fastapi.log)\033[0m"

echo -e "\033[1;32m[+] All services started successfully!\033[0m"

# 4. Surface the Control Room (live monitoring + time extensions)
LAB_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$LAB_IP" ]; then LAB_IP=$(ipconfig getifaddr en0 2>/dev/null); fi
if [ -z "$LAB_IP" ]; then LAB_IP="<server-ip>"; fi

echo -e "\033[1;36m------------------------------------------------------\033[0m"
echo -e "\033[1;36m[*] CONTROL ROOM: \033[1;33mhttp://$LAB_IP:8000/admin\033[0m"
if [ -n "$IG_ADMIN_TOKEN" ] || [ -n "$(jq -r '.admin_token // empty' config.json 2>/dev/null)" ]; then
    # Not echoed: logs/ travels back inside the lab package after the session.
    echo -e "\033[1;36m[*] ADMIN TOKEN: \033[1;32mset\033[0m\033[1;36m - the console will ask for it\033[0m"
else
    echo -e "\033[1;33m[*] ADMIN TOKEN: not set - anyone on the lab subnet can open the console\033[0m"
fi
echo -e "\033[1;30m   (Extend the lab, watch the queue and track submissions from there.)\033[0m"
echo -e "\033[1;36m------------------------------------------------------\033[0m"
echo -e "\033[1;36m[*] To monitor the server, run: tail -f logs/fastapi.log\033[0m"
echo -e "\033[1;36m[*] To monitor grading, run:    tail -f logs/celery.log\033[0m"
echo -e "\033[1;33m[-] To stop safely, run:        ./stop.sh\033[0m"
echo -e "\033[1;36m------------------------------------------------------\033[0m"
