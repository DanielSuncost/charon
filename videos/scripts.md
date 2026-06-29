# Charon — feature presentation scripts

Voice: a person who built a tool and wants you to understand it. Plain,
clear, sincere. Not selling, and not making a show of not selling. State
what it does and roughly how well, in ordinary words. Short sentences.
No flourishes, no "honestly", no rhetorical contrast. If a sentence would
sound good read by someone slightly pleased with themselves, cut it.

Format per beat: **[on screen]** the UI / motion · *narration* (one breath).
~40–55s per feature.

---

## 01 — Memory

- **[card: 01 / Memory]** · *Charon saves useful context so an agent can find it later.*
- **[a recall surfacing a past thread]** · *Each conversation is saved to a small database on your machine. Recall uses the meaning of the text.*
- **[the ranked results]** · *A search takes about ten milliseconds and never leaves the computer.*
- **[multi-session recall]** · *In the same conversation, the right result is usually near the top. Across many past sessions, it is first about a quarter of the time.*
- **[preferences across projects]** · *A preference you save in one project can be used in other projects.*
- **[outro]** · *The memory stays on your machine, and you can inspect it.*

## 02 — Shades

- **[card: 02 / Shades]** · *Charon lets an agent start other agents and run them at the same time.*
- **[an agent spawning a batch]** · *Each worker gets its own conversation, its own model, and a clear job.*
- **[scope contract highlighted]** · *A worker can only touch the files it was given. If it tries to write somewhere else, the tool stops it.*
- **[budgets ticking down]** · *Each one runs under a budget: tokens, time, and attempts. When it's out, it stops.*
- **[parallel batch finishing]** · *Dependent steps can run one after another. Independent steps can run at the same time.*
- **[outro]** · *The job, budget, and file boundary are part of each worker's setup.*

## 03 — Judge Loops

- **[card: 03 / Judge Loops]** · *Charon can repeat a task when there is a score to improve.*
- **[snapshot → change → score]** · *It makes a snapshot, changes the files, scores the result, and compares it to the last run.*
- **[keep/rollback in motion]** · *A better score keeps the change. A worse score restores the snapshot and removes files the attempt left behind.*
- **[judge type table]** · *The score can be a benchmark number, a passing test, or a model rating the result against a rubric you write.*
- **[converging on target]** · *It repeats until the score reaches your target, or the budget runs out.*
- **[outro]** · *The loop keeps the best result it has found.*

## 04 — Making Videos

- **[card: 04 / Making Videos]** · *This video was made with Charon.*
- **[director reading a repo]** · *One agent reads the project and proposes the features to show, and the order to show them in. You review that before anything is made.*
- **[the swarm rendering clips]** · *Workers then split the video into parts. Each one writes animation code, renders its part, and stitches the result.*
- **[the judge scoring a frame]** · *A judge loop checks frames against a style guide and sends back fixes for spacing, color, and motion.*
- **[outro]** · *You describe the video once. The agents turn that into clips.*

## 05 — The Fleet

- **[card: 05 / The Fleet]** · *Charon shows local and remote agent sessions in one place.*
- **[session grid, panes live]** · *Every session — on this machine or on a remote one over SSH — sits in a single grid, with the same controls.*
- **[dispatch a remote task]** · *A job can run on another machine. You can watch the session and send guidance from the same screen.*
- **[outro]** · *All active sessions are visible on one screen.*

---

### Notes
- Start each line from the thing itself, not from a claim about the thing.
- One idea per line. Plain words. A number when you have one.
- Don't tell the viewer you're being honest. Just be plain and let it stand.
- Numbers spelled for the voice ("about a quarter of the time").
- End quietly. No tagline that winks at the camera.
