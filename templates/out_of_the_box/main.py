DEBUG = False

import os
import asyncio
import base64
import csv
import glob
import itertools
import json
import secrets
import shutil
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import (APIRouter, Body, Depends, FastAPI, File, Form,
                     HTTPException, Request, UploadFile, status)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from celery.result import AsyncResult
from task import capp, handle_submission

try:
    import redis as redis_lib
except ImportError:  # pragma: no cover - redis ships with the requirements
    redis_lib = None

# Globals
STUDENTS_FILE = "students.txt"
REGISTRATIONS_FILE = "registrations.csv"
VIOLATIONS_FILE = "violations.csv"
PWD_STUDENTS_FILE = "pwd_students.txt"
CONFIG_FILE = "config.json"
ADMIN_ACTIONS_FILE = "admin_actions.csv"
ADMIN_PAGE = "admin.html"

# Keys in config.json that are held in memory as datetime objects.
TIME_KEYS = ("start_time", "end_time", "pwd_end_time")

lab_config = {}
pwd_rolls = set()

student_list = set()
# fast lookups: { "ROLL_NO": "ip_address" } and its reverse
ip_roll_map = {}
roll_by_ip = {}

# Optional. Set it in config.json as "admin_token", or export IG_ADMIN_TOKEN
# before starting the server. Left unset, the console is guarded by the network
# rules alone (loopback + allowed_subnets), which is the right trade when the
# server sits in a locked room on a closed lab network.
ADMIN_TOKEN = None

# Additional subnets trusted on top of config.json's allowed_subnets, supplied by
# the environment. start.sh sets this only when it runs the lab in a container on
# a non-Linux host, where Docker rewrites every client's source address to the
# gateway. It is a development convenience: it means per-student IP binding is not
# enforced, so it is announced loudly at startup and on the dashboard.
EXTRA_SUBNETS = [x.strip() for x in os.environ.get("IG_EXTRA_SUBNETS", "").split(",") if x.strip()]

# config.json is re-read whenever its mtime changes, so `nano config.json` on the
# server takes effect without a restart. A bad edit is rejected and the last good
# copy keeps serving.
CONFIG_POLL_INTERVAL = 1.0          # seconds between mtime checks, at most
_config_mtime = None
_config_checked_at = 0.0
_config_error = None                # last parse failure, surfaced in /admin
_config_reloaded_at = None

file_lock = asyncio.Lock()
config_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Live monitoring state (in-memory, reset on restart)
# ---------------------------------------------------------------------------
SERVER_BOOT = time.time()
_req_seq = itertools.count(1)
_inflight = {}                      # request id -> record of a request in flight
_recent_requests = deque(maxlen=300)
_submission_log = deque(maxlen=300)  # submissions accepted by this process
_traffic = Counter()                # total / blocked / errors
_minute_hits = Counter()            # epoch-minute -> request count
_last_seen = {}                     # ROLL -> epoch of last request

# Paths that are pure monitoring noise; kept out of the request feed.
_UNLOGGED_PREFIXES = ("/api/admin", "/admin", "/favicon.ico")


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _client_ip(request: Request):
    return request.client.host if request.client else "127.0.0.1"


def _roll_for_ip(ip):
    return roll_by_ip.get(ip)


def _bind_roll(roll_no, ip):
    """Bind a roll number to an IP, keeping the reverse index consistent."""
    previous_ip = ip_roll_map.get(roll_no)
    if previous_ip and roll_by_ip.get(previous_ip) == roll_no:
        del roll_by_ip[previous_ip]
    ip_roll_map[roll_no] = ip
    roll_by_ip[ip] = roll_no


def _unbind_roll(roll_no):
    ip = ip_roll_map.pop(roll_no, None)
    if ip and roll_by_ip.get(ip) == roll_no:
        del roll_by_ip[ip]
    return ip


def _log_violation(kind, roll_no, expected_ip, actual_ip):
    write_header = not os.path.exists(VIOLATIONS_FILE)
    with open(VIOLATIONS_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "violation_type", "roll_no", "expected_ip", "actual_ip"])
        writer.writerow([datetime.now().isoformat(), kind, roll_no, expected_ip, actual_ip])


def _write_registrations():
    with open(REGISTRATIONS_FILE, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["roll_no", "ip_address"])  # Header
        for r_no, ip in ip_roll_map.items():
            writer.writerow([r_no, ip])


def _register(roll_no, client_ip):
    """Shared binding logic for /starter and /rebind, including violation logging."""
    registered_ip = ip_roll_map.get(roll_no)

    # Same roll number showing up from a different machine
    if registered_ip and registered_ip != client_ip:
        _log_violation("Re-register Violation", roll_no, registered_ip, client_ip)
        print(f"LOGGED: Re-registration for {roll_no}. IP changed from {registered_ip} to {client_ip}")

    # A different roll number trying to use an already bound IP
    old_roll_for_ip = roll_by_ip.get(client_ip)
    if old_roll_for_ip and old_roll_for_ip != roll_no:
        _log_violation("IP Collision", roll_no, "N/A", client_ip)
        print(f"LOGGED: IP Collision - {roll_no} logged from {client_ip} (previously bound to {old_roll_for_ip})")
        _unbind_roll(old_roll_for_ip)

    _bind_roll(roll_no, client_ip)
    _write_registrations()


