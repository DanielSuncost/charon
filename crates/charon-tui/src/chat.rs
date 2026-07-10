use serde_json::{json, Map, Value};
use std::io::{self, BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::{Duration, Instant};

use crate::util::project_root;

#[derive(Clone, Debug, Default)]
pub struct LaunchOptions {
    pub provider: Option<String>,
    pub resume: Option<String>,
    pub agent: Option<String>,
}

pub struct BackendProcess {
    _child: Child,
    stdin_tx: mpsc::Sender<String>,
    rx: Receiver<BackendEvent>,
    request_id: u64,
    last_refresh: Instant,
}

#[derive(Debug)]
pub enum BackendEvent {
    Json(Value),
    Stderr(String),
    Eof,
}

impl BackendProcess {
    pub fn start(launch: &LaunchOptions) -> io::Result<Self> {
        let root = project_root();
        let script = root.join("apps/tui/opentui/chat_backend.py");

        let mut cmd = Command::new("python3");
        cmd.arg(script)
            .current_dir(&root)
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        if let Some(provider) = launch.provider.as_ref().filter(|s| !s.is_empty()) {
            cmd.env("CHARON_PROVIDER", provider);
        }
        if let Some(resume) = launch.resume.as_ref().filter(|s| !s.is_empty()) {
            cmd.env("CHARON_RESUME", resume);
        }
        if let Some(agent) = launch.agent.as_ref().filter(|s| !s.is_empty()) {
            cmd.env("CHARON_AGENT", agent);
        }

        let mut child = cmd.spawn()?;

        let mut stdin = child.stdin.take().ok_or_else(|| io::Error::new(io::ErrorKind::Other, "missing stdin"))?;
        let stdout = child.stdout.take().ok_or_else(|| io::Error::new(io::ErrorKind::Other, "missing stdout"))?;
        let stderr = child.stderr.take().ok_or_else(|| io::Error::new(io::ErrorKind::Other, "missing stderr"))?;

        // Writer thread — sends to backend stdin without blocking the UI
        let (stdin_tx, stdin_rx) = mpsc::channel::<String>();
        thread::spawn(move || {
            for line in stdin_rx {
                if stdin.write_all(line.as_bytes()).is_err() { break; }
                if stdin.write_all(b"\n").is_err() { break; }
                let _ = stdin.flush();
            }
        });

        let (tx, rx) = mpsc::channel();
        let tx_out = tx.clone();
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                match line {
                    Ok(line) => {
                        let line = line.trim();
                        if line.is_empty() {
                            continue;
                        }
                        if let Ok(v) = serde_json::from_str::<Value>(line) {
                            if tx_out.send(BackendEvent::Json(v)).is_err() {
                                return;
                            }
                        }
                    }
                    Err(_) => break,
                }
            }
            let _ = tx_out.send(BackendEvent::Eof);
        });

        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines() {
                match line {
                    Ok(line) => {
                        let trimmed = line.trim();
                        let noisy_progress = trimmed.starts_with("Loading weights:")
                            || trimmed.contains("|████")
                            || (trimmed.contains("it/s") && trimmed.contains('%'));
                        if !trimmed.is_empty() && !noisy_progress {
                            let _ = tx.send(BackendEvent::Stderr(line));
                        }
                    }
                    Err(_) => break,
                }
            }
        });

        Ok(Self {
            _child: child,
            stdin_tx,
            rx,
            request_id: 0,
            last_refresh: Instant::now(),
        })
    }

    pub fn poll(&mut self) -> Vec<BackendEvent> {
        let mut events = Vec::new();
        while let Ok(ev) = self.rx.try_recv() {
            events.push(ev);
        }
        events
    }

    pub fn maybe_refresh(&mut self) {
        if self.last_refresh.elapsed() >= Duration::from_secs(5) {
            let _ = self.send(json!({"type": "refresh"}));
            self.last_refresh = Instant::now();
        }
    }

    pub fn request_refresh(&mut self) {
        let _ = self.send(json!({"type": "refresh"}));
        self.last_refresh = Instant::now();
    }

    pub fn send_chat(&mut self, text: &str) -> io::Result<()> {
        self.send(json!({"type": "chat", "message": text}))
    }

    pub fn send_command(&mut self, command: &str) -> io::Result<()> {
        self.send(json!({"type": "command", "command": command}))
    }

    pub fn send_follow_up(&mut self, text: &str) -> io::Result<()> {
        self.send(json!({"type": "follow_up", "message": text}))
    }

    pub fn send_steer(&mut self, text: &str) -> io::Result<()> {
        self.send(json!({"type": "steer", "message": text}))
    }

    fn send(&mut self, mut v: Value) -> io::Result<()> {
        self.request_id += 1;
        if let Value::Object(ref mut map) = v {
            map.insert("request_id".to_string(), Value::String(format!("r{}", self.request_id)));
        }
        let line = serde_json::to_string(&v)?;
        self.stdin_tx.send(line).map_err(|e| io::Error::new(io::ErrorKind::BrokenPipe, e))?;
        Ok(())
    }
}

fn merge_json_objects(dst: &mut Map<String, Value>, src: &Map<String, Value>) {
    for (k, v) in src {
        match (dst.get_mut(k), v) {
            (Some(Value::Object(dst_obj)), Value::Object(src_obj)) => merge_json_objects(dst_obj, src_obj),
            _ => {
                dst.insert(k.clone(), v.clone());
            }
        }
    }
}

#[derive(Clone, Debug)]
pub struct MenuItem {
    pub cmd: String,
    pub desc: String,
    pub age: String,
    pub executable: bool,
}

#[derive(Clone, Debug, Default)]
pub struct UsageStats {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub last_input_tokens: u64,
    pub last_output_tokens: u64,
    pub context_pct: Option<f64>,
    pub context_window: Option<u64>,
}

#[derive(Clone, Debug)]
pub struct ApprovalRequest {
    pub tool: String,
    pub reason: String,
    pub risk: String,
    pub params: String,
    pub selected: usize,
}

