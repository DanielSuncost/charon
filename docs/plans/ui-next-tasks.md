# UI Next Tasks

## 1. Paste Handling
**Problem:** Pasting large text blocks floods the input box.
**Goal:** Like pi-agent, detect multi-line paste and collapse to `[N lines pasted]` 
in the chat, while injecting the full content into the conversation.

**Implementation:**
- OpenTUI's Input renderable may fire paste events (PasteEvent)
- Detect paste via `renderer.keyInput.on('paste', ...)` 
- If paste content has >3 lines, show `[N lines pasted]` in chat display
- Send full content to the backend as the actual message
- Store the full text so Ctrl+O expand can show it

## 2. Expanded View (Ctrl+O)
**Problem:** Long code blocks, file reads, and agent responses get truncated 
in the chat for legibility. Need a way to see everything.
**Goal:** Ctrl+O opens a full-screen view of the current/last message with 
all content visible, scrollable.

**Implementation:**
- Create an "expand" overlay that takes over the full terminal
- Shows the raw content of the selected message (markdown rendered)
- Scrollable with arrow keys / Page Up / Page Down
- Escape or Ctrl+O again returns to chat
- Could also allow expanding specific tool results

## 3. Tool Result Color Palette
**Problem:** All tool calls look the same in the chat. Different operations 
should have distinct but harmonious colors.
**Goal:** Each tool type gets its own color that fits the robe-red theme.

**Palette (all dark backgrounds, consistent with robe aesthetic):**
- Read:   `bg: #0d1a14` (dark forest green) — reading/examining
- Write:  `bg: #1a1a0d` (dark olive) — creating/writing
- Edit:   `bg: #1a140d` (dark amber) — modifying
- Bash:   `bg: #0d0d1a` (dark navy) — executing
- Search: `bg: #1a0d1a` (dark purple) — finding
- Error:  `bg: #1a0d0d` (dark red) — failures

**Each shows:**
- Tool name label (like charon's name label)
- Arguments (path, command, etc.)
- Result content (truncated in chat, expandable with Ctrl+O)

## 4. Charon Agent Sessions in Grid
**Problem:** Charon agents don't appear in the session grid because they 
aren't running in tmux sessions. They run as conversation engines inside 
the chat_backend.py process.

**Root cause:** When you chat with Charon, the conversation happens inside 
the backend Python process. There's no tmux session to capture because 
the agent IS the backend.

**Options:**
A. **Wrap each charon agent in its own tmux session** — when the agent 
   executes Bash commands, run them in the agent's tmux so the session 
   grid can show the terminal output. The chat itself stays in the TUI.

B. **Create a virtual session view** — the session grid shows Charon 
   agents as "virtual" sessions where the content is the chat history 
   rather than a tmux capture. Clicking on it switches to that agent's 
   chat view.

C. **Run charon agents as separate processes** — each agent runs as its 
   own `charon_loop.py` daemon with a tmux session. The TUI connects 
   to them like any other tmux session. This is the most architecturally 
   clean but requires the daemon to handle interactive chat.

**Recommendation:** Option B for now (virtual sessions), Option A later 
(tmux for bash commands). Option C is the long-term goal but requires 
significant daemon refactoring.

**For Option B:**
- When a Charon agent exists, show it in the session grid
- The "capture" content is the last N lines of chat history
- Entering the session switches to that agent's chat view
- Status detection works from the chat state (streaming = active, etc.)