# ---------------------------------------------------------------------------
# config.json read / write
# ---------------------------------------------------------------------------
def _config_for_disk():
    out = {}
    for key, value in lab_config.items():
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _parse_config(raw):
    """Validate a config.json payload and coerce its timestamps.

    Raises on anything malformed so a half-written or fat-fingered file can
    never replace a working configuration."""
    if not isinstance(raw, dict):
        raise ValueError("config.json must contain a JSON object")
    parsed = dict(raw)
    for key in TIME_KEYS:
        value = parsed.get(key)
        if value in (None, ""):
            parsed[key] = None
            continue
        if isinstance(value, datetime):
            continue
        parsed[key] = datetime.fromisoformat(value)
    for key in ("start_time", "end_time"):
        if not isinstance(parsed.get(key), datetime):
            raise ValueError(f"{key} is missing or unparseable")
    if parsed["end_time"] <= parsed["start_time"]:
        raise ValueError("end_time must be after start_time")
    if not parsed.get("lab_name"):
        raise ValueError("lab_name is missing")
    return parsed


def _load_config_sync():
    with open(CONFIG_FILE, "r") as f:
        return _parse_config(json.load(f))


def _describe_config_change(old_cfg, new_cfg):
    fields = list(TIME_KEYS) + ["allowed_subnets", "questions", "admin_token"]
    for qno in new_cfg.get("questions", []) or []:
        fields.append(qno)
    changes = []
    for key in fields:
        before, after = old_cfg.get(key), new_cfg.get(key)
        if before != after:
            label = "set" if key == "admin_token" else after
            prior = "set" if key == "admin_token" else before
            changes.append(f"{key}: {prior} -> {label}")
    return "; ".join(changes) or "no effective change"


async def _maybe_reload_config():
    """Pick up an edit to config.json made outside the console."""
    global _config_mtime, _config_checked_at, _config_error, _config_reloaded_at, ADMIN_TOKEN

    now = time.time()
    if now - _config_checked_at < CONFIG_POLL_INTERVAL:
        return
    _config_checked_at = now

    try:
        mtime = os.stat(CONFIG_FILE).st_mtime_ns
    except OSError:
        return
    if mtime == _config_mtime:
        return

    async with config_lock:
        try:
            mtime = os.stat(CONFIG_FILE).st_mtime_ns
        except OSError:
            return
        if mtime == _config_mtime:
            return
        try:
            fresh = await asyncio.to_thread(_load_config_sync)
        except Exception as exc:
            # A truncated save or a JSON typo: keep serving the last good copy
            # and let the console show what is wrong.
            if str(exc) != _config_error:
                print(f"CONFIG: refusing to reload config.json ({exc}). Still using the previous configuration.")
            _config_error = str(exc)
            _config_mtime = mtime      # do not re-report until the file changes again
            return

        summary = _describe_config_change(lab_config, fresh)
        lab_config.clear()
        lab_config.update(fresh)
        _config_mtime = mtime
        _config_error = None
        _config_reloaded_at = datetime.now()
        if not os.environ.get("IG_ADMIN_TOKEN"):
            ADMIN_TOKEN = (lab_config.get("admin_token") or "").strip() or None
        if summary != "no effective change":
            print(f"CONFIG: reloaded config.json from disk -> {summary}")
            _audit("config_reloaded_from_disk", summary, "file edit")


def _persist_config_sync():
    """Atomically rewrite config.json so the Celery worker never reads a torn file."""
    tmp_path = f"{CONFIG_FILE}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(_config_for_disk(), f, indent=4)
    os.replace(tmp_path, CONFIG_FILE)
    global _config_mtime
    try:
        _config_mtime = os.stat(CONFIG_FILE).st_mtime_ns
    except OSError:
        _config_mtime = None


async def _persist_config():
    async with config_lock:
        await asyncio.to_thread(_persist_config_sync)


def _audit(action, detail, actor_ip):
    write_header = not os.path.exists(ADMIN_ACTIONS_FILE)
    with open(ADMIN_ACTIONS_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "actor_ip", "action", "detail"])
        writer.writerow([datetime.now().isoformat(), actor_ip, action, detail])
    print(f"ADMIN: {action} -> {detail} (from {actor_ip})")


