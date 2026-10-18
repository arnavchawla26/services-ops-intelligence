/* services-ops-intelligence results page. Reads data/site.json and data/memo.html
   written by scripts/build_site.py from reports/ and docs/. */
(async function () {
  "use strict";
  const S = window.Site;
  const { fmt, esc, table, tiles, setHTML, setText } = S;

  let D;
  try {
    D = await S.loadJSON("data/site.json");
  } catch (e) {
    setHTML("failure-tiles", '<p class="note">Could not load data/site.json: ' + esc(e.message) + "</p>");
    return;
  }

  setText("commit", D.commit || "unknown");
  setText("generated", D.generated_at || "");
  if (D.intro) setText("intro", D.intro.replace(/^Project \d of \d:\s*/, "").replace(/^\w/, (c) => c.toUpperCase()));

  const MODEL_LABEL = { heuristic: "Backblaze rule", logistic: "Logistic regression", lightgbm: "LightGBM" };
  const MODELS = ["heuristic", "logistic", "lightgbm"];

  /* ---------------- Failure model ---------------- */
  const fm = D.failure_model, cfg = fm.config, cnt = fm.counts, test = fm.test;
  setText("failure-sub",
    `Label: failure within ${cfg.horizon_days} days. Features use only days at or before the prediction date. ` +
    `Training predicts on ${cfg.train_prediction_dates[0]} to ${cfg.train_prediction_dates[1]} (labels through ${cfg.train_labels_through}); ` +
    `the test predicts on ${cfg.test_prediction_dates[0]} to ${cfg.test_prediction_dates[1]} (labels through ${cfg.test_labels_through}), so no test label was ever a training label.`);

  setHTML("failure-tiles", tiles([
    { v: fmt.int(cnt.test_drive_days), l: "test drive-days" },
    { v: fmt.int(cnt.test_distinct_failing_drives), l: "distinct failing drives in the test" },
    { v: fmt.fixed(test.lightgbm.pr_auc, 3), l: "LightGBM PR-AUC", s: `rule ${fmt.fixed(test.heuristic.pr_auc, 3)}` },
    { v: fmt.pct(test.lightgbm["recall_top_0.01"], 0), l: "failures in LightGBM's top 1%", s: `rule ${fmt.pct(test.heuristic["recall_top_0.01"], 0)}` },
    { v: fmt.int(Math.round(test.heuristic_flag_set.flagged_per_day)), l: "drives the rule flags per day", s: `${fmt.pct(test.heuristic_flag_set.flagged_share_of_drive_days, 1)} of the fleet` },
    { v: fm.runtime_seconds != null ? fmt.num(fm.runtime_seconds, 0) + " s" : "", l: "model run time on a laptop" },
  ]));

  setHTML("ranking-table", table([
    { key: "name", label: "Ranking" },
    { key: "pr_auc", label: "PR-AUC", fmt: (v) => fmt.fixed(v, 3), cls: "n" },
    { key: "recall_top_0.001", label: "Recall in top 0.1%", fmt: (v) => fmt.pct(v, 0), cls: "n" },
    { key: "recall_top_0.01", label: "Recall in top 1%", fmt: (v) => fmt.pct(v, 0), cls: "n" },
    { key: "brier_calibrated", label: "Brier (calibrated)", fmt: (v) => (v == null ? "n/a" : fmt.fixed(v, 6)), cls: "n" },
  ], MODELS.map((m) => Object.assign({ name: MODEL_LABEL[m] }, test[m]))));
  setText("ranking-note",
    `Base rate ${test.baseline_rate} (a random ranking scores PR-AUC about ${test.baseline_rate}). A constant predictor scores Brier ${test.brier_constant_predictor}. ` +
    `Dispatching to every drive the rule flags would catch ${fmt.pct(test.heuristic_flag_set.recall_if_every_flagged_drive_dispatched, 0)} of failures at ${fmt.pct(test.heuristic_flag_set.precision_of_flag, 1)} precision. ` +
    `Calibration rests on ${fmt.int(cnt.cal_positives)} events, so expect the numbers to move by a few points on another window.`);

  S.chart("c-features", (el) => {
    const top = fm.feature_importance.slice(0, 10);
    return new Chart(el, {
      type: "bar",
      data: { labels: top.map((f) => f.feature), datasets: [S.barDataset("share of gain", top.map((f) => f.gain_share), 0, { borderSkipped: "start" })] },
      options: S.baseOptions({
        indexAxis: "y",
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => fmt.pct(c.parsed.x, 1) } } },
        scales: { x: { grid: { color: S.cssVar("--grid") }, border: { display: false }, ticks: { callback: (v) => fmt.pct(v, 0) } }, y: { grid: { display: false }, border: { color: S.cssVar("--axis") } } },
      }),
    });
  });

  /* ---------------- Dispatch budget ---------------- */
  const curve = D.dispatch_curve;
  const budgets = curve.lightgbm.map((r) => r.daily_budget);
  function rowAt(model, b) {
    const rows = curve[model];
    let best = rows[0];
    for (const r of rows) if (r.daily_budget <= b) best = r;
    return best;
  }
  let sliderValue = 50;
  const markerPlugin = {
    id: "budgetMarker",
    afterDatasetsDraw(chart) {
      const x = chart.scales.x.getPixelForValue(sliderValue);
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = S.cssVar("--muted");
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
      ctx.restore();
    },
  };
  let curveChart = null;
  S.chart("c-curve", (el) => {
    curveChart = new Chart(el, {
      type: "line",
      data: {
        datasets: MODELS.map((m, i) => S.lineDataset(MODEL_LABEL[m], curve[m].map((r) => ({ x: r.daily_budget, y: r.caught_per_day })), i)),
      },
      options: S.baseOptions({
        parsing: false,
        plugins: {
          tooltip: {
            callbacks: {
              title: (items) => items.length ? `${items[0].parsed.x} trucks per day` : "",
              label: (c) => `${c.dataset.label}: ${fmt.num(c.parsed.y, 1)} caught, ${fmt.num(c.parsed.x - c.parsed.y, 1)} for nothing`,
            },
          },
        },
        scales: {
          x: { type: "linear", min: 1, max: 1000, title: { display: true, text: "trucks per day", color: S.cssVar("--ink-2") } },
          y: { title: { display: true, text: "failing drives caught per day", color: S.cssVar("--ink-2") } },
        },
      }),
      plugins: [markerPlugin],
    });
    return curveChart;
  });

  function renderReadout() {
    const b = sliderValue;
    const rows = MODELS.map((m) => Object.assign({ name: MODEL_LABEL[m] }, rowAt(m, b)));
    const pos = curve.lightgbm[0] ? D.dispatch_budget[0].positives_per_day : null;
    setHTML("budget-readout", table([
      { key: "name", label: `At ${b} trucks per day` },
      { key: "caught_per_day", label: "Caught per day", fmt: (v) => fmt.num(v, 1), cls: "n" },
      { key: "wasted_per_day", label: "For nothing per day", fmt: (v) => fmt.num(v, 1), cls: "n" },
      { key: "precision", label: "Precision", fmt: (v) => fmt.pct(v, 1), cls: "n" },
      { key: "recall", label: `Share of the day's ${fmt.num(pos, 0)} failing drives`, fmt: (v) => fmt.pct(v, 1), cls: "n" },
    ], rows));
    setText("budget-out", `${b} trucks/day`);
    if (curveChart) curveChart.draw();
  }
  const slider = document.getElementById("budget-slider");
  slider.addEventListener("input", () => { sliderValue = Number(slider.value); renderReadout(); });
  renderReadout();

  const byBudget = {};
  for (const r of D.dispatch_budget) {
    byBudget[r.daily_budget] = byBudget[r.daily_budget] || { daily_budget: r.daily_budget };
    byBudget[r.daily_budget][r.model] = r;
  }
  setHTML("budget-table", table([
    { key: "daily_budget", label: "Trucks per day", cls: "n" },
    { key: (r) => r.heuristic.caught_per_day, label: "Rule caught", fmt: (v) => fmt.num(v, 1), cls: "n" },
    { key: (r) => r.heuristic.wasted_per_day, label: "Rule for nothing", fmt: (v) => fmt.num(v, 1), cls: "n" },
    { key: (r) => r.logistic.caught_per_day, label: "Logistic caught", fmt: (v) => fmt.num(v, 1), cls: "n" },
    { key: (r) => r.lightgbm.caught_per_day, label: "LightGBM caught", fmt: (v) => fmt.num(v, 1), cls: "n" },
    { key: (r) => r.lightgbm.wasted_per_day, label: "LightGBM for nothing", fmt: (v) => fmt.num(v, 1), cls: "n" },
    { key: (r) => r.lightgbm.recall, label: "LightGBM recall", fmt: (v) => fmt.pct(v, 1), cls: "n" },
  ], Object.values(byBudget).sort((a, b) => a.daily_budget - b.daily_budget)));

  /* ---------------- Reliability ---------------- */
  S.chart("c-reliability", (el) => new Chart(el, {
    type: "bar",
    data: {
      labels: D.reliability.map((r) => "bin " + r.bin),
      datasets: [
        S.barDataset("mean predicted", D.reliability.map((r) => r.mean_predicted), 0),
        S.barDataset("observed rate", D.reliability.map((r) => r.observed_rate), 1),
      ],
    },
    options: S.baseOptions({
      plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmt.pct(c.parsed.y, 3)}` } } },
      scales: { y: { type: "logarithmic", ticks: { callback: (v) => fmt.pct(v, 3) }, title: { display: true, text: "30-day failure probability (log scale)", color: S.cssVar("--ink-2") } } },
    }),
  }));
  setHTML("reliability-table", table([
    { key: "bin", label: "Bin", cls: "n" },
    { key: "n", label: "Drive-days", fmt: fmt.int, cls: "n" },
    { key: "p_min", label: "p min", fmt: (v) => fmt.pct(v, 4), cls: "n" },
    { key: "p_max", label: "p max", fmt: (v) => fmt.pct(v, 2), cls: "n" },
    { key: "mean_predicted", label: "Mean predicted", fmt: (v) => fmt.pct(v, 3), cls: "n" },
    { key: "observed_rate", label: "Observed", fmt: (v) => fmt.pct(v, 3), cls: "n" },
    { key: "positives", label: "Failures", fmt: fmt.int, cls: "n" },
  ], D.reliability));

  /* ---------------- Parts ---------------- */
  const P = D.parts, ps = P.summary;
  const PM = ["naive", "ma4", "ses", "lgbm"];
  const PM_LABEL = { naive: "Last week (naive)", ma4: "4-week average", ses: "Exponential smoothing", lgbm: "Pooled LightGBM" };
  setText("parts-sub",
    `Weekly units per region and part from the generated dispatch_part table. ${ps.weeks} weeks exist, so the backtest is a weekly rolling origin over the last ${ps.origins} weeks, horizon ${ps.horizon_weeks} week, across ${ps.series} series. ` +
    `The mean week uses ${fmt.num(ps.mean_weekly_demand_per_series, 2)} units per series.`);
  S.chart("c-parts", (el) => new Chart(el, {
    type: "bar",
    data: {
      labels: ["per region x part series", "fleet total per part"],
      datasets: PM.map((m, i) => S.barDataset(PM_LABEL[m], [ps.region_part_mae[m], ps.fleet_total_mae[m]], i)),
    },
    options: S.baseOptions({
      plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: MAE ${fmt.num(c.parsed.y, 2)}` } } },
      scales: { y: { title: { display: true, text: "mean absolute error (units per week)", color: S.cssVar("--ink-2") } } },
    }),
  }));
  setHTML("parts-table", table([
    { key: "m", label: "Model", fmt: (v) => PM_LABEL[v] },
    { key: (r) => ps.region_part_mae[r.m], label: "MAE, series", fmt: (v) => fmt.num(v, 3), cls: "n" },
    { key: (r) => ps.region_part_bias[r.m], label: "Bias, series", fmt: (v) => fmt.num(v, 3), cls: "n" },
    { key: (r) => ps.fleet_total_mae[r.m], label: "MAE, fleet", fmt: (v) => fmt.num(v, 3), cls: "n" },
    { key: (r) => ps.fleet_total_bias[r.m], label: "Bias, fleet", fmt: (v) => fmt.num(v, 3), cls: "n" },
  ], PM.map((m) => ({ m }))));

  /* ---------------- Repeat dispatch ---------------- */
  const R = D.repeat, rs = R.summary;
  setText("repeat-sub",
    `${fmt.int(rs.dispatches)} dispatches, ${fmt.int(rs.repeats)} repeats (${fmt.pct(rs.repeat_rate, 1)}: ${fmt.int(rs.same_case_repeats)} same-case, ${fmt.int(rs.cross_case_repeats)} cross-case within ${rs.cross_case_window_days} days). ` +
    `Against the ground truth ${fmt.int(rs.repeats_necessary)} were necessary and ${fmt.int(rs.repeats_avoidable)} avoidable; ${fmt.pct(rs.first_visits_unnecessary_share, 0)} of first visits were unnecessary.`);
  const cats = R.by_category.slice().sort((a, b) => b.dispatches - a.dispatches);
  S.chart("c-repeat", (el) => new Chart(el, {
    type: "bar",
    data: {
      labels: cats.map((r) => r.key.replace(/_/g, " ")),
      datasets: [
        S.barDataset("repeat rate", cats.map((r) => r.repeat_rate), 0),
        S.barDataset("avoidable repeat rate", cats.map((r) => r.avoidable_repeat_rate), 1),
      ],
    },
    options: S.baseOptions({
      plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmt.pct(c.parsed.y, 1)}` } } },
      scales: { y: { ticks: { callback: (v) => fmt.pct(v, 0) }, max: 0.4 } },
    }),
  }));
  setHTML("repeat-table", table([
    { key: "key", label: "Category", fmt: (v) => v.replace(/_/g, " ") },
    { key: "dispatches", label: "Dispatches", fmt: fmt.int, cls: "n" },
    { key: "repeats", label: "Repeats", fmt: fmt.int, cls: "n" },
    { key: "repeat_rate", label: "Repeat rate", fmt: (v) => fmt.pct(v, 1), cls: "n" },
    { key: "repeats_necessary", label: "Necessary", fmt: fmt.int, cls: "n" },
    { key: "repeats_avoidable", label: "Avoidable", fmt: fmt.int, cls: "n" },
    { key: "avoidable_repeat_rate", label: "Avoidable rate", fmt: (v) => fmt.pct(v, 1), cls: "n" },
    { key: "first_visits_unnecessary", label: "First visits unnecessary", fmt: fmt.int, cls: "n" },
  ], cats));

  /* ---------------- Deflection ---------------- */
  const F = D.deflection;
  setText("deflection-sub",
    `Each of ${fmt.int(F.cases)} case descriptions is matched against ${F.runbooks} runbooks with TF-IDF cosine and, independently, Okapi BM25. ` +
    `The threshold is chosen on one half (${fmt.int(F.split.tune)} cases) and scored on the other (${fmt.int(F.split.report)} cases, ${fmt.int(F.variants.tfidf.deflectable)} deflectable per the truth file). ` +
    `${fmt.int(F.deflectable_in_truth)} of all cases are deflectable in truth.`);
  const VARIANT_LABEL = {
    tfidf: "TF-IDF, family-restricted",
    bm25: "BM25, family-restricted",
    "tfidf+severity": "TF-IDF + severity >= 3 (policy rule)",
    tfidf_all_runbooks: "TF-IDF, every runbook eligible",
    bm25_all_runbooks: "BM25, every runbook eligible",
  };
  const variantRows = ["tfidf", "bm25", "tfidf+severity", "tfidf_all_runbooks", "bm25_all_runbooks"].filter((k) => F.variants[k]).map((k) => Object.assign({ name: VARIANT_LABEL[k] || k }, F.variants[k]));
  setHTML("deflection-table", table([
    { key: "name", label: "Variant" },
    { key: "threshold", label: "Threshold", fmt: (v) => fmt.num(v, 4), cls: "n" },
    { key: "flagged", label: "Flagged", fmt: fmt.int, cls: "n" },
    { key: "tp", label: "True", fmt: fmt.int, cls: "n" },
    { key: "fp", label: "False", fmt: fmt.int, cls: "n" },
    { key: "precision", label: "Precision", fmt: (v) => fmt.pct(v, 0), cls: "n" },
    { key: "recall", label: "Recall", fmt: (v) => fmt.pct(v, 0), cls: "n" },
    { key: "f1", label: "F1", fmt: (v) => fmt.fixed(v, 2), cls: "n" },
  ], variantRows));

  const SCORER_LABEL = { tfidf: "TF-IDF, family", bm25: "BM25, family", tfidf_all_runbooks: "TF-IDF, all runbooks", bm25_all_runbooks: "BM25, all runbooks" };
  const scorers = ["tfidf", "bm25", "tfidf_all_runbooks", "bm25_all_runbooks"].filter((s) => F.pr_table.some((r) => r.scorer === s));
  S.chart("c-pr", (el) => new Chart(el, {
    type: "line",
    data: {
      datasets: scorers.map((s, i) => {
        const rows = F.pr_table.filter((r) => r.scorer === s).sort((a, b) => a.recall - b.recall);
        return S.lineDataset(SCORER_LABEL[s] || s, rows.map((r) => ({ x: r.recall, y: r.precision, chosen: r.chosen === "True" || r.chosen === true, threshold: r.threshold })), i, {
          pointRadius: (ctx) => (ctx.raw && ctx.raw.chosen ? 7 : 2.5),
          pointHoverRadius: 7,
          pointBorderColor: S.cssVar("--surface"),
          pointBorderWidth: (ctx) => (ctx.raw && ctx.raw.chosen ? 2 : 0),
        });
      }),
    },
    options: S.baseOptions({
      parsing: false,
      interaction: { mode: "nearest", intersect: false },
      plugins: { tooltip: { callbacks: {
        title: (items) => items.length ? `${items[0].dataset.label}, threshold ${fmt.num(items[0].raw.threshold, 4)}${items[0].raw.chosen ? " (chosen)" : ""}` : "",
        label: (c) => `precision ${fmt.pct(c.parsed.y, 1)}, recall ${fmt.pct(c.parsed.x, 1)}`,
      } } },
      scales: {
        x: { type: "linear", min: 0, max: 1, ticks: { callback: (v) => fmt.pct(v, 0) }, title: { display: true, text: "recall", color: S.cssVar("--ink-2") } },
        y: { min: 0, max: 1, ticks: { callback: (v) => fmt.pct(v, 0) }, title: { display: true, text: "precision", color: S.cssVar("--ink-2") } },
      },
    }),
  }));

  const catRows = F.by_category.filter((r) => r.scorer === "tfidf").sort((a, b) => b.cases - a.cases);
  setHTML("deflection-cat-table", table([
    { key: "category", label: "Category", fmt: (v) => v.replace(/_/g, " ") },
    { key: "cases", label: "Cases", fmt: fmt.int, cls: "n" },
    { key: "deflectable", label: "Deflectable", fmt: fmt.int, cls: "n" },
    { key: "flagged", label: "Flagged", fmt: fmt.int, cls: "n" },
    { key: "precision", label: "Precision", fmt: (v) => fmt.pct(v, 0), cls: "n" },
    { key: "recall", label: "Recall", fmt: (v) => fmt.pct(v, 0), cls: "n" },
  ], catRows));
  setText("deflection-caveat", "Caveat from reports/case_deflection_metrics.json: " + F.caveat +
    ` Runbook hit rate on the report half (share of truly deflectable cases whose top runbook is the generator's): TF-IDF ${fmt.pct(F.runbook_hit_rate.tfidf, 0)} family-restricted, ${fmt.pct(F.runbook_hit_rate.tfidf_all_runbooks, 0)} with every runbook eligible; BM25 ${fmt.pct(F.runbook_hit_rate.bm25, 0)} and ${fmt.pct(F.runbook_hit_rate.bm25_all_runbooks, 0)}.`);

  /* ---------------- Memo ---------------- */
  try {
    setHTML("memo-doc", await S.loadText("data/memo.html"));
  } catch (e) {
    setHTML("memo-doc", '<p class="note">Could not load the memo: ' + esc(e.message) + "</p>");
  }
})();
