# Remote Agent Teams

A remote agent team is a group of persistent agents running on a
server, each with a defined role, managed as a unit from your local
Charon.

## Why

Most agent setups are single-machine, single-session. You talk to one
agent, it does one thing, it forgets. If you need work done on a
remote server, you SSH in manually.

A remote agent team gives you persistent, specialized workers on a
server that your local Charon coordinates as a group. Each agent has a
role, stays running, and shares project context through Harbor. You
interact with all of them from one terminal.

## Example: Production Server Team

Three agents on your production server, each with a distinct role:

**ops** — handles deployments, releases, and repository management.
Knows the deploy scripts, the release process, the rollback
procedures.

**builder** — implements features from a backlog. Has ongoing context
about what's been built and what's next. Works asynchronously on tasks
dispatched from your local Charon.

**watchdog** — runs on a cron schedule, periodically checking service
health, disk usage, certificate expiry, and log anomalies. Reports
findings back to Harbor where they're indexed for future recall.

### Fleet configuration

```json
{
  "servers": [
    {
      "id": "prod",
      "host": "prod.example.com",
      "user": "deploy",
      "agents": [
        {
          "name": "ops",
          "type": "hermes",
          "specialization": "deployment, releases, repo management",
          "project": "myapp",
          "auto_start": true
        },
        {
          "name": "builder",
          "type": "hermes",
          "specialization": "feature implementation",
          "project": "myapp",
          "auto_start": true
        },
        {
          "name": "watchdog",
          "type": "bash",
          "specialization": "health monitoring, alerting",
          "project": "myapp",
          "auto_start": true
        }
      ]
    }
  ]
}
```

Save this as `~/.charon/fleet.json`. Charon's fleet sync polls the
server every 30 seconds. Agents marked `auto_start: true` are
launched automatically via `charons-boat wrap` if they aren't
already running.

### Setup

Deploy charons-boat to the server once:

```bash
charons-boat deploy deploy@prod.example.com
```

Charon handles the rest. On the next fleet sync cycle, it connects
via SSH, discovers the agents, and starts any that aren't running.
They appear in the Session Grid (F3) — you can watch their terminals
and type into them directly.

### Daily use

From your local Charon chat:

```
/voyage dispatch prod ops "tag and deploy the current main branch to production"
```

The ops agent gets a voyage manifest with your project knowledge
(deploy paths, environment variables, rollback procedures) and
executes the deployment. Progress streams back in real time. The
result — what was deployed, what changed, any issues — gets indexed
into Harbor's memory.

```
/voyage dispatch prod builder "implement the user avatar upload feature from the backlog"
```

The builder agent works on the feature on the server, with access to
your project conventions and recent decisions via Harbor recall. When
done, the implementation details flow back to your local memory.

The watchdog doesn't need manual dispatch — it runs on its own
schedule. Its findings accumulate in Harbor's memory. When you ask
your local agent "any issues on prod lately?", it recalls the
watchdog's reports.

```
You: "any health issues on prod this week?"
Agent: 3 relevant memories from watchdog:
  1. Disk usage on /var/log hit 85% on Tuesday, rotated
  2. SSL cert for api.example.com expires in 12 days
  3. Response time p99 spiked to 800ms Wednesday evening, recovered
```

### Interaction modes

Each remote agent supports two interaction modes:

**Terminal (Boat)** — press F3, select the agent in the Session Grid,
type directly into its terminal. This is live, interactive, real-time.
Useful for debugging, watching logs, or ad-hoc commands.

**Structured (Harbor)** — dispatch a task with `/voyage`. The agent
gets a context packet from your memory and project knowledge, works
independently, and returns structured results. Useful for defined
tasks that should be tracked and indexed.

Both modes work through the same SSH connection (ControlMaster
multiplexing — one TCP connection, multiple channels, zero
reconnection overhead).

## Other team configurations

### Development + Staging

```json
{
  "agents": [
    {"name": "dev", "specialization": "feature development, testing", "auto_start": true},
    {"name": "ci", "specialization": "test runner, build validation", "auto_start": true}
  ]
}
```

Your local agent makes changes, dispatches "run the full test suite"
to the CI agent, and gets structured pass/fail results back without
blocking your local context.

### GPU Training Cluster

```json
{
  "agents": [
    {"name": "trainer", "specialization": "model training, hyperparameter tuning"},
    {"name": "evaluator", "specialization": "benchmark evaluation, metrics reporting"}
  ]
}
```

Dispatch training runs and evaluations in parallel. Both agents can
recall previous runs via Harbor — "what learning rate worked best
last time?" — without you having to re-provide that context.

### Multi-Server Fleet

Multiple servers, each with their own team:

```json
{
  "servers": [
    {"id": "prod", "host": "prod.example.com", "agents": [...]},
    {"id": "staging", "host": "staging.example.com", "agents": [...]},
    {"id": "gpu", "host": "gpu-box.local", "agents": [...]}
  ]
}
```

```
/voyage dispatch prod ops "check why the API is slow"
/voyage dispatch gpu trainer "start training run with config v12"
/voyage list
```

All results flow back to your local Harbor. Your local agent has
unified memory across all servers.

## What's implemented today

- Fleet discovery and polling (fleet_sync.py, 30s interval)
- Auto-start agents via SSH + charons-boat wrap
- Session Grid display of remote agents (live terminal view)
- Interactive terminal access via Boat protocol
- Harbor task dispatch with voyage manifests (/voyage)
- Mid-task memory recall from Harbor
- Structured result ingestion into local memory
- Fleet tools (FleetStatus, FleetSend, FleetHistory, FleetOnboard)

## What's not yet implemented

- Team templates (define a team config once, deploy to any server)
- Watchdog cron integration (watchdog agent exists but cron dispatch
  from fleet config is manual — you'd set up the cron on the server
  side or use Charon's automation runtime locally)
- Cross-agent task dependencies within a team (ops waits for builder
  to finish before deploying)
- Agent-to-agent communication on the same server without going
  through Harbor