#[derive(Clone, Debug)]
pub enum ChatMessage {
    User { text: String },
    Assistant { text: String, streaming: bool },
    Thinking { text: String, streaming: bool },
    ToolCall { tool: String, summary: String },
    ToolResult { tool: String, content: String, is_error: bool },
    Status { text: String },
    Error { text: String },
    Stderr { text: String },
    /// A user message waiting in the queue (follow-up or steer).
    /// `tag` is "queued" or "steering". Converted to User on delivery.
    QueuedUser { text: String, tag: String },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ChatTextPoint {
    pub row: usize,
    pub col: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChatViewMode {
    Transcript,
    Workspace,
}

#[derive(Clone, Debug)]
pub struct ProvisionalOutcome {
    pub summary: String,
    pub done: bool,
}

pub struct ChatState {
    pub transcript: Vec<String>,
    pub messages: Vec<ChatMessage>,
    pub input: String,
    pub streaming: bool,
    pub backend: BackendProcess,
    pub refresh_payload: Option<Value>,
    pub session_id: String,
    pub scroll: usize,
    pub menu_title: Option<String>,
    pub menu_items: Vec<MenuItem>,
    pub menu_index: usize,
    pub input_history: Vec<String>,
    pub history_index: Option<usize>,
    pub show_timestamps: bool,
    pub show_thoughts: bool,
    pub thoughts_supported: bool,
    pub usage: UsageStats,
    pub auth_provider: Option<String>,
    pub auth_url: Option<String>,
    pub auth_action_index: usize,
    pub approval: Option<ApprovalRequest>,
    pub view_mode: ChatViewMode,
    pub info_pane_open: bool,
    pub info_pane_tab: usize,
    pub copy_mode: bool,
    pub app_mouse_mode: bool,
    pub selection_anchor: Option<ChatTextPoint>,
    pub selection_focus: Option<ChatTextPoint>,
    pub selection_dragging: bool,
    pub clipboard_notice: Option<(String, bool, Instant)>,
    pub provisional_outcomes: Vec<ProvisionalOutcome>,
    pub context_menu: Option<ContextMenu>,
    pub pending_queue: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct ContextMenu {
    pub x: u16,
    pub y: u16,
    pub selected: usize,
    pub has_selection: bool,
}

impl ChatState {
    pub fn new(launch: LaunchOptions) -> io::Result<Self> {
        let mut backend = BackendProcess::start(&launch)?;
        let _ = backend.send(json!({"type": "refresh"}));
        Ok(Self {
            transcript: vec![],
            messages: vec![],
            input: String::new(),
            streaming: false,
            backend,
            refresh_payload: None,
            session_id: String::new(),
            scroll: 0,
            menu_title: None,
            menu_items: Vec::new(),
            menu_index: 0,
            input_history: Vec::new(),
            history_index: None,
            show_timestamps: false,
            show_thoughts: false,
            thoughts_supported: true,
            usage: UsageStats::default(),
            auth_provider: None,
            auth_url: None,
            auth_action_index: 0,
            approval: None,
            view_mode: ChatViewMode::Transcript,
            info_pane_open: false,
            info_pane_tab: 0,
            copy_mode: false,
            app_mouse_mode: true,
            selection_anchor: None,
            selection_focus: None,
            selection_dragging: false,
            clipboard_notice: None,
            provisional_outcomes: Vec::new(),
            context_menu: None,
            pending_queue: Vec::new(),
        })
    }

    pub fn poll(&mut self) -> bool {
        if self.copy_mode {
            return false;
        }
        // Skip refresh while streaming — the stdin write can block if the
        // backend is busy and not reading its pipe, stalling the entire UI.
        if !self.streaming {
            self.backend.maybe_refresh();
        }
        let events = self.backend.poll();
        let changed = !events.is_empty();
        for ev in events {
            self.handle_event(ev);
        }
        changed
    }

    pub fn request_refresh(&mut self) {
        self.backend.request_refresh();
    }

    pub fn set_clipboard_notice(&mut self, text: impl Into<String>, ok: bool) {
        self.clipboard_notice = Some((text.into(), ok, Instant::now()));
    }

    pub fn clipboard_notice_text(&self) -> Option<(&str, bool)> {
        let (text, ok, at) = self.clipboard_notice.as_ref()?;
        if at.elapsed() > Duration::from_secs(2) {
            return None;
        }
        Some((text.as_str(), *ok))
    }

    pub fn clear_expired_notices(&mut self) {
        if self.clipboard_notice.as_ref().is_some_and(|(_, _, at)| at.elapsed() > Duration::from_secs(2)) {
            self.clipboard_notice = None;
        }
    }

    pub fn submit_input(&mut self) {
        if self.input.trim().is_empty() {
            return;
        }
        let text = self.input.trim().to_string();
        self.input_history.push(text.clone());
        self.history_index = None;
        self.scroll = 0;

        if text.starts_with('/') {
            // Commands always go through immediately
            if text.starts_with("/steer ") {
                let msg = text.trim_start_matches("/steer ").trim();
                if !msg.is_empty() {
                    let _ = self.backend.send_steer(msg);
                    self.messages.push(ChatMessage::QueuedUser { text: msg.to_string(), tag: "steering".to_string() });
                }
            } else {
                self.transcript.push(format!("> {}", text));
                let _ = self.backend.send_command(&text);
            }
        } else if self.streaming {
            // During streaming, queue as follow-up
            let _ = self.backend.send_follow_up(&text);
            self.pending_queue.push(text.clone());
            self.messages.push(ChatMessage::QueuedUser { text: text.clone(), tag: "queued".to_string() });
        } else {
            // Normal send
            self.transcript.push(format!("> {}", text));
            self.messages.push(ChatMessage::User { text: text.clone() });
            self.start_provisional_outcome(&text);
            let _ = self.backend.send_chat(&text);
        }
        self.input.clear();
    }

    pub fn history_up(&mut self) {
        if self.input_history.is_empty() {
            return;
        }
        let next = match self.history_index {
            None => self.input_history.len().saturating_sub(1),
            Some(idx) => idx.saturating_sub(1),
        };
        self.history_index = Some(next);
        if let Some(item) = self.input_history.get(next) {
            self.input = item.clone();
        }
    }

    pub fn history_down(&mut self) {
        let Some(idx) = self.history_index else { return; };
        if idx + 1 >= self.input_history.len() {
            self.history_index = None;
            self.input.clear();
        } else {
            self.history_index = Some(idx + 1);
            self.input = self.input_history[idx + 1].clone();
        }
    }

    pub fn maybe_open_command_menu(&mut self) {
        let input = self.input.trim();
        if !input.starts_with('/') {
            if self.menu_title.as_deref() == Some("Commands") || self.menu_title.as_deref() == Some("Setup") {
                self.close_menu();
            }
            return;
        }
        let items = self.command_suggestions(input);
        if items.is_empty() {
            return;
        }
        self.menu_title = Some(if input.starts_with("/setup") { "Setup" } else { "Commands" }.to_string());
        self.menu_items = items;
        self.menu_index = 0;
    }

    pub fn menu_open(&self) -> bool {
        self.menu_title.is_some() && !self.menu_items.is_empty()
    }

    pub fn close_menu(&mut self) {
        self.menu_title = None;
        self.menu_items.clear();
        self.menu_index = 0;
    }

    pub fn menu_move_up(&mut self) {
        if self.menu_open() {
            self.menu_index = self.menu_index.saturating_sub(1);
        }
    }

    pub fn menu_move_down(&mut self) {
        if self.menu_open() && self.menu_index + 1 < self.menu_items.len() {
            self.menu_index += 1;
        }
    }

    pub fn menu_select(&mut self) {
        if !self.menu_open() {
            return;
        }
        if let Some(item) = self.menu_items.get(self.menu_index).cloned() {
            // If the user has typed more than the menu item (e.g. typed
            // "/fleet setup deploy@host" while menu shows "/fleet setup"),
            // send the user's full input, not the menu item.
            let user_input = self.input.trim().to_string();
            if item.executable && user_input.len() <= item.cmd.len() {
                // User input matches the menu item — execute the menu item
                self.transcript.push(format!("> {}", item.cmd));
                self.messages.push(ChatMessage::User { text: item.cmd.clone() });
                self.scroll = 0;
                self.input_history.push(item.cmd.clone());
                self.history_index = None;
                let _ = self.backend.send_command(&item.cmd);
                self.input.clear();
                self.close_menu();
                return;
            }
            if !user_input.is_empty() && user_input.len() > item.cmd.len() {
                // User typed additional args beyond the menu item — send the full input
                self.transcript.push(format!("> {}", user_input));
                self.messages.push(ChatMessage::User { text: user_input.clone() });
                self.scroll = 0;
                self.input_history.push(user_input.clone());
                self.history_index = None;
                if user_input.starts_with('/') {
                    let _ = self.backend.send_command(&user_input);
                } else {
                    let _ = self.backend.send_chat(&user_input);
                }
                self.input.clear();
                self.close_menu();
                return;
            }

            self.input = if item.cmd.ends_with(' ') { item.cmd.clone() } else { format!("{} ", item.cmd) };
            self.close_menu();
            self.maybe_open_command_menu();
        }
    }

    pub fn menu_fill_input(&mut self) {
        if let Some(item) = self.menu_items.get(self.menu_index) {
            self.input = if item.cmd.ends_with(' ') { item.cmd.clone() } else { format!("{} ", item.cmd) };
        }
    }

    pub fn auth_open(&self) -> bool {
        self.auth_url.is_some()
    }

    pub fn auth_move_prev(&mut self) {
        self.auth_action_index = self.auth_action_index.saturating_sub(1);
    }

    pub fn auth_move_next(&mut self) {
        if self.auth_action_index < 2 {
            self.auth_action_index += 1;
        }
    }

    pub fn auth_dismiss(&mut self) {
        self.auth_provider = None;
        self.auth_url = None;
        self.auth_action_index = 0;
    }

    pub fn auth_activate_selected(&mut self) {
        let Some(url) = self.auth_url.clone() else { return; };
        match self.auth_action_index {
            0 => {
                if open_url(&url) {
                    self.push_status("Opened auth link in browser.");
                } else {
                    self.push_error("Could not open browser automatically.");
                }
            }
            1 => {
                if copy_to_clipboard(&url) {
                    self.push_status("Copied auth link to clipboard.");
                } else {
                    self.push_error("Could not copy auth link to clipboard.");
                }
            }
            _ => self.auth_dismiss(),
        }
    }

    pub fn approval_open(&self) -> bool {
        self.approval.is_some()
    }

    pub fn approval_move_prev(&mut self) {
        if let Some(approval) = self.approval.as_mut() {
            approval.selected = approval.selected.saturating_sub(1);
        }
    }

    pub fn approval_move_next(&mut self) {
        if let Some(approval) = self.approval.as_mut() {
            if approval.selected < 2 {
                approval.selected += 1;
            }
        }
    }

    pub fn approval_deny(&mut self) {
        self.approval = None;
        let _ = self.backend.send(json!({"type": "approval_response", "approved": false}));
        self.push_status("✗ Denied");
    }

    pub fn approval_accept_selected(&mut self) {
        let Some(approval) = self.approval.take() else { return; };
        let _ = self.backend.send(json!({"type": "approval_response", "approved": true}));
        match approval.selected {
            2 => {
                let _ = self.backend.send_command("/approve all");
                self.push_status("✓ All tools approved for session");
            }
            _ => {
                self.push_status("✓ Approved");
            }
        }
    }

    fn summarize_outcome_prompt(text: &str) -> String {
        fn clean_tail(s: &str) -> String {
            s.trim()
                .trim_matches(|c: char| matches!(c, '.' | ',' | ';' | ':' | '!' | '?'))
                .to_string()
        }

        let trimmed = text.trim().trim_matches(|c: char| c == '"' || c == '\'' || c.is_whitespace());
        if trimmed.is_empty() {
            return "Untitled task".to_string();
        }
        let mut base = trimmed.to_string();
        let lower = trimmed.to_lowercase();
        let prefixes = [
            "please ", "can you ", "could you ", "would you ", "i want you to ", "help me ", "let's ", "lets ",
        ];
        for prefix in prefixes {
            if lower.starts_with(prefix) {
                base = trimmed[prefix.len()..].trim().to_string();
                break;
            }
        }

        let lower = base.to_lowercase();
        let summary = if let Some(rest) = lower.strip_prefix("explain what ") {
            if let Some((thing, other)) = rest.split_once(" is and how it can interact with ") {
                format!("Explain {} and {} integration", clean_tail(thing), clean_tail(other))
            } else if let Some((thing, _)) = rest.split_once(" is") {
                format!("Explain {}", clean_tail(thing))
            } else {
                format!("Explain {}", clean_tail(rest))
            }
        } else if let Some(rest) = lower.strip_prefix("explain ") {
            format!("Explain {}", clean_tail(rest))
        } else if let Some(rest) = lower.strip_prefix("compare ") {
            format!("Compare {}", clean_tail(rest))
        } else if let Some(rest) = lower.strip_prefix("fix ") {
            format!("Fix {}", clean_tail(rest))
        } else if let Some(rest) = lower.strip_prefix("implement ") {
            format!("Implement {}", clean_tail(rest))
        } else if let Some(rest) = lower.strip_prefix("add ") {
            format!("Add {}", clean_tail(rest))
        } else if let Some(rest) = lower.strip_prefix("create ") {
            format!("Create {}", clean_tail(rest))
        } else if let Some(rest) = lower.strip_prefix("build ") {
            format!("Build {}", clean_tail(rest))
        } else if let Some(rest) = lower.strip_prefix("investigate ") {
            format!("Investigate {}", clean_tail(rest))
        } else if let Some(rest) = lower.strip_prefix("research ") {
            format!("Research {}", clean_tail(rest))
        } else if let Some(rest) = lower.strip_prefix("update ") {
            format!("Update {}", clean_tail(rest))
        } else {
            let mut words: Vec<&str> = base.split_whitespace().collect();
            if words.len() > 5 {
                words.truncate(5);
            }
            clean_tail(&words.join(" "))
        };

        let summary = if summary.is_empty() { "Untitled task".to_string() } else { summary };
        let mut chars = summary.chars();
        if let Some(first) = chars.next() {
            format!("{}{}", first.to_uppercase(), chars.collect::<String>())
        } else {
            "Untitled task".to_string()
        }
    }

    fn start_provisional_outcome(&mut self, text: &str) {
        let summary = Self::summarize_outcome_prompt(text);
        self.provisional_outcomes.push(ProvisionalOutcome { summary, done: false });
        if self.provisional_outcomes.len() > 12 {
            let drain = self.provisional_outcomes.len() - 12;
            self.provisional_outcomes.drain(0..drain);
        }
    }

    fn finish_latest_provisional_outcome(&mut self) {
        if let Some(last) = self.provisional_outcomes.iter_mut().rev().find(|o| !o.done) {
            last.done = true;
        }
    }

    fn reconcile_provisional_outcomes(&mut self) {
        let Some(payload) = self.refresh_payload.as_ref() else { return; };
        let task_titles: Vec<String> = payload
            .get("session_info")
            .and_then(|i| i.get("tasks"))
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|task| {
                        task.get("title")
                            .or_else(|| task.get("summary"))
                            .or_else(|| task.get("instruction"))
                            .and_then(|v| v.as_str())
                            .map(|s| s.to_lowercase())
                    })
                    .collect()
            })
            .unwrap_or_default();
        if task_titles.is_empty() {
            return;
        }
        self.provisional_outcomes.retain(|outcome| {
            let needle = outcome.summary.to_lowercase();
            !task_titles.iter().any(|title| title.contains(&needle) || needle.contains(title))
        });
    }

