# Worker Brief W6 — First-Class MCP Support

Parent plan: `docs/plans/charon-autonomous-implementation-master-plan.md`
Status: Ready for autonomous implementation

## Objective
Make Charon interoperable with MCP in a safe, clear, first-class way.

## Scope
Implement:
- MCP client integration
- dynamic discovery of MCP tools
- namespaced registration / collision handling
- per-agent/per-project enablement
- auth/config path
- policy controls for MCP-originated tools

## Must not break
- existing built-in tool registration
- existing tool-call flows when MCP is disabled
- prompt assembly stability unless changes are additive

## Constraints
- MCP tools must not collide ambiguously with built-ins
- disabled MCP state must preserve current behavior
- tool provenance should be inspectable
- remote MCP use must respect approval/safety policy

## Required tests
- discover tools from configured MCP server
- invoke discovered tools successfully
- deterministic collision handling
- disable MCP per project or agent

## Acceptance benchmark
- connect an MCP server, discover tools, and use them safely and clearly

## Deliverables
1. implementation
2. tests
3. short design note
4. explicit list of unchanged public interfaces
5. known limitations
