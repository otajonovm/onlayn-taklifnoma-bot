"""Heroku bitta workerda bot va, sessiya bo'lsa, userbotni birga ishga tushiradi."""
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv()

commands = [[sys.executable, "bot.py"]]
if os.getenv("USERBOT_SESSION", "").strip():
    commands.append([sys.executable, "userbot.py"])

procs = [subprocess.Popen(cmd) for cmd in commands]
try:
    while True:
        for proc in procs:
            code = proc.poll()
            if code is not None:
                for other in procs:
                    if other.poll() is None:
                        other.terminate()
                sys.exit(code)
        time.sleep(1)
except KeyboardInterrupt:
    for proc in procs:
        proc.terminate()
