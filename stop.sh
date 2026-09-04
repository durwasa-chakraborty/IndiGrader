#!/bin/bash
# ==============================================================================
# IndiGrader Graceful Shutdown Script
# ==============================================================================


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

echo -e "\033[1;36m[*] Initiating graceful shutdown of IndiGrader...\033[0m"

# If this lab is running in a container, drain and stop it from the outside.
if [ -z "$IG_IN_CONTAINER" ] && command -v docker >/dev/null 2>&1; then
    CONTAINER_NAME="indigrader-$(basename "$PWD" | tr '[:upper:]' '[:lower:]')"
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER_NAME"; then
        echo -e "\033[1;34m[*] Lab is running in container $CONTAINER_NAME. Draining it first...\033[0m"
        docker exec "$CONTAINER_NAME" ./stop.sh
        RC=$?
        if [ $RC -ne 0 ]; then
            echo -e "\033[0;31m[-] The in-container shutdown refused to complete. Container left running.\033[0m"
            echo -e "\033[0;31m    Nothing has been discarded. Investigate, then re-run ./stop.sh\033[0m"
            exit $RC
        fi
        docker stop "$CONTAINER_NAME" >/dev/null && docker rm "$CONTAINER_NAME" >/dev/null
        echo -e "\033[1;32m[+] Container stopped and removed.\033[0m"
        echo -e "\033[1;32m[+] Shutdown Complete! It is now safe to zip this folder and take it back.\033[0m"
        exit 0
    fi
fi


# 1. Stop FastAPI to prevent new submissions
echo -e "\033[1;34m[*] Stopping FastAPI Server...\033[0m"
pkill -f "fastapi run main.py" || pkill -f "uvicorn main:app"
echo -e "\033[1;32m[+] FastAPI stopped. No new submissions will be accepted.\033[0m"

# 2. Wait for the grading queue to drain.
#
#    Counting the broker queue alone is not enough: Celery prefetches, so the
#    broker list empties within a second of a submission burst while the worker
#    still holds dozens of tasks reserved. Ask the worker itself.
outstanding() {
    python3 - <<'PYEOF' 2>/dev/null
from task import capp

# An absent queue is an EMPTY queue, not an unknown one: in Redis an empty list
# has no key, and a passive declare then raises NOT_FOUND. Reporting that as
# "unknown" would let this loop mistake a live backlog for a drained one.
queued = -1
try:
    from kombu import Connection
    with Connection(capp.conf.broker_url) as conn:
        conn.ensure_connection(max_retries=1, timeout=3)   # broker is definitely up
        try:
            queued = conn.default_channel.queue_declare(queue="celery", passive=True).message_count
        except Exception:
            try:
                queued = conn.default_channel.client.llen("celery")   # redis transport
            except Exception:
                queued = 0                                            # up, queue empty
except Exception:
    queued = -1                                                       # genuinely unreachable

active = reserved = 0
workers = 0
try:
    inspector = capp.control.inspect(timeout=3.0)
    act = inspector.active() or {}
    res = inspector.reserved() or {}
    workers = len(set(act) | set(res))
    active = sum(len(v) for v in act.values())
    reserved = sum(len(v) for v in res.values())
except Exception:
    pass

print(f"{max(queued,0)} {active} {reserved} {workers} {queued}")
PYEOF
}

echo -e "\033[1;33m[*] Waiting for Celery to process all pending submissions...\033[0m"
DEADLINE=$(( $(date +%s) + 900 ))    # never hang forever
ZEROES=0
while true; do
    read -r Q A R W RAWQ <<< "$(outstanding)"
    if [ -z "$W" ]; then
        echo -e "\n\033[0;31m[-] Could not query the workers. Not killing anything; investigate before zipping.\033[0m"
        echo -e "\033[0;31m    Check that the broker is reachable and the virtualenv is active.\033[0m"
        exit 1
    fi
    if [ "$W" -eq 0 ]; then
        echo -e "\n\033[1;33m[*] No Celery workers are running.\033[0m"
        if [ "$Q" -gt 0 ]; then
            echo -e "\033[0;31m[-] WARNING: $Q submission(s) are still queued and nothing is grading them.\033[0m"
            echo -e "\033[0;31m    Start a worker and re-run ./stop.sh, or those submissions go ungraded.\033[0m"
            exit 1
        fi
        break
    fi
    TOTAL=$(( Q + A + R ))
    if [ "$TOTAL" -eq 0 ]; then
        # Celery's prefetch buffer is not always visible as active/reserved, so a
        # single zero reading can be transient. Require a few in a row.
        ZEROES=$(( ZEROES + 1 ))
        if [ "$ZEROES" -ge 3 ]; then
            echo -e "\n\033[1;32m[+] Queue drained. Every submission has been graded.\033[0m"
            break
        fi
        sleep 2
        continue
    fi
    ZEROES=0
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        echo -e "\n\033[0;31m[-] Still $TOTAL outstanding after 15 minutes. Refusing to kill the worker.\033[0m"
        echo -e "\033[0;31m    Investigate (a submission may be stuck) and re-run ./stop.sh.\033[0m"
        exit 1
    fi
    if [ "$RAWQ" -lt 0 ]; then
        echo -ne "\r\033[1;33m[*] Outstanding: $TOTAL (running $A, reserved $R; broker unreachable)   \033[0m"
    else
        echo -ne "\r\033[1;33m[*] Outstanding: $TOTAL (queued $Q, running $A, reserved $R)   \033[0m"
    fi
    sleep 2
done

# 3. Warm shutdown. Broadcast it through Celery rather than signalling the
#    processes: `pkill -15 -f celery` also hits the forked pool children and
#    aborts whatever they are grading mid-task.
echo -e "\033[1;34m[*] Sending graceful shutdown signal to Celery workers...\033[0m"
python3 - <<'PYEOF' 2>/dev/null || pkill -15 -f "celery -A task.capp worker"
from task import capp
capp.control.shutdown()
PYEOF

echo -e "\033[1;33m[*] Waiting for Celery to wrap up active grading...\033[0m"
WAIT_UNTIL=$(( $(date +%s) + 120 ))
while pgrep -f "celery -A task.capp worker" > /dev/null; do
    if [ "$(date +%s)" -ge "$WAIT_UNTIL" ]; then
        echo -e "\n\033[1;33m[*] Worker still up after 2 minutes; sending SIGTERM to the main process.\033[0m"
        pkill -15 -f "celery -A task.capp worker"
        sleep 5
        break
    fi
    sleep 1
done
echo -e "\033[1;32m[+] Celery workers stopped.\033[0m"

# 4. Stop the broker only if this package started it (logs/redis.log is our marker).
if [ -f logs/redis.log ] && command -v redis-cli >/dev/null 2>&1; then
    BROKER_URL="${IG_BROKER_URL:-$(json_get config.json broker_url "")}"
    if [ -z "$BROKER_URL" ]; then BROKER_URL="redis://localhost:6379"; fi
    PORT=$(echo "$BROKER_URL" | sed -n 's#.*:\([0-9][0-9]*\).*#\1#p')
    if [ -z "$PORT" ]; then PORT=6379; fi
    echo -e "\033[1;34m[*] Stopping the Redis instance this package started...\033[0m"
    redis-cli -p "$PORT" shutdown nosave 2>/dev/null
fi

echo -e "\033[1;32m[+] Shutdown Complete! It is now safe to zip this folder and take it back.\033[0m"
