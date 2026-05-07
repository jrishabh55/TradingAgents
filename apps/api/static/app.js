// TradingAgents webapp — single-file vanilla JS, terminal-style layout.
//
// Backend contract:
//   GET  /api/config                       -> populate dropdowns
//   POST /api/runs   {RunRequest}          -> {run_id, ...}
//   GET  /api/runs                         -> [{RunSummary}]
//   GET  /api/runs/{id}                    -> {RunDetail}
//   GET  /api/runs/{id}/events  (SSE)      -> stream of EventEnvelopes
//   POST /api/runs/{id}/cancel
//
// Rendering:
//   - The "Current Report" panel is rendered with marked + DOMPurify, both
//     vendored locally under /static/.
//   - Layout mirrors the Rich CLI: top-left Progress (teams/agents/status),
//     top-right Messages & Tools (time/type/content), bottom Current Report
//     (scrollable), footer stats.

(() => {
  "use strict";

  // ------------------------------------------------------------------
  // Markdown helper — wraps marked + DOMPurify with sane defaults.
  // ------------------------------------------------------------------

  function renderMarkdown(text) {
    if (!text) return "";
    if (typeof marked === "undefined") return _escapeHtml(text);
    const html = marked.parse(String(text), { breaks: true, gfm: true });
    if (typeof DOMPurify === "undefined") return html;
    return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
  }
  function _escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // ------------------------------------------------------------------
  // DOM helpers
  // ------------------------------------------------------------------

  const $  = (sel, ctx=document) => ctx.querySelector(sel);
  const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));
  const el = (tag, attrs={}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) {
      if (c == null || c === false) continue;
      node.append(c.nodeType ? c : document.createTextNode(c));
    }
    return node;
  };

  let _previousView = "form";  // remembers where the result view should "back" to
  const setView = (name) => {
    const main = $("main#app");
    const current = main.dataset.view;
    if (current && current !== name && current !== "result") {
      _previousView = current;
    }
    main.dataset.view = name;
    $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === name));
  };

  $$(".tab").forEach(tab => tab.addEventListener("click", () => {
    if (tab.dataset.view === "history") loadHistory();
    setView(tab.dataset.view);
  }));

  // ------------------------------------------------------------------
  // Agent + team taxonomy
  //
  // The CLI groups agents into logical teams in its Progress panel; we
  // mirror that grouping here. Agent keys match what the translator emits.
  // ------------------------------------------------------------------

  const TEAM_GROUPS = [
    { team: "Analyst Team",        agents: [
      { key: "market",       label: "Market Analyst" },
      { key: "social",       label: "Social Analyst" },
      { key: "news",         label: "News Analyst" },
      { key: "fundamentals", label: "Fundamentals Analyst" },
    ]},
    { team: "Research Team",       agents: [
      { key: "bull",            label: "Bull Researcher" },
      { key: "bear",            label: "Bear Researcher" },
      { key: "research_judge",  label: "Research Manager" },
    ]},
    { team: "Trading Team",        agents: [
      { key: "trader", label: "Trader" },
    ]},
    { team: "Risk Management",     agents: [
      { key: "aggressive",   label: "Risky Analyst" },
      { key: "conservative", label: "Safe Analyst" },
      { key: "neutral",      label: "Neutral Analyst" },
    ]},
    { team: "Portfolio Management", agents: [
      { key: "portfolio_manager", label: "Portfolio Manager" },
    ]},
  ];

  // Map analyst key → which team divider to draw. Keep flat lookup too.
  const AGENT_LABEL = {};
  TEAM_GROUPS.forEach(g => g.agents.forEach(a => { AGENT_LABEL[a.key] = a.label; }));

  // Order of report sections to render in the report panel as they arrive.
  // The "winning" section (most recent) becomes the visible report.
  const SECTION_ORDER = [
    { key: "market_report",         label: "Market Analyst"        },
    { key: "sentiment_report",      label: "Social Analyst"        },
    { key: "news_report",           label: "News Analyst"          },
    { key: "fundamentals_report",   label: "Fundamentals Analyst"  },
    { key: "investment_plan",       label: "Research Team"         },
    { key: "trader_investment_plan",label: "Trader Plan"           },
    { key: "final_trade_decision",  label: "Portfolio Decision"    },
  ];
  const SECTION_LABEL = Object.fromEntries(SECTION_ORDER.map(s => [s.key, s.label]));

  // ------------------------------------------------------------------
  // /api/config bootstrap
  // ------------------------------------------------------------------

  let CONFIG = null;
  async function loadConfig() {
    const res = await fetch("/api/config");
    if (!res.ok) throw new Error(`/api/config failed: ${res.status}`);
    CONFIG = await res.json();

    const ag = $("#analysts-group");
    ag.innerHTML = "";
    CONFIG.analysts.forEach(a => {
      const cb = el("label", {},
        el("input", { type: "checkbox", name: "analysts", value: a.value, checked: true }),
        " " + a.label,
      );
      ag.append(cb);
    });

    const rd = $("#research_depth");
    rd.innerHTML = "";
    CONFIG.research_depths.forEach(d => rd.append(el("option", { value: d.value }, d.label)));
    rd.value = "1";

    const ol = $("#output_language");
    ol.innerHTML = "";
    CONFIG.output_languages.forEach(l => ol.append(el("option", { value: l.value }, l.label)));
    ol.value = "English";

    const lp = $("#llm_provider");
    lp.innerHTML = "";
    CONFIG.providers.forEach(p => lp.append(el("option", { value: p.key }, p.label)));
    lp.addEventListener("change", () => populateProvider(lp.value));
    populateProvider(lp.value);

    $("#analysis_date").value = new Date().toISOString().slice(0, 10);
  }

  function populateProvider(providerKey) {
    const provider = CONFIG.providers.find(p => p.key === providerKey);
    const models = CONFIG.models_by_provider[providerKey] || [];

    const fillSelect = (id) => {
      const sel = $("#" + id);
      sel.innerHTML = "";
      if (models.length === 0) {
        sel.append(el("option", { value: "" }, "(no preset; type a custom model id below)"));
      } else {
        models.forEach(m => sel.append(el("option", { value: m.id }, m.label)));
      }
    };
    fillSelect("shallow_thinker");
    fillSelect("deep_thinker");

    $("#backend_url").placeholder = provider.backend_url || "provider default";
    $("#backend_url").value = "";

    $("#field-openai-effort").hidden    = !provider.supports_reasoning_effort;
    $("#field-google-thinking").hidden  = !provider.supports_google_thinking;
    $("#field-anthropic-effort").hidden = !provider.supports_anthropic_effort;
  }

  // ------------------------------------------------------------------
  // Form submit
  // ------------------------------------------------------------------

  $("#run-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const analysts = fd.getAll("analysts");
    if (analysts.length === 0) {
      $("#form-error").textContent = "Pick at least one analyst.";
      return;
    }

    const provider = CONFIG.providers.find(p => p.key === fd.get("llm_provider"));
    const body = {
      ticker: fd.get("ticker").trim(),
      analysis_date: fd.get("analysis_date"),
      analysts,
      research_depth: parseInt(fd.get("research_depth"), 10),
      llm_provider: fd.get("llm_provider"),
      backend_url: fd.get("backend_url") || provider.backend_url || null,
      shallow_thinker: fd.get("shallow_thinker"),
      deep_thinker: fd.get("deep_thinker"),
      openai_reasoning_effort: fd.get("openai_reasoning_effort") || null,
      google_thinking_level: fd.get("google_thinking_level") || null,
      anthropic_effort: fd.get("anthropic_effort") || null,
      output_language: fd.get("output_language"),
      checkpoint_enabled: fd.get("checkpoint_enabled") === "on",
    };

    $("#form-error").textContent = "";
    $("#submit-run").disabled = true;
    try {
      const res = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || res.statusText);
      }
      const run = await res.json();
      attachToRun(run.id, run.ticker, run.analysis_date, body.analysts);
    } catch (err) {
      $("#form-error").textContent = String(err.message || err);
    } finally {
      $("#submit-run").disabled = false;
    }
  });

  // ------------------------------------------------------------------
  // Running view: render the Progress table + Messages table
  // ------------------------------------------------------------------

  const RUN_STATE = {
    runId: null,
    selectedAnalysts: [],
    agentStatus: {},          // key -> "pending" | "in_progress" | "completed" | "failed"
    sections: {},             // section key -> latest content
    activeSection: null,      // section currently shown in the Report panel
    userPickedTab: false,     // user clicked a tab → freeze auto-switch
    stats: { tool_calls: 0, llm_calls: 0, tokens_in: 0, tokens_out: 0 },
    reportCount: 0,
    messageCount: 0,
  };

  function renderProgressTable(target) {
    const body = $("#" + target);
    body.innerHTML = "";

    TEAM_GROUPS.forEach((group, gi) => {
      // Filter analyst row to only include user-selected analysts.
      let agents = group.agents;
      if (group.team === "Analyst Team" && RUN_STATE.selectedAnalysts.length) {
        agents = group.agents.filter(a => RUN_STATE.selectedAnalysts.includes(a.key));
      }
      if (agents.length === 0) return;

      agents.forEach((a, ai) => {
        const status = RUN_STATE.agentStatus[a.key] || "pending";
        const tr = el("tr", {
          "data-status": status,
          "data-agent":  a.key,
          class: ai === 0 && gi !== 0 ? "team-divider" : "",
        });
        tr.append(
          el("td", { class: "team-cell" }, ai === 0 ? group.team : ""),
          el("td", { class: "agent-cell" }, a.label),
          el("td", { class: "status-cell" }, status === "in_progress" ? "in progress" : status),
        );
        body.append(tr);
      });
    });
  }

  function setAgentStatus(key, status) {
    RUN_STATE.agentStatus[key] = status;
    // Patch just the touched row instead of re-rendering everything.
    const tr = $(`#progress-body tr[data-agent="${key}"]`);
    if (tr) {
      tr.dataset.status = status;
      const cell = tr.querySelector(".status-cell");
      if (cell) cell.textContent = status === "in_progress" ? "in progress" : status;
    } else {
      renderProgressTable("progress-body");
    }
  }

  function appendMessageRow(env, typeLabel, contentText) {
    const tbody = $("#messages-body");
    const ts = new Date(env.ts);
    const time = ts.toLocaleTimeString([], { hour12: false });
    const tr = el("tr", {},
      el("td", { class: "col-time" }, time),
      el("td", { class: "col-type" }, typeLabel),
      el("td", { class: "col-content" }, contentText),
    );
    tbody.append(tr);
    // Keep the panel scrolled to the latest message.
    const tableEl = tbody.closest(".messages-table");
    if (tableEl) tableEl.scrollTop = tableEl.scrollHeight;
    RUN_STATE.messageCount += 1;
  }

  function setReportSection(sectionKey, content) {
    if (!content) return;
    const isNew = !(sectionKey in RUN_STATE.sections);
    RUN_STATE.sections[sectionKey] = content;
    rebuildReportTabs("report-tabs", () => RUN_STATE.activeSection, (key) => {
      RUN_STATE.userPickedTab = true;
      RUN_STATE.activeSection = key;
      renderReportBody("report-body", key);
      setActiveTab("report-tabs", key);
    });
    // Auto-switch to newest section if the user hasn't taken control of the tabs.
    if (!RUN_STATE.userPickedTab || RUN_STATE.activeSection === sectionKey) {
      RUN_STATE.activeSection = sectionKey;
      renderReportBody("report-body", sectionKey);
      setActiveTab("report-tabs", sectionKey);
    }
    RUN_STATE.reportCount = Object.keys(RUN_STATE.sections).length;
    updateStatsFooter();
  }

  /**
   * Render a tab strip from the in-memory sections dict, in pipeline order.
   * targetId: id of the .report-tabs container to (re)build.
   * activeKeyFn: returns the currently-active section key.
   * onClick(key): tab click handler.
   */
  function rebuildReportTabs(targetId, activeKeyFn, onClick) {
    const host = document.getElementById(targetId);
    if (!host) return;
    host.innerHTML = "";
    const active = activeKeyFn();
    SECTION_ORDER.forEach(s => {
      if (!(s.key in RUN_STATE.sections)) return;
      const tab = el("button", {
        class: "report-tab" + (s.key === active ? " active" : ""),
        "data-key": s.key,
        type: "button",
        onclick: () => onClick(s.key),
      }, s.label);
      host.append(tab);
    });
  }

  function setActiveTab(targetId, key) {
    const host = document.getElementById(targetId);
    if (!host) return;
    Array.from(host.querySelectorAll(".report-tab")).forEach(t =>
      t.classList.toggle("active", t.dataset.key === key)
    );
  }

  function renderReportBody(targetId, sectionKey) {
    const host = document.getElementById(targetId);
    if (!host) return;
    if (!sectionKey || !RUN_STATE.sections[sectionKey]) {
      host.innerHTML = `<p class="muted" style="font-family:var(--mono);font-size:12px;">No content yet.</p>`;
      return;
    }
    host.innerHTML = renderMarkdown(RUN_STATE.sections[sectionKey]);
    host.scrollTop = 0;
  }

  function updateStatsFooter() {
    $("#stat-tool").textContent    = RUN_STATE.stats.tool_calls;
    $("#stat-llm").textContent     = RUN_STATE.stats.llm_calls;
    $("#stat-reports").textContent = RUN_STATE.reportCount;
  }

  function attachToRun(runId, ticker, analysisDate, selectedAnalysts) {
    RUN_STATE.runId = runId;
    RUN_STATE.selectedAnalysts = selectedAnalysts || [];
    RUN_STATE.agentStatus = {};
    RUN_STATE.sections = {};
    RUN_STATE.activeSection = null;
    RUN_STATE.userPickedTab = false;
    RUN_STATE.stats = { tool_calls: 0, llm_calls: 0, tokens_in: 0, tokens_out: 0 };
    RUN_STATE.reportCount = 0;
    RUN_STATE.messageCount = 0;

    sessionStorage.setItem("currentRunId", runId);
    $("#running-title").textContent = `${ticker}  ·  ${analysisDate}`;
    $("#running-meta").textContent = `Run ${runId.slice(0, 8)}`;
    $("#messages-body").innerHTML = "";
    $("#report-tabs").innerHTML = "";
    $("#report-body").innerHTML =
      `<p class="muted" style="font-family:var(--mono);font-size:12px;">Waiting for the first agent to produce output…</p>`;
    // Wire the download button to /report.md (works even mid-run because the
    // runner persists final_state after every chunk).
    $("#report-download").href = `/api/runs/${encodeURIComponent(runId)}/report.md`;
    renderProgressTable("progress-body");
    updateStatsFooter();
    setView("running");
    openStream(runId);
  }

  function openStream(runId) {
    if (window._activeES) { window._activeES.close(); window._activeES = null; }
    const es = new EventSource("/api/runs/" + encodeURIComponent(runId) + "/events");
    window._activeES = es;

    const handle = (typeName, fn) => {
      es.addEventListener(typeName, ev => {
        try { fn(JSON.parse(ev.data)); } catch (err) { console.error(typeName, err); }
      });
    };

    handle("run.started", (env) => {
      appendMessageRow(env, "System", `Run started for ${env.data.ticker} on ${env.data.analysis_date}.`);
      // First analyst in selection becomes active.
      const first = (env.data.selected_analysts || [])[0];
      if (first) setAgentStatus(first, "in_progress");
    });

    handle("analyst.started", (env) => {
      setAgentStatus(env.data.analyst, "in_progress");
      const label = AGENT_LABEL[env.data.analyst] || env.data.analyst;
      appendMessageRow(env, "Status", `${label} started`);
    });

    handle("analyst.completed", (env) => {
      setAgentStatus(env.data.analyst, "completed");
      const label = AGENT_LABEL[env.data.analyst] || env.data.analyst;
      appendMessageRow(env, "Status", `${label} completed`);
    });

    handle("analyst.report", (env) => {
      setReportSection(env.data.section, env.data.content);
      const label = AGENT_LABEL[env.data.analyst] || env.data.analyst;
      appendMessageRow(env, "Report", `${label} produced ${env.data.content?.length || 0} chars`);
    });

    handle("team.started", (env) => {
      const team = env.data.team;
      if (team === "research") {
        setAgentStatus("bull", "in_progress");
        setAgentStatus("bear", "in_progress");
      } else if (team === "trading") {
        setAgentStatus("trader", "in_progress");
      } else if (team === "risk") {
        setAgentStatus("aggressive",   "in_progress");
        setAgentStatus("conservative", "in_progress");
        setAgentStatus("neutral",      "in_progress");
      }
      appendMessageRow(env, "Status", `${team} team started`);
    });

    handle("team.completed", (env) => {
      const team = env.data.team;
      if (team === "research") {
        setAgentStatus("bull", "completed");
        setAgentStatus("bear", "completed");
        setAgentStatus("research_judge", "completed");
      } else if (team === "trading") {
        setAgentStatus("trader", "completed");
      } else if (team === "risk") {
        setAgentStatus("aggressive",   "completed");
        setAgentStatus("conservative", "completed");
        setAgentStatus("neutral",      "completed");
        setAgentStatus("portfolio_manager", "completed");
      }
      appendMessageRow(env, "Status", `${team} team completed`);
    });

    handle("debate.update", (env) => {
      const role = env.data.role;
      const team = env.data.team;
      // The CLI labels these by analyst name; mirror it.
      const labelMap = {
        bull: "Bull Researcher", bear: "Bear Researcher", judge: "Research Manager",
        aggressive: "Risky Analyst", conservative: "Safe Analyst", neutral: "Neutral Analyst",
        portfolio_manager: "Portfolio Manager",
      };
      const label = labelMap[role] || role;
      const txt = env.data.delta || env.data.full || "";
      const preview = txt.length > 220 ? txt.slice(0, 220) + "…" : txt;
      appendMessageRow(env, "Reasoning", `${label}: ${preview}`);
      // Map to a section key so the report panel keeps up.
      if (team === "investment") {
        setReportSection("investment_plan", env.data.full);
      } else if (team === "risk") {
        setReportSection("final_trade_decision", env.data.full);
      }
    });

    handle("report.section", (env) => {
      setReportSection(env.data.section, env.data.content);
      appendMessageRow(env, "Report", `${SECTION_LABEL[env.data.section] || env.data.section} updated`);
    });

    handle("message", (env) => {
      const role = env.data.role || "Agent";
      const content = env.data.content || "";
      appendMessageRow(env, role, content);
    });

    handle("tool.called", (env) => {
      const args = env.data.args ? JSON.stringify(env.data.args) : "";
      const argsPreview = args.length > 80 ? args.slice(0, 80) + "…" : args;
      appendMessageRow(env, "Tool Call", `${env.data.name}(${argsPreview})`);
    });

    handle("stats.update", (env) => {
      RUN_STATE.stats = { ...RUN_STATE.stats, ...env.data };
      updateStatsFooter();
    });

    handle("heartbeat", () => { /* keepalive */ });

    handle("run.final", async (env) => {
      es.close();
      window._activeES = null;
      sessionStorage.removeItem("currentRunId");
      const detail = await fetch("/api/runs/" + encodeURIComponent(runId)).then(r => r.json());
      renderResult(detail);
    });

    handle("run.failed", (env) => {
      appendMessageRow(env, "Failed", env.data.error || "run failed");
      es.close();
      window._activeES = null;
      sessionStorage.removeItem("currentRunId");
    });

    handle("run.cancelled", (env) => {
      appendMessageRow(env, "Cancelled", "run cancelled");
      es.close();
      window._activeES = null;
      sessionStorage.removeItem("currentRunId");
    });

    es.onerror = (ev) => {
      // EventSource auto-reconnects; just log.
      console.warn("SSE error", ev);
    };
  }

  // Cancel
  $("#cancel-run").addEventListener("click", async () => {
    if (!RUN_STATE.runId) return;
    if (!confirm("Cancel this analysis? It will stop after the current agent finishes.")) return;
    await fetch("/api/runs/" + encodeURIComponent(RUN_STATE.runId) + "/cancel", { method: "POST" });
  });

  // Back from result → wherever the user came from (history, or form for new runs)
  $("#result-back").addEventListener("click", () => {
    if (_previousView === "history") loadHistory();
    setView(_previousView || "form");
  });

  // ------------------------------------------------------------------
  // Result view
  // ------------------------------------------------------------------

  const RATING_LABELS = {
    "Buy":         { tier: "buy",         label: "STRONG BUY" },
    "Overweight":  { tier: "overweight",  label: "BUY" },
    "Hold":        { tier: "hold",        label: "HOLD" },
    "Underweight": { tier: "underweight", label: "REDUCE" },
    "Sell":        { tier: "sell",        label: "STRONG SELL" },
  };

  // Extra section ordering for the result view — adds debate sub-tabs that
  // aren't in the live SECTION_ORDER (those get aggregated into investment_plan
  // and final_trade_decision while running).
  const RESULT_DEBATE_TABS = [
    { key: "bull",        label: "Bull Researcher",  from: d => d.investment_debate_state?.bull_history },
    { key: "bear",        label: "Bear Researcher",  from: d => d.investment_debate_state?.bear_history },
    { key: "research_mgr",label: "Research Manager", from: d => d.investment_debate_state?.judge_decision },
    { key: "aggressive",  label: "Risky Analyst",    from: d => d.risk_debate_state?.aggressive_history },
    { key: "conservative",label: "Safe Analyst",     from: d => d.risk_debate_state?.conservative_history },
    { key: "neutral",     label: "Neutral Analyst",  from: d => d.risk_debate_state?.neutral_history },
  ];

  function renderResult(detail) {
    $("#result-title").textContent = `${detail.ticker}  ·  ${detail.analysis_date}`;
    $("#result-download").href = "/api/runs/" + encodeURIComponent(detail.id) + "/report.md";

    renderRatingBanner(detail.ticker, detail.rating, detail.analysis_date);

    // Rebuild progress table from detail (everything is "completed" by definition,
    // but the user may have selected a subset of analysts).
    const selected = (detail.config?.analysts) || [];
    RUN_STATE.selectedAnalysts = selected;
    RUN_STATE.agentStatus = {};
    [...selected, "bull", "bear", "research_judge", "trader",
     "aggressive", "conservative", "neutral", "portfolio_manager"]
      .forEach(k => { RUN_STATE.agentStatus[k] = "completed"; });
    renderProgressTable("result-progress-body");

    // Run summary pane (middle column)
    const sum = $("#result-summary");
    sum.innerHTML = "";
    const cfg = detail.config || {};
    const addRow = (label, value) => {
      if (!value && value !== 0) return;
      sum.append(el("dt", {}, label), el("dd", {}, String(value)));
    };
    addRow("Status",       detail.status);
    addRow("Rating",       detail.rating);
    addRow("Provider",     cfg.llm_provider);
    addRow("Deep model",   cfg.deep_thinker);
    addRow("Quick model",  cfg.shallow_thinker);
    addRow("Depth",        cfg.research_depth);
    addRow("Language",     cfg.output_language);
    addRow("Analysts",     (cfg.analysts || []).join(", "));
    addRow("Started",      detail.started_at?.slice(0, 19).replace("T", " "));
    addRow("Finished",     detail.finished_at?.slice(0, 19).replace("T", " "));
    addRow("Run id",       detail.id);
    if (detail.error) addRow("Error", detail.error);

    // Build the section dict + extended order including debate sub-tabs.
    const sectionsDict = {};
    const add = (key, content) => { if (content) sectionsDict[key] = content; };
    add("market_report",          detail.market_report);
    add("sentiment_report",       detail.sentiment_report);
    add("news_report",            detail.news_report);
    add("fundamentals_report",    detail.fundamentals_report);
    add("investment_plan",        detail.investment_plan);
    RESULT_DEBATE_TABS.forEach(t => add(t.key, t.from(detail)));
    add("trader_investment_plan", detail.trader_investment_plan);
    add("final_trade_decision",   detail.final_trade_decision);

    // Stash into RUN_STATE so the same tab/render helpers work.
    RUN_STATE.sections = sectionsDict;
    RUN_STATE.activeSection = null;
    RUN_STATE.userPickedTab = true;  // result view is fully user-driven

    // Build a result-view-specific tab list using the extended order.
    const RESULT_ORDER = [
      ...SECTION_ORDER.slice(0, 5),  // 4 analyst reports + investment_plan
      ...RESULT_DEBATE_TABS,
      ...SECTION_ORDER.slice(5),     // trader, final_decision
    ];
    rebuildResultTabs(RESULT_ORDER, sectionsDict);

    // Default view: prefer the final decision, then investment plan, then anything.
    const preferred = ["final_trade_decision", "investment_plan", "market_report"];
    const first = preferred.find(k => k in sectionsDict)
              || Object.keys(sectionsDict)[0]
              || null;
    if (first) {
      RUN_STATE.activeSection = first;
      $("#result-body").innerHTML = renderMarkdown(sectionsDict[first]);
      setActiveTab("result-tabs", first);
    } else {
      $("#result-body").innerHTML = `<p class="muted">No report sections produced.</p>`;
    }

    setView("result");
  }

  function rebuildResultTabs(order, sectionsDict) {
    const host = $("#result-tabs");
    host.innerHTML = "";
    order.forEach(s => {
      if (!(s.key in sectionsDict)) return;
      const tab = el("button", {
        class: "report-tab",
        "data-key": s.key,
        type: "button",
        onclick: () => {
          RUN_STATE.activeSection = s.key;
          $("#result-body").innerHTML = renderMarkdown(sectionsDict[s.key]);
          $("#result-body").scrollTop = 0;
          setActiveTab("result-tabs", s.key);
        },
      }, s.label);
      host.append(tab);
    });
  }

  function renderRatingBanner(ticker, rating, date) {
    const banner = $("#rating-banner");
    banner.className = "rating-banner";
    banner.innerHTML = "";
    if (!rating) {
      banner.append(
        el("p", { class: "label" }, "—"),
        el("div", { class: "meta" }, ticker, el("br"), date),
      );
      return;
    }
    const meta = RATING_LABELS[rating] || { tier: "", label: rating.toUpperCase() };
    banner.classList.add("tier-" + meta.tier);
    banner.append(
      el("p", { class: "label" }, meta.label),
      el("div", { class: "meta" },
        el("strong", {}, ticker), el("br"),
        date, el("br"),
        "Rating: " + rating,
      ),
    );
  }

  // ------------------------------------------------------------------
  // History
  // ------------------------------------------------------------------

  async function loadHistory() {
    const rows = await fetch("/api/runs").then(r => r.json());
    const body = $("#history-body");
    body.innerHTML = "";
    if (rows.length === 0) {
      body.append(el("tr", {}, el("td", { colspan: "6", class: "muted" }, "No runs yet.")));
      return;
    }
    rows.forEach(r => {
      const tr = el("tr", {},
        el("td", {}, r.ticker),
        el("td", {}, r.analysis_date),
        el("td", {}, r.created_at?.slice(0, 19).replace("T", " ") || ""),
        el("td", {}, r.status),
        el("td", { class: "row-rating" }, r.rating || "—"),
        el("td", {}, el("a", { href: "#", onclick: (e) => { e.preventDefault(); openRun(r.id); } }, "view")),
      );
      body.append(tr);
    });
  }

  async function openRun(runId) {
    const detail = await fetch("/api/runs/" + encodeURIComponent(runId)).then(r => r.json());
    if (detail.status === "running" || detail.status === "queued") {
      attachToRun(runId, detail.ticker, detail.analysis_date, detail.config?.analysts || []);
    } else {
      renderResult(detail);
    }
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------

  async function boot() {
    try {
      await loadConfig();
    } catch (err) {
      $("#form-error").textContent = "Failed to load /api/config: " + err.message;
      return;
    }
    const stashedId = sessionStorage.getItem("currentRunId");
    if (stashedId) {
      try {
        const detail = await fetch("/api/runs/" + encodeURIComponent(stashedId)).then(r => r.json());
        if (detail.status === "running" || detail.status === "queued") {
          attachToRun(stashedId, detail.ticker, detail.analysis_date, detail.config?.analysts || []);
          return;
        }
      } catch {/* ignore */}
      sessionStorage.removeItem("currentRunId");
    }
    setView("form");
  }
  boot();
})();
