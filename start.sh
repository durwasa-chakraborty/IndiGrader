#!/bin/bash
# ==============================================================================
# IndiGrader Startup Script
# ==============================================================================

echo -e "\033[1;36m[*] Starting IndiGrader Server...\033[0m"

# ---------------------------------------------------------------------------
# Native, or containerised?
#
# Grading needs Linux with firejail, GNU time and ulimit -v. When this machine
# cannot provide that (a Mac, or a Linux box without firejail) and Docker is
# available, run the whole lab inside the image instead. Identical environment,
# same ./start.sh. Set IG_NATIVE=1 to force the native path.
# ---------------------------------------------------------------------------
IG_PORT="${IG_PORT:-8000}"
CONTAINER_NAME="indigrader-$(basename "$PWD" | tr '[:upper:]' '[:lower:]')"
IMAGE_NAME="${IG_IMAGE:-indigrader:local}"

if [ -z "$IG_IN_CONTAINER" ] && [ "$IG_NATIVE" != "1" ]; then
    WHY=""
    if [ "$(uname -s)" != "Linux" ]; then
        WHY="this is $(uname -s), and grading needs Linux"
    elif ! command -v firejail >/dev/null 2>&1; then
        WHY="firejail is not installed"
    fi

    if [ -n "$WHY" ]; then
        if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
            echo -e "\033[1;33m[*] $WHY.\033[0m"
            echo -e "\033[1;33m[*] Running the lab in Docker instead, so grading behaves exactly as it will on the lab server.\033[0m"

            if [ ! -f Dockerfile ]; then
                echo -e "\033[0;31m[-] ERROR: Dockerfile not found in this package.\033[0m"
                exit 1
            fi
            if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
                echo -e "\033[1;32m[+] Building $IMAGE_NAME (first run only, takes a minute)...\033[0m"
                docker build -t "$IMAGE_NAME" -f Dockerfile . || exit 1
            fi

            docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
            echo -e "\033[1;32m[+] Starting container $CONTAINER_NAME...\033[0m"

            # Networking matters here. With published ports, Docker rewrites every
            # client's source address to the gateway, so all students would look
            # like one IP and per-student binding would be meaningless. On Linux we
            # use host networking, which preserves real client addresses. Elsewhere
            # (a developer's Mac) we publish the port and trust the gateway range,
            # which is fine for a demo and is announced as not production-safe.
            if [ "$(uname -s)" = "Linux" ]; then
                docker run -d --name "$CONTAINER_NAME" \
                    --network host \
                    -v "$PWD:/lab" \
                    -w /lab \
                    "$IMAGE_NAME" >/dev/null || exit 1
            else
                echo -e "\033[1;33m[!] Not Linux: publishing port ${IG_PORT} and trusting the Docker gateway.\033[0m"
                echo -e "\033[1;33m    Per-student IP binding is NOT enforced in this mode. Use it to\033[0m"
                echo -e "\033[1;33m    develop and demo, never to run a real lab.\033[0m"
                docker run -d --name "$CONTAINER_NAME" \
                    -p "${IG_PORT}:8000" \
                    -e IG_EXTRA_SUBNETS="192.168.65.,172.17.,172.18.,172.19.,10.88." \
                    -v "$PWD:/lab" \
                    -w /lab \
                    "$IMAGE_NAME" >/dev/null || exit 1
            fi

            echo -e "\033[1;33m[*] Waiting for the server to come up...\033[0m"
            for i in $(seq 1 60); do
                if curl -fsS "http://localhost:${IG_PORT}/api/admin/ping" >/dev/null 2>&1; then
                    echo -e "\033[1;32m[+] All services started successfully!\033[0m"
                    echo -e "\033[1;36m------------------------------------------------------\033[0m"
                    echo -e "\033[1;36m[*] CONTROL ROOM: \033[1;33mhttp://localhost:${IG_PORT}/admin\033[0m"
                    echo -e "\033[1;30m   (running in container $CONTAINER_NAME)\033[0m"
                    echo -e "\033[1;36m[*] Logs:  docker logs -f $CONTAINER_NAME   (also in logs/)\033[0m"
                    echo -e "\033[1;33m[-] Stop:  ./stop.sh\033[0m"
                    echo -e "\033[1;36m------------------------------------------------------\033[0m"
                    exit 0
                fi
                sleep 1
            done
            echo -e "\033[0;31m[-] The container did not become healthy in 60s. Last output:\033[0m"
            docker logs --tail 30 "$CONTAINER_NAME"
            exit 1
        else
            echo -e "\033[1;33m[!] $WHY, and Docker is not available.\033[0m"
            echo -e "\033[1;33m    Continuing natively: the server and console will work,\033[0m"
            echo -e "\033[1;33m    but every submission will score zero.\033[0m"
        fi
    fi
fi


# Pre-flight Checks
echo -e "\033[1;34m[*] Running pre-flight checks...\033[0m"

# Read JSON without needing jq. python3 already has to be present to run any of
# this, so the lab server needs no extra system packages for config parsing.
json_get() {   # json_get <file> <dotted.path> [default]
    python3 - "$1" "$2" "${3-}" <<'PYEOF'
import json, sys
path, default = sys.argv[2], sys.argv[3]
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    print("")
    sys.exit(0)
cur = doc
try:
    for part in path.split("."):
        cur = cur[part]
except Exception:
    cur = None
if cur is None:
    cur = default
if isinstance(cur, bool):
    cur = "true" if cur else "false"
print(cur)
PYEOF
}

json_valid() {  # json_valid <file>
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" 2>/dev/null
}

if ! json_valid config.json; then
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
BROKER_URL="${IG_BROKER_URL:-$(json_get config.json broker_url "")}"
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
if [ -n "$IG_ADMIN_TOKEN" ] || [ -n "$(json_get config.json admin_token "")" ]; then
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
