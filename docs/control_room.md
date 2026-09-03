# The Control Room (Live Monitoring & Time Extensions)

Every lab package ships with an operations console at **`http://<server-ip>:8000/admin`**.
It is the one place to extend the lab, watch the grading queue and see who is
actually working, all while the server keeps running. Nothing here requires a
restart.

---

## 1. Getting in

There is no login. The console opens straight from the URL, and **reachability is
the access control**:

- **From the server itself** (`127.0.0.1`) - always allowed.
- **From anywhere else** - the same `allowed_subnets` rule that guards the rest
  of the server applies, so a machine outside the lab network is refused.
- **After the lab has ended** - still reachable. Unlike the student endpoints,
  admin routes are exempt from the deadline gate, because that is exactly when
  you need to grant an extension.

This assumes what IndiGrader already assumes everywhere else: a closed lab
network, with the server in a controlled room. Note that student workstations
share `allowed_subnets`, so anyone on a lab machine who knows the URL can reach
the console. If that matters for your deployment, narrow the admin branch in
`_access_control()` to loopback only and drive it from the server console.

Every change is appended to `admin_actions.csv` (timestamp, source IP, action,
before → after) and shown in the **Admin actions** panel, so changes are always
attributable to a machine.

---

## 2. Extending the lab while it runs

The **Lab window** panel is the headline feature.

- A live countdown, driven by the *server's* clock, so a wrong clock on your
  laptop cannot mislead you. It turns amber under 10 minutes and pulses red
  under 2.
- **+5 / +10 / +15 / +30** buttons, plus a free-form `±minutes` box, plus an
  exact **Set end time** picker.
- Negative (lab-shortening) changes require a second click to confirm.
- A running tally of every change: `2 changes · +25 min`.

What happens on a click:

1. `end_time` is updated in the server's memory - the access-control middleware
   uses it on the *very next request*, so blocked students are unblocked instantly.
2. `config.json` is rewritten atomically (temp file + `rename`), so the Celery
   worker and `grade.sh` never see a half-written file.
3. The change is recorded in `time_extensions` inside `config.json`, so it
   survives a restart and is auditable after the lab.

If a lab has already ended and you extend it, submissions reopen immediately and
stop being marked late.

### PWD deadlines

`builder.py` asks for "PWD Extra Time" and now writes it to `config.json` as
`pwd_end_time`. Students listed in `pwd_students.txt` may submit until that
moment. Extending the main window shifts the PWD deadline by the same amount;
the `+15` / `+30` buttons on the **PWD deadline** row move it on its own.

> **Backwards compatibility:** if `pwd_end_time` is absent (older packages, or a
> build with PWD extra time set to `0`), PWD students keep the previous
> behaviour - an open-ended window with no cut-off.

---

## 3. What the console monitors

**Stat tiles** - registered vs. class size, students who have submitted,
total attempts, queue depth, submissions being graded, HTTP requests in flight,
requests per minute, violations. Tiles outline in amber or red when a number
needs attention (students who never fetched the kit, a growing backlog, any
violation).

**Two charts** - graded submissions per minute and HTTP requests per minute,
45 minutes of history, hover for exact counts. The submission chart is how you
spot the end-of-lab rush before it becomes a backlog.

**Request pipeline** - the three stages a submission passes through, side by side:

| In flight (HTTP) | Queued - next up | Grading now |
|---|---|---|
| requests open right now, with age in ms | everything still waiting, by roll and question | what each worker is running, and for how long |

The **Queued** count is `pending + reserved`, not just what is left in Redis.
Celery prefetches aggressively: within a second of a submission burst the Redis
list drains to zero while the tasks sit *reserved* inside the worker, waiting for
a free slot. Counting only Redis would show an empty queue during exactly the
rush you are trying to watch. Each row is tagged `broker` (still in Redis) or
`prefetched` (claimed by a worker, runs first).

This is the panel that answers "is the server stuck, or just busy?" - a long
queue with idle workers means Celery is down; slow *Grading now* entries mean a
student's submission is spinning against its timeout.

**Questions** - attempts, students attempted, average best score and top score
per question, with editable **timeout**, **memory cap** and **full marks**.
`grade.sh` re-reads `config.json` for every submission, so a new timeout applies
to the next graded submission - no restart, no requeue. Use it when a question
turns out to be tighter than intended.

