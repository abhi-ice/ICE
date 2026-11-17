"""Helper for copy_files_to_live.ps1: reads settings as JSON from stdin (so
no secret is ever on a command line), then runs the backend file-copy
script."""
import json
import os
import runpy
import sys

p = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
os.environ.update(p["env"])
os.environ["STORAGE_BACKEND"] = "s3"
os.chdir("/mnt/c/Users/admin/Desktop/ICE/backend")
sys.path.insert(0, os.getcwd())
sys.argv = ["copy_emergent_files"] + p["argv"]
try:
    runpy.run_module("scripts.copy_emergent_files", run_name="__main__")
except SystemExit as e:
    print("Finished with exit code", e.code)
