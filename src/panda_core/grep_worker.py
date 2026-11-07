"""Search worker run in a child process by the `grep` tool (SEC-006).

A regular expression chosen by the model can take exponential time, and CPython's
`re` holds the GIL while it matches, so a thread cannot be stopped and would freeze
the event loop. The tool runs this script in its own process instead and kills it on
a timeout. It uses the standard library only, so it can run under `python -I`.

Input (JSON on stdin): pattern, flags, fixed, limit, max_bytes, files (relative paths).
Output (JSON on stdout): matches, scanned, truncated.
"""

from __future__ import annotations

import json
import os
import re
import sys

_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


def main() -> None:
    request = json.load(sys.stdin)
    pattern = re.escape(request["pattern"]) if request["fixed"] else request["pattern"]
    regex = re.compile(pattern, request["flags"])
    limit, max_bytes = request["limit"], request["max_bytes"]
    matches: list[str] = []
    scanned = 0
    truncated = False
    for relative, device, inode in request["files"]:
        # R2-215: the parent checked the policy on a file; open that same file or
        # nothing. O_NOFOLLOW refuses a final link, and the inode check refuses a
        # file (or parent directory) swapped after the check.
        try:
            fd = os.open(relative, os.O_RDONLY | _NO_FOLLOW)
        except OSError:
            continue
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (device, inode):
                continue
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(max_bytes + 1)
        except OSError:
            continue
        finally:
            os.close(fd)
        if len(raw) > max_bytes or b"\x00" in raw[:2048]:
            continue
        scanned += 1
        for number, line in enumerate(raw.decode("utf-8", "replace").splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{relative}:{number}: {line[:300]}")
                if len(matches) >= limit:
                    truncated = True
                    break
        if truncated:
            break
    json.dump({"matches": matches, "scanned": scanned, "truncated": truncated}, sys.stdout)


if __name__ == "__main__":
    main()
