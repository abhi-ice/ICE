# ICE Site Ops — Microsoft Project worker

This small program runs on a Windows computer that has Microsoft Project
installed. It connects your ICE site to Microsoft Project: when someone in ICE
presses "Sync", the worker opens your Project file, applies ICE's progress
updates or reads the schedule back, and sends the results to ICE.

## Setup (one time)

1. Install Python 3.10 or newer from https://www.python.org/downloads/
   (tick "Add python.exe to PATH" during install).
2. In this folder, copy `worker.env.example` to a new file named `worker.env`.
3. In ICE, open Settings -> Microsoft Project connector and copy the token.
   Paste it into `worker.env` on the `MS_PROJECT_WORKER_TOKEN=` line, and set
   `ICE_BASE_URL=` to your ICE site's web address.
4. Put your Microsoft Project files in a `projects` folder next to
   `msp_worker.py`, each file named after its ICE project id (for example
   `a1b2c3.mpp`). An ICE admin can instead set an exact file path per project.

## Run

Double-click `run-worker.bat`. Leave the window open; it waits for work from
ICE and handles it automatically. Press Ctrl+C (or close the window) to stop.

## Check the machine first (optional)

Open a terminal in this folder and run: `python msp_worker.py --selftest`
It confirms settings can be read and that Microsoft Project responds. Activity
is also written to `worker.log` next to the script.