    fn push_status(&mut self, text: &str) {
        self.transcript.push(format!("[status] {}", text));
        self.messages.push(ChatMessage::Status { text: text.to_string() });
    }

    fn push_error(&mut self, text: &str) {
        self.transcript.push(format!("[error] {}", text));
        self.messages.push(ChatMessage::Error { text: text.to_string() });
    }

    fn push_stderr(&mut self, text: &str) {
        self.transcript.push(format!("[stderr] {}", text));
        self.messages.push(ChatMessage::Stderr { text: text.to_string() });
    }

    fn trim_messages(&mut self) {
        if self.messages.len() > 5000 {
            let drain = self.messages.len() - 5000;
            self.messages.drain(0..drain);
        }
    }

    fn merge_refresh_payload(&mut self, incoming: Value) {
        match (&mut self.refresh_payload, incoming) {
            (Some(Value::Object(existing)), Value::Object(new_map)) => {
                merge_json_objects(existing, &new_map);
            }
            (_, other) => {
                self.refresh_payload = Some(other);
            }
        }
    }

    fn push_restored_message(&mut self, m: &Value) {
        let role = m.get("role").and_then(|x| x.as_str()).unwrap_or("msg");
        let content = m.get("content").and_then(|x| x.as_str()).unwrap_or("");
        let thinking = m.get("thinking").and_then(|x| x.as_str()).unwrap_or("");

        if !thinking.trim().is_empty() {
            self.transcript.push(format!("[thinking…]{}", thinking));
            self.messages.push(ChatMessage::Thinking {
                text: thinking.to_string(),
                streaming: false,
            });
        }

        match role {
            "user" => {
                if !content.trim().is_empty() {
                    self.transcript.push(format!("> {}", content));
                    self.messages.push(ChatMessage::User { text: content.to_string() });
                }
            }
            "assistant" => {
                if let Some(tool_calls) = m.get("tool_calls").and_then(|x| x.as_array()) {
                    for tc in tool_calls {
                        let tool = tc.get("name").and_then(|x| x.as_str()).unwrap_or("tool");
                        let arguments = tc.get("arguments").cloned().unwrap_or(Value::Null);
                        let summary = if let Some(obj) = arguments.as_object() {
                            if let Some(cmd) = obj.get("command").and_then(|x| x.as_str()) {
                                cmd.chars().take(120).collect()
                            } else if let Some(path) = obj.get("path").and_then(|x| x.as_str()) {
                                path.to_string()
                            } else {
                                serde_json::to_string(&arguments).unwrap_or_default().chars().take(120).collect()
                            }
                        } else {
                            serde_json::to_string(&arguments).unwrap_or_default().chars().take(120).collect()
                        };
                        self.transcript.push(format!("[tool] {}", tool));
                        self.messages.push(ChatMessage::ToolCall {
                            tool: tool.to_string(),
                            summary,
                        });
                    }
                }
                if !content.trim().is_empty() {
                    self.transcript.push(content.to_string());
                    self.messages.push(ChatMessage::Assistant {
                        text: content.to_string(),
                        streaming: false,
                    });
                }
            }
            "tool_result" => {
                let tool = m.get("tool_name").and_then(|x| x.as_str()).unwrap_or("tool");
                let is_error = m.get("is_error").and_then(|x| x.as_bool()).unwrap_or(false);
                let summary: String = content.lines().take(3).collect::<Vec<_>>().join(" ");
                self.transcript.push(format!("[tool result] {}: {}", tool, summary));
                self.messages.push(ChatMessage::ToolResult {
                    tool: tool.to_string(),
                    content: content.to_string(),
                    is_error,
                });
            }
            _ => {
                if !content.trim().is_empty() {
                    self.transcript.push(content.to_string());
                    self.messages.push(ChatMessage::Status { text: content.to_string() });
                }
            }
        }
    }

