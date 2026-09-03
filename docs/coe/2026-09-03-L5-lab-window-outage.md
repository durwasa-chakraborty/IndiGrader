# COE: L5 lab window could not be extended once it had closed

**Lab:** L5 · **Date:** 2026-09-03 · **Configured window:** 09:00 → 12:00
**Status:** Resolved - remediation merged with this document.

---

## Summary

When the L5 lab reached its configured `end_time`, the server began refusing
student traffic. There was no supported way to move the deadline on a running
server: the only documented remedy was to edit `config.json` and restart FastAPI
mid-session. Students who had already submitted once past the deadline were then
hard-blocked by the single-late-submission cap, which surfaced to them as
`ig submit` no longer working.

## Impact

- Students could not submit for the remainder of the session once the window had
  closed, despite the session still being in progress.
- Students who submitted once after the deadline could not submit again for that
  question at all. From the CLI this looked like the server rejecting repeat
  submissions rather than a deadline being enforced.
- Recovery required restarting the web server during an active lab, which risks
  dropping in-flight requests and was not something anyone wanted to do with a
  room full of students mid-submission.
- No operator visibility: there was no way to see queue depth, whether the Celery
  worker was alive, or how many students were still working. Diagnosis meant
  tailing `logs/fastapi.log`.

## Root cause

**The lab deadline was immutable for the lifetime of the process.**

`main.py` read `config.json` exactly once, in `lifespan()`, into an in-process
dict - a deliberate choice to keep per-request work low during peak lab hours:

```python
with open(CONFIG_FILE, "r") as f:
    lab_config = json.load(f)
    lab_config["end_time"] = datetime.fromisoformat(lab_config["end_time"])
```

Every request was then checked against that in-memory value:

```python
if not DEBUG and not is_allowed_public_path and current_time > lab_config["end_time"] and not is_privileged:
    return JSONResponse(status_code=403, content={"detail": "Lab has ended. No more submissions allowed."})
```

Nothing could change `lab_config` after startup. Editing `config.json` on disk had
no effect on the running server, so `docs/setup_guide.md` §7 correctly told
operators to restart FastAPI - an acceptable instruction between labs, and a bad
one during a live session.

**The contributing factor** was the single-late-submission cap in `handleSubmit`,
which allows exactly one submission per question after the deadline:

```python
late_dir = os.path.join("late_submissions", qno_upper, roll_upper)
if os.path.exists(late_dir) and len(os.listdir(late_dir)) > 0:
    raise HTTPException(status_code=403,
        detail=f"You have already exhausted your single late submission for {qno_upper}.")
```

This is working as designed - it is a grace allowance, not a bug. But combined
with an un-extendable deadline it turned a schedule problem into a hard block,
and the resulting error text did not tell students (or the invigilator) that the
underlying cause was the lab clock.

**Why it was not caught earlier:** the deadline path is only exercised at the end
of a real lab. There was no dashboard, no countdown, and no alerting, so the first
signal was students reporting failures.

## Resolution

A Control Room at `/admin`, served by the same FastAPI process, with a
`POST /api/admin/time` endpoint behind it. On an extension it:

1. mutates `lab_config["end_time"]` in memory - the deadline gate reads that dict
   on every request, so blocked students are unblocked on their next request;
2. rewrites `config.json` atomically (temp file + `os.replace`) so the Celery
   worker and `grade.sh`, which both re-read the file per task, agree with the web
   server, and the change survives a restart;
3. appends the change to `time_extensions` in `config.json` and to
   `admin_actions.csv` for post-lab audit.

Admin routes are deliberately exempt from the deadline gate - the moment you most
need the console is after the lab has closed.

The same console addresses the visibility gap: countdown, queue depth (including
tasks prefetched into the worker, which a naive Redis `LLEN` misses), live worker
activity, in-flight requests, per-student progress, and the violations feed.

The terminal is a first-class path to the same thing. `config.json` is re-read
whenever its mtime changes, so `nano config.json` over SSH extends the lab without
a restart and without a browser. A candidate file is validated before it replaces
anything, so a typo or a half-written save leaves the running configuration alone
and is reported instead of silently applied.

## What else the investigation turned up

Working through the shutdown and startup paths surfaced three more defects of the
same family: silent failure discovered by students rather than by the operator.
None of them caused the L5 incident, all of them could have.

- **`stop.sh` discarded submissions while reporting success.** It polled
  `redis-cli llen celery` and read 0 as "drained", but Celery prefetches, so the
  broker list empties within a second of a burst while the worker still holds the
  work. It then ran `pkill -15 -f celery`, which matches the forked pool children
  and aborts their in-progress grading. Measured: enqueue 32, run `stop.sh`
  immediately, 0 graded, 22 returned to the broker, **10 permanently lost**, and
  the script printed "safe to zip this folder and take it back". After the fix:
  32 of 32.
