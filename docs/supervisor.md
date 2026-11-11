# Supervisor (optional)

[Back to the README](../README.md)

`supervisor/` is an optional supervisor for source checkouts on Windows, Linux,
and macOS. It runs `python -m TwitchChannelPointsMiner.runner --config-dir config`
from the checkout, restarts it on crashes and on a daily schedule, applies
`git` updates, and sends alerts. It reads `config/config.py` like every other
run method. The Windows executable and Docker do not need it.

```sh
cd Twitch-Channel-Points-Miner-v3
python3 supervisor/minerctl.py install   # checks config/config.py, creates .venv, registers the jobs
python3 supervisor/minerctl.py health    # HEALTHY / WARNING / DOWN
python3 supervisor/minerctl.py start | stop | uninstall
```

On Windows, run `.\supervisor\MinerTask.ps1` from an Administrator PowerShell
in the checkout (`-Action Health`, `-Action Stop`, and `-Action Uninstall` also
work). Optional alerts go in `supervisor/miner-alerts.json`; copy
`supervisor/miner-alerts.example.json` and keep the file private. Logs and
status are written to `supervisor-logs/`.
