# Running in Docker

`./start.sh` runs the lab natively when the machine can grade: Linux, with
firejail installed. When it cannot, and Docker is available, it builds and runs
the bundled image instead and tells you it is doing so. There is no second
command to learn.

```bash
cd Labs/packageIG_<LAB>
./start.sh          # native on a lab server, containerised on your laptop
./stop.sh           # drains the queue first either way
```

## Why this exists

Grading needs more than firejail. `grade.sh` also uses `ulimit -v` and GNU
`/usr/bin/time -f`, neither of which exists on macOS. On a Mac the server, the
Control Room and the whole submission path work natively, but every submission
scores zero, which looks like broken student code rather than a missing
toolchain. In the container, grading produces real verdicts:

```
[VERDICT] 01: PASSED (0.00s)          correct solution
[VERDICT] 01: WRONG_ANSWER (0.00s)    solution that multiplies instead of adding
```

## What the image contains

`Dockerfile` in the lab package: `python:3.12-slim-bookworm` plus
`build-essential` (students submit C and C++), `firejail`, GNU `time`, `procps`,
and everything in `requirements.txt`. It is built once and reused; the first
`./start.sh` takes a minute or two, later ones are immediate.

The lab package itself is bind-mounted at `/lab`, so `submissions/`, `logs/` and
`config.json` all live on the host. You can still edit `config.json` from outside
the container and the server picks it up within a second, and you still zip the
folder afterwards exactly as before.

## Networking, which matters more than it looks

Docker rewrites the source address of published-port traffic to the gateway.
IndiGrader binds each roll number to the IP it first appeared from, so under
port publishing **every student would appear to be the same machine** and IP
binding, collision detection and the violation log would all stop meaning
anything.

So the two cases are handled differently:

| Host | Networking | Consequence |
|---|---|---|
| Linux | `--network host` | Real client addresses are preserved. Safe for a real lab. |
| Anything else | published port plus `IG_EXTRA_SUBNETS` | The gateway range is trusted so the console is reachable. Per-student IP binding is **not** enforced. |

The second mode announces itself loudly: `start.sh` says so, the server prints a
banner at startup, and the Control Room health bar shows
`DEV NETWORKING: ... per-student IP binding is not enforced`. Use it to develop
and demo. Do not run a real lab that way.

On a lab server you would normally not use Docker at all: install firejail and
run natively.

## Knobs

| Variable | Effect |
|---|---|
| `IG_NATIVE=1` | Never containerise, even if grading will fail. Useful to test the native path. |
| `IG_PORT` | Host port to publish, default 8000. |
| `IG_IMAGE` | Image tag to build and run, default `indigrader:local`. |
| `IG_EXTRA_SUBNETS` | Extra trusted subnet prefixes, comma separated. Set automatically in the non-Linux path. |

Rebuild the image after changing `requirements.txt` or the `Dockerfile`:

```bash
docker rmi indigrader:local && ./start.sh
```

## Caveats

- On a **Linux** host the container writes as root, so files under `submissions/`
  end up root-owned. Reading and zipping still work; editing needs `sudo`. This
  does not happen on macOS, where Docker Desktop maps ownership to you.
- `./stop.sh` drains the queue inside the container before stopping it, and
  refuses to stop it if the drain fails, so a shutdown cannot silently discard
  ungraded submissions.