- **`start.sh` claimed success with no broker at all.** It ran
  `redis-server --daemonize yes`, ignored the result, and printed "All services
  started successfully!". With Redis absent, every submission then hung ~20s and
  returned a 500. It now verifies the broker through kombu and exits non-zero.
- **Pre-flight blamed the wrong thing.** With `jq` missing, the check reported
  "config.json is missing or contains invalid JSON" about an intact file. Missing
  `firejail` was not checked at all, and its absence makes every submission score
  zero, which reads as the students' code being wrong.

## Reducing what has to be installed

The incident happened on a machine an instructor does not administer, so every
system package is a dependency on someone else's cooperation. Two of the three
have been removed:

- **Redis** no longer needs `sudo apt install`. `requirements.txt` pulls in
  `redislite`, which puts a real `redis-server` and `redis-cli` into the
  virtualenv. The broker is also configurable now (`IG_BROKER_URL`, or
  `broker_url` in `config.json`), so it can live on another machine entirely.
- **jq** is gone from all 32 call sites across `start.sh`, `stop.sh`, both copies
  of `grade.sh`, and the student-facing `submit.sh`, `check.sh` and `ig`. They use
  `python3`, which has to be present anyway. Students previously needed
  `sudo apt install jq` on their own machines, which was never documented.
  `grade.sh` is the file where a subtle change corrupts marks, so its five config
  reads were proved byte-identical to jq across five config shapes rather than
  eyeballed.
- **firejail** stays, and should. It is a setuid binary providing kernel namespace
  isolation for untrusted student code; no pip package supplies it, and it should
  not come from PyPI if one did. It is now the single system prerequisite, and
  `start.sh` warns clearly when it is absent.

## Action items

| # | Action | Status |
|---|---|---|
| 1 | Make the lab window changeable on a running server | **Done** - `POST /api/admin/time` |
| 2 | Operator visibility into queue, workers and per-student progress | **Done** - `/admin` |
| 3 | Live per-question `timeout` / `memory_cap_mb` / `full_marks` tuning | **Done** - `POST /api/admin/questions/{qno}` |
| 4 | Enforce the PWD extra time the builder had always collected but never written | **Done** - `pwd_end_time` |
| 5 | Update §7 of the setup guide so the restart dance is no longer the primary advice | **Done** |
| 6 | Re-read `config.json` when it changes, so a hand edit needs no restart either | **Done** - validated before it replaces anything |
| 7 | Document that FastAPI must run single-process (`--workers` would shard the in-memory deadline) | **Done** - `docs/control_room.md` |
| 8 | Stop `stop.sh` discarding queued and in-flight submissions | **Done** - counts broker + running + reserved, shuts down over Celery's control channel |
| 9 | Stop `start.sh` reporting success when no broker is reachable | **Done** - verifies through kombu, exits non-zero |
| 10 | Remove the `sudo apt` dependencies an instructor may not be able to satisfy | **Done** - Redis via `redislite`, jq removed entirely; firejail remains by necessity |
| 11 | Pre-flight checks must name the real problem | **Done** - `jq` and `firejail` checked by name |
| 12 | Make the single-late-submission cap configurable, and make its error text name the deadline as the cause | **Open** - unchanged by this work |
| 13 | Broker outage handling: with Redis down, `/submit` hangs ~20s and returns a 500 | **Open** - pre-existing Celery behaviour; now visible on the console and refused at startup |

## Lessons

- **State an operator must be able to change during an incident cannot live only
  in process memory.** The read-once-at-startup optimisation was reasonable; having
  no write path for it was the defect.
- **A grace allowance becomes a hard failure when the thing it is a grace for
  cannot be adjusted.** Item 6 remains open: extending the window prevents this
  situation, but does not fix the cap itself.
- **An error message should name the cause the operator can act on.** "You have
  already exhausted your single late submission" is accurate and unactionable;
  neither the student nor the invigilator could tell it was a clock problem. The
  same fault appeared twice more: `start.sh` blaming `config.json` for a missing
  `jq`, and `stop.sh` announcing "safe to zip this folder" over discarded work.
- **A success message that is not checked is worse than no message.** Both shell
  scripts reported success on paths that had already failed. Each now verifies the
  thing it is claiming, and exits non-zero when it cannot.
- **Count what is outstanding, not what is convenient to query.** `llen celery`
  was the easy number to reach for and it is wrong by design, because Celery
  prefetches. Reserved work is still outstanding work.
- **Every system package is a dependency on someone else's cooperation.** On a lab
  machine the instructor does not administer, "just apt install it" may not be
  available on the day. Redis and jq were both removable; firejail was not, and
  saying which is which is part of the answer.
