# core-daemon (cleanroom)

Run the persistent loop MVP:

```bash
cd cleanroom/charon
python apps/core-daemon/charon_loop.py
```

Stop it:

```bash
touch CHARON_STOP
```

State/logs are written to `.charon_state/` by default.
