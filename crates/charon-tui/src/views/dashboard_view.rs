//! F2 Dashboard view: agent status overview panels.

use std::io::{self, Write};

use crossterm::{cursor, QueueableCommand};
use serde_json::Value;

use crate::app::App;
use crate::render::{self, Rect};

use super::{payload_agents, payload_automations, payload_projects};

pub(crate) fn dashboard_sparkline(points: &[u64]) -> String {
    let glyphs = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
    let max = points.iter().copied().max().unwrap_or(0);
    if max == 0 {
        return "▁".repeat(points.len().max(1));
    }
    points.iter().map(|v| {
        let idx = ((*v as f64 / max as f64) * (glyphs.len() as f64 - 1.0)).round() as usize;
        glyphs[idx.min(glyphs.len() - 1)]
    }).collect()
}

pub(crate) fn draw_dashboard_panel<W: Write>(stdout: &mut W, area: Rect, title: &str, lines: &[String], focused: bool) -> io::Result<()> {
    render::render_border(stdout, area, title, focused)?;
    let max_lines = area.height as usize;
    for (i, line) in lines.iter().take(max_lines).enumerate() {
        stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
        let visible: String = line.chars().take(area.width as usize).collect();
        write!(stdout, "{}", visible)?;
    }
    Ok(())
}

pub(crate) fn rect_rows(area: Rect, rows: usize) -> Vec<Rect> {
    let mut out = Vec::new();
    let base_h = area.height / rows as u16;
    let extra = area.height % rows as u16;
    let mut y = area.y;
    for idx in 0..rows {
        let h = base_h + if idx < extra as usize { 1 } else { 0 };
        out.push(Rect { x: area.x, y, width: area.width, height: h });
        y += h;
    }
    out
}

pub(crate) fn rect_cols(area: Rect, widths: [u16; 3]) -> [Rect; 3] {
    let total = widths[0] + widths[1] + widths[2];
    let w1 = area.width.saturating_mul(widths[0]) / total.max(1);
    let w2 = area.width.saturating_mul(widths[1]) / total.max(1);
    let used = w1 + w2;
    let w3 = area.width.saturating_sub(used);
    [
        Rect { x: area.x, y: area.y, width: w1.saturating_sub(1), height: area.height.saturating_sub(1) },
        Rect { x: area.x + w1, y: area.y, width: w2.saturating_sub(1), height: area.height.saturating_sub(1) },
        Rect { x: area.x + used, y: area.y, width: w3.saturating_sub(1), height: area.height.saturating_sub(1) },
    ]
}

pub(crate) fn flatten_goal_tree(node: &Value, depth: usize, out: &mut Vec<String>) {
    let title = node.get("title").and_then(|v| v.as_str()).unwrap_or("goal");
    let status = node.get("status").and_then(|v| v.as_str()).unwrap_or("");
    let marker = if matches!(status, "completed") { "[x]" } else if matches!(status, "active" | "executing" | "planning" | "verifying") { "[>]" } else { "[ ]" };
    out.push(format!("{}{} {}", "  ".repeat(depth), marker, title));
    if let Some(children) = node.get("children").and_then(|v| v.as_array()) {
        for child in children {
            flatten_goal_tree(child, depth + 1, out);
        }
    }
}

