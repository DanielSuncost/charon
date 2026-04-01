use crate::chat::ChatState;
use crate::session::SessionCell;
use std::collections::HashSet;
use std::io;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum View {
    Chat,
    Dashboard,
    Sessions,
    InterAgent,
}

pub struct DashboardState {
    pub selected: usize,
}

impl DashboardState {
    pub fn new() -> Self {
        Self { selected: 0 }
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
}

impl SessionsState {
    pub fn new(panes: Vec<SessionCell>) -> Self {
        Self {
            panes,
            focused: 0,
            terminal_mode: false,
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
    pub fn new(panes: Vec<SessionCell>) -> io::Result<Self> {
        Ok(Self {
            active_view: View::Chat,
            chat: ChatState::new()?,
            dashboard: DashboardState::new(),
            sessions: SessionsState::new(panes),
            inter_agent: InterAgentState::new(),
        })
    }
}