    fn handle_event(&mut self, event: BackendEvent) {
        let was_following = self.scroll == 0;
        match event {
            BackendEvent::Json(v) => self.handle_json(v),
            BackendEvent::Stderr(line) => self.push_stderr(&line),
            BackendEvent::Eof => self.push_error("backend exited"),
        }
        if was_following {
            self.scroll = 0;
        }
        if self.transcript.len() > 10000 {
            let drain = self.transcript.len() - 10000;
            self.transcript.drain(0..drain);
        }
        self.trim_messages();
    }

    fn handle_json(&mut self, v: Value) {
        let typ = v.get("type").and_then(|x| x.as_str()).unwrap_or("");
        match typ {
            "chat_delta" => {
                let delta = v.get("text").or_else(|| v.get("delta")).and_then(|x| x.as_str()).unwrap_or("");
                if !self.streaming {
                    self.transcript.push(String::new());
                    self.messages.push(ChatMessage::Assistant { text: String::new(), streaming: true });
                    self.streaming = true;
                }
                if let Some(last) = self.transcript.last_mut() {
                    last.push_str(delta);
                }
                match self.messages.last_mut() {
                    Some(ChatMessage::Assistant { text, streaming }) => {
                        text.push_str(delta);
                        *streaming = true;
                    }
                    _ => self.messages.push(ChatMessage::Assistant { text: delta.to_string(), streaming: true }),
                }
            }
            "thinking_start" => {
                self.transcript.push("[thinking…]".to_string());
                self.messages.push(ChatMessage::Thinking { text: String::new(), streaming: true });
            }
            "thinking_delta" => {
                let delta = v.get("text").and_then(|x| x.as_str()).unwrap_or("");
                if let Some(last) = self.transcript.last_mut() {
                    if last == "[thinking…]" {
                        last.push_str(delta);
                    } else {
                        self.transcript.push(format!("[thinking…]{}", delta));
                    }
                }
                match self.messages.last_mut() {
                    Some(ChatMessage::Thinking { text, streaming }) => {
                        text.push_str(delta);
                        *streaming = true;
                    }
                    _ => self.messages.push(ChatMessage::Thinking { text: delta.to_string(), streaming: true }),
                }
            }
            "tool_call" => {
                let tool = v.get("tool_name").and_then(|x| x.as_str()).unwrap_or("tool");
                let arguments = v.get("arguments").cloned().unwrap_or(Value::Null);
                let summary = if let Some(obj) = arguments.as_object() {
                    if let Some(cmd) = obj.get("command").and_then(|x| x.as_str()) {
                        cmd.chars().take(80).collect()
                    } else if let Some(path) = obj.get("path").and_then(|x| x.as_str()) {
                        path.to_string()
                    } else {
                        serde_json::to_string(&arguments).unwrap_or_default().chars().take(80).collect()
                    }
                } else {
                    serde_json::to_string(&arguments).unwrap_or_default().chars().take(80).collect()
                };
                self.transcript.push(format!("[tool] {}", tool));
                self.messages.push(ChatMessage::ToolCall { tool: tool.to_string(), summary });
            }
            "tool_result" => {
                let tool = v.get("tool_name").and_then(|x| x.as_str()).unwrap_or("tool");
                let content = v.get("content").and_then(|x| x.as_str()).unwrap_or("");
                let summary: String = content.lines().take(3).collect::<Vec<_>>().join(" ");
                self.transcript.push(format!("[tool result] {}: {}", tool, summary));
                self.messages.push(ChatMessage::ToolResult { tool: tool.to_string(), content: content.to_string(), is_error: v.get("is_error").and_then(|x| x.as_bool()).unwrap_or(false) });
            }
            "turn_complete" | "chat_complete" => {
                self.streaming = false;
                self.finish_latest_provisional_outcome();
                self.request_refresh();
                if let Some(ChatMessage::Assistant { streaming, .. }) = self.messages.last_mut() {
                    *streaming = false;
                }
                if let Some(ChatMessage::Thinking { streaming, .. }) = self.messages.last_mut() {
                    *streaming = false;
                }
            }
            "status" => {
                let msg = v.get("message").and_then(|x| x.as_str()).unwrap_or("");
                self.streaming = false;
                self.push_status(msg);
            }
            "error" => {
                let msg = v.get("error").and_then(|x| x.as_str()).unwrap_or("unknown error");
                self.streaming = false;
                // Dismiss auth dialog if open — auth failures should not leave the UI stuck
                if self.auth_open() {
                    self.auth_dismiss();
                }
                self.push_error(msg);
            }
            "follow_up_queued" => {
                // Backend acknowledged the follow-up; already tracked locally
            }
            "follow_up_delivered" => {
                // Convert the QueuedUser message to a normal User message
                if let Some(msg) = self.pending_queue.first().cloned() {
                    self.pending_queue.remove(0);
                    // Find and convert the matching QueuedUser in messages
                    for m in self.messages.iter_mut().rev() {
                        if let ChatMessage::QueuedUser { text, tag } = m {
                            if *tag == "queued" && *text == msg {
                                *m = ChatMessage::User { text: msg.clone() };
                                break;
                            }
                        }
                    }
                }
            }
            "steer_queued" => {
                // Backend acknowledged — QueuedUser already in messages
            }
            "steer_delivered" => {
                // Convert the steering QueuedUser to a normal User message
                for m in self.messages.iter_mut().rev() {
                    if let ChatMessage::QueuedUser { text, tag } = m {
                        if *tag == "steering" {
                            *m = ChatMessage::User { text: text.clone() };
                            break;
                        }
                    }
                }
            }
            "done" => {
                self.streaming = false;
                self.pending_queue.clear();
            }
            "approval_request" => {
                let tool = v.get("tool").and_then(|x| x.as_str()).unwrap_or("tool");
                let reason = v.get("reason").and_then(|x| x.as_str()).unwrap_or("");
                let risk = v.get("risk").and_then(|x| x.as_str()).unwrap_or("unknown");
                let params = v.get("params").and_then(|x| x.as_str()).unwrap_or("");
                self.approval = Some(ApprovalRequest {
                    tool: tool.to_string(),
                    reason: reason.to_string(),
                    risk: risk.to_string(),
                    params: params.to_string(),
                    selected: 0,
                });
                self.transcript.push(format!("[approval] {} — {}", tool, reason));
            }
            "conversation_restored" => {
                let count = v.get("count").and_then(|x| x.as_u64()).unwrap_or(0);
                let agent_id = v.get("agent_id").and_then(|x| x.as_str()).unwrap_or("");
                self.transcript.clear();
                self.messages.clear();
                self.streaming = false;
                if !agent_id.is_empty() {
                    self.session_id = agent_id.to_string();
                }
                if let Some(messages) = v.get("messages").and_then(|x| x.as_array()) {
                    for m in messages {
                        self.push_restored_message(m);
                    }
                }
                if count > 0 {
                    self.push_status(&format!("conversation resumed ({}) messages", count));
                }
            }
            "suggestions" => {
                let title = v.get("title").and_then(|x| x.as_str()).unwrap_or("Commands");
                let mut items = Vec::new();
                if let Some(arr) = v.get("items").and_then(|x| x.as_array()) {
                    for item in arr {
                        let cmd = item.get("cmd").and_then(|x| x.as_str()).unwrap_or("").to_string();
                        items.push(MenuItem {
                            executable: cmd.starts_with('/'),
                            cmd,
                            desc: item.get("desc").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                            age: item.get("age").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                        });
                    }
                }
                if !items.is_empty() {
                    self.menu_title = Some(title.to_string());
                    self.menu_items = items;
                    self.menu_index = 0;
                }
            }
            "model_picker" => {
                self.auth_provider = None;
                self.auth_url = None;
                self.auth_action_index = 0;
                let picker_type = v.get("provider").and_then(|x| x.as_str()).unwrap_or("");
                let title = if picker_type == "resume" {
                    "Resume Session"
                } else if picker_type == "switch" {
                    "Provider Picker"
                } else {
                    "Model Picker"
                };
                let mut items = Vec::new();
                if let Some(arr) = v.get("models").and_then(|x| x.as_array()) {
                    for item in arr {
                        let id = item.get("id").and_then(|x| x.as_str()).unwrap_or("");
                        let desc = item.get("desc").and_then(|x| x.as_str()).unwrap_or("");
                        let age = item.get("age").and_then(|x| x.as_str()).unwrap_or("");
                        let context = v.get("context").and_then(|x| x.as_str()).unwrap_or("");
                        let cmd = if picker_type == "resume" {
                            format!("/resume {}", id)
                        } else if picker_type == "switch" {
                            format!("/provider {}", id)
                        } else if context == "shade" {
                            format!("/setup shade-model {}", id)
                        } else {
                            format!("/setup model {}", id)
                        };
                        items.push(MenuItem { executable: true, cmd, desc: desc.to_string(), age: age.to_string() });
                    }
                }
                if !items.is_empty() {
                    self.menu_title = Some(title.to_string());
                    self.menu_items = items;
                    self.menu_index = 0;
                }
            }
            "shade_provider_picker" => {
                let mut items = Vec::new();
                if let Some(arr) = v.get("options").and_then(|x| x.as_array()) {
                    for item in arr {
                        let id = item.get("id").and_then(|x| x.as_str()).unwrap_or("");
                        let desc = item.get("desc").and_then(|x| x.as_str()).unwrap_or("");
                        items.push(MenuItem {
                            executable: true,
                            cmd: format!("/setup shade-provider {}", id),
                            desc: desc.to_string(),
                            age: String::new(),
                        });
                    }
                }
                if !items.is_empty() {
                    self.menu_title = Some("Shade Provider".to_string());
                    self.menu_items = items;
                    self.menu_index = 0;
                }
            }
            "auth_url" => {
                let provider = v.get("provider").and_then(|x| x.as_str()).unwrap_or("");
                let url = v.get("url").and_then(|x| x.as_str()).unwrap_or("");
                self.auth_provider = Some(provider.to_string());
                self.auth_url = Some(url.to_string());
                self.auth_action_index = 0;
                self.push_status(&format!("Authentication required for {}", provider));
                if open_url(url) {
                    self.push_status("Opened auth link in browser.");
                } else {
                    self.push_status("Could not open browser automatically.");
                }
                if copy_to_clipboard(url) {
                    self.push_status("Copied auth link to clipboard.");
                }
                self.push_status(&format!("Open this URL: {}", url));
                self.push_status("Fallback: /setup auth-code <CODE>");
            }
            "setup_complete" => {
                let agent = v.get("agent").and_then(|x| x.as_str()).unwrap_or("");
                let provider = v.get("provider").and_then(|x| x.as_str()).unwrap_or("");
                let model = v.get("model").and_then(|x| x.as_str()).unwrap_or("");
                self.auth_provider = None;
                self.auth_url = None;
                self.auth_action_index = 0;
                self.transcript.clear();
                self.messages.clear();
                self.push_status("✓ Setup complete");
                if !agent.is_empty() {
                    self.push_status(&format!("Agent: {}", agent));
                }
                self.push_status(&format!("Provider: {}  Model: {}", provider, model));
                self.push_status("Type a message to start chatting.");
                self.streaming = false;
            }
            "toggle_timestamps" => {
                self.show_timestamps = !self.show_timestamps;
                self.push_status(&format!("Timestamps {}", if self.show_timestamps { "enabled" } else { "disabled" }));
            }
            "toggle_visible_thoughts" => {
                self.show_thoughts = v.get("enabled").and_then(|x| x.as_bool()).unwrap_or(self.show_thoughts);
                self.thoughts_supported = v.get("supported").and_then(|x| x.as_bool()).unwrap_or(self.thoughts_supported);
                let provider = v.get("provider").and_then(|x| x.as_str()).unwrap_or("");
                let suffix = if self.show_thoughts && !self.thoughts_supported {
                    if provider.is_empty() { " (provider may not expose thoughts)".to_string() } else { format!(" (provider {} may not expose thoughts)", provider) }
                } else {
                    String::new()
                };
                self.push_status(&format!("Visible thoughts {}{}", if self.show_thoughts { "enabled" } else { "disabled" }, suffix));
            }
            "usage" => {
                let input_tokens = v.get("input_tokens").and_then(|x| x.as_u64()).unwrap_or(0);
                let output_tokens = v.get("output_tokens").and_then(|x| x.as_u64()).unwrap_or(0);
                self.usage.input_tokens += input_tokens;
                self.usage.output_tokens += output_tokens;
                self.usage.last_input_tokens = input_tokens;
                self.usage.last_output_tokens = output_tokens;
                self.usage.context_pct = v.get("context_pct").and_then(|x| x.as_f64());
                self.usage.context_window = v.get("context_window").and_then(|x| x.as_u64());
            }
            "refresh" => {
                if let Some(payload) = v.get("payload").cloned() {
                    self.merge_refresh_payload(payload);
                } else {
                    self.merge_refresh_payload(v.clone());
                }
                self.reconcile_provisional_outcomes();
                if let Some(p) = self.refresh_payload.as_ref() {
                    if let Some(id) = p.get("session_id").and_then(|x| x.as_str()) {
                        self.session_id = id.to_string();
                    }
                    self.show_thoughts = p.get("visible_thoughts").and_then(|x| x.as_bool()).unwrap_or(self.show_thoughts);
                    self.thoughts_supported = p.get("thoughts_supported").and_then(|x| x.as_bool()).unwrap_or(self.thoughts_supported);
                    let step = p.get("onboarding").and_then(|o| o.get("step")).and_then(|x| x.as_str()).unwrap_or("");
                    if step != "provider-auth" {
                        self.auth_provider = None;
                        self.auth_url = None;
                        self.auth_action_index = 0;
                    }
                }
                if let Some(id) = v.get("session_id").and_then(|x| x.as_str()) {
                    self.session_id = id.to_string();
                }
            }
            _ => {}
        }
    }

