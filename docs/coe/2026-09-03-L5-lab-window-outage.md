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

## Action items

| # | Action | Status |
|---|---|---|
| 1 | Make the lab window changeable on a running server | **Done** - `POST /api/admin/time` |
| 2 | Operator visibility into queue, workers and per-student progress | **Done** - `/admin` |
| 3 | Live per-question `timeout` / `memory_cap_mb` / `full_marks` tuning | **Done** - `POST /api/admin/questions/{qno}` |
| 4 | Enforce the PWD extra time the builder had always collected but never written | **Done** - `pwd_end_time` |
| 5 | Update §7 of the setup guide so the restart dance is no longer the primary advice | **Done** |
| 9 | Re-read `config.json` when it changes, so a hand edit needs no restart either | **Done** - validated before it replaces anything |
| 6 | Make the single-late-submission cap configurable, and make its error text name the deadline as the cause | **Open** - unchanged by this work |
| 7 | Broker outage handling: with Redis down, `/submit` hangs ~20s and returns a 500 | **Open** - pre-existing Celery behaviour; now at least visible on the console |
| 8 | Document that FastAPI must run single-process (`--workers` would shard the in-memory deadline) | **Done** - `docs/control_room.md` |

## Lessons

- **State an operator must be able to change during an incident cannot live only
  in process memory.** The read-once-at-startup optimisation was reasonable; having
  no write path for it was the defect.
- **A grace allowance becomes a hard failure when the thing it is a grace for
  cannot be adjusted.** Item 6 remains open: extending the window prevents this
  situation, but does not fix the cap itself.
- **An error message should name the cause the operator can act on.** "You have
  already exhausted your single late submission" is accurate and unactionable;
  neither the student nor the invigilator could tell it was a clock problem.
