# ApprovalStorm

ApprovalStorm is a simulation platform for testing whether an organization can maintain human oversight of autonomous agents during demand spikes, service outages, and reviewer shortages.

## Architecture

A discrete-event simulator replays synthetic agent requests against reviewer schedules, approval deadlines, and permission rules. An audit layer checks which workflows completed, missed deadlines, or were blocked by a safety rule. A scenario comparison report shows how staffing and scheduling changes affect throughput and oversight.

## Running locally

Run these commands from the project directory in PowerShell with Python 3.10 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m approvalstorm --seed 42
.\.venv\Scripts\python.exe -m unittest -v
```

Add `--events` to stream the audit log as JSON Lines for the same seed.

Run the [example scenario file](examples/staffing.json) to compare two reviewer schedules under the same workload and outage.

```powershell
.\.venv\Scripts\python.exe -m approvalstorm --scenario-file examples/staffing.json
```

Each scenario defines its request arrivals, deadlines, review durations, and reviewer start times in simulated minutes. Edit a copy of the example to try different values. Add `--events` to inspect the audit log.