**Recent grading results** and **Live request log** - the last 40 grades
(roll, question, marks, late flag) and the last 60 student requests (status
colour-coded, path, roll or IP, latency). The console's own polling is
deliberately excluded from the request log so the feed stays readable.

**Students** - one row per student: bound IP, attempts, per-question best score,
total, last submission, last seen. Searchable, sortable on any column, and
filterable by *Never fetched*, *No submission*, *PWD*, *Late*. Each bound row has
an **Unbind** button (two-click) that releases the IP binding so a student who
moved machines can fetch again - the same effect as `ig rebind`, but from your side.

**Violations** - the live tail of `violations.csv`: re-registrations, IP
collisions and submissions from unregistered IPs.

**Server health** - uptime, free disk, load average, connected Celery workers,
allowed subnets, and a loud warning if `DEBUG = True` left access control off.

---

## 4. Practical notes

- **Refresh rate** is 2s / 3s / 5s / 10s, with a Pause button. Each tick is one
  request; the server memoises the filesystem scan for 2s and broker stats for
  3s, so several people can watch at once without loading the box.
- **The console is read-mostly.** The only writes are the four controls above
  (lab time, PWD time, question tuning, unbind), each confirmed with a toast.
- **A red dot** next to the lab name means the last poll failed. The countdown
  keeps ticking off the last known end time until contact returns.
- **The console needs no extra dependencies** - it is served from `admin.html`
  in the lab package, with no CDN and no build step, which matters on a closed
  lab network. It does use modern JavaScript (`??`, spread), so it wants a
  browser from roughly 2020 onwards.
- **Run FastAPI as a single process.** `fastapi run main.py` - what `start.sh`
  uses - defaults to one worker, which is what the whole server assumes: the
  live deadline, the IP bindings and the request metrics all live in that
  process's memory. Adding `--workers N` would give each worker its own copy,
  so an extension would only apply to whichever worker served the click.
- **If Redis dies mid-lab**, the console is the fastest way to find out: the
  pipeline header flips both dots to red. Student submissions will hang for
  ~20s and then return a 500 while the broker is down - that is pre-existing
  Celery behaviour, not something the console changes.

---

## 5. HTTP API

Everything the page does is a plain endpoint, so it also works from `curl` on
the server - handy if you are on a terminal-only session.

```bash
# Everything the dashboard shows, in one JSON document
curl -s localhost:8000/api/admin/overview | jq .lab

# Give the class another 20 minutes
curl -s -X POST localhost:8000/api/admin/time \
     -H 'Content-Type: application/json' -d '{"minutes": 20}'

# Or set an exact end time
curl -s -X POST localhost:8000/api/admin/time \
     -H 'Content-Type: application/json' -d '{"end_time": "2026-09-03T12:30:00"}'

# Loosen Q2's execution timeout to 10s for the next submission onwards
curl -s -X POST localhost:8000/api/admin/questions/Q2 \
     -H 'Content-Type: application/json' -d '{"timeout": 10}'

# Release a student's IP binding
curl -s -X POST localhost:8000/api/admin/unbind \
     -H 'Content-Type: application/json' -d '{"roll": "CS25B012"}'
```

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/admin/ping` | GET | liveness check |
| `/api/admin/overview` | GET | the entire dashboard payload |
| `/api/admin/config` | GET | current `config.json` as served |
| `/api/admin/violations?limit=200` | GET | violation log |
| `/api/admin/time` | POST | `{"minutes": n}` or `{"end_time": "ISO"}`, plus `apply_to_pwd` |
| `/api/admin/pwd-time` | POST | `{"minutes": n}` or `{"pwd_end_time": "ISO"}` |
| `/api/admin/questions/{qno}` | POST | `timeout`, `memory_cap_mb`, `full_marks` |
| `/api/admin/unbind` | POST | `{"roll": "..."}` |

Run these on the lab server, or from a machine inside `allowed_subnets`.

---

## 6. Adding the console to a lab package built earlier

Packages built before this feature existed need three things:

```bash
cd packageIG_<LAB>
cp /path/to/IndiGrader/templates/out_of_the_box/main.py .
cp /path/to/IndiGrader/templates/out_of_the_box/admin.html .
pkill -f "fastapi run main.py"; fastapi run main.py > logs/fastapi.log 2>&1 &
```

Celery does not need restarting. Rebuilding the lab with `builder.py` does all of
this for you.
