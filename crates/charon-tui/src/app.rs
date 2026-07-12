use crate::chat::{ChatState, LaunchOptions};
use crate::protocol::SessionInfo;
use crate::session::SessionCell;
use std::collections::{HashMap, HashSet};
use std::io;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum View {
    Chat,
    Dashboard,
    Sessions,
    InterAgent,
}

pub struct DashboardState {
    pub focus_row: usize,
    pub focus_col: usize,
    pub agent_index: usize,
    pub project_index: usize,
    pub automation_index: usize,
}

impl DashboardState {
    pub fn new() -> Self {
        Self {
            focus_row: 0,
            focus_col: 0,
            agent_index: 0,
            project_index: 0,
            automation_index: 0,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionsSection {
    Agents,
    Projects,
    Grid,
}

pub struct SessionsState {
    pub panes: Vec<SessionCell>,
    pub focused: usize,
    pub terminal_mode: bool,
    pub app_mouse_mode: bool,
    pub section: SessionsSection,
    pub agent_index: usize,
    pub project_index: usize,
    pub agent_scroll: usize,
    pub project_scroll: usize,
    pub selected_project: Option<String>,
    pub visible_agents: HashSet<String>,
    pub collapsed_agents: HashSet<String>,
    pub backend_filter_pending: bool,
    pub known_session_ids: HashSet<String>,
    /// Latest state per charond session id (from the daemon inventory poll),
    /// used to color daemon pane borders. Keyed by session id.
    pub daemon_states: HashMap<String, String>,
    /// Full charond inventory from the latest poll, for sidebar workspace grouping.
    pub daemon_sessions: Vec<SessionInfo>,
    /// Manual split layout for the grid, keyed by pane `uid`. `None` = auto-tile.
    pub layout: Option<crate::layout::Node>,
    /// When true, the focused pane is shown fullscreen (zoom).
    pub zoom: bool,
    /// Active tab in the F3 grid; the grid shows only this tab's panes.
    pub active_tab: String,
}

impl SessionsState {
    pub fn new(panes: Vec<SessionCell>) -> Self {
        Self {
            panes,
            focused: 0,
            terminal_mode: false,
            app_mouse_mode: true,
            section: SessionsSection::Grid,
            agent_index: 0,
            project_index: 0,
            agent_scroll: 0,
            project_scroll: 0,
            selected_project: None,
            visible_agents: HashSet::new(),
            collapsed_agents: HashSet::new(),
            backend_filter_pending: false,
            known_session_ids: HashSet::new(),
            daemon_states: HashMap::new(),
            daemon_sessions: Vec::new(),
            layout: None,
            zoom: false,
            active_tab: "main".to_string(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TextPoint {
    pub row: usize,
    pub col: usize,
}

pub struct InterAgentState {
    pub selected: usize,
    pub scroll: usize,
    pub event_scroll: usize,
    pub graph_focus: bool,
    pub selected_node: usize,
    pub topic_detail: bool,
    pub room_panes: Vec<SessionCell>,
    pub room_panes_room_id: String,
    pub delete_confirm_open: bool,
    pub delete_target_room_id: String,
    pub delete_target_title: String,
    pub transcript_anchor: Option<TextPoint>,
    pub transcript_focus: Option<TextPoint>,
    pub transcript_dragging: bool,
    pub app_mouse_mode: bool,
    pub clipboard_notice: Option<(String, bool, Instant)>,
}

impl InterAgentState {
    pub fn new() -> Self {
        Self {
            selected: 0,
            scroll: 0,
            event_scroll: 0,
            graph_focus: false,
            selected_node: 0,
            topic_detail: false,
            room_panes: Vec::new(),
            room_panes_room_id: String::new(),
            delete_confirm_open: false,
            delete_target_room_id: String::new(),
            delete_target_title: String::new(),
            transcript_anchor: None,
            transcript_focus: None,
            transcript_dragging: false,
            app_mouse_mode: true,
            clipboard_notice: None,
        }
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
}

pub struct App {
    pub active_view: View,
    pub chat: ChatState,
    pub dashboard: DashboardState,
    pub sessions: SessionsState,
    pub inter_agent: InterAgentState,
}

impl App {
    pub fn new(panes: Vec<SessionCell>, launch: LaunchOptions) -> io::Result<Self> {
        Ok(Self {
            active_view: View::Chat,
            chat: ChatState::new(launch)?,
            dashboard: DashboardState::new(),
            sessions: SessionsState::new(panes),
            inter_agent: InterAgentState::new(),
        })
    }
}
