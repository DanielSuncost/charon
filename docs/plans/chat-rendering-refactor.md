# Chat Rendering Refactor Plan

## Current Architecture
- One `mainText` (instantiated Text renderable) holds ALL chat content
- `buildChat()` rebuilds the entire StyledText on every change
- Messages are stored as `{styled: StyledText}` in `S.msgs[]`
- Markdown is manually parsed with `renderSimpleMarkdown()`
- Tool results are styled inline

## Pi-Agent Architecture (target)
Pi-agent uses individual components per message, added to a scrollable container:
- `AssistantMessageComponent` — uses `Markdown` renderable with streaming
- `ToolExecutionComponent` — uses `Box` with background, `Text` for content
- `Spacer(1)` between messages
- Edit diffs rendered with a custom `renderDiff()` component
- Visual truncation for long outputs
- Syntax highlighting via `highlightCode()`
- Each component is independently updateable (streaming updates only touch one component)

## Refactor Steps

### Phase 1: Per-message renderables
- Replace `S.msgs[]` of StyledText with actual renderable references
- Each message gets its own Text or Markdown renderable added to `chatScroll`
- `buildChat()` becomes `addChatMessage()` which appends a renderable
- Streaming updates modify only the current message's renderable

### Phase 2: Use OpenTUI Markdown
- Use `MarkdownRenderable` for assistant responses
- Set `streaming: true` during streaming, `false` on complete
- Use `SyntaxStyle.create()` for code highlighting
- Set `conceal: true` to hide markdown markers

### Phase 3: Tool execution components
- Each tool call gets a Box with tool-specific background color
- Syntax highlighting for code in read/write results
- Edit diffs shown as colored diff output
- Truncation with expand support

### Phase 4: Diffs and edit preview
- When Edit tool is called, show a before/after diff
- Use green/red coloring for additions/removals
- Collapsible to save space

## Benefits
- Only the current message re-renders during streaming (much faster)
- Native markdown rendering with proper code blocks
- Per-message scroll position tracking
- Easier to add features (expand, copy, etc.) per message