pub(crate) fn draw_dashboard<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16) -> io::Result<()> {
    let outer = Rect { x: 1, y: 2, width: w.saturating_sub(2), height: h.saturating_sub(4) };
    let rows = rect_rows(outer, 3);
    let payload = app.chat.refresh_payload.as_ref();
    let agents = payload_agents(payload);
    let projects = payload_projects(payload);
    let automations = payload_automations(payload);

    let agent_idx = app.dashboard.agent_index.min(agents.len().saturating_sub(1));
    let project_idx = app.dashboard.project_index.min(projects.len().saturating_sub(1));
    let automation_idx = app.dashboard.automation_index.min(automations.len().saturating_sub(1));

    // Row 1: Agents
    {
        let cols = rect_cols(rows[0], [28, 38, 34]);
        let mut list = vec![format!("Provider/model: {}", app.chat.provider_model())];
        for (i, agent) in agents.iter().enumerate() {
            let prefix = if i == agent_idx { ">" } else { " " };
            let name = agent.get("name").and_then(|v| v.as_str()).unwrap_or("agent");
            let status = agent.get("status").and_then(|v| v.as_str()).unwrap_or("idle");
            let role = agent.get("role").and_then(|v| v.as_str()).unwrap_or("");
            list.push(format!("{} {} [{}] {}", prefix, name, status, role));
        }
        if agents.is_empty() { list.push("No agents yet.".to_string()); }

        let mut detail = Vec::new();
        let mut recent = Vec::new();
        if let Some(agent) = agents.get(agent_idx) {
            detail.push(format!("Name: {}", agent.get("name").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("ID: {}", agent.get("id").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Role: {}", agent.get("role").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Status: {}", agent.get("status").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Mode: {}", agent.get("mode").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Project: {}", agent.get("project").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Parent: {}", agent.get("parent_agent_id").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Last active: {}", agent.get("last_active").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Goal: {}", agent.get("goal").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Summary: {}", agent.get("last_summary").and_then(|v| v.as_str()).unwrap_or("")));
            recent.push("Recent outcomes".to_string());
            if let Some(ledger) = agent.get("ledger").and_then(|v| v.as_array()) {
                for item in ledger.iter().take(6) {
                    recent.push(format!("- {} {}", item.get("status").and_then(|v| v.as_str()).unwrap_or(""), item.get("task_id").and_then(|v| v.as_str()).unwrap_or("")));
                }
            }
            if let Some(actions) = agent.get("recent_actions").and_then(|v| v.as_array()) {
                for item in actions.iter().take(4) {
                    if let Some(s) = item.as_str() { recent.push(format!("- {}", s)); }
                }
            }
        } else {
            detail.push("No agent selected.".to_string());
            recent.push("No recent outcomes.".to_string());
        }
        draw_dashboard_panel(stdout, cols[0], "agents list", &list, app.dashboard.focus_row == 0 && app.dashboard.focus_col == 0)?;
        draw_dashboard_panel(stdout, cols[1], "agent details", &detail, app.dashboard.focus_row == 0 && app.dashboard.focus_col == 1)?;
        draw_dashboard_panel(stdout, cols[2], "agent outcomes", &recent, app.dashboard.focus_row == 0 && app.dashboard.focus_col == 2)?;
    }

    // Row 2: Projects
    {
        let cols = rect_cols(rows[1], [24, 42, 34]);
        let mut list = Vec::new();
        for (i, project) in projects.iter().enumerate() {
            let prefix = if i == project_idx { ">" } else { " " };
            let name = project.get("name").and_then(|v| v.as_str()).unwrap_or("project");
            let active = if project.get("active").and_then(|v| v.as_bool()).unwrap_or(false) { "active" } else { "idle" };
            let agents_count = project.get("agent_details").and_then(|v| v.as_array()).map(|v| v.len()).unwrap_or(0);
            list.push(format!("{} {} [{}] {}a", prefix, name, active, agents_count));
        }
        if projects.is_empty() { list.push("No projects yet.".to_string()); }

        let mut detail = Vec::new();
        let mut goals = Vec::new();
        if let Some(project) = projects.get(project_idx) {
            let usage = project.get("usage").unwrap_or(&Value::Null);
            let points: Vec<u64> = project.get("activity_points").and_then(|v| v.as_array()).map(|arr| arr.iter().filter_map(|v| v.as_u64()).collect()).unwrap_or_default();
            detail.push(format!("Name: {}", project.get("name").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Path: {}", project.get("path").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Active: {}", project.get("active").and_then(|v| v.as_bool()).unwrap_or(false)));
            detail.push(format!("Agents: {}", project.get("agent_details").and_then(|v| v.as_array()).map(|v| v.len()).unwrap_or(0)));
            detail.push(format!("Tokens: {}", usage.get("total_tokens").and_then(|v| v.as_u64()).unwrap_or(0)));
            detail.push(format!("Cost USD: {:.4}", usage.get("estimated_cost_usd").and_then(|v| v.as_f64()).unwrap_or(0.0)));
            detail.push(format!("Hours est: {:.2}", usage.get("hours_spent_estimate").and_then(|v| v.as_f64()).unwrap_or(0.0)));
            detail.push(format!("Libris ops: {}", usage.get("libris_operations").and_then(|v| v.as_u64()).unwrap_or(0)));
            detail.push(format!("Dev ops: {}", usage.get("devop_operations").and_then(|v| v.as_u64()).unwrap_or(0)));
            detail.push(format!("Activity: {}", dashboard_sparkline(&points)));
            goals.push("Goal tree".to_string());
            if let Some(tree) = project.get("goal_tree").and_then(|v| v.as_array()) {
                for node in tree.iter().take(12) {
                    flatten_goal_tree(node, 0, &mut goals);
                }
            }
            if goals.len() == 1 {
                goals.push("No goals recorded yet.".to_string());
            }
        } else {
            detail.push("No project selected.".to_string());
            goals.push("No goals recorded yet.".to_string());
        }
        draw_dashboard_panel(stdout, cols[0], "projects list", &list, app.dashboard.focus_row == 1 && app.dashboard.focus_col == 0)?;
        draw_dashboard_panel(stdout, cols[1], "project details", &detail, app.dashboard.focus_row == 1 && app.dashboard.focus_col == 1)?;
        draw_dashboard_panel(stdout, cols[2], "project goals", &goals, app.dashboard.focus_row == 1 && app.dashboard.focus_col == 2)?;
    }

    // Row 3: Automations
    {
        let cols = rect_cols(rows[2], [24, 40, 36]);
        let mut list = Vec::new();
        for (i, automation) in automations.iter().enumerate() {
            let prefix = if i == automation_idx { ">" } else { " " };
            let title = automation.get("title").and_then(|v| v.as_str()).unwrap_or("automation");
            let status = automation.get("status").and_then(|v| v.as_str()).unwrap_or("active");
            let health = automation.get("health").and_then(|v| v.as_str()).unwrap_or("unknown");
            let mode = automation.get("mode").and_then(|v| v.as_str()).unwrap_or("");
            list.push(format!("{} {} [{}:{}]", prefix, title, status, if mode.is_empty() { health } else { mode }));
        }
        if automations.is_empty() { list.push("No automations yet.".to_string()); }

        let mut detail = Vec::new();
        let mut runs = Vec::new();
        if let Some(automation) = automations.get(automation_idx) {
            let schedule = automation.get("schedule").unwrap_or(&Value::Null);
            let sched_desc = if schedule.get("type").and_then(|v| v.as_str()) == Some("cron") {
                format!("cron {}", schedule.get("cron").and_then(|v| v.as_str()).unwrap_or(""))
            } else if automation.get("mode").and_then(|v| v.as_str()) == Some("continuous") {
                format!("continuous/{}s", schedule.get("poll_seconds").and_then(|v| v.as_u64()).unwrap_or(60))
            } else {
                format!("every {}s", schedule.get("interval_seconds").and_then(|v| v.as_u64()).unwrap_or(0))
            };
            detail.push(format!("Title: {}", automation.get("title").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("ID: {}", automation.get("automation_id").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Kind: {}", automation.get("kind").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Mode: {}", automation.get("mode").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Schedule: {}", sched_desc));
            detail.push(format!("Status: {}", automation.get("status").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Health: {}", automation.get("health").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Next run: {}", automation.get("next_run_at").and_then(|v| v.as_str()).unwrap_or("continuous")));
            detail.push(format!("Heartbeat: {}", automation.get("last_heartbeat_at").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Last success: {}", automation.get("last_success_at").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Last failure: {}", automation.get("last_failure_at").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Consecutive failures: {}", automation.get("consecutive_failures").and_then(|v| v.as_u64()).unwrap_or(0)));
            detail.push(format!("Result: {}", automation.get("last_result_summary").and_then(|v| v.as_str()).unwrap_or("")));
            runs.push("Recent runs".to_string());
            if let Some(items) = automation.get("runs_tail").and_then(|v| v.as_array()) {
                for item in items.iter().rev().take(8) {
                    let ts = item.get("ts").and_then(|v| v.as_str()).unwrap_or("");
                    let ok = item.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
                    let summary = item.get("summary").and_then(|v| v.as_str()).unwrap_or("");
                    runs.push(format!("- {} [{}] {}", ts, if ok { "ok" } else { "fail" }, summary));
                    if let Some(details) = item.get("details") {
                        if let Some(path) = details.get("screenshot").and_then(|v| v.as_str()) {
                            runs.push(format!("  screenshot: {}", path));
                        }
                    }
                }
            }
            if runs.len() == 1 { runs.push("No runs yet.".to_string()); }
        } else {
            detail.push("No automation selected.".to_string());
            runs.push("No runs yet.".to_string());
        }
        draw_dashboard_panel(stdout, cols[0], "automations list", &list, app.dashboard.focus_row == 2 && app.dashboard.focus_col == 0)?;
        draw_dashboard_panel(stdout, cols[1], "automation details", &detail, app.dashboard.focus_row == 2 && app.dashboard.focus_col == 1)?;
        draw_dashboard_panel(stdout, cols[2], "automation runs", &runs, app.dashboard.focus_row == 2 && app.dashboard.focus_col == 2)?;
    }

    Ok(())
}
