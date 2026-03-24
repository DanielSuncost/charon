# Local Delegation Runner

Script: `scripts/charon_delegate.py`

## Add a task
```bash
cd cleanroom/charon
python scripts/charon_delegate.py add \
  --title "Rename queue module" \
  --type rename \
  --instructions "Rename file A to B and update imports." \
  --acceptance "All imports resolve; tests for queue module pass."
```

## Execute one pending task
```bash
python scripts/charon_delegate.py run-once
```

## Run continuously (queue worker)
```bash
python scripts/charon_delegate.py clear-stop
python scripts/charon_delegate.py run-forever --poll-interval 5
```

## Stop continuous worker
```bash
python scripts/charon_delegate.py stop
```

## List tasks
```bash
python scripts/charon_delegate.py list
```

## Model selection
- Uses `CHARON_LOCAL_MODEL` env var if set.
- Otherwise auto-detects first LM Studio model in `~/.config/opencode/opencode.json`.

Results are saved in `.charon_state/delegation/results/`.
Stop file defaults to `.charon_state/delegation/STOP_DELEGATE`.
