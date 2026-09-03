# Architecture

IndiGrader utilizes a client-server architecture optimized for closed-network university labs. The design prioritizes isolated code execution, network-level access control, and offline grading capabilities.

## System Components

### 1. Application Server (FastAPI)
The core server (`main.py`) manages request orchestration and state. It exposes RESTful endpoints for:
- Fetching starter kits (`/starter/{roll}`)
- Submitting solutions (`/submit/{qno}`)
- Polling task status and retrieving submission history
- Serving the static leaderboard (`/leaderboard`)

### 2. IP-Binding Middleware
To mitigate impersonation, the server implements an IP-binding middleware.
- Upon the initial fetch of a starter kit, a student's IP address is bound to their roll number for the duration of the session.
- Subsequent requests for that roll number from differing IP addresses are rejected, and these events are logged in `violations.csv`.

### 3. Asynchronous Grading Pipeline (Celery + Redis)
To manage high concurrency during lab sessions:
- Submissions are enqueued to a Redis message broker.
- A Celery worker pool (`task.py`) processes these tasks asynchronously.
- The worker executes the grading engine within a Firejail sandbox.
- Results are written to the local file system, while the client periodically polls the server for task completion.

### 4. Unified Grading Engine
Both the server and the client utilize the same underlying evaluation script (`grade.sh`).
- Server-side execution (`task.py`) runs `grade.sh --sandbox` against private test cases.
- Client-side execution (`check.sh`) runs `grade.sh` (without sandboxing constraints) against public test cases.
- This unified approach ensures behavioral consistency between local testing and server-side grading.

### 5. CLI Client (`ig`)
The primary interface for students is a terminal-based CLI (`ig`). It provides automation for:
- Resolving the configured `SERVER_URL`.
- Identifying the active question via directory context.
- Packaging and transmitting source files to the server.

### 6. Control Room (`/admin`)
An operations console served from `admin.html`, backed by the
`/api/admin/*` routes. It exists because the lab window is state the instructor
must be able to change mid-session:
- **Mutable schedule.** `end_time` and `pwd_end_time` are held in the same
  in-memory `lab_config` the access-control middleware reads, so an extension
  takes effect on the next request. The console then rewrites `config.json`
  atomically (temp file plus `rename`), which is what propagates the change to
  the Celery worker and to `grade.sh` - both of which read the file per task -
  and what makes it survive a restart.
- **Live telemetry.** A single HTTP middleware records every student request
  (in-flight set, ring buffer, per-minute counters); queue depth and the next
  queued tasks are read directly from the Redis list, and worker activity from
  `celery.control.inspect`. Filesystem aggregation over `marks.txt` is memoised
  on `(mtime, size)` so polling stays cheap during a lab.
- **Access.** Reachability *is* the control: admin routes are always open on
  loopback and otherwise subject to the same `allowed_subnets` rule as the rest
  of the server, on the assumption that the lab server sits on a closed network
  in a controlled room. They are exempt from the lab-ended gate, since that is
  precisely when an extension is needed. Every mutation is appended to
  `admin_actions.csv` with the caller's IP.

See [The Control Room](control_room.md) for the operator-facing guide.