    fn command_suggestions(&self, input: &str) -> Vec<MenuItem> {
        fn item(cmd: &str, desc: &str) -> MenuItem {
            let non_exec = [
                "/setup provider",
                "/setup project",
                "/setup api-key",
                "/setup auth-code",
                "/setup shade-provider",
                "/setup shade-url",
                "/setup shade-key",
                "/autonomous time",
                "/autonomous tokens",
                "/consolidation model",
                "/consolidation interval",
                "/fleet setup",
                "/voyage dispatch",
            ];
            let executable = cmd.starts_with('/') && !non_exec.contains(&cmd);
            MenuItem { cmd: cmd.to_string(), desc: desc.to_string(), age: String::new(), executable }
        }

        let provider = self.refresh_payload.as_ref()
            .and_then(|p| p.get("onboarding"))
            .and_then(|o| o.get("provider"))
            .and_then(|v| v.as_str())
            .unwrap_or("");

        let mut items = vec![
            item("/help", "Show available commands"),
            item("/?", "Show available commands"),
            item("/setup", "Open setup command list"),
            item("/setup status", "Show onboarding state"),
            item("/setup reset", "Reset onboarding"),
            item("/setup provider", "Configure provider"),
            item("/setup model", "Choose model"),
            item("/setup project", "Set current project"),
            item("/setup api-key", "Save API key for API provider"),
            item("/setup auth-code", "Paste OAuth auth code manually"),
            item("/setup complete", "Mark setup complete"),
            item("/setup no-provider", "Run without provider setup"),
            item("/setup shade-provider", "Configure shade provider"),
            item("/setup shade-url", "Set shade base URL"),
            item("/setup shade-model", "Set shade model"),
            item("/setup shade-key", "Set shade API key"),
            item("/provider", "Show/switch provider"),
            item("/model", "Show/switch model"),
            item("/resume", "Resume saved conversation"),
            item("/project", "List explicit projects"),
            item("/project list", "List explicit projects"),
            item("/project create", "Create an explicit project object"),
            item("/project use", "Select an explicit project"),
            item("/conversation", "Create a live conversation room with Hermes participants"),
            item("/conversation hermes", "Start a Hermes live conversation room"),
            item("/conversation hermes teacher student", "Create a teacher/student Hermes room with live participants"),
            item("/conversation hermes strategist critic", "Create a strategist/critic Hermes room with live participants"),
            item("/conversation hermes planner critic", "Create a planner/critic Hermes room with live participants"),
            item("/conversation hermes architect reviewer", "Create an architect/reviewer Hermes room with live participants"),
            item("/conversation hermes optimist skeptic", "Create an optimist/skeptic Hermes room with live participants"),
            item("/conversation hermes dialogue", "Create a 2-agent Hermes dialogue room with live participants"),
            item("/conversation hermes 2", "Create a 2-agent Hermes conversation room with live participants"),
            item("/team", "Create a live multi-agent room/team"),
            item("/team hermes", "Create a Hermes discussion room with live participants"),
            item("/devteam", "Create a live developer team room"),
            item("/devteam hermes", "Create a Hermes developer team room with live participants"),
            item("/libris", "Start a Libris research room with live participants"),
            item("/hotkeys", "Keyboard shortcuts"),
            item("/timestamps", "Toggle timestamps"),
            item("/thoughts", "Toggle visible thoughts"),
            item("/settings", "Show current settings"),
            item("/config", "Show current settings"),
            item("/models", "List available models"),
            item("/tools", "List tools"),
            item("/tools reload", "Reload dynamic tools"),
            item("/approve", "Approve tools for current session"),
            item("/approve status", "Show tool approvals"),
            item("/approve all", "Approve all tools for session"),
            item("/approve network", "Approve network tools"),
            item("/approve write", "Approve file modification tools"),
            item("/autonomous", "Show autonomous status"),
            item("/autonomous status", "Show autonomous mode status"),
            item("/autonomous on", "Enable autonomous mode"),
            item("/autonomous off", "Disable autonomous mode"),
            item("/autonomous time", "Set autonomous time budget"),
            item("/autonomous tokens", "Set autonomous token budget"),
            item("/confirm", "Confirm first proposed goal"),
            item("/reject", "Reject first proposed goal"),
            item("/history", "Show session history"),
            item("/consolidation", "Show consolidation status"),
            item("/consolidation status", "Show consolidation status"),
            item("/consolidation run", "Run consolidation now"),
            item("/consolidation model", "Set consolidation model tier"),
            item("/consolidation interval", "Set consolidation scan interval"),
            item("/consolidation on", "Enable consolidation"),
            item("/consolidation off", "Disable consolidation"),
            item("/batch", "List batches"),
            item("/reset", "Clear current conversation"),
            item("/shades", "Show shade stats"),
            item("/shade stats", "Show shade stats"),
            item("/1", "Provider switch: continue with context transfer"),
            item("/2", "Provider switch: start a new session"),
            item("/fleet setup", "Set up a remote agent team (install, auth, start agents)"),
            item("/fleet status", "Show fleet status"),
            item("/voyage dispatch", "Dispatch a task to a remote agent worker"),
            item("/voyage status", "Check status of a voyage"),
            item("/voyage list", "List recent voyages"),
            item("/harvest_souls", "Scan sibling agent repos for abilities to assimilate"),
            item("/harvest_souls list", "Show numbered findings from last scan"),
            item("/harvest_souls evaluate", "Evaluate real capability gaps from the last scan"),
            item("/harvest_souls review", "Show capability-level harvest decisions"),
            item("/harvest_souls decide", "Inspect one capability harvest decision"),
            item("/harvest_souls harvest", "Queue capability clusters for assimilation"),
            item("/harvest_souls harvest all", "Queue all recommended capability clusters"),
            item("/harvest_souls plan", "Show implementation path for a raw ability"),
            item("/harvest_souls adopt", "Legacy: mark raw abilities for adoption"),
            item("/harvest_souls adopt all", "Legacy: adopt all raw discovered abilities"),
            item("/harvest_souls roadmap", "Show adoption roadmap and progress"),
            item("/harvest_souls status", "Show last scan summary"),
        ];

        if input == "/hotkeys" || input.starts_with("/hotkeys") {
            items.extend([
                item("F1", "Switch to Chat view"),
                item("F2", "Switch to Dashboard view"),
                item("F3", "Switch to Session Grid view"),
                item("Tab", "Navigate menus and views"),
                item("Enter", "Select menu item or submit input"),
                item("Escape", "Close menu"),
                item("Ctrl+F", "Zoom/unzoom session in grid"),
                item("Ctrl+T", "Toggle timestamps"),
                item("Ctrl+Y", "Toggle visible thoughts"),
            ]);
        }

        if input.starts_with("/setup provider") || input == "/provider" {
            items.extend([
                item("/setup provider claude-code", "Anthropic Claude OAuth"),
                item("/setup provider codex", "OpenAI Codex OAuth"),
                item("/setup provider opencode", "OpenCode provider"),
                item("/setup provider lmstudio", "Local LM Studio"),
                item("/setup provider api", "Custom API endpoint"),
                item("/setup provider claude-code --force", "Force fresh Claude OAuth"),
            ]);
        }

        if input.starts_with("/setup shade-provider") {
            items.extend([
                item("/setup shade-provider same", "Same as main provider"),
                item("/setup shade-provider lmstudio", "LM Studio local shade model"),
                item("/setup shade-provider api", "OpenAI-compatible API for shade"),
            ]);
        }

        if input.starts_with("/setup shade-model") {
            items.extend([
                item("/setup shade-model same", "Use same model as main agent"),
                item("/setup shade-model auto", "Let Charon choose automatically"),
            ]);
        }

        if input.starts_with("/provider ") {
            items.extend([
                item("/provider claude-code", "Switch to Anthropic Claude"),
                item("/provider codex", "Switch to OpenAI Codex"),
                item("/provider lmstudio", "Switch to LM Studio"),
                item("/provider api", "Switch to API provider"),
            ]);
        }

        if input == "/project" || input.starts_with("/project ") {
            items.extend([
                item("/project list", "List explicit projects"),
                item("/project create ", "Create explicit project: /project create <name> [path]"),
                item("/project use ", "Use explicit project: /project use <name>"),
            ]);
        }

        if input == "/conversation" || input.starts_with("/conversation ") || input == "/conv" || input.starts_with("/conv") {
            items.extend([
                item("/conversation hermes teacher student ", "Create a teacher/student Hermes room with live participants"),
                item("/conversation hermes debate ", "Create a 2-agent Hermes debate/dialogue room with live participants"),
                item("/conversation hermes strategist critic ", "Create a strategist/critic Hermes room with live participants"),
                item("/conversation hermes planner critic ", "Create a planner/critic Hermes room with live participants"),
                item("/conversation hermes architect reviewer ", "Create an architect/reviewer Hermes room with live participants"),
                item("/conversation hermes optimist skeptic ", "Create an optimist/skeptic Hermes room with live participants"),
                item("/conversation hermes dialogue ", "Create a 2-agent Hermes dialogue room with live participants"),
                item("/conversation hermes 2 ", "Create a 2-agent Hermes conversation room with live participants"),
            ]);
        }

        if input == "/team" || input.starts_with("/team ") {
            items.extend([
                item("/team hermes 2 ", "Create a 2-agent Hermes discussion room with live participants"),
                item("/team hermes 3 ", "Create a 3-agent Hermes discussion room with live participants"),
                item("/team hermes 4 ", "Create a 4-agent Hermes discussion room with live participants"),
            ]);
        }

        if input == "/devteam" || input.starts_with("/devteam ") {
            items.extend([
                item("/devteam hermes 2 ", "Create a 2-agent Hermes developer team room with live participants"),
                item("/devteam hermes 3 ", "Create a 3-agent Hermes developer team room with live participants"),
                item("/devteam hermes 4 ", "Create a 4-agent Hermes developer team room with live participants"),
            ]);
        }

        if input == "/libris" || input.starts_with("/libris ") {
            items.extend([
                item("/libris ", "Start a Libris research room from a broad prompt"),
                item("/libris status ", "Inspect a Libris room / swarm operation"),
            ]);
        }

        if input.starts_with("/setup model") || input == "/model" || input.starts_with("/model ") {
            let model_items: Vec<MenuItem> = match provider {
                "claude-code" => vec![
                    ("claude-sonnet-4-6", "Sonnet 4.6 — latest, fast"),
                    ("claude-opus-4-6", "Opus 4.6 — latest, most capable"),
                    ("claude-sonnet-4-5", "Sonnet 4.5"),
                    ("claude-opus-4-5", "Opus 4.5"),
                    ("claude-opus-4-1", "Opus 4.1"),
                    ("claude-sonnet-4-20250514", "Sonnet 4.0"),
                    ("claude-opus-4-20250514", "Opus 4.0"),
                    ("claude-haiku-4-5", "Haiku 4.5 — fastest"),
                    ("claude-3-7-sonnet-20250219", "Sonnet 3.7"),
                    ("claude-3-5-sonnet-20241022", "Sonnet 3.5 v2"),
                    ("claude-3-5-haiku-20241022", "Haiku 3.5"),
                ],
                "codex" => vec![
                    ("gpt-5.4", "GPT 5.4 — most capable (recommended)"),
                    ("gpt-5", "GPT 5"),
                ],
                _ => vec![],
            }
            .into_iter()
            .map(|(id, desc)| {
                let prefix = if input.starts_with("/model") { "/model" } else { "/setup model" };
                item(&format!("{} {}", prefix, id), desc)
            })
            .collect();
            items.extend(model_items);
        }

        let query = input.to_ascii_lowercase();
        let needle = query.trim_start_matches('/');
        let needle_alias = if needle == "conv" { "conversation" } else { needle };
        items.into_iter()
            .filter(|item| {
                item.cmd.to_ascii_lowercase().starts_with(&query)
                    || (!needle.is_empty() && item.cmd.to_ascii_lowercase().contains(needle))
                    || (!needle_alias.is_empty() && item.cmd.to_ascii_lowercase().contains(needle_alias))
                    || (!needle.is_empty() && item.desc.to_ascii_lowercase().contains(needle))
                    || (!needle_alias.is_empty() && item.desc.to_ascii_lowercase().contains(needle_alias))
            })
            .take(20)
            .collect()
    }

