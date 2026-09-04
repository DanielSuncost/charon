(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const NODE_WIDTH = 196;
  const NODE_HEIGHT = 92;
  const VIEWBOX_WIDTH = 1160;
  const VIEWBOX_HEIGHT = 680;
  const TERMINAL_STATUSES = new Set(["complete", "completed", "success", "failed", "error", "cancelled"]);
  const TYPE_COLORS = {
    router: "#72e6cc",
    agent: "#8db8ff",
    tool: "#f4c879",
    evaluator: "#b5a2ff",
    gate: "#f59ab1",
    input: "#9aa6b8",
    output: "#8be5a7",
  };
  const POLICY_COLORS = ["#72e6cc", "#8db8ff", "#b5a2ff", "#f4c879", "#f59ab1"];

  const FALLBACK_SNAPSHOT = {
    definition: {
      graph_id: "adaptive-build-v3",
      name: "Adaptive build graph",
      nodes: [
        {
          id: "request",
          label: "Task intake",
          type: "input",
          description: "Normalize goal, constraints, and repository context.",
          position: { x: 24, y: 294 },
          config: {
            schema: "software_task.v2",
            required: ["goal", "workspace"],
            source: "operator",
          },
        },
        {
          id: "router",
          label: "Intent router",
          type: "router",
          description: "Classify work and allocate the model mix.",
          position: { x: 255, y: 294 },
          config: {
            model: "small-reasoner",
            policy: "adaptive-confidence",
            confidence_floor: 0.72,
            fallback: "frontier-reasoner",
          },
        },
        {
          id: "planner",
          label: "Repository mapper",
          type: "agent",
          description: "Map architecture, ownership, and change boundaries.",
          position: { x: 500, y: 55 },
          config: {
            model: "balanced-coder",
            objective: "Produce an evidence-backed implementation map.",
            tools: ["search", "filesystem"],
            temperature: 0.1,
            timeout_s: 180,
          },
        },
        {
          id: "builder",
          label: "Implementation agent",
          type: "agent",
          description: "Apply the smallest coherent change set.",
          position: { x: 500, y: 235 },
          config: {
            model: "frontier-coder",
            objective: "Implement the approved change with bounded edits.",
            tools: ["filesystem", "shell", "patch"],
            temperature: 0.2,
            timeout_s: 420,
          },
        },
        {
          id: "reviewer",
          label: "Adversarial reviewer",
          type: "evaluator",
          description: "Probe assumptions, regressions, and missed constraints.",
          position: { x: 500, y: 415 },
          config: {
            model: "frontier-reasoner",
            rubric: "correctness-risk-v4",
            independence: "cross-family",
            minimum_evidence: 3,
          },
        },
        {
          id: "synth",
          label: "Evidence synthesizer",
          type: "evaluator",
          description: "Score implementation evidence against the rubric.",
          position: { x: 755, y: 112 },
          config: {
            model: "balanced-reasoner",
            rubric: "implementation-quality-v3",
            score_floor: 0.82,
            confidence_method: "bootstrap",
          },
        },
        {
          id: "tests",
          label: "Verification harness",
          type: "tool",
          description: "Run targeted checks and collect structured evidence.",
          position: { x: 755, y: 312 },
          config: {
            command_profile: "changed-surface",
            parallelism: 4,
            timeout_s: 300,
            capture: ["stdout", "exit_code", "duration"],
          },
        },
        {
          id: "gate",
          label: "Promotion gate",
          type: "gate",
          description: "Promote, revise, or stop based on calibrated evidence.",
          position: { x: 755, y: 512 },
          config: {
            policy: "quality-and-risk",
            accept_if: "quality >= 0.82 && critical_failures == 0",
            max_revisions: 2,
          },
        },
        {
          id: "result",
          label: "Verified change",
          type: "output",
          description: "Return the change, evidence, and routing trace.",
          position: { x: 985, y: 294 },
          config: {
            format: "change_bundle.v1",
            includes: ["summary", "evidence", "route", "cost"],
          },
        },
      ],
      edges: [
        { id: "e-request-router", source: "request", target: "router", on: "accepted", label: "normalize" },
        { id: "e-router-plan", source: "router", target: "planner", on: "map", label: "discovery" },
        { id: "e-router-build", source: "router", target: "builder", on: "build", label: "implementation" },
        { id: "e-router-review", source: "router", target: "reviewer", on: "review", label: "independent review" },
        { id: "e-plan-synth", source: "planner", target: "synth", on: "complete", label: "architecture evidence" },
        { id: "e-build-synth", source: "builder", target: "synth", on: "complete", label: "change evidence" },
        { id: "e-build-tests", source: "builder", target: "tests", on: "changed", label: "verify" },
        { id: "e-review-tests", source: "reviewer", target: "tests", on: "risks", label: "targeted checks" },
        { id: "e-synth-gate", source: "synth", target: "gate", on: "scored", label: "quality score" },
        { id: "e-tests-gate", source: "tests", target: "gate", on: "complete", label: "test evidence" },
        { id: "e-gate-result", source: "gate", target: "result", on: "accept", label: "promote" },
        { id: "e-gate-builder", source: "gate", target: "builder", on: "revise", label: "bounded revision" },
      ],
    },
    run: {
      run_id: "run-demo-042",
      status: "ready",
      active_nodes: [],
      node_states: {
        request: {
          status: "queued",
          attempt: 0,
          duration_ms: 0,
          output_summary: "Waiting for a demo run.",
        },
        router: { status: "queued", attempt: 0, duration_ms: 0 },
        planner: { status: "queued", attempt: 0, duration_ms: 0 },
        builder: { status: "queued", attempt: 0, duration_ms: 0 },
        reviewer: { status: "queued", attempt: 0, duration_ms: 0 },
        synth: { status: "queued", attempt: 0, duration_ms: 0 },
        tests: { status: "queued", attempt: 0, duration_ms: 0 },
        gate: { status: "queued", attempt: 0, duration_ms: 0 },
        result: { status: "queued", attempt: 0, duration_ms: 0 },
      },
      started_at: null,
      metrics: {
        quality_score: 0,
        success_rate: 0,
        elapsed_ms: 0,
        tokens: 0,
        cost_usd: 0,
        retries: 0,
        latency_p95_ms: 0,
      },
    },
    events: [
      {
        id: "evt-ready",
        timestamp: "2026-07-19T14:02:10.000Z",
        kind: "system",
        source: "runtime",
        message: "Graph definition loaded. Demo controls are ready.",
        severity: "info",
      },
      {
        id: "evt-policy",
        timestamp: "2026-07-19T14:02:10.180Z",
        kind: "route",
        source: "policy-registry",
        message: "Adaptive confidence profile selected for this graph.",
        severity: "info",
      },
    ],
    benchmark: {
      data_kind: "illustrative_fixture",
      seed: null,
      description: "Illustrative embedded UI values; not measured benchmark outcomes.",
      recommendation: {
        rule_id: "illustrative_operating_envelope",
        evaluation_split: "illustrative",
        eligibility: {
          mean_cost_usd_lte: 0.12,
          p95_latency_ms_lte: 55000,
        },
        rank_by: ["success_rate_desc", "mean_score_desc", "policy_id_asc"],
        eligible_policy_ids: ["cost-first", "adaptive"],
        selected_policy_id: "adaptive",
      },
      provenance: {
        generation_method: "embedded_illustrative_fixture",
        evaluation_scenario_count: 140,
        repetitions_per_scenario: 3,
        confidence_level: 0.95,
      },
      models: [
        { id: "fast", name: "Fast coder" },
        { id: "balanced", name: "Balanced coder" },
        { id: "frontier", name: "Frontier reasoner" },
      ],
      policies: [
        {
          id: "cost-first",
          name: "Cost first",
          description: "Prefer the smallest capable model and escalate on low confidence.",
          samples: 420,
          trials: 420,
          scenarios: 140,
          repetitions_per_scenario: 3,
          confidence_level: 0.95,
          interval_method: "illustrative_interval",
          interval_status: "illustrative",
          pass_rate: 0.784,
          quality_mean: 0.798,
          success_ci: [0.742, 0.821],
          cost_mean: 0.061,
          latency_p50_ms: 14200,
          latency_p95_ms: 44100,
          calibration_error: 0.071,
          pareto: true,
        },
        {
          id: "adaptive",
          name: "Adaptive confidence",
          description: "Route by task class, uncertainty, and observed error profile.",
          samples: 420,
          trials: 420,
          scenarios: 140,
          repetitions_per_scenario: 3,
          confidence_level: 0.95,
          interval_method: "illustrative_interval",
          interval_status: "illustrative",
          pass_rate: 0.867,
          quality_mean: 0.874,
          success_ci: [0.831, 0.897],
          cost_mean: 0.104,
          latency_p50_ms: 18800,
          latency_p95_ms: 51200,
          calibration_error: 0.034,
          pareto: true,
        },
        {
          id: "frontier-only",
          name: "Frontier only",
          description: "Use the strongest reasoning profile for every task class.",
          samples: 420,
          trials: 420,
          scenarios: 140,
          repetitions_per_scenario: 3,
          confidence_level: 0.95,
          interval_method: "illustrative_interval",
          interval_status: "illustrative",
          pass_rate: 0.889,
          quality_mean: 0.895,
          success_ci: [0.854, 0.916],
          cost_mean: 0.221,
          latency_p50_ms: 25700,
          latency_p95_ms: 68400,
          calibration_error: 0.052,
          pareto: false,
        },
      ],
      comparisons: [
        {
          baseline: "Cost first",
          challenger: "Adaptive confidence",
          metric: "Task pass rate",
          delta: 0.083,
          ci: [0.047, 0.118],
          conclusion: "higher",
        },
        {
          baseline: "Frontier only",
          challenger: "Adaptive confidence",
          metric: "Mean request cost",
          delta: -0.117,
          ci: [-0.129, -0.105],
          conclusion: "lower",
        },
        {
          baseline: "Frontier only",
          challenger: "Adaptive confidence",
          metric: "Task pass rate",
          delta: -0.022,
          ci: [-0.048, 0.006],
          conclusion: "inconclusive",
        },
      ],
      routing_matrix: [
        {
          task_class: "Repository mapping",
          weights: { fast: 0.08, balanced: 0.67, frontier: 0.25 },
        },
        {
          task_class: "Bounded implementation",
          weights: { fast: 0.18, balanced: 0.56, frontier: 0.26 },
        },
        {
          task_class: "Ambiguous debugging",
          weights: { fast: 0.03, balanced: 0.25, frontier: 0.72 },
        },
        {
          task_class: "Test generation",
          weights: { fast: 0.31, balanced: 0.56, frontier: 0.13 },
        },
        {
          task_class: "Adversarial review",
          weights: { fast: 0.02, balanced: 0.22, frontier: 0.76 },
        },
      ],
    },
  };

  const LOCAL_STAGES = [
    {
      active: ["request"],
      complete: [],
      message: "Task intake accepted the goal and workspace constraints.",
      source: "request",
      kind: "system",
    },
    {
      active: ["router"],
      complete: ["request"],
      message: "Intent router classified the task as mixed implementation and evaluation.",
      source: "router",
      kind: "route",
    },
    {
      active: ["planner", "builder", "reviewer"],
      complete: ["request", "router"],
      message: "Three bounded workstreams dispatched in parallel.",
      source: "router",
      kind: "route",
    },
    {
      active: ["builder", "synth", "tests"],
      complete: ["request", "router", "planner", "reviewer"],
      message: "Repository map and risk review converged; verification is running.",
      source: "synth",
      kind: "evaluation",
    },
    {
      active: ["synth", "tests"],
      complete: ["request", "router", "planner", "builder", "reviewer"],
      message: "Implementation finished with a bounded change surface.",
      source: "builder",
      kind: "success",
    },
    {
      active: ["gate"],
      complete: ["request", "router", "planner", "builder", "reviewer", "synth", "tests"],
      message: "Evidence bundle scored 0.87; promotion policy is evaluating.",
      source: "gate",
      kind: "evaluation",
    },
    {
      active: ["result"],
      complete: ["request", "router", "planner", "builder", "reviewer", "synth", "tests", "gate"],
      message: "Promotion gate accepted the run with no critical failures.",
      source: "gate",
      kind: "success",
    },
    {
      active: [],
      complete: [
        "request",
        "router",
        "planner",
        "builder",
        "reviewer",
        "synth",
        "tests",
        "gate",
        "result",
      ],
      message: "Verified change bundle is ready for review.",
      source: "result",
      kind: "success",
    },
  ];

  const state = {
    snapshot: deepClone(FALLBACK_SNAPSHOT),
    usingFallback: true,
    apiAvailable: false,
    selectedNodeId: "router",
    requestedNodeId: new URLSearchParams(window.location.search).get("node"),
    activeView: "graph",
    localStage: -1,
    localTimer: null,
    pollTimer: null,
    pollingPaused: false,
    busy: false,
    view: { scale: 1, x: 0, y: 0 },
    lastGraphId: null,
    drag: null,
    toastTimer: null,
  };

  const elements = {};

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    cacheElements();
    bindUI();
    renderAll({ fit: true });
    const requestedView = new URLSearchParams(window.location.search).get("view");
    if (requestedView === "calibration") {
      setView(requestedView);
    }
    loadSnapshot();
  }

  function cacheElements() {
    [
      "studio",
      "workflowRail",
      "workflowHeading",
      "workflowId",
      "sourceChip",
      "railLiveIndicator",
      "metricQuartet",
      "runId",
      "nodeCount",
      "nodeRunList",
      "breadcrumbRun",
      "topbarTitle",
      "connectionState",
      "graphTab",
      "calibrationTab",
      "graphPanel",
      "calibrationPanel",
      "canvasStage",
      "graphSvg",
      "viewport",
      "edgeLayer",
      "nodeLayer",
      "canvasEmpty",
      "zoomReadout",
      "progressLabel",
      "progressValue",
      "progressTrack",
      "progressBar",
      "runButtonLabel",
      "nodeInspector",
      "inspectorTitle",
      "inspectorBody",
      "provenanceNotice",
      "policyScorecards",
      "recommendationSummary",
      "confidenceUnit",
      "confidenceChart",
      "paretoChart",
      "routingMatrix",
      "comparisonList",
      "eventCount",
      "eventStream",
      "toast",
      "announcer",
    ].forEach((id) => {
      elements[id] = document.getElementById(id);
    });
  }

  function bindUI() {
    document.addEventListener("click", (event) => {
      const actionButton = event.target.closest("[data-action]");
      if (actionButton) {
        handleAction(actionButton.dataset.action);
      }

      const tab = event.target.closest("[data-view]");
      if (tab) {
        setView(tab.dataset.view);
      }
    });

    elements.graphSvg.addEventListener("wheel", handleWheel, { passive: false });
    elements.graphSvg.addEventListener("pointerdown", handlePointerDown);
    elements.graphSvg.addEventListener("pointermove", handlePointerMove);
    elements.graphSvg.addEventListener("pointerup", handlePointerUp);
    elements.graphSvg.addEventListener("pointercancel", handlePointerUp);

    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        elements.studio.classList.remove("rail-open", "inspector-open");
        syncRailToggle();
      }
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        handleAction("run");
      }
    });

    window.addEventListener(
      "resize",
      debounce(() => {
        if (window.innerWidth > 860) {
          elements.studio.classList.remove("rail-open");
          syncRailToggle();
        }
      }, 120),
    );
  }

  async function loadSnapshot(options = {}) {
    if (window.location.protocol === "file:") {
      setDataSource(false, "Embedded fixture · direct file mode");
      if (!options.silent) {
        announce("Using embedded demonstration data because this page was opened directly.");
      }
      return;
    }

    try {
      const response = await fetch("/api/snapshot", {
        method: "GET",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`Snapshot request returned ${response.status}`);
      }
      const payload = await response.json();
      applySnapshot(extractSnapshot(payload), { source: "api", fit: !state.apiAvailable });
      state.apiAvailable = true;
      state.usingFallback = false;
      setDataSource(true, "Live snapshot API");
      if (!options.silent) {
        announce("Live graph snapshot connected.");
      }
      syncPolling();
    } catch (error) {
      state.apiAvailable = false;
      state.usingFallback = true;
      setDataSource(false, "Embedded fixture · API unavailable");
      if (!options.silent) {
        showToast("Snapshot API unavailable. The embedded demo remains fully interactive.");
      }
      console.info("[Graph Studio] Using embedded fixture:", error.message);
    }
  }

  function applySnapshot(snapshot, options = {}) {
    if (!snapshot || typeof snapshot !== "object") {
      throw new Error("Snapshot payload is not an object");
    }
    const normalized = normalizeSnapshot(snapshot);
    const nextGraphId = normalized.definition.graph_id;
    const shouldFit = options.fit || state.lastGraphId !== nextGraphId;
    state.snapshot = normalized;
    state.lastGraphId = nextGraphId;
    if (
      state.requestedNodeId &&
      normalized.definition.nodes.some((node) => node.id === state.requestedNodeId)
    ) {
      state.selectedNodeId = state.requestedNodeId;
      state.requestedNodeId = null;
    } else if (state.selectedNodeId && !findNode(state.selectedNodeId)) {
      state.selectedNodeId = null;
    }
    renderAll({ fit: shouldFit });
  }

  function normalizeSnapshot(snapshot) {
    const definition = snapshot.definition && typeof snapshot.definition === "object" ? snapshot.definition : {};
    const nodes = Array.isArray(definition.nodes)
      ? definition.nodes.map((node, index) => normalizeNode(node, index))
      : [];
    const nodeIds = new Set(nodes.map((node) => node.id));
    const edges = Array.isArray(definition.edges)
      ? definition.edges
          .map((edge, index) => ({
            id: String(edge?.id || `edge-${index + 1}`),
            source: String(edge?.source || ""),
            target: String(edge?.target || ""),
            on: String(edge?.on || ""),
            label: String(edge?.label || edge?.on || ""),
          }))
          .filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
      : [];
    const run = snapshot.run && typeof snapshot.run === "object" ? snapshot.run : {};
    return {
      definition: {
        graph_id: String(definition.graph_id || "untitled-graph"),
        name: String(definition.name || "Untitled graph"),
        nodes,
        edges,
      },
      run: {
        run_id: String(run.run_id || "not-started"),
        status: String(run.status || "ready").toLowerCase(),
        active_nodes: Array.isArray(run.active_nodes) ? run.active_nodes.map(String) : [],
        node_states: run.node_states && typeof run.node_states === "object" ? run.node_states : {},
        started_at: run.started_at || null,
        metrics: run.metrics && typeof run.metrics === "object" ? run.metrics : {},
      },
      events: Array.isArray(snapshot.events) ? snapshot.events : [],
      benchmark:
        snapshot.benchmark && typeof snapshot.benchmark === "object"
          ? {
              data_kind: String(snapshot.benchmark.data_kind || ""),
              seed: snapshot.benchmark.seed ?? null,
              description: String(snapshot.benchmark.description || ""),
              recommendation:
                snapshot.benchmark.recommendation &&
                typeof snapshot.benchmark.recommendation === "object"
                  ? snapshot.benchmark.recommendation
                  : {},
              provenance:
                snapshot.benchmark.provenance &&
                typeof snapshot.benchmark.provenance === "object"
                  ? snapshot.benchmark.provenance
                  : {},
              policies: Array.isArray(snapshot.benchmark.policies) ? snapshot.benchmark.policies : [],
              comparisons: Array.isArray(snapshot.benchmark.comparisons) ? snapshot.benchmark.comparisons : [],
              routing_matrix: Array.isArray(snapshot.benchmark.routing_matrix)
                ? snapshot.benchmark.routing_matrix
                : [],
              models: Array.isArray(snapshot.benchmark.models) ? snapshot.benchmark.models : [],
            }
          : {
              data_kind: "",
              seed: null,
              description: "",
              recommendation: {},
              provenance: {},
              policies: [],
              comparisons: [],
              routing_matrix: [],
              models: [],
            },
      control_token: String(snapshot.control_token || snapshot.control?.token || ""),
    };
  }

  function normalizeNode(node, index) {
    const position = node?.position && typeof node.position === "object" ? node.position : {};
    const fallbackColumn = index % 4;
    const fallbackRow = Math.floor(index / 4);
    const rawType = String(node?.type || "agent").toLowerCase();
    return {
      id: String(node?.id || `node-${index + 1}`),
      label: String(node?.label || node?.id || `Node ${index + 1}`),
      type: TYPE_COLORS[rawType] ? rawType : "agent",
      description: String(node?.description || "No description provided."),
      position: {
        x: finiteNumber(position.x, 45 + fallbackColumn * 255),
        y: finiteNumber(position.y, 55 + fallbackRow * 165),
      },
      config: node?.config && typeof node.config === "object" ? node.config : {},
    };
  }

  function renderAll(options = {}) {
    renderChrome();
    renderRail();
    renderGraph();
    renderInspector();
    renderEvents();
    renderCalibration();
    if (options.fit) {
      requestAnimationFrame(fitGraph);
    } else {
      applyViewTransform();
    }
  }

  function renderChrome() {
    const { definition, run } = state.snapshot;
    elements.workflowHeading.textContent = definition.name;
    elements.workflowId.textContent = `graph://${definition.graph_id}`;
    elements.topbarTitle.textContent = definition.name;
    elements.breadcrumbRun.textContent = run.run_id;
    elements.runId.textContent = run.run_id;

    const status = normalizeStatus(run.status);
    const running = status === "active";
    elements.railLiveIndicator.classList.toggle("is-running", running);
    elements.railLiveIndicator.lastChild.textContent = ` ${humanStatus(run.status)}`;

    const runButton = document.querySelector('[data-action="run"]');
    runButton.classList.toggle("is-running", running && state.usingFallback && Boolean(state.localTimer));
    runButton.disabled = state.busy;
    if (state.busy) {
      elements.runButtonLabel.textContent = "Working";
    } else if (running && state.usingFallback && state.localTimer) {
      elements.runButtonLabel.textContent = "Pause demo";
    } else if (running && state.apiAvailable) {
      elements.runButtonLabel.textContent = "Refresh run";
    } else if (TERMINAL_STATUSES.has(String(run.status).toLowerCase())) {
      elements.runButtonLabel.textContent = "Run again";
    } else {
      elements.runButtonLabel.textContent = "Run graph";
    }

    document.querySelectorAll('[data-action="reset"], [data-action="tick"]').forEach((button) => {
      button.disabled = state.busy;
    });

    const completeCount = definition.nodes.filter((node) => nodeVisualStatus(node.id) === "complete").length;
    const percent = definition.nodes.length ? Math.round((completeCount / definition.nodes.length) * 100) : 0;
    elements.progressLabel.textContent =
      run.status === "ready"
        ? "Ready to run"
        : running
          ? "Execution in progress"
          : TERMINAL_STATUSES.has(String(run.status).toLowerCase())
            ? humanStatus(run.status)
            : humanStatus(run.status);
    elements.progressValue.textContent = `${completeCount} / ${definition.nodes.length}`;
    elements.progressBar.style.width = `${percent}%`;
    elements.progressTrack.setAttribute("aria-valuenow", String(percent));
  }

  function renderRail() {
    const { definition, run } = state.snapshot;
    const metrics = run.metrics || {};
    const metricEntries = [
      ["Quality", formatScore(metricValue(metrics, ["quality_score", "quality", "score"], 0))],
      ["Elapsed", formatDuration(metricValue(metrics, ["elapsed_ms", "duration_ms"], 0))],
      ["Steps", compactNumber(metricValue(metrics, ["run_steps"], 0))],
      ["Est. cost", formatMoney(metricValue(metrics, ["cost_usd", "cost", "total_cost"], 0))],
    ];
    elements.metricQuartet.innerHTML = metricEntries
      .map(
        ([label, value]) =>
          `<div class="rail-metric"><span>${escapeHTML(label)}</span><strong>${escapeHTML(value)}</strong></div>`,
      )
      .join("");

    elements.nodeCount.textContent = `${definition.nodes.length} node${definition.nodes.length === 1 ? "" : "s"}`;
    elements.nodeRunList.innerHTML = definition.nodes
      .map((node) => {
        const visualStatus = nodeVisualStatus(node.id);
        const rawStatus = nodeRawStatus(node.id);
        const current = state.selectedNodeId === node.id;
        return `
          <li>
            <button
              class="node-run-button"
              type="button"
              data-node-select="${escapeAttribute(node.id)}"
              aria-current="${current ? "true" : "false"}"
              style="--node-color:${TYPE_COLORS[node.type]}"
            >
              <span class="run-node-icon" aria-hidden="true">${escapeHTML(typeInitial(node.type))}</span>
              <span class="run-node-copy">
                <strong>${escapeHTML(node.label)}</strong>
                <span>${escapeHTML(rawStatus)}</span>
              </span>
              <i class="run-node-state is-${visualStatus}" aria-hidden="true"></i>
            </button>
          </li>
        `;
      })
      .join("");

    elements.nodeRunList.querySelectorAll("[data-node-select]").forEach((button) => {
      button.addEventListener("click", () => selectNode(button.dataset.nodeSelect));
    });
  }

  function renderGraph() {
    const { nodes, edges } = state.snapshot.definition;
    elements.canvasEmpty.hidden = nodes.length > 0;
    if (!nodes.length) {
      elements.edgeLayer.innerHTML = "";
      elements.nodeLayer.innerHTML = "";
      return;
    }

    const nodeMap = new Map(nodes.map((node) => [node.id, node]));
    elements.edgeLayer.innerHTML = edges
      .map((edge) => renderEdge(edge, nodeMap))
      .filter(Boolean)
      .join("");
    elements.nodeLayer.innerHTML = nodes.map((node) => renderNode(node, edges)).join("");

    elements.nodeLayer.querySelectorAll(".graph-node").forEach((nodeElement) => {
      nodeElement.addEventListener("click", (event) => {
        event.stopPropagation();
        selectNode(nodeElement.dataset.nodeId);
      });
      nodeElement.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectNode(nodeElement.dataset.nodeId);
        }
      });
    });
    applyViewTransform();
  }

  function renderEdge(edge, nodeMap) {
    const source = nodeMap.get(edge.source);
    const target = nodeMap.get(edge.target);
    if (!source || !target) return "";

    const geometry = edgeGeometry(source, target);
    const sourceStatus = nodeVisualStatus(source.id);
    const targetStatus = nodeVisualStatus(target.id);
    const classes = ["graph-edge"];
    if (targetStatus === "active" || sourceStatus === "active") classes.push("is-active");
    else if (targetStatus === "complete") classes.push("is-complete");
    else if (sourceStatus === "error" || targetStatus === "error") classes.push("is-error");
    else if (state.selectedNodeId && source.id !== state.selectedNodeId && target.id !== state.selectedNodeId) {
      classes.push("is-muted");
    }

    const label = edge.label || edge.on;
    const labelMarkup = label
      ? renderEdgeLabel(truncate(label, 22), geometry.labelX, geometry.labelY, classes.includes("is-active"))
      : "";
    return `
      <g class="edge-group" data-edge-id="${escapeAttribute(edge.id)}">
        <path class="graph-edge-halo" d="${geometry.path}" />
        <path class="${classes.join(" ")}" d="${geometry.path}" />
        ${labelMarkup}
      </g>
    `;
  }

  function edgeGeometry(source, target) {
    const sx = source.position.x + NODE_WIDTH;
    const sy = source.position.y + NODE_HEIGHT / 2;
    const tx = target.position.x;
    const ty = target.position.y + NODE_HEIGHT / 2;
    if (tx > sx + 35) {
      const distance = tx - sx;
      const bend = Math.max(46, distance * 0.45);
      return {
        path: `M ${sx} ${sy} C ${sx + bend} ${sy}, ${tx - bend} ${ty}, ${tx} ${ty}`,
        labelX: (sx + tx) / 2,
        labelY: (sy + ty) / 2 - 7,
      };
    }

    const routeY = Math.min(VIEWBOX_HEIGHT - 18, Math.max(source.position.y, target.position.y) + NODE_HEIGHT + 58);
    const sourceTurn = sx + 48;
    const targetTurn = tx - 42;
    return {
      path: `M ${sx} ${sy} C ${sourceTurn} ${sy}, ${sourceTurn} ${routeY}, ${sx} ${routeY} L ${targetTurn} ${routeY} C ${targetTurn - 34} ${routeY}, ${targetTurn - 34} ${ty}, ${tx} ${ty}`,
      labelX: (sx + tx) / 2,
      labelY: routeY - 8,
    };
  }

  function renderEdgeLabel(label, x, y, active) {
    const width = Math.max(42, label.length * 4.5 + 12);
    return `
      <g class="edge-label${active ? " is-active" : ""}" transform="translate(${x - width / 2} ${y - 8})">
        <rect width="${width}" height="15" rx="7.5"></rect>
        <text x="${width / 2}" y="10.3" text-anchor="middle">${escapeHTML(label)}</text>
      </g>
    `;
  }

  function renderNode(node, edges) {
    const visualStatus = nodeVisualStatus(node.id);
    const selected = state.selectedNodeId === node.id;
    const incoming = edges.some((edge) => edge.target === node.id);
    const outgoing = edges.some((edge) => edge.source === node.id);
    const model = configValue(node.config, ["model", "policy", "schema", "format"], node.type);
    const descriptionLines = wrapText(node.description, 32, 2);
    return `
      <g
        class="graph-node is-${visualStatus}${selected ? " is-selected" : ""}"
        data-node-id="${escapeAttribute(node.id)}"
        data-type="${escapeAttribute(node.type)}"
        transform="translate(${node.position.x} ${node.position.y})"
        role="button"
        tabindex="0"
        aria-label="${escapeAttribute(`${node.label}, ${node.type}, ${nodeRawStatus(node.id)}`)}"
      >
        <rect class="node-selection" x="-6" y="-6" width="${NODE_WIDTH + 12}" height="${NODE_HEIGHT + 12}" rx="16" />
        <rect class="node-shell" width="${NODE_WIDTH}" height="${NODE_HEIGHT}" rx="12" />
        <path class="node-accent" d="M 0 12 Q 0 0 12 0 L 3 0 L 3 ${NODE_HEIGHT} L 12 ${NODE_HEIGHT} Q 0 ${NODE_HEIGHT} 0 ${NODE_HEIGHT - 12} Z" />
        <rect class="node-icon-bg" x="14" y="18" width="34" height="34" rx="9" />
        <g class="node-icon" transform="translate(21 25)">${nodeIcon(node.type)}</g>
        <text class="node-type" x="58" y="18">${escapeHTML(node.type.toUpperCase())}</text>
        <text class="node-label" x="58" y="37">${escapeHTML(truncate(node.label, 22))}</text>
        <text class="node-description" x="58" y="51">
          ${descriptionLines.map((line, index) => `<tspan x="58" dy="${index === 0 ? 0 : 10}">${escapeHTML(line)}</tspan>`).join("")}
        </text>
        <rect class="node-model-pill" x="58" y="68" width="${Math.min(116, Math.max(50, String(model).length * 4.8 + 14))}" height="15" rx="7.5" />
        <text class="node-model" x="65" y="78.4">${escapeHTML(truncate(String(model), 22))}</text>
        <circle class="node-status-ring" cx="178" cy="18" r="5.5" />
        <circle class="node-status-core" cx="178" cy="18" r="2.1" />
        ${
          incoming
            ? `<rect class="node-port" x="-5" y="38" width="10" height="16" rx="5" /><circle class="node-port-dot" cx="0" cy="46" r="2" />`
            : ""
        }
        ${
          outgoing
            ? `<rect class="node-port" x="${NODE_WIDTH - 5}" y="38" width="10" height="16" rx="5" /><circle class="node-port-dot" cx="${NODE_WIDTH}" cy="46" r="2" />`
            : ""
        }
      </g>
    `;
  }

  function nodeIcon(type) {
    const icons = {
      router:
        '<circle cx="3" cy="10" r="2"/><circle cx="16" cy="3" r="2"/><circle cx="16" cy="17" r="2"/><path d="m5 9 9-5M5 11l9 5"/>',
      agent:
        '<rect x="3" y="6" width="14" height="11" rx="3"/><path d="M10 6V2M7 11h.1M13 11h.1M7 15h6"/>',
      tool: '<path d="m4 16 5-5M12 3a5 5 0 0 0 5 5l-8.5 8.5a2.1 2.1 0 0 1-3-3L14 5a5 5 0 0 0-2-2Z"/>',
      evaluator:
        '<path d="M4 3h12v14H4zM7 7h6M7 10h6M7 13h3"/><path d="m12 14 1.5 1.5L17 12"/>',
      gate: '<path d="m10 2 7 8-7 8-7-8Z"/><path d="M7 10h6M10 7v6"/>',
      input: '<path d="M3 3h10l4 4v10H3zM13 3v4h4M7 12h6M10 9l3 3-3 3"/>',
      output: '<path d="M3 3h10l4 4v10H3zM13 3v4h4M13 12H7M10 9l-3 3 3 3"/>',
    };
    return icons[type] || icons.agent;
  }

  function renderRoutingDecision(decision) {
    const candidates = Array.isArray(decision.candidates) ? decision.candidates.slice(0, 6) : [];
    const selected = candidates.find((candidate) => candidate?.selected);
    const endpoint =
      decision.selected_endpoint && typeof decision.selected_endpoint === "object"
        ? decision.selected_endpoint
        : {};
    const weights =
      decision.policy_weights && typeof decision.policy_weights === "object"
        ? Object.entries(decision.policy_weights)
        : [];
    const endpointLabel = [endpoint.provider, endpoint.model_id].filter(Boolean).join(" · ");

    return `
      <section class="inspector-section route-decision">
        <div class="inspector-section-heading">
          <h3>Routing decision</h3>
          <span>${escapeHTML(decision.policy_version || "policy")}</span>
        </div>
        <div class="route-decision-hero">
          <div>
            <span class="route-decision-kicker">${escapeHTML(decision.policy || "balanced")} policy</span>
            <strong>${escapeHTML(decision.selected_candidate_id || "No feasible endpoint")}</strong>
            <small>${escapeHTML(endpointLabel || decision.selection_reason || "Selection unavailable")}</small>
          </div>
          <span class="route-decision-mark ${decision.feasible === false ? "is-rejected" : ""}" aria-hidden="true">
            ${decision.feasible === false ? "×" : "✓"}
          </span>
        </div>
        ${
          weights.length
            ? `<div class="route-weight-grid" aria-label="Policy weights">${weights
                .map(
                  ([name, weight]) => `
                    <div class="route-weight">
                      <span>${escapeHTML(humanize(name))}</span>
                      <strong>${formatPercent(weight)}</strong>
                    </div>`,
                )
                .join("")}</div>`
            : ""
        }
        <div class="route-candidates">
          ${candidates
            .map((candidate, index) => {
              const estimate =
                candidate?.estimate && typeof candidate.estimate === "object"
                  ? candidate.estimate
                  : {};
              const feasible = candidate?.feasible !== false;
              const score = finiteNumber(candidate?.total_score, 0);
              const violation = Array.isArray(candidate?.violations)
                ? candidate.violations[0]?.message
                : "";
              return `
                <article class="route-candidate ${candidate?.selected ? "is-selected" : ""} ${feasible ? "" : "is-rejected"}">
                  <div class="route-candidate-heading">
                    <span class="route-rank">${candidate?.rank ? `#${candidate.rank}` : "×"}</span>
                    <strong>${escapeHTML(candidate?.candidate_id || `candidate-${index + 1}`)}</strong>
                    <span class="route-score">${feasible ? formatScore(score) : "blocked"}</span>
                  </div>
                  ${
                    feasible
                      ? `
                        <div class="route-score-track" aria-label="${escapeAttribute(`Routing score ${formatScore(score)}`)}">
                          <i style="width:${Math.round(normalize01(score) * 100)}%"></i>
                        </div>
                        <div class="route-candidate-metrics">
                          <span><b>Quality</b>${formatPercent(estimate.quality_lower_bound)}</span>
                          <span><b>Reliable</b>${formatPercent(estimate.reliability_lower_bound)}</span>
                          <span><b>Cost</b>${formatMoney(estimate.cost_usd)}</span>
                          <span><b>SLO</b>${formatDuration(estimate.slo_latency_ms)}</span>
                        </div>`
                      : `<p class="route-violation">${escapeHTML(violation || "Hard constraints rejected this endpoint.")}</p>`
                  }
                </article>
              `;
            })
            .join("")}
        </div>
        ${
          selected
            ? `<p class="route-selection-reason">${escapeHTML(decision.selection_reason || "Highest feasible policy score.")}</p>`
            : ""
        }
      </section>
    `;
  }

  function renderInspector() {
    const node = findNode(state.selectedNodeId);
    if (!node) {
      elements.inspectorTitle.textContent = "Select a node";
      elements.inspectorBody.innerHTML = `
        <div class="inspector-empty">
          <div class="empty-orbit" aria-hidden="true"><span></span><span></span><i></i></div>
          <p>Choose a node to inspect its model, policy, runtime state, and configuration.</p>
        </div>
      `;
      return;
    }

    const rawState = nodeState(node.id);
    const status = nodeVisualStatus(node.id);
    const configEntries = Object.entries(node.config || {}).slice(0, 9);
    const edges = state.snapshot.definition.edges;
    const incoming = edges.filter((edge) => edge.target === node.id).length;
    const outgoing = edges.filter((edge) => edge.source === node.id).length;
    const duration = finiteNumber(
      typeof rawState === "object" ? rawState.duration_ms || rawState.elapsed_ms : 0,
      0,
    );
    const attempt = finiteNumber(typeof rawState === "object" ? rawState.attempt : 0, 0);
    const structuredOutput =
      rawState &&
      typeof rawState === "object" &&
      rawState.output &&
      typeof rawState.output === "object"
        ? rawState.output
        : null;
    const routingDecision =
      structuredOutput && Array.isArray(structuredOutput.candidates) ? structuredOutput : null;
    const output =
      typeof rawState === "object"
        ? rawState.output_summary || rawState.summary || rawState.output || "No runtime output yet."
        : "No runtime output yet.";

    elements.inspectorTitle.textContent = node.label;
    elements.inspectorBody.innerHTML = `
      <div class="inspector-content" data-type="${escapeAttribute(node.type)}">
        <section class="inspector-node-summary">
          <div class="inspector-node-meta">
            <span class="type-badge">${escapeHTML(node.type)}</span>
            <span class="state-badge is-${status}">${escapeHTML(nodeRawStatus(node.id))}</span>
          </div>
          <p class="inspector-description">${escapeHTML(node.description)}</p>
        </section>

        <div class="inspector-stat-grid">
          <div class="inspector-stat"><span>Duration</span><strong>${escapeHTML(formatDuration(duration))}</strong></div>
          <div class="inspector-stat"><span>Attempt</span><strong>${attempt || "—"}</strong></div>
          <div class="inspector-stat"><span>Inputs</span><strong>${incoming}</strong></div>
          <div class="inspector-stat"><span>Routes</span><strong>${outgoing}</strong></div>
        </div>

        ${routingDecision ? renderRoutingDecision(routingDecision) : ""}

        <section class="inspector-section">
          <div class="inspector-section-heading">
            <h3>Configuration</h3>
            <span>${configEntries.length} fields</span>
          </div>
          ${
            configEntries.length
              ? `<dl class="config-list">${configEntries
                  .map(
                    ([key, value]) => `
                    <div class="config-row">
                      <dt>${escapeHTML(humanize(key))}</dt>
                      <dd>${escapeHTML(displayValue(value))}</dd>
                    </div>`,
                  )
                  .join("")}</dl>`
              : '<div class="runtime-output">No node configuration supplied.</div>'
          }
        </section>

        <section class="inspector-section">
          <div class="inspector-section-heading">
            <h3>Latest output</h3>
            <span>${escapeHTML(node.id)}</span>
          </div>
          <div class="runtime-output">${escapeHTML(displayValue(output))}</div>
        </section>

        <section class="inspector-section">
          <details class="raw-config">
            <summary>Raw node record</summary>
            <pre>${escapeHTML(JSON.stringify({ ...node, runtime: rawState }, null, 2))}</pre>
          </details>
        </section>
      </div>
    `;
  }

  function renderEvents() {
    const events = state.snapshot.events.slice(-18).reverse();
    elements.eventCount.textContent = `${events.length} signal${events.length === 1 ? "" : "s"}`;
    if (!events.length) {
      elements.eventStream.innerHTML = '<div class="event-empty">No runtime signals yet.</div>';
      return;
    }
    elements.eventStream.innerHTML = events
      .map((event, index) => {
        const kind = String(event?.kind || event?.type || "event").toLowerCase();
        const severity = String(event?.severity || event?.status || "").toLowerCase();
        const className = eventClass(kind, severity);
        const time = formatEventTime(event?.timestamp || event?.ts || event?.created_at);
        const source = String(event?.source || event?.node_id || event?.agent || "runtime");
        const message = String(event?.message || event?.summary || event?.detail || "Runtime event");
        return `
          <article class="event-card ${className}" aria-label="${escapeAttribute(`${kind} event from ${source}`)}">
            <div class="event-meta">
              <span class="event-kind">${escapeHTML(kind)}</span>
              <time>${escapeHTML(time || `#${events.length - index}`)}</time>
            </div>
            <p class="event-message">${escapeHTML(message)}</p>
            <span class="event-source">${escapeHTML(source)}</span>
          </article>
        `;
      })
      .join("");
  }

  function renderCalibration() {
    const benchmark = state.snapshot.benchmark;
    const policies = benchmark.policies || [];
    renderPolicyCards(policies, benchmark.recommendation || {});
    renderRecommendation(benchmark);
    renderConfidence(policies, benchmark);
    renderPareto(policies);
    renderRoutingMatrix(benchmark);
    renderComparisons(benchmark.comparisons || []);
  }

  function renderPolicyCards(policies, recommendation) {
    if (!policies.length) {
      elements.policyScorecards.innerHTML = '<div class="empty-analytics">No policy scorecards in this snapshot.</div>';
      return;
    }
    elements.policyScorecards.innerHTML = policies
      .map((policy, index) => {
        const color = POLICY_COLORS[index % POLICY_COLORS.length];
        const quality = policyQuality(policy);
        const selectedPolicyId = String(recommendation?.selected_policy_id || "");
        const recommended =
          selectedPolicyId
            ? String(policy.id || "") === selectedPolicyId
            : String(policy.status || "").toLowerCase() === "recommended" ||
              Boolean(policy.recommended);
        const scenarios = finiteNumber(policy.scenarios, 0);
        const trials = finiteNumber(policy.trials ?? policy.samples, 0);
        const repetitions = finiteNumber(policy.repetitions_per_scenario, 0);
        const sampleDetail = scenarios
          ? `${compactNumber(trials)} trials across ${compactNumber(scenarios)} scenario clusters${repetitions ? ` with ${compactNumber(repetitions)} repetitions each` : ""}`
          : `${compactNumber(trials)} trials`;
        return `
          <article class="policy-card${recommended ? " is-recommended" : ""}" style="--policy-color:${color}">
            <div class="policy-card-heading">
              <h3>${escapeHTML(String(policy.name || policy.id || `Policy ${index + 1}`))}</h3>
              ${recommended ? '<span class="recommendation-chip">Recommended</span>' : ""}
            </div>
            <p class="policy-description">${escapeHTML(String(policy.description || "Routing policy evaluation profile."))}</p>
            <div class="policy-primary-score">
              <strong>${formatPercent(quality)}</strong>
              <span>mean quality</span>
            </div>
            <div class="policy-metrics">
              <div title="${escapeAttribute(sampleDetail)}"><span>${scenarios ? "Scenarios" : "Trials"}</span><strong>${compactNumber(scenarios || trials)}</strong></div>
              <div><span>Mean cost</span><strong>${formatMoney(policyCost(policy))}</strong></div>
              <div><span>ECE</span><strong>${formatScore(numberValue(policy, ["calibration_error", "ece"], 0))}</strong></div>
            </div>
          </article>
        `;
      })
      .join("");
  }

  function renderRecommendation(benchmark) {
    const recommendation = benchmark.recommendation || {};
    const selectedId = String(recommendation.selected_policy_id || "");
    if (!selectedId) {
      elements.recommendationSummary.hidden = true;
      elements.recommendationSummary.innerHTML = "";
      return;
    }
    const policy = (benchmark.policies || []).find(
      (candidate) => String(candidate.id || "") === selectedId,
    );
    const eligibility = recommendation.eligibility || {};
    const eligible = Array.isArray(recommendation.eligible_policy_ids)
      ? recommendation.eligible_policy_ids
      : [];
    const rankLabels = {
      success_rate_desc: "held-out success",
      mean_score_desc: "mean quality",
      policy_id_asc: "policy ID",
    };
    const ranking = Array.isArray(recommendation.rank_by)
      ? recommendation.rank_by
          .map((item) => rankLabels[item] || humanize(item))
          .join(" → ")
      : "declared policy ranking";
    const costLimit = finiteNumber(
      eligibility.mean_cost_usd_lte,
      Number.NaN,
    );
    const latencyLimit = finiteNumber(
      eligibility.p95_latency_ms_lte,
      Number.NaN,
    );
    const constraints = [
      Number.isFinite(costLimit)
        ? `mean cost ≤ ${formatMoney(costLimit)}`
        : "",
      Number.isFinite(latencyLimit)
        ? `p95 ≤ ${formatDuration(latencyLimit)}`
        : "",
    ]
      .filter(Boolean)
      .join(" · ");
    elements.recommendationSummary.hidden = false;
    elements.recommendationSummary.innerHTML = `
      <span>Operating envelope</span>
      <strong>${escapeHTML(policy?.name || humanize(selectedId))} selected from ${eligible.length || "declared"} eligible polic${eligible.length === 1 ? "y" : "ies"}</strong>
      <p>${escapeHTML(
        [constraints, `ranked by ${ranking}`].filter(Boolean).join(" · "),
      )}</p>
    `;
  }

  function renderConfidence(policies, benchmark) {
    if (!policies.length) {
      elements.confidenceChart.innerHTML = '<div class="empty-analytics">No interval estimates available.</div>';
      return;
    }
    const provenance = benchmark.provenance || {};
    const representative =
      policies.find((policy) => policy.interval_method) || policies[0] || {};
    const confidenceLevel = finiteNumber(
      representative.confidence_level ?? provenance.confidence_level,
      0.95,
    );
    const confidencePercent = Math.round(confidenceLevel * 100);
    const scenarios = finiteNumber(
      representative.scenarios ?? provenance.evaluation_scenario_count,
      0,
    );
    const repetitions = finiteNumber(
      representative.repetitions_per_scenario ??
        provenance.repetitions_per_scenario,
      0,
    );
    const intervalMethod = String(
      representative.interval_method || "interval estimate",
    );
    const intervalStatus = String(representative.interval_status || "");
    elements.confidenceUnit.textContent = `${confidencePercent}% CI`;
    const intervals = policies.map((policy) => {
      const mean = normalize01(finiteNumber(policy.pass_rate ?? policy.success_rate, 0));
      const raw = Array.isArray(policy.success_ci)
        ? policy.success_ci
        : Array.isArray(policy.quality_ci)
          ? policy.quality_ci
        : Array.isArray(policy.ci)
          ? policy.ci
          : [mean, mean];
      return {
        name: String(policy.name || policy.id || "Policy"),
        mean,
        low: normalize01(finiteNumber(raw[0], mean)),
        high: normalize01(finiteNumber(raw[1], mean)),
      };
    });
    const domainLow = Math.max(0, Math.floor((Math.min(...intervals.map((item) => item.low)) - 0.03) * 20) / 20);
    const domainHigh = Math.min(1, Math.ceil((Math.max(...intervals.map((item) => item.high)) + 0.03) * 20) / 20);
    const span = Math.max(0.05, domainHigh - domainLow);

    elements.confidenceChart.innerHTML = `
      ${intervals
        .map((item, index) => {
          const low = ((item.low - domainLow) / span) * 100;
          const high = ((item.high - domainLow) / span) * 100;
          const mean = ((item.mean - domainLow) / span) * 100;
          return `
            <div class="confidence-row" style="--policy-color:${POLICY_COLORS[index % POLICY_COLORS.length]}">
              <span class="confidence-name" title="${escapeAttribute(item.name)}">${escapeHTML(item.name)}</span>
              <div class="confidence-track" aria-label="${escapeAttribute(`${item.name}: ${formatPercent(item.mean)}, ${confidencePercent} percent confidence interval ${formatPercent(item.low)} to ${formatPercent(item.high)}`)}">
                <span class="confidence-interval" style="left:${clamp(low, 0, 100)}%;width:${clamp(high - low, 1, 100)}%"></span>
                <i class="confidence-mean" style="left:${clamp(mean, 0, 100)}%"></i>
              </div>
              <span class="confidence-value">${formatPercent(item.mean)}</span>
            </div>
          `;
        })
        .join("")}
      <div class="confidence-axis">
        <span>${formatPercent(domainLow)}</span>
        <span>${formatPercent(domainLow + span / 2)}</span>
        <span>${formatPercent(domainHigh)}</span>
      </div>
      <p class="confidence-method">${escapeHTML(
        [
          scenarios ? `${compactNumber(scenarios)} scenario clusters` : "",
          repetitions
            ? `${compactNumber(repetitions)} repetitions per scenario`
            : "",
          humanize(intervalMethod),
          intervalStatus ? humanize(intervalStatus) : "",
        ]
          .filter(Boolean)
          .join(" · "),
      )}</p>
    `;
  }

  function renderPareto(policies) {
    if (!policies.length) {
      elements.paretoChart.innerHTML = '<div class="empty-analytics">No policy points available.</div>';
      return;
    }
    const points = policies.map((policy, index) => ({
      name: String(policy.name || policy.id || `Policy ${index + 1}`),
      cost: policyCost(policy),
      quality: policyQuality(policy),
      latency: numberValue(policy, ["latency_p50_ms", "latency_ms", "latency"], 0),
      pareto: Boolean(policy.pareto),
      color: POLICY_COLORS[index % POLICY_COLORS.length],
    }));
    const width = 420;
    const height = 205;
    const margin = { top: 18, right: 28, bottom: 33, left: 42 };
    const xMin = Math.min(...points.map((point) => point.cost), 0);
    const xMax = Math.max(...points.map((point) => point.cost), 0.01) * 1.13;
    const qMin = Math.max(0, Math.min(...points.map((point) => point.quality)) - 0.05);
    const qMax = Math.min(1, Math.max(...points.map((point) => point.quality)) + 0.035);
    const x = (value) =>
      margin.left + ((value - xMin) / Math.max(0.001, xMax - xMin)) * (width - margin.left - margin.right);
    const y = (value) =>
      height -
      margin.bottom -
      ((value - qMin) / Math.max(0.001, qMax - qMin)) * (height - margin.top - margin.bottom);
    const latencyMax = Math.max(...points.map((point) => point.latency), 1);
    const frontier = points
      .filter((point) => point.pareto)
      .sort((a, b) => a.cost - b.cost)
      .map((point) => `${x(point.cost)},${y(point.quality)}`)
      .join(" ");

    const grid = [0, 0.25, 0.5, 0.75, 1]
      .map((ratio) => {
        const gx = margin.left + ratio * (width - margin.left - margin.right);
        const gy = margin.top + ratio * (height - margin.top - margin.bottom);
        return `
          <line class="chart-grid-line" x1="${gx}" y1="${margin.top}" x2="${gx}" y2="${height - margin.bottom}" />
          <line class="chart-grid-line" x1="${margin.left}" y1="${gy}" x2="${width - margin.right}" y2="${gy}" />
        `;
      })
      .join("");

    elements.paretoChart.innerHTML = `
      <svg class="pareto-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Scatter plot comparing policy quality and request cost">
        ${grid}
        ${frontier ? `<polyline class="pareto-frontier" points="${frontier}" />` : ""}
        ${points
          .map((point) => {
            const radius = 7 + (point.latency / latencyMax) * 7;
            const px = x(point.cost);
            const py = y(point.quality);
            return `
              <g style="--point-color:${point.color}" aria-label="${escapeAttribute(`${point.name}: ${formatPercent(point.quality)} quality at ${formatMoney(point.cost)}`)}">
                <circle class="pareto-point" cx="${px}" cy="${py}" r="${radius}" />
                <circle class="pareto-point-core" cx="${px}" cy="${py}" r="2.2" />
                <text class="pareto-label" x="${px + radius + 4}" y="${py + 2}">${escapeHTML(truncate(point.name, 18))}</text>
              </g>
            `;
          })
          .join("")}
        <text class="chart-axis-label" x="${width / 2}" y="${height - 5}" text-anchor="middle">mean request cost (USD)</text>
        <text class="chart-axis-label" transform="translate(10 ${height / 2}) rotate(-90)" text-anchor="middle">quality</text>
        <text class="chart-tick" x="${margin.left}" y="${height - 19}">${formatMoney(xMin)}</text>
        <text class="chart-tick" x="${width - margin.right}" y="${height - 19}" text-anchor="end">${formatMoney(xMax)}</text>
        <text class="chart-tick" x="${margin.left - 6}" y="${height - margin.bottom}" text-anchor="end">${formatPercent(qMin)}</text>
        <text class="chart-tick" x="${margin.left - 6}" y="${margin.top + 2}" text-anchor="end">${formatPercent(qMax)}</text>
      </svg>
    `;
  }

  function renderRoutingMatrix(benchmark) {
    const rows = benchmark.routing_matrix || [];
    let models = (benchmark.models || []).map((model, index) => ({
      id: String(typeof model === "string" ? model : model.id || model.name || `model-${index + 1}`),
      name: String(typeof model === "string" ? model : model.name || model.label || model.id || `Model ${index + 1}`),
    }));
    if (!models.length && rows.length) {
      const sample = rows[0]?.weights || rows[0]?.routes || {};
      models = Object.keys(sample).map((key) => ({ id: key, name: humanize(key) }));
    }
    if (!rows.length || !models.length) {
      elements.routingMatrix.innerHTML = '<div class="empty-analytics">No routing matrix supplied.</div>';
      return;
    }

    elements.routingMatrix.innerHTML = `
      <table class="routing-table">
        <thead>
          <tr>
            <th scope="col">Task class</th>
            ${models.map((model) => `<th scope="col">${escapeHTML(model.name)}</th>`).join("")}
          </tr>
        </thead>
        <tbody>
          ${rows
            .map(
              (row) => `
                <tr>
                  <td>${escapeHTML(String(row.task_class || row.class || row.name || "Task"))}</td>
                  ${models
                    .map((model, index) => {
                      const value = routingWeight(row, model, index);
                      const color = POLICY_COLORS[index % POLICY_COLORS.length];
                      const alpha = Math.round(6 + value * 42);
                      return `<td class="matrix-cell" style="--cell-color:${color};--cell-alpha:${alpha}" aria-label="${escapeAttribute(`${model.name}: ${formatPercent(value)}`)}">${formatPercent(value)}</td>`;
                    })
                    .join("")}
                </tr>
              `,
            )
            .join("")}
        </tbody>
      </table>
    `;
  }

  function renderComparisons(comparisons) {
    if (!comparisons.length) {
      elements.comparisonList.innerHTML = '<div class="empty-analytics">No paired comparisons supplied.</div>';
      return;
    }
    elements.comparisonList.innerHTML = comparisons
      .map((comparison) => {
        const baseline = String(comparison.baseline || comparison.control || "Baseline");
        const challenger = String(comparison.challenger || comparison.policy || "Challenger");
        const delta = finiteNumber(comparison.delta, 0);
        const ci = Array.isArray(comparison.ci) ? comparison.ci : comparison.confidence_interval;
        const ciText =
          Array.isArray(ci) && ci.length >= 2
            ? `${formatSigned(ci[0])} to ${formatSigned(ci[1])}`
            : "interval unavailable";
        const conclusion = humanize(String(comparison.conclusion || "inconclusive"));
        return `
          <div class="comparison-item">
            <div>
              <div class="comparison-pair">${escapeHTML(challenger)} <span>vs.</span> ${escapeHTML(baseline)}</div>
              <div class="comparison-metric">${escapeHTML(String(comparison.metric || "metric delta"))} · CI ${escapeHTML(ciText)}</div>
            </div>
            <div class="comparison-result">
              <strong class="${delta < 0 ? "is-negative" : ""}">${escapeHTML(formatSigned(delta))}</strong>
              <span>${escapeHTML(conclusion)}</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function setView(view) {
    if (!["graph", "calibration"].includes(view) || state.activeView === view) return;
    state.activeView = view;
    const graphActive = view === "graph";
    elements.graphTab.classList.toggle("is-selected", graphActive);
    elements.graphTab.setAttribute("aria-selected", String(graphActive));
    elements.calibrationTab.classList.toggle("is-selected", !graphActive);
    elements.calibrationTab.setAttribute("aria-selected", String(!graphActive));
    elements.calibrationPanel.hidden = graphActive;
    elements.studio.classList.toggle("is-calibration", !graphActive);
    if (graphActive) requestAnimationFrame(applyViewTransform);
    announce(`${graphActive ? "Graph" : "Calibration"} view selected.`);
  }

  function handleAction(action) {
    const actions = {
      "toggle-rail": toggleRail,
      "close-inspector": closeInspector,
      reset: () => invokeDemoAction("reset"),
      tick: () => invokeDemoAction("tick"),
      run: handleRunAction,
      "zoom-in": () => zoomBy(1.16),
      "zoom-out": () => zoomBy(1 / 1.16),
      fit: fitGraph,
      "focus-active": focusActiveNode,
    };
    actions[action]?.();
  }

  async function invokeDemoAction(action) {
    if (state.busy) return;
    if (!state.apiAvailable || state.usingFallback || window.location.protocol === "file:") {
      applyLocalAction(action);
      return;
    }

    setBusy(true);
    try {
      const response = await fetch(`/api/demo/${action}`, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Charon-Control-Token": state.snapshot.control_token,
        },
        body: JSON.stringify({ graph_id: state.snapshot.definition.graph_id, run_id: state.snapshot.run.run_id }),
      });
      if (!response.ok) throw new Error(`Demo action returned ${response.status}`);
      const payload = await response.json().catch(() => null);
      const snapshot = extractSnapshot(payload);
      if (snapshot?.definition || snapshot?.run) {
        applySnapshot(snapshot, { source: "api", fit: false });
      } else {
        await loadSnapshot({ silent: true });
      }
      showToast(`${capitalize(action)} accepted by the demo runtime.`);
      syncPolling();
    } catch (error) {
      showToast(`The ${action} endpoint was unavailable. Continuing with the local demonstration.`, true);
      state.apiAvailable = false;
      state.usingFallback = true;
      setDataSource(false, "Embedded fixture · API action failed");
      applyLocalAction(action);
      console.info("[Graph Studio] Demo action fallback:", error.message);
    } finally {
      setBusy(false);
    }
  }

  function handleRunAction() {
    if (state.busy) return;
    const isLocalRunning = Boolean(state.localTimer);
    if (state.usingFallback && isLocalRunning) {
      stopLocalTimer();
      state.snapshot.run.status = "paused";
      renderChrome();
      showToast("Local demonstration paused.");
      return;
    }
    if (state.apiAvailable && normalizeStatus(state.snapshot.run.status) === "active") {
      loadSnapshot({ silent: true });
      showToast("Live run refreshed.");
      return;
    }
    invokeDemoAction("run");
  }

  function applyLocalAction(action) {
    if (action === "reset") {
      stopLocalTimer();
      const benchmark = state.snapshot.benchmark;
      state.snapshot = normalizeSnapshot(deepClone(FALLBACK_SNAPSHOT));
      if (benchmark?.policies?.length) state.snapshot.benchmark = benchmark;
      state.localStage = -1;
      state.selectedNodeId = null;
      renderAll({ fit: true });
      showToast("Demonstration reset to a clean graph.");
      announce("Demo reset.");
      return;
    }
    if (action === "tick") {
      stopLocalTimer();
      simulateTick();
      return;
    }
    if (action === "run") {
      if (TERMINAL_STATUSES.has(String(state.snapshot.run.status).toLowerCase())) {
        const benchmark = state.snapshot.benchmark;
        state.snapshot = normalizeSnapshot(deepClone(FALLBACK_SNAPSHOT));
        state.snapshot.benchmark = benchmark;
        state.localStage = -1;
      }
      simulateTick();
      startLocalTimer();
      showToast("Local graph simulation started.");
    }
  }

  function simulateTick() {
    state.localStage = Math.min(state.localStage + 1, LOCAL_STAGES.length - 1);
    const stage = LOCAL_STAGES[state.localStage];
    const run = state.snapshot.run;
    if (!run.started_at) run.started_at = new Date().toISOString();
    const completedSet = new Set(stage.complete);
    const activeSet = new Set(stage.active);
    run.active_nodes = [...stage.active];
    run.status = state.localStage === LOCAL_STAGES.length - 1 ? "completed" : "running";

    state.snapshot.definition.nodes.forEach((node, index) => {
      const existing = typeof run.node_states[node.id] === "object" ? run.node_states[node.id] : {};
      if (completedSet.has(node.id)) {
        run.node_states[node.id] = {
          ...existing,
          status: "completed",
          attempt: Math.max(1, existing.attempt || 0),
          duration_ms: existing.duration_ms || 420 + index * 137,
          output_summary:
            existing.output_summary ||
            `${node.label} completed and attached structured evidence to the run.`,
        };
      } else if (activeSet.has(node.id)) {
        run.node_states[node.id] = {
          ...existing,
          status: "running",
          attempt: Math.max(1, existing.attempt || 0),
          duration_ms: existing.duration_ms || 0,
          output_summary: `${node.label} is processing the current execution frame.`,
        };
      } else {
        run.node_states[node.id] = { ...existing, status: "queued" };
      }
    });

    const progress = (state.localStage + 1) / LOCAL_STAGES.length;
    run.metrics = {
      ...run.metrics,
      quality_score: state.localStage >= 5 ? 0.87 : Math.max(0, 0.64 + state.localStage * 0.035),
      success_rate: Math.min(1, completedSet.size / Math.max(1, state.snapshot.definition.nodes.length)),
      elapsed_ms: 1800 + state.localStage * 2680,
      run_steps: state.localStage + 1,
      cost_usd: 0.012 + state.localStage * 0.017,
      retries: 0,
      latency_p95_ms: 5400 + state.localStage * 720,
    };
    state.snapshot.events.push({
      id: `evt-local-${Date.now()}-${state.localStage}`,
      timestamp: new Date().toISOString(),
      kind: stage.kind,
      source: stage.source,
      message: stage.message,
      severity: stage.kind === "success" ? "success" : "info",
    });
    renderAll({ fit: false });
    if (stage.active[0]) state.selectedNodeId = state.selectedNodeId || stage.active[0];
    renderRail();
    renderGraph();
    renderInspector();
    if (state.localStage === LOCAL_STAGES.length - 1) {
      stopLocalTimer();
      showToast("Graph run completed with a verified output bundle.");
      announce("Local graph run completed.");
    } else {
      announce(stage.message);
    }
  }

  function startLocalTimer() {
    stopLocalTimer();
    state.localTimer = window.setInterval(simulateTick, 1900);
    renderChrome();
  }

  function stopLocalTimer() {
    if (state.localTimer) window.clearInterval(state.localTimer);
    state.localTimer = null;
  }

  function syncPolling() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    state.pollTimer = null;
    if (!state.apiAvailable || state.pollingPaused || normalizeStatus(state.snapshot.run.status) !== "active") return;
    state.pollTimer = window.setInterval(async () => {
      await loadSnapshot({ silent: true });
      if (TERMINAL_STATUSES.has(String(state.snapshot.run.status).toLowerCase())) {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
      }
    }, 1400);
  }

  function setBusy(busy) {
    state.busy = busy;
    renderChrome();
  }

  function setDataSource(isApi, label) {
    const benchmark = state.snapshot.benchmark || {};
    const isGeneratedFixture = benchmark.data_kind === "generated_fixture";
    const seedLabel =
      benchmark.seed === null || benchmark.seed === undefined || benchmark.seed === ""
        ? ""
        : ` · seed ${benchmark.seed}`;
    const sourceLabel = isApi && isGeneratedFixture ? `Live API · generated fixture${seedLabel}` : label;

    state.apiAvailable = isApi;
    state.usingFallback = !isApi;
    elements.sourceChip.classList.toggle("is-fixture", !isApi || isGeneratedFixture);
    elements.sourceChip.querySelector("span:last-child").textContent = sourceLabel;
    elements.connectionState.classList.toggle("is-live", isApi);
    elements.connectionState.classList.toggle("is-offline", !isApi);
    elements.connectionState.lastChild.textContent = isApi ? " Live API" : " Offline-ready";
    elements.provenanceNotice.classList.toggle("is-api", isApi);
    elements.provenanceNotice.querySelector("span").textContent = isGeneratedFixture
      ? `Reproducible generated fixture${seedLabel} — interface demonstration only.`
      : isApi
        ? benchmark.description || "Snapshot-provided benchmark data — verify provenance before publication."
        : "Synthetic fixture data — interface demonstration only.";
  }

  function selectNode(nodeId) {
    if (!findNode(nodeId)) return;
    state.selectedNodeId = nodeId;
    renderRail();
    renderGraph();
    renderInspector();
    if (window.innerWidth <= 860) elements.studio.classList.add("inspector-open");
    announce(`${findNode(nodeId).label} selected.`);
  }

  function closeInspector() {
    elements.studio.classList.remove("inspector-open");
  }

  function toggleRail() {
    elements.studio.classList.toggle("rail-open");
    syncRailToggle();
  }

  function syncRailToggle() {
    const open = elements.studio.classList.contains("rail-open");
    document.querySelectorAll('[data-action="toggle-rail"][aria-expanded]').forEach((button) => {
      button.setAttribute("aria-expanded", String(open));
    });
  }

  function handleWheel(event) {
    if (state.activeView !== "graph") return;
    event.preventDefault();
    const point = svgPoint(event.clientX, event.clientY);
    const oldScale = state.view.scale;
    const factor = event.deltaY > 0 ? 0.9 : 1.1;
    const newScale = clamp(oldScale * factor, 0.42, 2.25);
    const ratio = newScale / oldScale;
    state.view.x = point.x - (point.x - state.view.x) * ratio;
    state.view.y = point.y - (point.y - state.view.y) * ratio;
    state.view.scale = newScale;
    applyViewTransform();
  }

  function handlePointerDown(event) {
    if (event.button !== 0 || event.target.closest(".graph-node")) return;
    const point = svgPoint(event.clientX, event.clientY);
    state.drag = { pointerId: event.pointerId, point, x: state.view.x, y: state.view.y };
    elements.graphSvg.setPointerCapture(event.pointerId);
    elements.canvasStage.classList.add("is-panning");
  }

  function handlePointerMove(event) {
    if (!state.drag || state.drag.pointerId !== event.pointerId) return;
    const point = svgPoint(event.clientX, event.clientY);
    state.view.x = state.drag.x + (point.x - state.drag.point.x);
    state.view.y = state.drag.y + (point.y - state.drag.point.y);
    applyViewTransform();
  }

  function handlePointerUp(event) {
    if (!state.drag || state.drag.pointerId !== event.pointerId) return;
    state.drag = null;
    elements.canvasStage.classList.remove("is-panning");
    if (elements.graphSvg.hasPointerCapture(event.pointerId)) {
      elements.graphSvg.releasePointerCapture(event.pointerId);
    }
  }

  function zoomBy(factor) {
    const oldScale = state.view.scale;
    const newScale = clamp(oldScale * factor, 0.42, 2.25);
    const center = { x: VIEWBOX_WIDTH / 2, y: VIEWBOX_HEIGHT / 2 };
    const ratio = newScale / oldScale;
    state.view.x = center.x - (center.x - state.view.x) * ratio;
    state.view.y = center.y - (center.y - state.view.y) * ratio;
    state.view.scale = newScale;
    applyViewTransform();
  }

  function fitGraph() {
    const nodes = state.snapshot.definition.nodes;
    if (!nodes.length) return;
    const minX = Math.min(...nodes.map((node) => node.position.x));
    const minY = Math.min(...nodes.map((node) => node.position.y));
    const maxX = Math.max(...nodes.map((node) => node.position.x + NODE_WIDTH));
    const maxY = Math.max(...nodes.map((node) => node.position.y + NODE_HEIGHT));
    const graphWidth = Math.max(1, maxX - minX);
    const graphHeight = Math.max(1, maxY - minY);
    const paddingX = 74;
    const paddingY = 62;
    const scale = clamp(
      Math.min((VIEWBOX_WIDTH - paddingX * 2) / graphWidth, (VIEWBOX_HEIGHT - paddingY * 2) / graphHeight),
      0.42,
      1.24,
    );
    state.view.scale = scale;
    state.view.x = (VIEWBOX_WIDTH - graphWidth * scale) / 2 - minX * scale;
    state.view.y = (VIEWBOX_HEIGHT - graphHeight * scale) / 2 - minY * scale;
    applyViewTransform();
  }

  function focusActiveNode() {
    const activeId = state.snapshot.run.active_nodes[0] || state.selectedNodeId;
    const node = findNode(activeId);
    if (!node) {
      showToast("There is no active node to focus.");
      return;
    }
    state.selectedNodeId = node.id;
    const scale = clamp(Math.max(state.view.scale, 0.92), 0.42, 1.25);
    state.view.scale = scale;
    state.view.x = VIEWBOX_WIDTH / 2 - (node.position.x + NODE_WIDTH / 2) * scale;
    state.view.y = VIEWBOX_HEIGHT / 2 - (node.position.y + NODE_HEIGHT / 2) * scale;
    renderRail();
    renderGraph();
    renderInspector();
    applyViewTransform();
  }

  function applyViewTransform() {
    elements.viewport.setAttribute(
      "transform",
      `translate(${state.view.x.toFixed(2)} ${state.view.y.toFixed(2)}) scale(${state.view.scale.toFixed(3)})`,
    );
    elements.zoomReadout.textContent = `${Math.round(state.view.scale * 100)}%`;
  }

  function svgPoint(clientX, clientY) {
    const rect = elements.graphSvg.getBoundingClientRect();
    return {
      x: ((clientX - rect.left) / Math.max(1, rect.width)) * VIEWBOX_WIDTH,
      y: ((clientY - rect.top) / Math.max(1, rect.height)) * VIEWBOX_HEIGHT,
    };
  }

  function findNode(nodeId) {
    if (!nodeId) return null;
    return state.snapshot.definition.nodes.find((node) => node.id === nodeId) || null;
  }

  function nodeState(nodeId) {
    return state.snapshot.run.node_states?.[nodeId] ?? "queued";
  }

  function nodeRawStatus(nodeId) {
    if (state.snapshot.run.active_nodes.includes(nodeId)) return "running";
    const raw = nodeState(nodeId);
    return String(typeof raw === "object" ? raw.status || raw.state || "queued" : raw || "queued").toLowerCase();
  }

  function nodeVisualStatus(nodeId) {
    if (state.snapshot.run.active_nodes.includes(nodeId)) return "active";
    return normalizeStatus(nodeRawStatus(nodeId));
  }

  function normalizeStatus(status) {
    const value = String(status || "").toLowerCase().replaceAll("_", "-");
    if (["running", "active", "in-progress", "processing", "started"].includes(value)) return "active";
    if (["complete", "completed", "success", "succeeded", "passed", "done"].includes(value)) return "complete";
    if (["failed", "failure", "error", "blocked", "cancelled"].includes(value)) return "error";
    return "waiting";
  }

  function eventClass(kind, severity) {
    if (["error", "failed", "critical"].includes(severity) || ["error", "failure"].includes(kind)) return "is-error";
    if (["success", "complete", "completed"].includes(severity) || ["success", "complete"].includes(kind)) {
      return "is-success";
    }
    if (kind.includes("route") || kind.includes("dispatch")) return "is-route";
    if (kind.includes("eval") || kind.includes("score") || kind.includes("gate")) return "is-evaluation";
    return "";
  }

  function routingWeight(row, model, index) {
    const sources = [row.weights, row.routes, row.probabilities, row.traffic];
    for (const source of sources) {
      if (Array.isArray(source)) return normalize01(finiteNumber(source[index], 0));
      if (source && typeof source === "object") {
        if (source[model.id] != null) return normalize01(finiteNumber(source[model.id], 0));
        if (source[model.name] != null) return normalize01(finiteNumber(source[model.name], 0));
      }
    }
    if (row[model.id] != null) return normalize01(finiteNumber(row[model.id], 0));
    if (row[model.name] != null) return normalize01(finiteNumber(row[model.name], 0));
    return 0;
  }

  function policyQuality(policy) {
    return normalize01(numberValue(policy, ["quality_mean", "quality", "pass_rate", "score"], 0));
  }

  function policyCost(policy) {
    return Math.max(0, numberValue(policy, ["cost_mean", "mean_cost", "cost_usd", "cost"], 0));
  }

  function metricValue(metrics, keys, fallback = 0) {
    return numberValue(metrics || {}, keys, fallback);
  }

  function numberValue(object, keys, fallback = 0) {
    for (const key of keys) {
      if (object?.[key] != null && Number.isFinite(Number(object[key]))) return Number(object[key]);
    }
    return fallback;
  }

  function configValue(config, keys, fallback) {
    for (const key of keys) {
      if (config?.[key] != null) return config[key];
    }
    return fallback;
  }

  function extractSnapshot(payload) {
    if (!payload || typeof payload !== "object") return payload;
    return payload.snapshot && typeof payload.snapshot === "object" ? payload.snapshot : payload;
  }

  function setBusyButtonsDisabled(disabled) {
    document.querySelectorAll("[data-action='run'],[data-action='tick'],[data-action='reset']").forEach((button) => {
      button.disabled = disabled;
    });
  }

  function showToast(message, isError = false) {
    window.clearTimeout(state.toastTimer);
    elements.toast.textContent = message;
    elements.toast.classList.toggle("is-error", isError);
    elements.toast.classList.add("is-visible");
    state.toastTimer = window.setTimeout(() => elements.toast.classList.remove("is-visible"), 3400);
  }

  function announce(message) {
    elements.announcer.textContent = "";
    window.setTimeout(() => {
      elements.announcer.textContent = message;
    }, 20);
  }

  function formatEventTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function formatDuration(ms) {
    const value = finiteNumber(ms, 0);
    if (!value) return "—";
    if (value < 1000) return `${Math.round(value)}ms`;
    if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)}s`;
    return `${Math.floor(value / 60000)}m ${Math.round((value % 60000) / 1000)}s`;
  }

  function formatMoney(value) {
    const amount = finiteNumber(value, 0);
    if (amount >= 10) return `$${amount.toFixed(0)}`;
    if (amount >= 1) return `$${amount.toFixed(2)}`;
    return `$${amount.toFixed(3)}`;
  }

  function formatScore(value) {
    const score = finiteNumber(value, 0);
    return score > 1 ? score.toFixed(2) : score.toFixed(2);
  }

  function formatPercent(value) {
    return `${Math.round(normalize01(finiteNumber(value, 0)) * 100)}%`;
  }

  function formatSigned(value) {
    const number = finiteNumber(value, 0);
    const formatted = Math.abs(number) < 1 ? `${(number * 100).toFixed(1)}pp` : number.toFixed(2);
    return `${number > 0 ? "+" : ""}${formatted}`;
  }

  function compactNumber(value) {
    const number = finiteNumber(value, 0);
    if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}m`;
    if (number >= 1_000) return `${(number / 1_000).toFixed(number < 10_000 ? 1 : 0)}k`;
    return String(Math.round(number));
  }

  function displayValue(value) {
    if (value == null || value === "") return "—";
    if (Array.isArray(value)) return value.map((item) => displayValue(item)).join(", ");
    if (typeof value === "object") return JSON.stringify(value);
    if (typeof value === "boolean") return value ? "yes" : "no";
    return String(value);
  }

  function humanStatus(value) {
    const text = humanize(String(value || "ready"));
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function humanize(value) {
    return String(value).replaceAll(/[_-]+/g, " ").replaceAll(/\b\w/g, (char) => char.toUpperCase());
  }

  function capitalize(value) {
    const text = String(value || "");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function typeInitial(type) {
    const initials = { router: "R", agent: "A", tool: "T", evaluator: "E", gate: "G", input: "I", output: "O" };
    return initials[type] || "N";
  }

  function wrapText(value, maxLength, maxLines) {
    const words = String(value || "").split(/\s+/).filter(Boolean);
    const lines = [];
    let line = "";
    for (const word of words) {
      const candidate = line ? `${line} ${word}` : word;
      if (candidate.length > maxLength && line) {
        lines.push(line);
        line = word;
        if (lines.length === maxLines - 1) break;
      } else {
        line = candidate;
      }
    }
    if (line && lines.length < maxLines) lines.push(line);
    if (!lines.length) lines.push("");
    if (words.join(" ").length > lines.join(" ").length) {
      lines[lines.length - 1] = `${truncate(lines[lines.length - 1], maxLength - 1)}…`;
    }
    return lines;
  }

  function truncate(value, maxLength) {
    const text = String(value || "");
    return text.length > maxLength ? `${text.slice(0, Math.max(0, maxLength - 1)).trimEnd()}…` : text;
  }

  function normalize01(value) {
    const number = finiteNumber(value, 0);
    return clamp(number > 1 && number <= 100 ? number / 100 : number, 0, 1);
  }

  function finiteNumber(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function deepClone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function escapeHTML(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function escapeAttribute(value) {
    return escapeHTML(value).replaceAll("`", "&#096;");
  }

  function debounce(fn, wait) {
    let timeout;
    return (...args) => {
      window.clearTimeout(timeout);
      timeout = window.setTimeout(() => fn(...args), wait);
    };
  }
})();
