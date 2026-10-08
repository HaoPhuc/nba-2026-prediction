"""Run the daily data fetches and append their output to logs/daily.log.

Windows Task Scheduler runs this every day at 12:00 AM (task "NBA daily fetch").
To run it by hand:
    python run_daily.py
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
SCRIPTS = ["fetch_streamed.py", "create_embedurl.py"]
LOG_PATH = ROOT / "logs" / "daily.log"


def main() -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    # The scheduler starts this with pythonw.exe (no console window); run the
    # scripts with the matching python.exe, also without a window.
    python = Path(sys.executable).with_name("python.exe")
    env = {**os.environ, "PYTHONUTF8": "1"}
    failed = False

    with LOG_PATH.open("a", encoding="utf-8") as log:
        for script in SCRIPTS:
            result = subprocess.run(
                [str(python), str(ROOT / script)],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            log.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {script} exited {result.returncode}\n")
            log.write(result.stdout + result.stderr + "\n")
            failed |= result.returncode != 0

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
