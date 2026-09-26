"""Run one Linux child and save its kernel-accounted peak RSS and elapsed time."""
import json
from pathlib import Path
import resource
import subprocess
import sys
import time

started = time.monotonic()
result = subprocess.run(sys.argv[2:])
usage = resource.getrusage(resource.RUSAGE_CHILDREN)
Path(sys.argv[1]).write_text(json.dumps({
    'elapsed_seconds': round(time.monotonic() - started, 4),
    'peak_rss_bytes': usage.ru_maxrss * 1024,
    'user_cpu_seconds': usage.ru_utime,
    'system_cpu_seconds': usage.ru_stime,
    'exit_code': result.returncode,
}))
raise SystemExit(result.returncode)