def _question_list():
    questions = lab_config.get("questions")
    if isinstance(questions, list) and questions:
        return [str(q).upper() for q in questions]
    found = set()
    for base in ("submissions", "late_submissions"):
        if os.path.isdir(base):
            found.update(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    return sorted(found)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global lab_config, ADMIN_TOKEN, _config_mtime
    print("Application starting up...")

    print("Loading config.json...")
    lab_config = _load_config_sync()
    try:
        _config_mtime = os.stat(CONFIG_FILE).st_mtime_ns
    except OSError:
        _config_mtime = None

    print("Loading pwd_students.txt...")
    if os.path.exists(PWD_STUDENTS_FILE):
        with open(PWD_STUDENTS_FILE, mode='r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    pwd_rolls.add(line.strip().upper())

    if os.path.exists(STUDENTS_FILE):
        with open(STUDENTS_FILE, mode='r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    student_list.add(line.strip())

    if len(student_list) == 0:
        print("No student in the class. Exiting...")
        return

    if os.path.exists(REGISTRATIONS_FILE):
        with open(REGISTRATIONS_FILE, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            try:
                next(reader)  # Skip header
            except StopIteration:
                pass  # File is empty

            for row in reader:
                if row and len(row) == 2:
                    roll_no, ip_address = row
                    _bind_roll(roll_no, ip_address)

    print(f"Loaded {len(ip_roll_map)} registrations into memory.")

    # Env wins over config.json so a token can be set without editing the file.
    ADMIN_TOKEN = (os.environ.get("IG_ADMIN_TOKEN") or lab_config.get("admin_token") or "").strip() or None

    print("=" * 62)
    print("  CONTROL ROOM  : http://<this-server>:8000/admin")
    print(f"  LAB WINDOW    : {lab_config['start_time']}  ->  {lab_config['end_time']}")
    print(f"  REACHABLE FROM: loopback + {lab_config.get('allowed_subnets', [])}")
    if EXTRA_SUBNETS:
        print("  " + "!" * 58)
        print(f"  !! IG_EXTRA_SUBNETS is set: also trusting {EXTRA_SUBNETS}")
        print("  !! Clients behind those addresses share one source IP, so")
        print("  !! per-student IP binding is NOT enforced. Development only.")
        print("  " + "!" * 58)
    if ADMIN_TOKEN:
        # Deliberately not echoed: logs/ travels back inside the lab package.
        print("  ADMIN TOKEN   : required (set - not printed)")
    else:
        print("  ADMIN TOKEN   : not set (network rules are the only control)")
    print("=" * 62)

    yield
    print("Application shutting down...")

app = FastAPI(lifespan=lifespan)

app.mount("/clients", StaticFiles(directory="clients"), name="clients")


def _is_admin_path(path):
    return path == "/admin" or path.startswith("/api/admin")


async def _access_control(request: Request, call_next):
    # Cheap: one os.stat at most once a second. Lets `nano config.json` on the
    # server take effect on this very request, no restart needed.
    await _maybe_reload_config()

    client_ip = _client_ip(request)
    request_path = request.url.path
    current_time = datetime.now()

    is_allowed_public_path = False
    if (request_path == "/leaderboard" or
        request_path == "/api/leaderboard" or
        request_path.startswith("/api/history/") or
        request_path.startswith("/download/") or
        request_path.startswith("/clients/") or
        request_path == "/submission-status" or
            request_path == "/favicon.ico"):

        is_allowed_public_path = True

    if request_path in ["/docs", "/redoc", "/openapi.json"]:
        is_allowed_public_path = True

    is_admin_path = _is_admin_path(request_path)

    # The console is how the lab gets extended, so it must stay reachable from
    # the server itself no matter what the clock or the subnet rules say.
    # Off-loopback it falls through to the usual allowed_subnets check below.
    if is_admin_path and client_ip in ("127.0.0.1", "::1", "localhost"):
        return await call_next(request)

    roll_no = _roll_for_ip(client_ip)
    is_privileged = roll_no in pwd_rolls if roll_no else False
    # A PWD student stays privileged only until their own deadline, when one is
    # configured. Without pwd_end_time their window is open-ended, as before.
    pwd_deadline = lab_config.get("pwd_end_time")
    if is_privileged and pwd_deadline and current_time > pwd_deadline:
        is_privileged = False

    # Reject everyone before start time
    # if current_time < lab_config["start_time"]:
    #     return JSONResponse(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         content={"detail": "Lab has not started yet. Access denied."}
    #     )

    # Reject if after end time and not privileged
    if not DEBUG and not is_allowed_public_path and not is_admin_path and current_time > lab_config["end_time"] and not is_privileged:
        path = request.url.path
        if not (path.startswith("/submit/") or path.startswith("/history/") or path.startswith("/task-status/") or path.startswith("/download/")):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Lab has ended. No more submissions allowed."}
            )

    allowed_subnets = list(lab_config.get("allowed_subnets", ["127.0."])) + EXTRA_SUBNETS
    is_safe_ip = False
    if client_ip:
        for subnet in allowed_subnets:
            if client_ip.startswith(subnet):
                is_safe_ip = True
                break

    if DEBUG:
        is_safe_ip = True

    if is_safe_ip:
        response = await call_next(request)
        return response

    if is_allowed_public_path:
        print(f"ALLOWED: Non-standard IP {client_ip} accessing public endpoint {request_path}")
        response = await call_next(request)
        return response

    print(f"BLOCKED: Non-standard IP {client_ip} tried to access restricted path {request_path}")

    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": f"Access from your IP ({client_ip}) to this endpoint is not permitted."}
    )


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Wraps access control so every request lands in the live monitor."""
    path = request.url.path
    tracked = not path.startswith(_UNLOGGED_PREFIXES)

    client_ip = _client_ip(request)
    roll_no = _roll_for_ip(client_ip)
    if roll_no:
        _last_seen[roll_no] = time.time()

    if not tracked:
        return await _access_control(request, call_next)

    req_id = next(_req_seq)
    started_wall = time.time()
    started = time.perf_counter()
    _inflight[req_id] = {
        "id": req_id,
        "method": request.method,
        "path": path,
        "ip": client_ip,
        "roll": roll_no,
        "started": started_wall,
    }
    _minute_hits[int(started_wall // 60)] += 1
    _traffic["total"] += 1

    status_code = 500
    try:
        response = await _access_control(request, call_next)
        status_code = response.status_code
        return response
    finally:
        _inflight.pop(req_id, None)
        if status_code == 403:
            _traffic["blocked"] += 1
        elif status_code >= 500:
            _traffic["errors"] += 1
        _recent_requests.appendleft({
            "id": req_id,
            "time": datetime.fromtimestamp(started_wall).strftime("%H:%M:%S"),
            "method": request.method,
            "path": path,
            "ip": client_ip,
            "roll": roll_no,
            "status": status_code,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        })


# Registration and IP logging
@app.get("/starter/{roll_no}")
async def starter_kit(request: Request, roll_no: str):
    client_ip = _client_ip(request)
    capitalized_roll_no = roll_no.upper()

    if capitalized_roll_no not in student_list and roll_no not in student_list:
        return JSONResponse({
            "response": "YOU ARE NOT REGISTERED!!!"
        })

    async with file_lock:
        _register(capitalized_roll_no, client_ip)

    path_to_file = f"./statics/{lab_config['lab_name']}.zip"
    if not os.path.exists(path_to_file):
        raise HTTPException(status_code=404, detail="Starter kit not found on server")

    return FileResponse(
        path=path_to_file,
        filename=f"{lab_config['lab_name']}.zip",
        media_type='application/zip'
    )

# Re-registration
@app.get("/rebind/{roll_no}")
async def rebind(request: Request, roll_no: str):
    client_ip = _client_ip(request)
    capitalized_roll_no = roll_no.upper()

    async with file_lock:
        if not ip_roll_map.get(capitalized_roll_no):
            return JSONResponse(
                status_code=403,
                content={"response": "You are not registered, rebinding FORBIDDEN!!!"}
            )
        _register(capitalized_roll_no, client_ip)

    return JSONResponse({
        "Status": "Re-registration Successfull!!!"
    })


# Leaderboard UI
@app.get("/leaderboard", response_class=HTMLResponse)
async def serve_leaderboard_ui(request: Request):
    try:
        return FileResponse("leaderboard.html")
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "leaderboard.html not found."})

# Leaderboard API
@app.get("/api/leaderboard")
async def get_leaderboard_data():
    base_dir = "submissions"
    if not os.path.isdir(base_dir):
        return JSONResponse(status_code=404, content={"detail": "No submissions found."})

    student_scores = {}

    for qno in os.listdir(base_dir):
        q_dir = os.path.join(base_dir, qno)
        if os.path.isdir(q_dir):
            for roll_dir in os.listdir(q_dir):
                student_path = os.path.join(q_dir, roll_dir)
                if os.path.isdir(student_path):
                    marks_log_path = os.path.join(student_path, "marks.txt")
                    if os.path.exists(marks_log_path):
                        max_marks = -1
                        try:
                            with open(marks_log_path, "r") as f:
                                for line in f:
                                    try:
                                        marks = float(line.strip().split(',')[1])
                                        if marks > max_marks:
                                            max_marks = marks
                                    except (IndexError, ValueError):
                                        continue
                            if max_marks != -1:
                                if roll_dir not in student_scores:
                                    student_scores[roll_dir] = {"scores": {}, "total_marks": 0.0}
                                student_scores[roll_dir]["scores"][qno] = max_marks
                                student_scores[roll_dir]["total_marks"] += max_marks
                        except Exception:
                            continue

    leaderboard_data = []
    for roll, data in student_scores.items():
        leaderboard_data.append({
            "roll": roll,
            "scores": data["scores"],
            "total_marks": data["total_marks"]
        })

    if not leaderboard_data:
        return JSONResponse(content=[])

    # Sort by total_marks (descending) to determine ranks
    sorted_data = sorted(leaderboard_data, key=lambda x: x["total_marks"], reverse=True)

    # Assign ranks
    ranked_leaderboard = []
    last_mark = -1
    current_rank = 0
    for i, entry in enumerate(sorted_data):
        if entry["total_marks"] != last_mark:
            current_rank = i + 1

        entry["rank"] = current_rank
        ranked_leaderboard.append(entry)
        last_mark = entry["total_marks"]

    return JSONResponse(content=ranked_leaderboard)

# History API
@app.get("/api/history/{qno}")
async def get_history(request: Request, qno: str):
    client_ip = _client_ip(request)

    # Authenticate via ip_roll_map
    roll_no = _roll_for_ip(client_ip)

    if not roll_no:
        return JSONResponse(status_code=403, content={"detail": "You are not registered from this IP."})

    qno_upper = qno.upper()
    if qno_upper.isdigit():
        qno_upper = f"Q{qno_upper}"
    marks_log_path = os.path.join("submissions", qno_upper, roll_no, "marks.txt")

    if not os.path.exists(marks_log_path):
        return JSONResponse(content=[])

    history_data = []
    serial = 1
    with open(marks_log_path, "r") as f:
        for line in f:
            try:
                parts = line.strip().split(',')
                timestamp = parts[0].strip()
                marks = float(parts[1].strip())
                history_data.append({
                    "sn": serial,
                    "timestamp": timestamp,
                    "marks": marks
                })
                serial += 1
            except (IndexError, ValueError):
                continue

    return JSONResponse(content=history_data)

# Download API
@app.get("/download/{qno}/{sn}")
async def download_submission(request: Request, qno: str, sn: int):
    client_ip = _client_ip(request)

    # Authenticate via ip_roll_map
    roll_no = _roll_for_ip(client_ip)

    if not roll_no:
        return JSONResponse(status_code=403, content={"detail": "You are not registered from this IP."})

    qno_upper = qno.upper()
    if qno_upper.isdigit():
        qno_upper = f"Q{qno_upper}"
    marks_log_path = os.path.join("submissions", qno_upper, roll_no, "marks.txt")

    if not os.path.exists(marks_log_path):
        raise HTTPException(status_code=404, detail="No submissions found.")

    target_timestamp = None
    current_sn = 1
    with open(marks_log_path, "r") as f:
        for line in f:
            try:
                parts = line.strip().split(',')
                if current_sn == sn:
                    target_timestamp = parts[0].strip()
                    break
                current_sn += 1
            except IndexError:
                continue

    if not target_timestamp:
        raise HTTPException(status_code=404, detail="Invalid Serial Number.")

    # Search for the source code file matching the timestamp
    student_dir = os.path.join("submissions", qno_upper, roll_no)
    search_pattern = os.path.join(student_dir, f"*_{target_timestamp}.*")

    matching_files = glob.glob(search_pattern)
    source_file = None

    for file in matching_files:
        # Exclude the result logs and submission executables
        if not file.endswith(".txt") and not file.endswith(".out"):
            source_file = file
            break

    if not source_file:
        raise HTTPException(status_code=404, detail="Source code file not found for this submission.")

    return FileResponse(
        path=source_file,
        filename=os.path.basename(source_file),
        media_type='application/octet-stream'
    )

# Async Grading Submission Endpoint
@app.post("/submit/{qno}")
async def handleSubmit(
    qno: str,
    request: Request,
    roll: str = Form(...),
    file: UploadFile = File(...)
):

    client_ip = _client_ip(request)
    qno_upper = qno.upper()
    if "questions" in lab_config and qno_upper not in lab_config["questions"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid question '{qno_upper}'. Allowed questions are: {', '.join(lab_config['questions'])}")

    roll_upper = roll.upper()

    registered_ip = ip_roll_map.get(roll_upper)
    if not registered_ip:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Roll number '{roll_upper}' is not registered. Please call /starter/{{your_roll_no}} first.")

    if client_ip != registered_ip:
        async with file_lock:
            _log_violation("Submit Violation", roll_upper, registered_ip, client_ip)

        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Violation: Submission from an unregistered IP. Incident logged.")

    file_content = await file.read()

    current_time = datetime.now()
    submission_timestamp = current_time.strftime("%Y%m%d-%H%M%S")
    is_late = False
    is_privileged = roll_upper in pwd_rolls
    if is_privileged:
        # PWD students run to their own deadline; with none configured their
        # window stays open-ended.
        pwd_deadline = lab_config.get("pwd_end_time")
        past_deadline = bool(pwd_deadline) and current_time > pwd_deadline
    else:
        past_deadline = current_time > lab_config["end_time"]

    if not DEBUG and past_deadline:
        # Check if already submitted late for this question
        late_dir = os.path.join("late_submissions", qno_upper, roll_upper)
        if os.path.exists(late_dir) and len(os.listdir(late_dir)) > 0:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"You have already exhausted your single late submission for {qno_upper}.")

        is_late = True

    work = handle_submission.delay(qno_upper, roll_upper, file.filename, file_content, is_late, submission_timestamp)

    _submission_log.appendleft({
        "time": datetime.now().strftime("%H:%M:%S"),
        "roll": roll_upper,
        "qno": qno_upper,
        "filename": file.filename,
        "bytes": len(file_content),
        "late": is_late,
        "task_id": work.id,
    })

    return JSONResponse(
        {
            "taskid": work.id
        }
    )


@app.get("/submission-status")
async def get_submission_status():
    base_submission_dir = "submissions"

    question_dirs = _question_list()

    if not question_dirs:
        return JSONResponse(
            status_code=404,
            content={"detail": "No question submission directories found in 'submissions'."}
        )

    registered_students = ip_roll_map.keys()

    report = {}

    for roll_no in registered_students:
        student_status = {}
        for qno in question_dirs:
            submission_path = os.path.join(base_submission_dir, qno, roll_no)
            student_status[qno] = os.path.isdir(submission_path)

        report[roll_no] = student_status

    return JSONResponse(content=report)


@app.get("/task-status/{task_id}")
async def get_task_status(task_id: str):

    async_result = AsyncResult(task_id)
    return JSONResponse(
        {
            "task-id": task_id,
            "result": async_result.result,
            "status": async_result.status
        }
    )


# ===========================================================================
# Admin console
# ===========================================================================
_marks_cache = {}
_scan_cache = {"stamp": 0.0, "data": None}
_broker_cache = {"stamp": 0.0, "data": None}
_broker_lock = asyncio.Lock()
SCAN_TTL = 2.0
BROKER_TTL = 3.0
QUEUE_PEEK = 8              # how many waiting submissions to name
QUEUE_PEEK_MAX_BYTES = 1_000_000   # skip decoding a pathologically large message


def _parse_marks(path):
    """Read a marks.txt, memoised on (mtime, size) so polling stays cheap."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    hit = _marks_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]

    rows = []
    try:
        with open(path, "r") as f:
            for line in f:
                parts = line.strip().split(',')
                if len(parts) < 2:
                    continue
                try:
                    rows.append((parts[0].strip(), float(parts[1].strip())))
                except ValueError:
                    continue
    except OSError:
        return []

    _marks_cache[path] = (key, rows)
    return rows


def _stamp_to_dt(stamp):
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _scan_submissions():
    """Aggregate every marks.txt on disk into per-student / per-question views."""
    now = time.time()
    if _scan_cache["data"] is not None and now - _scan_cache["stamp"] < SCAN_TTL:
        return _scan_cache["data"]

    questions = _question_list()
    per_student = {}
    per_question = {}
    events = []

    def q_bucket(qno):
        return per_question.setdefault(qno, {
            "qno": qno,
            "attempts": 0,
            "students": set(),
            "best_scores": [],
            "perfect": 0,
        })

    for qno in questions:
        q_bucket(qno)

    for base, late in (("submissions", False), ("late_submissions", True)):
        if not os.path.isdir(base):
            continue
        for qno in sorted(os.listdir(base)):
            q_dir = os.path.join(base, qno)
            if not os.path.isdir(q_dir):
                continue
            for roll in sorted(os.listdir(q_dir)):
                marks_path = os.path.join(q_dir, roll, "marks.txt")
                rows = _parse_marks(marks_path)
                if not rows:
                    continue

                student = per_student.setdefault(roll, {
                    "roll": roll,
                    "questions": {},
                    "attempts": 0,
                    "total": 0.0,
                    "last": None,
                    "late": 0,
                })
                entry = student["questions"].setdefault(qno, {"best": 0.0, "attempts": 0, "last": None})

                for stamp, marks in rows:
                    entry["best"] = max(entry["best"], marks)
                    entry["attempts"] += 1
                    if entry["last"] is None or stamp > entry["last"]:
                        entry["last"] = stamp
                    student["attempts"] += 1
                    if student["last"] is None or stamp > student["last"]:
                        student["last"] = stamp
                    if late:
                        student["late"] += 1
                    events.append({
                        "stamp": stamp,
                        "roll": roll,
                        "qno": qno,
                        "marks": marks,
                        "late": late,
                    })

                bucket = q_bucket(qno)
                bucket["attempts"] += len(rows)
                bucket["students"].add(roll)

    for student in per_student.values():
        student["total"] = round(sum(q["best"] for q in student["questions"].values()), 2)
        for qno, q in student["questions"].items():
            q_bucket(qno)["best_scores"].append(q["best"])

    questions_out = []
    for qno in sorted(per_question.keys()):
        bucket = per_question[qno]
        conf = lab_config.get(qno, {}) if isinstance(lab_config.get(qno), dict) else {}
        full_marks = float(conf.get("full_marks", 0) or 0)
        scores = bucket["best_scores"]
        perfect = sum(1 for s in scores if full_marks and s >= full_marks)
        questions_out.append({
            "qno": qno,
            "full_marks": full_marks,
            "timeout": conf.get("timeout"),
            "memory_cap_mb": conf.get("memory_cap_mb"),
            "attempts": bucket["attempts"],
            "students": len(bucket["students"]),
            "avg": round(sum(scores) / len(scores), 2) if scores else 0.0,
            "max": round(max(scores), 2) if scores else 0.0,
            "perfect": perfect,
        })

    events.sort(key=lambda e: e["stamp"], reverse=True)

    data = {
        "per_student": per_student,
        "questions": questions_out,
        "events": events,
        "totals": {
            "attempts": sum(q["attempts"] for q in questions_out),
            "students_submitted": len(per_student),
            "late": sum(1 for e in events if e["late"]),
        },
    }
    _scan_cache["stamp"] = now
    _scan_cache["data"] = data
    return data


def _timeline(events, minutes=45):
    """Graded-submission counts per minute for the activity chart."""
    now = datetime.now().replace(second=0, microsecond=0)
    buckets = {now - timedelta(minutes=i): 0 for i in range(minutes)}
    for event in events:
        dt = _stamp_to_dt(event["stamp"])
        if dt is None:
            continue
        key = dt.replace(second=0, microsecond=0)
        if key in buckets:
            buckets[key] += 1
    ordered = sorted(buckets.items())
    return [{"t": k.strftime("%H:%M"), "n": v} for k, v in ordered]


def _request_timeline(minutes=45):
    now_minute = int(time.time() // 60)
    out = []
    for i in range(minutes - 1, -1, -1):
        minute = now_minute - i
        out.append({
            "t": datetime.fromtimestamp(minute * 60).strftime("%H:%M"),
            "n": _minute_hits.get(minute, 0),
        })
    # keep the counter from growing forever
    for key in [k for k in _minute_hits if k < now_minute - 180]:
        del _minute_hits[key]
    return out


def _decode_queued(raw):
    """Pull roll / question out of a queued Celery message without the payload."""
    try:
        if len(raw) > QUEUE_PEEK_MAX_BYTES:
            return {"task_id": None, "qno": None, "roll": None,
                    "filename": None, "late": False, "where": "broker"}
        envelope = json.loads(raw)
        headers = envelope.get("headers", {}) or {}
        body = json.loads(base64.b64decode(envelope["body"]))
        args = body[0] if isinstance(body, list) and body else []
        return _task_summary(args, headers.get("id"), "broker")
    except Exception:
        return {"task_id": None, "qno": None, "roll": None,
                "filename": None, "late": False, "where": "broker"}


def _task_summary(args, task_id, where):
    """Roll / question out of a Celery task's args, never the file payload."""
    if isinstance(args, (list, tuple)):
        qno = args[0] if len(args) > 0 else None
        roll = args[1] if len(args) > 1 else None
        filename = args[2] if len(args) > 2 else None
        late = bool(args[4]) if len(args) > 4 else False
    else:
        qno = roll = filename = None
        late = False
    return {"task_id": task_id, "qno": qno, "roll": roll,
            "filename": filename, "late": late, "where": where}


def _broker_stats_blocking():
    out = {
        "redis_ok": False,
        "pending": 0,
        "reserved": 0,
        "waiting": 0,
        "upcoming": [],
        "workers": [],
        "active": 0,
        "active_tasks": [],
        "celery_ok": False,
    }

    broker_peek = []
    broker_url = getattr(capp.conf, "broker_url", None) or "redis://localhost:6379"
    if redis_lib is not None:
        try:
            client = redis_lib.Redis.from_url(
                broker_url, socket_connect_timeout=0.5, socket_timeout=0.5)
            out["pending"] = client.llen("celery")
            out["redis_ok"] = True
            if out["pending"]:
                # Celery LPUSHes and BRPOPs, so the tail of the list runs next.
                raw_items = client.lrange("celery", -QUEUE_PEEK, -1)
                for item in reversed(raw_items):
                    broker_peek.append(_decode_queued(item))
        except Exception as exc:
            out["redis_error"] = str(exc)

    reserved_peek = []
    try:
        inspector = capp.control.inspect(timeout=0.6)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        out["celery_ok"] = bool(active) or bool(reserved)
        out["workers"] = sorted(set(active) | set(reserved))

        for worker, tasks in active.items():
            out["active"] += len(tasks)
            for task in tasks:
                summary = _task_summary(task.get("args"), task.get("id"), "worker")
                started = task.get("time_start")
                summary["worker"] = worker
                summary["running_for"] = round(time.time() - started, 1) if started else None
                out["active_tasks"].append(summary)

        # "Reserved" means a worker has already claimed the task and it is
        # waiting for a free slot. Celery prefetches aggressively, so during a
        # rush these outnumber - and run before - whatever is left in Redis.
        for worker, tasks in reserved.items():
            out["reserved"] += len(tasks)
            for task in tasks:
                if len(reserved_peek) < QUEUE_PEEK:
                    reserved_peek.append(_task_summary(task.get("args"), task.get("id"), "prefetched"))
    except Exception as exc:
        out["celery_error"] = str(exc)

    out["waiting"] = out["pending"] + out["reserved"]
    out["upcoming"] = (reserved_peek + broker_peek)[:QUEUE_PEEK]
    return out


async def _broker_stats():
    """Cached, single-flight: several open dashboards share one refresh."""
    if _broker_cache["data"] is not None and time.time() - _broker_cache["stamp"] < BROKER_TTL:
        return _broker_cache["data"]
    async with _broker_lock:
        # Another request may have refreshed it while we waited for the lock.
        if _broker_cache["data"] is not None and time.time() - _broker_cache["stamp"] < BROKER_TTL:
            return _broker_cache["data"]
        data = await asyncio.to_thread(_broker_stats_blocking)
        _broker_cache["stamp"] = time.time()
        _broker_cache["data"] = data
        return data


def _read_csv_tail(path, limit):
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if row:
                    rows.append(row)
    except OSError:
        return []
    rows.reverse()
    return rows[:limit]


def _lab_state(now):
    start = lab_config["start_time"]
    end = lab_config["end_time"]
    pwd_end = lab_config.get("pwd_end_time")
    if now < start:
        return "scheduled"
    if now <= end:
        return "running"
    if pwd_end and now <= pwd_end:
        return "pwd_window"
    return "ended"


def require_admin(request: Request):
    """Token check, only when one is configured.

    Reachability is always the first control: _access_control() lets /admin and
    /api/admin through only from loopback or from an address inside
    allowed_subnets. A token adds a second factor on top of that, for when the
    lab subnet is shared with the student workstations."""
    if not ADMIN_TOKEN:
        return True
    supplied = request.headers.get("x-admin-token") or request.query_params.get("token")
    if not supplied or not secrets.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or missing admin token.")
    return True


admin = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin_ui():
    if not os.path.exists(ADMIN_PAGE):
        return JSONResponse(status_code=404, content={"detail": "admin.html not found."})
    return FileResponse(ADMIN_PAGE)


@admin.get("/ping")
async def admin_ping():
    return {"ok": True, "lab": lab_config.get("lab_name")}


@admin.get("/overview")
async def admin_overview():
    now = datetime.now()
    scan = _scan_submissions()
    broker = await _broker_stats()

    start = lab_config["start_time"]
    end = lab_config["end_time"]
    pwd_end = lab_config.get("pwd_end_time")
    total_seconds = max((end - start).total_seconds(), 1)

    registered = set(ip_roll_map.keys())
    submitted = set(scan["per_student"].keys())

    students = []
    for roll in sorted(student_list | registered | submitted):
        record = scan["per_student"].get(roll, {})
        seen = _last_seen.get(roll)
        students.append({
            "roll": roll,
            "ip": ip_roll_map.get(roll),
            "pwd": roll in pwd_rolls,
            "registered": roll in registered,
            "in_class": roll in student_list,
            "attempts": record.get("attempts", 0),
            "total": record.get("total", 0.0),
            "late": record.get("late", 0),
            "last": record.get("last"),
            "scores": {q: round(v["best"], 2) for q, v in record.get("questions", {}).items()},
            "idle_seconds": round(time.time() - seen) if seen else None,
        })

    inflight = sorted(_inflight.values(), key=lambda r: r["started"])
    total_marks = sum(q["full_marks"] for q in scan["questions"]) or 0

    return {
        "server_time": now.isoformat(timespec="seconds"),
        "lab": {
            "name": lab_config.get("lab_name"),
            "state": _lab_state(now),
            "start_time": start.isoformat(timespec="seconds"),
            "end_time": end.isoformat(timespec="seconds"),
            "pwd_end_time": pwd_end.isoformat(timespec="seconds") if pwd_end else None,
            "seconds_remaining": round((end - now).total_seconds()),
            "pwd_seconds_remaining": round((pwd_end - now).total_seconds()) if pwd_end else None,
            "elapsed_fraction": min(max((now - start).total_seconds() / total_seconds, 0.0), 1.0),
            "duration_minutes": round(total_seconds / 60),
            "extensions": lab_config.get("time_extensions", []),
            "allowed_subnets": lab_config.get("allowed_subnets", []),
            "total_marks": total_marks,
        },
        "counts": {
            "class_size": len(student_list),
            "registered": len(registered),
            "submitted": len(submitted),
            "attempts": scan["totals"]["attempts"],
            "late": scan["totals"]["late"],
            "violations": len(_read_csv_tail(VIOLATIONS_FILE, 100000)),
        },
        "queue": {
            # "waiting" is what an operator means by the queue: still in Redis
            # plus already prefetched by a worker but not yet running.
            "waiting": broker.get("waiting", 0),
            "pending": broker.get("pending", 0),
            "active": broker.get("active", 0),
            "reserved": broker.get("reserved", 0),
            "workers": broker.get("workers", []),
            "redis_ok": broker.get("redis_ok", False),
            "celery_ok": broker.get("celery_ok", False),
            "upcoming": broker.get("upcoming", []),
            "active_tasks": broker.get("active_tasks", []),
            "error": broker.get("redis_error") or broker.get("celery_error"),
        },
        "traffic": {
            "inflight": len(inflight),
            "inflight_requests": [
                {**r, "age_ms": round((time.time() - r["started"]) * 1000)} for r in inflight[:20]
            ],
            "total": _traffic["total"],
            "blocked": _traffic["blocked"],
            "errors": _traffic["errors"],
            "rpm": _minute_hits.get(int(time.time() // 60), 0),
            "timeline": _request_timeline(),
        },
        "questions": scan["questions"],
        "students": students,
        "activity": _timeline(scan["events"]),
        "feeds": {
            "graded": scan["events"][:40],
            "accepted": list(itertools.islice(_submission_log, 40)),
            "requests": list(itertools.islice(_recent_requests, 60)),
            "violations": [
                {"timestamp": r[0], "type": r[1], "roll": r[2], "expected_ip": r[3], "actual_ip": r[4]}
                for r in _read_csv_tail(VIOLATIONS_FILE, 25) if len(r) >= 5
            ],
            "admin_actions": [
                {"timestamp": r[0], "actor_ip": r[1], "action": r[2], "detail": r[3]}
                for r in _read_csv_tail(ADMIN_ACTIONS_FILE, 15) if len(r) >= 4
            ],
        },
        "health": {
            "uptime_seconds": round(time.time() - SERVER_BOOT),
            "disk_free_gb": round(shutil.disk_usage(".").free / 1e9, 2),
            "load_avg": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
            "debug_mode": DEBUG,
            "extra_subnets": EXTRA_SUBNETS,
            "config_error": _config_error,
            "config_reloaded_at": _config_reloaded_at.strftime("%H:%M:%S") if _config_reloaded_at else None,
        },
    }


@admin.post("/time")
async def admin_set_time(request: Request, payload: dict = Body(...)):
    """Extend (or shorten) the live lab window. Persisted to config.json."""
    now = datetime.now()
    old_end = lab_config["end_time"]
    old_pwd_end = lab_config.get("pwd_end_time")

    minutes = payload.get("minutes")
    absolute = payload.get("end_time")
    apply_to_pwd = payload.get("apply_to_pwd", True)

    if absolute:
        try:
            new_end = datetime.fromisoformat(absolute)
        except ValueError:
            raise HTTPException(status_code=400, detail="end_time must be ISO format (YYYY-MM-DDTHH:MM:SS).")
        delta = new_end - old_end
    elif minutes is not None:
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="minutes must be a number.")
        if not -720 <= minutes <= 720:
            raise HTTPException(status_code=400, detail="minutes must be between -720 and 720.")
        delta = timedelta(minutes=minutes)
        new_end = old_end + delta
    else:
        raise HTTPException(status_code=400, detail="Provide either 'minutes' or 'end_time'.")

    if new_end <= lab_config["start_time"]:
        raise HTTPException(status_code=400, detail="The new end time would fall before the lab start time.")

    lab_config["end_time"] = new_end
    if apply_to_pwd and old_pwd_end:
        lab_config["pwd_end_time"] = old_pwd_end + delta

    history = lab_config.setdefault("time_extensions", [])
    history.append({
        "at": now.isoformat(timespec="seconds"),
        "minutes": round(delta.total_seconds() / 60, 2),
        "new_end": new_end.isoformat(timespec="seconds"),
        "by": _client_ip(request),
    })
    await _persist_config()
    _audit("set_end_time", f"{old_end.isoformat()} -> {new_end.isoformat()}", _client_ip(request))

    return {
        "ok": True,
        "end_time": new_end.isoformat(timespec="seconds"),
        "pwd_end_time": lab_config["pwd_end_time"].isoformat(timespec="seconds") if lab_config.get("pwd_end_time") else None,
        "shifted_minutes": round(delta.total_seconds() / 60, 2),
    }


@admin.post("/pwd-time")
async def admin_set_pwd_time(request: Request, payload: dict = Body(...)):
    """Set the extra-time deadline for PWD students independently."""
    minutes = payload.get("minutes")
    absolute = payload.get("pwd_end_time")

    if absolute:
        try:
            new_pwd_end = datetime.fromisoformat(absolute)
        except ValueError:
            raise HTTPException(status_code=400, detail="pwd_end_time must be ISO format.")
    elif minutes is not None:
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="minutes must be a number.")
        base = lab_config.get("pwd_end_time") or lab_config["end_time"]
        new_pwd_end = base + timedelta(minutes=minutes)
    else:
        raise HTTPException(status_code=400, detail="Provide either 'minutes' or 'pwd_end_time'.")

    lab_config["pwd_end_time"] = new_pwd_end
    await _persist_config()
    _audit("set_pwd_end_time", new_pwd_end.isoformat(), _client_ip(request))
    return {"ok": True, "pwd_end_time": new_pwd_end.isoformat(timespec="seconds")}


@admin.post("/questions/{qno}")
async def admin_update_question(request: Request, qno: str, payload: dict = Body(...)):
    """Live-tune a question's execution timeout / memory cap / full marks.

    grade.sh re-reads config.json for every submission, so changes apply to the
    very next grading run without restarting anything."""
    qno_upper = qno.upper()
    conf = lab_config.get(qno_upper)
    if not isinstance(conf, dict):
        raise HTTPException(status_code=404, detail=f"Unknown question '{qno_upper}'.")

    changes = []
    for field, caster, low, high in (
        ("timeout", float, 0.1, 600.0),
        ("memory_cap_mb", int, 16, 65536),
        ("full_marks", float, 0.0, 10000.0),
    ):
        if payload.get(field) is None:
            continue
        try:
            value = caster(payload[field])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{field} must be a number.")
        if not low <= value <= high:
            raise HTTPException(status_code=400, detail=f"{field} must be between {low} and {high}.")
        changes.append(f"{field} {conf.get(field)} -> {value}")
        conf[field] = value

    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    await _persist_config()
    _audit("update_question", f"{qno_upper}: " + "; ".join(changes), _client_ip(request))
    return {"ok": True, "qno": qno_upper, "config": conf}


@admin.post("/unbind")
async def admin_unbind(request: Request, payload: dict = Body(...)):
    """Release a student's IP binding so they can re-register from a new machine."""
    roll = str(payload.get("roll", "")).upper().strip()
    if not roll:
        raise HTTPException(status_code=400, detail="roll is required.")
    async with file_lock:
        released = _unbind_roll(roll)
        if released is None:
            raise HTTPException(status_code=404, detail=f"{roll} is not currently bound to an IP.")
        _write_registrations()
    _audit("unbind", f"{roll} released from {released}", _client_ip(request))
    return {"ok": True, "roll": roll, "released_ip": released}


@admin.get("/config")
async def admin_config():
    return _config_for_disk()


@admin.get("/violations")
async def admin_violations(limit: int = 200):
    return [
        {"timestamp": r[0], "type": r[1], "roll": r[2], "expected_ip": r[3], "actual_ip": r[4]}
        for r in _read_csv_tail(VIOLATIONS_FILE, limit) if len(r) >= 5
    ]


app.include_router(admin)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
