# IndiGrader lab server.
#
# The point of this image is that the grading environment is identical everywhere:
# Linux, firejail, gcc/g++, GNU time and coreutils, whatever the instructor's own
# machine happens to be. ./start.sh uses it automatically when the host cannot
# grade natively (a Mac, or a Linux box without firejail).
FROM python:3.12-slim-bookworm

# build-essential: students submit C/C++ and grade.sh compiles it here.
# firejail:        per-submission sandbox.
# time:            grade.sh calls /usr/bin/time -f, which is GNU time, not the shell builtin.
# procps:          start.sh / stop.sh use pgrep and pkill.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        firejail \
        time \
        procps \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# The lab package itself is bind-mounted here at run time, so submissions and
# logs land on the host and the instructor can still zip the folder afterwards.
WORKDIR /lab

# Writable HOME for whatever uid the host passes in with --user.
ENV HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    IG_IN_CONTAINER=1

EXPOSE 8000

CMD ["bash", "-c", "./start.sh && tail -f /dev/null"]