    pub fn onboarding_complete(&self) -> bool {
        self.refresh_payload.as_ref()
            .and_then(|p| p.get("onboarding"))
            .and_then(|o| o.get("complete"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
    }

    pub fn onboarding_step(&self) -> String {
        self.refresh_payload.as_ref()
            .and_then(|p| p.get("onboarding"))
            .and_then(|o| o.get("step"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    }

    pub fn onboarding_provider(&self) -> String {
        self.refresh_payload.as_ref()
            .and_then(|p| p.get("onboarding"))
            .and_then(|o| o.get("provider"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    }

    pub fn onboarding_project(&self) -> String {
        self.refresh_payload.as_ref()
            .and_then(|p| p.get("onboarding"))
            .and_then(|o| o.get("project"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    }

    pub fn engine_ready(&self) -> bool {
        self.refresh_payload.as_ref()
            .and_then(|p| p.get("engine_ready"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
    }

    pub fn provider_model(&self) -> String {
        let provider = self.refresh_payload.as_ref()
            .and_then(|p| p.get("onboarding"))
            .and_then(|o| o.get("provider"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let model = self.refresh_payload.as_ref()
            .and_then(|p| p.get("onboarding"))
            .and_then(|o| o.get("model"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if provider.is_empty() && model.is_empty() {
            "unconfigured".to_string()
        } else if model.is_empty() {
            provider.to_string()
        } else {
            format!("{}/{}", provider, model)
        }
    }

    pub fn orchestration_parse_hint(&self) -> Option<String> {
        let parse = self.refresh_payload.as_ref()
            .and_then(|p| p.get("orchestration_parse"))?;
        let source = parse.get("source").and_then(|v| v.as_str()).unwrap_or("");
        let command = parse.get("command").and_then(|v| v.as_str()).unwrap_or("");
        if source.is_empty() || command.is_empty() {
            return None;
        }
        let label = match source {
            "fast-path" => "parse:fast",
            "shades-parser" => "parse:shades",
            other => other,
        };
        Some(format!("{} {}", label, command))
    }
}

fn open_url(url: &str) -> bool {
    let attempts: &[(&str, &[&str])] = &[
        ("xdg-open", &[]),
        ("gio", &["open"]),
        ("sensible-browser", &[]),
        ("open", &[]),
        ("wslview", &[]),
        ("cmd.exe", &["/C", "start", ""]),
    ];
    for (cmd, args) in attempts {
        let mut command = Command::new(cmd);
        command.args(*args).arg(url).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        if command.spawn().is_ok() {
            return true;
        }
    }
    false
}

fn copy_to_clipboard(text: &str) -> bool {
    crate::clipboard::copy_to_clipboard_bool(text)
}
