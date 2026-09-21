# Step 40 - Local (on-premise) deployment of the web UI

## The question this answers

`web/server.py` (docs/39 and earlier) has only ever been run by hand via
`uvicorn --reload` for development. The user asked how to actually deploy
it - specifically, whether a platform like Vercel would work. It would
not: this project's hard constraint (stated 2026-09-05, see step-log.md)
is on-premise only, no cloud, and technically the app spawns long-running
subprocesses (the analyzed project's own dev server, for `--api-test`)
which serverless platforms cannot host at all (ephemeral filesystem, no
persistent child processes, hard execution timeouts). The only correct
target is the user's own always-on PC, which the project's own README has
always assumed.

This step makes that runnable as an unattended local service instead of a
manually-run dev command.

## What changed

- `web/requirements.txt`: pinned to the versions actually installed and
  tested (`fastapi==0.141.1`, `uvicorn==0.53.0`,
  `python-multipart==0.0.32`) - previously unpinned, which is fine for
  interactive dev but not for something meant to keep running unattended
  indefinitely.
- `web/run_service.ps1` (new): a launch wrapper for unattended use. Resolves
  the project root from its own location (portable if the repo ever moves),
  invokes the project's `.venv` interpreter directly (no PATH/activation
  dependency), binds `uvicorn` to `127.0.0.1:8000` only (never `0.0.0.0` -
  see Scope below), and appends all output to `web/logs/server.log` with a
  timestamped `---- start ... ----` marker per launch so a crash leaves a
  trace instead of vanishing. `web/logs/` added to `.gitignore`.
- `.gitignore`: added `web/logs/`.

Verified by running the exact `uvicorn web.server:app --host 127.0.0.1
--port 8000` command by hand: server starts, binds, and returns a real
`HTTP 200` on `/`.

## What did not get done here, and why

Registering the Windows Scheduled Task that actually makes this run
unattended (at logon, auto-restart up to 3x on crash, no execution time
limit) was blocked by this harness's own safety classifier
("Unauthorized Persistence") - creating an auto-run-at-logon task is a
persistent system change outside the repo and outside this session, which
correctly requires the user's own action rather than an agent's.

The exact command (run once, from an elevated or normal PowerShell -
admin not required for an `AtLogOn` trigger for the current user):

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument '-NoProfile -ExecutionPolicy Bypass -File "D:\Working\Qa-Agent\web\run_service.ps1"'
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBattery -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "QA Agent Web Service" -Action $action -Trigger $trigger -Settings $settings -Description "Runs the QA Agent web UI (web/server.py) bound to 127.0.0.1:8000 at logon. See docs/40-local-deployment.md."
```

To start it immediately without waiting for the next logon:
`Start-ScheduledTask -TaskName "QA Agent Web Service"`. To check it's
alive: `Get-ScheduledTask -TaskName "QA Agent Web Service" | Select
State`, or just open `http://127.0.0.1:8000`. To remove it:
`Unregister-ScheduledTask -TaskName "QA Agent Web Service" -Confirm:$false`.

## Scope and honesty boundary

- Bound to `127.0.0.1` deliberately - reachable only from this machine.
  Widening to the LAN (binding the machine's LAN IP instead, plus a
  Windows Firewall inbound rule scoped to the local subnet) is a real,
  separate decision the user has not made, because the "run live API
  tests" option executes an uploaded project's own start script with no
  auth (web/README.md's own documented trade-off) - anyone who could reach
  it could trigger arbitrary code execution on this machine. Not done
  here.
- No reverse proxy, no TLS, no auth layer added - none of those are
  needed for a `127.0.0.1`-only deployment, and adding them now would be
  unused complexity for a target that doesn't exist yet.
- Log rotation is not implemented - `web/logs/server.log` grows
  unbounded across restarts. Untested how large it gets in practice; a
  real gap, not hidden, deferred until it's actually observed to matter.

## Files changed

`web/requirements.txt` (pinned), `web/run_service.ps1` (new),
`.gitignore` (added `web/logs/`), `docs/40-local-deployment.md` (new).
