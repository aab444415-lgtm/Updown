const state = {
  mode: "sample",
  report: null,
  backtest: null,
  snapshots: null,
  selectedTicker: null,
  industryFilter: "all",
  currentPage: "home",
};

const elements = {
  pageViews: [...document.querySelectorAll("[data-page]")],
  pageLinks: [...document.querySelectorAll("[data-page-link]")],
  createdAt: document.querySelector("#createdAt"),
  newsStatus: document.querySelector("#newsStatus"),
  marketStatus: document.querySelector("#marketStatus"),
  fundamentalStatus: document.querySelector("#fundamentalStatus"),
  topTicker: document.querySelector("#topTicker"),
  usTopList: document.querySelector("#usTopList"),
  krTopList: document.querySelector("#krTopList"),
  overviewBacktest: document.querySelector("#overviewBacktest"),
  overviewSnapshots: document.querySelector("#overviewSnapshots"),
  macroContext: document.querySelector("#macroContext"),
  macroSnapshot: document.querySelector("#macroSnapshot"),
  warnings: document.querySelector("#warnings"),
  industryList: document.querySelector("#industryList"),
  stockList: document.querySelector("#stockList"),
  shortTermList: document.querySelector("#shortTermList"),
  mediumTermList: document.querySelector("#mediumTermList"),
  earlyGrowthList: document.querySelector("#earlyGrowthList"),
  industryFilter: document.querySelector("#industryFilter"),
  detailTitle: document.querySelector("#detailTitle"),
  detailMeta: document.querySelector("#detailMeta"),
  detailScore: document.querySelector("#detailScore"),
  detailInsight: document.querySelector("#detailInsight"),
  detailScoreGrid: document.querySelector("#detailScoreGrid"),
  technicalTrend: document.querySelector("#technicalTrend"),
  technicalChart: document.querySelector("#technicalChart"),
  technicalMetrics: document.querySelector("#technicalMetrics"),
  metricGrid: document.querySelector("#metricGrid"),
  reasonList: document.querySelector("#reasonList"),
  analysisCheckList: document.querySelector("#analysisCheckList"),
  secondOrderList: document.querySelector("#secondOrderList"),
  issueList: document.querySelector("#issueList"),
  riskList: document.querySelector("#riskList"),
  newsList: document.querySelector("#newsList"),
  loading: document.querySelector("#loading"),
  refreshButton: document.querySelector("#refreshButton"),
  backtestMonths: document.querySelector("#backtestMonths"),
  backtestTop: document.querySelector("#backtestTop"),
  backtestBenchmark: document.querySelector("#backtestBenchmark"),
  backtestButton: document.querySelector("#backtestButton"),
  backtestSummary: document.querySelector("#backtestSummary"),
  backtestBenchmarks: document.querySelector("#backtestBenchmarks"),
  backtestPeriods: document.querySelector("#backtestPeriods"),
  backtestNotes: document.querySelector("#backtestNotes"),
  snapshotRefreshButton: document.querySelector("#snapshotRefreshButton"),
  snapshotSummary: document.querySelector("#snapshotSummary"),
  snapshotList: document.querySelector("#snapshotList"),
};

const PAGE_IDS = new Set([
  "home",
  "backtest",
  "snapshots",
  "macro",
  "industries",
  "stocks",
  "short-term",
  "medium-term",
  "early-growth",
  "detail",
  "news",
]);

document.querySelectorAll(".segment").forEach((button) => {
  button.addEventListener("click", () => {
    state.mode = button.dataset.mode;
    document.querySelectorAll(".segment").forEach((item) => {
      item.classList.toggle("active", item.dataset.mode === state.mode);
    });
    loadReport();
  });
});

elements.refreshButton.addEventListener("click", () => loadReport());
elements.backtestButton.addEventListener("click", () => loadBacktest());
elements.snapshotRefreshButton.addEventListener("click", () => loadSnapshots());
elements.industryFilter.addEventListener("change", () => {
  state.industryFilter = elements.industryFilter.value;
  renderStocks();
});

window.addEventListener("hashchange", () => showPage(pageFromHash()));

async function loadReport() {
  setLoading(true);
  try {
    const live = state.mode === "live" ? "1" : "0";
    state.report = await fetchJsonWithFallback(
      `/api/report?live=${live}`,
      [`/data/report-${state.mode}.json`, "/data/report-sample.json"],
      "리포트를 불러오지 못했습니다."
    );
    state.selectedTicker = state.report.stocks[0]?.ticker ?? null;
    render();
  } catch (error) {
    renderError(error);
  } finally {
    setLoading(false);
  }
}

async function loadBacktest() {
  setBacktestLoading(true);
  try {
    const params = new URLSearchParams({
      months: elements.backtestMonths.value,
      top: elements.backtestTop.value,
      benchmark: elements.backtestBenchmark.value,
    });
    const staticKey = `${elements.backtestMonths.value}-${elements.backtestTop.value}-${elements.backtestBenchmark.value}`;
    state.backtest = await fetchJsonWithFallback(
      `/api/backtest?${params.toString()}`,
      [`/data/backtest-${staticKey}.json`, "/data/backtest-12-5-SPY.json"],
      "백테스트를 불러오지 못했습니다."
    );
    renderBacktest();
  } catch (error) {
    renderBacktestError(error);
  } finally {
    setBacktestLoading(false);
  }
}

async function loadSnapshots() {
  setSnapshotLoading(true);
  try {
    state.snapshots = await fetchJsonWithFallback(
      "/api/snapshots?limit=30",
      ["/data/snapshots.json"],
      "스냅샷 기록을 불러오지 못했습니다."
    );
    renderSnapshots();
  } catch (error) {
    renderSnapshotError(error);
  } finally {
    setSnapshotLoading(false);
  }
}

function render() {
  const report = state.report;
  if (!report) return;

  elements.createdAt.textContent = report.createdAt;
  elements.newsStatus.textContent = report.dataQuality.liveNews ? "실시간" : "샘플";
  elements.marketStatus.textContent = report.dataQuality.liveMarketData ? "실시간" : "중립";
  elements.fundamentalStatus.textContent = report.dataQuality.liveFundamentals ? "공식 반영" : "샘플";
  elements.topTicker.textContent = report.stocks[0]
    ? `${report.stocks[0].ticker} ${formatScore(report.stocks[0].score)}`
    : "-";
  elements.macroContext.textContent = report.macroContext;

  renderWarnings();
  renderMacroSnapshot();
  renderIndustryFilter();
  renderIndustries();
  renderStocks();
  renderShortTerm();
  renderMediumTerm();
  renderEarlyGrowth();
  renderNews();
  renderOverview();
}

function renderBacktest() {
  const backtest = state.backtest;
  if (!backtest) return;

  elements.backtestSummary.innerHTML = `
    ${backtestMetric("전략 누적", formatReturn(backtest.strategyReturnPct, true))}
    ${backtestMetric(`${escapeHtml(backtest.benchmarkTicker)} 누적`, formatReturn(backtest.benchmarkReturnPct, true))}
    ${backtestMetric("초과수익", formatReturn(backtest.alphaPct, true), backtest.alphaPct)}
    ${backtestMetric("월 승률", formatReturn(backtest.winRatePct))}
    ${backtestMetric("최대낙폭", formatReturn(backtest.maxDrawdownPct))}
    ${backtestMetric("변동성", formatReturn(backtest.volatilityPct))}
    ${backtestMetric("데이터", formatReturn(backtest.dataCoveragePct))}
  `;

  elements.backtestBenchmarks.innerHTML = (backtest.benchmarks || [])
    .map(
      (item) => `
        <span class="benchmark-chip">
          ${escapeHtml(item.ticker)}
          <strong>${formatReturn(item.returnPct, true)}</strong>
        </span>
      `
    )
    .join("");

  const periods = backtest.periods || [];
  if (periods.length === 0) {
    elements.backtestPeriods.innerHTML = `<div class="empty-state">검증 가능한 구간이 없습니다.</div>`;
  } else {
    elements.backtestPeriods.innerHTML = `
      <div class="period-head">
        <span>구간</span>
        <span>선정 종목</span>
        <span>전략</span>
        <span>비교</span>
        <span>초과</span>
      </div>
      ${periods
        .slice()
        .reverse()
        .slice(0, 8)
        .map((period) => periodRow(period))
        .join("")}
    `;
  }

  const notes = [...(backtest.warnings || []), ...(backtest.assumptions || [])];
  elements.backtestNotes.innerHTML = notes
    .map((note) => `<span class="warning-chip">${escapeHtml(note)}</span>`)
    .join("");
  renderOverview();
}

function renderSnapshots() {
  const snapshots = state.snapshots;
  if (!snapshots) return;

  elements.snapshotSummary.innerHTML = `
    ${snapshotMetric("누적 기록", `${snapshots.uniqueDays || 0}일`)}
    ${snapshotMetric("저장 건수", `${snapshots.snapshotCount || 0}건`)}
    ${snapshotMetric("준비도", formatReturn(snapshots.readinessScore))}
    ${snapshotMetric("상태", snapshots.coverageLabel || "기록 없음")}
    ${snapshotMetric("필요 기준", `${snapshots.minimumDaysForPointInTimeBacktest || 30}일`)}
  `;

  const rows = snapshots.rows || [];
  if (!rows.length) {
    elements.snapshotList.innerHTML = `<div class="empty-state">아직 저장된 추천 스냅샷이 없습니다. CLI에서 스냅샷을 저장하면 여기에 쌓입니다.</div>`;
    return;
  }

  elements.snapshotList.innerHTML = rows
    .map((row) => {
      const topStocks = (row.topStocks || [])
        .slice(0, 5)
        .map((stock) => {
          return `<span>${escapeHtml(stock.ticker)} ${formatScore(stock.score)} · ${escapeHtml(stock.decisionGrade)}</span>`;
        })
        .join("");
      const coverage = row.liveCoverage || {};
      return `
        <article class="snapshot-card">
          <div class="snapshot-card-head">
            <div>
              <strong>${escapeHtml(row.snapshotDate)} · ${escapeHtml(row.mode)}</strong>
              <small>${escapeHtml(row.createdAt || "")}</small>
            </div>
            <span>${escapeHtml(row.topTicker || "-")} ${formatScore(row.topScore)}</span>
          </div>
          <div class="snapshot-tags">
            <span class="${coverage.news ? "ok" : ""}">뉴스</span>
            <span class="${coverage.market ? "ok" : ""}">시장</span>
            <span class="${coverage.fundamentals ? "ok" : ""}">재무</span>
            <span class="${coverage.macro ? "ok" : ""}">거시</span>
          </div>
          <div class="snapshot-stocks">${topStocks}</div>
        </article>
      `;
    })
    .join("");
  renderOverview();
}

function renderMacroSnapshot() {
  const snapshot = state.report.macroSnapshot;
  elements.macroSnapshot.innerHTML = "";
  if (!snapshot) {
    return;
  }
  const scoreCards = [
    ["성장", snapshot.growthScore],
    ["방어", snapshot.defensiveScore],
    ["인프라", snapshot.infrastructureScore],
    ["환율", snapshot.koreaFxScore],
  ]
    .map(([label, score]) => `<div class="macro-score"><span>${label}</span><strong>${formatScore(score)}</strong></div>`)
    .join("");
  const indicators = snapshot.indicators
    .map(
      (item) => `
        <div class="macro-indicator">
          <span>${escapeHtml(item.source)}</span>
          <strong>${escapeHtml(item.name)}</strong>
          <em>${formatIndicatorValue(item.value, item.unit)}</em>
          <small>${escapeHtml(item.latestDate || "-")} · ${escapeHtml(item.note || "")}</small>
        </div>
      `
    )
    .join("");
  elements.macroSnapshot.innerHTML = `
    <div class="macro-summary">${escapeHtml(snapshot.summary)}</div>
    <div class="macro-score-grid">${scoreCards}</div>
    <div class="macro-indicator-grid">${indicators}</div>
  `;
}

function renderWarnings() {
  const warnings = state.report.dataQuality.warnings;
  elements.warnings.innerHTML = "";
  warnings.forEach((warning) => {
    const chip = document.createElement("span");
    chip.className = "warning-chip";
    chip.textContent = warning;
    elements.warnings.appendChild(chip);
  });
  if (state.report.dataQuality.configuredSources?.length) {
    const chip = document.createElement("span");
    chip.className = "warning-chip source-chip";
    chip.textContent = `연결됨: ${state.report.dataQuality.configuredSources.join(", ")}`;
    elements.warnings.appendChild(chip);
  }
}

function renderIndustryFilter() {
  const current = elements.industryFilter.value || "all";
  elements.industryFilter.innerHTML = `<option value="all">전체</option>`;
  state.report.industries.forEach((industry) => {
    const option = document.createElement("option");
    option.value = industry.name;
    option.textContent = industry.name;
    elements.industryFilter.appendChild(option);
  });
  if ([...elements.industryFilter.options].some((option) => option.value === current)) {
    elements.industryFilter.value = current;
    state.industryFilter = current;
  } else {
    state.industryFilter = "all";
  }
}

function renderIndustries() {
  elements.industryList.innerHTML = "";
  state.report.industries.slice(0, 5).forEach((industry, index) => {
    const card = document.createElement("article");
    card.className = "industry-card";
    card.innerHTML = `
      <div class="industry-head">
        <h3>${index + 1}. ${escapeHtml(industry.name)}</h3>
        <span class="score">${formatScore(industry.score)}</span>
      </div>
      <p>${escapeHtml(industry.description)}</p>
      ${scoreBar(industry.score)}
      <div class="mini-bars">
        ${miniBar("거시", industry.macroScore)}
        ${miniBar("뉴스", industry.newsScore)}
        ${miniBar("시장", industry.marketScore)}
      </div>
    `;
    elements.industryList.appendChild(card);
  });
}

function renderStocks() {
  const stocks = state.report.stocks.filter((stock) => {
    return state.industryFilter === "all" || stock.industry === state.industryFilter;
  });
  elements.stockList.innerHTML = "";

  if (stocks.length === 0) {
    elements.stockList.innerHTML = `<div class="empty-state">표시할 종목이 없습니다.</div>`;
    return;
  }

  if (!stocks.some((stock) => stock.ticker === state.selectedTicker)) {
    state.selectedTicker = stocks[0].ticker;
  }

  stocks.slice(0, 10).forEach((stock, index) => {
    const button = document.createElement("button");
    button.className = `stock-card ${stock.ticker === state.selectedTicker ? "active" : ""}`;
    button.type = "button";
    button.innerHTML = `
      <div class="stock-card-inner">
        <div class="stock-head">
          <h3>${index + 1}. ${escapeHtml(stock.name)} (${escapeHtml(stock.ticker)})</h3>
          <span class="score">${formatScore(stock.score)}</span>
        </div>
        <div class="stock-meta">
          <span class="tag decision">${escapeHtml(stock.decisionGrade)}</span>
          <span class="tag">${escapeHtml(stock.industry)}</span>
          <span class="tag role">${escapeHtml(stock.role)}</span>
          <span class="tag style">${escapeHtml(stock.analysisStyle || "분석")}</span>
          <span class="tag risk">리스크 ${escapeHtml(stock.riskLevel)}</span>
          ${stock.shortTerm ? `<span class="tag short-term">${escapeHtml(stock.shortTerm.signalLabel)}</span>` : ""}
          ${stock.mediumTerm ? `<span class="tag medium-term">${escapeHtml(stock.mediumTerm.signalLabel)}</span>` : ""}
          ${stock.earlyGrowth ? `<span class="tag growth">${escapeHtml(stock.earlyGrowth.entryLabel)}</span>` : ""}
        </div>
        <div class="stock-scores">
          ${scoreTile("산업", stock.industryScore)}
          ${scoreTile("기본", stock.qualityScore)}
          ${scoreTile("밸류", stock.valuationScore)}
          ${scoreTile("모멘텀", stock.momentumScore)}
        </div>
      </div>
    `;
    button.addEventListener("click", () => {
      state.selectedTicker = stock.ticker;
      renderStocks();
      if (state.currentPage === "stocks") {
        window.location.hash = "detail";
      }
    });
    elements.stockList.appendChild(button);
  });

  renderDetail();
}

function renderMediumTerm() {
  if (!elements.mediumTermList) return;
  const candidates = state.report.mediumTermCandidates || [];
  elements.mediumTermList.innerHTML = "";

  if (!candidates.length) {
    elements.mediumTermList.innerHTML = `<div class="empty-state">중기 후보가 없습니다.</div>`;
    return;
  }

  candidates.slice(0, 12).forEach((candidate, index) => {
    const stock = state.report.stocks.find((item) => item.ticker === candidate.ticker) || candidate;
    const technicalStock = { ...candidate, technical: stock.technical || candidate.technical };
    const button = document.createElement("button");
    button.className = `stock-card medium-term-card ${candidate.ticker === state.selectedTicker ? "active" : ""}`;
    button.type = "button";
    button.innerHTML = `
      <div class="stock-card-inner">
        <div class="stock-head">
          <h3>${index + 1}. ${escapeHtml(candidate.name)} (${escapeHtml(candidate.ticker)})</h3>
          <span class="score">${formatScore(candidate.score)}</span>
        </div>
        <div class="stock-meta">
          <span class="tag medium-term">${escapeHtml(candidate.signalLabel)}</span>
          <span class="tag">${escapeHtml(candidate.industry)}</span>
          <span class="tag decision">${escapeHtml(candidate.timeHorizon || "2주~3개월")}</span>
          <span class="tag risk">리스크 ${escapeHtml(candidate.riskLevel)}</span>
          ${technicalStock.technical ? technicalTopBadge(technicalStock) : ""}
        </div>
        <p class="candidate-reason">${escapeHtml((candidate.reasons || [])[0] || pickPrimaryReason(stock))}</p>
        <div class="stock-scores medium-term-scores">
          ${scoreTile("기업", candidate.companyScore)}
          ${scoreTile("시장", candidate.marketScore)}
          ${scoreTile("차트", candidate.chartScore)}
          ${scoreTile("뉴스", candidate.newsScore)}
        </div>
      </div>
    `;
    button.addEventListener("click", () => {
      state.selectedTicker = candidate.ticker;
      renderStocks();
      renderShortTerm();
      renderMediumTerm();
      renderEarlyGrowth();
      window.location.hash = "detail";
    });
    elements.mediumTermList.appendChild(button);
  });
}

function renderShortTerm() {
  if (!elements.shortTermList) return;
  const candidates = state.report.shortTermCandidates || [];
  elements.shortTermList.innerHTML = "";

  if (!candidates.length) {
    elements.shortTermList.innerHTML = `<div class="empty-state">단기 후보가 없습니다.</div>`;
    return;
  }

  candidates.slice(0, 12).forEach((candidate, index) => {
    const stock = state.report.stocks.find((item) => item.ticker === candidate.ticker) || candidate;
    const technicalStock = { ...candidate, technical: stock.technical || candidate.technical };
    const button = document.createElement("button");
    button.className = `stock-card short-term-card ${candidate.ticker === state.selectedTicker ? "active" : ""}`;
    button.type = "button";
    button.innerHTML = `
      <div class="stock-card-inner">
        <div class="stock-head">
          <h3>${index + 1}. ${escapeHtml(candidate.name)} (${escapeHtml(candidate.ticker)})</h3>
          <span class="score">${formatScore(candidate.score)}</span>
        </div>
        <div class="stock-meta">
          <span class="tag short-term">${escapeHtml(candidate.signalLabel)}</span>
          <span class="tag">${escapeHtml(candidate.industry)}</span>
          <span class="tag decision">${escapeHtml(candidate.timeHorizon || "당일~2주")}</span>
          <span class="tag risk">리스크 ${escapeHtml(candidate.riskLevel)}</span>
          ${technicalStock.technical ? technicalTopBadge(technicalStock) : ""}
        </div>
        <p class="candidate-reason">${escapeHtml((candidate.reasons || [])[0] || pickPrimaryReason(stock))}</p>
        <div class="stock-scores short-term-scores">
          ${scoreTile("뉴스", candidate.newsScore)}
          ${scoreTile("시장", candidate.marketScore)}
          ${scoreTile("차트", candidate.chartScore)}
          ${scoreTile("기업", candidate.companyScore)}
        </div>
      </div>
    `;
    button.addEventListener("click", () => {
      state.selectedTicker = candidate.ticker;
      renderStocks();
      renderShortTerm();
      renderMediumTerm();
      renderEarlyGrowth();
      window.location.hash = "detail";
    });
    elements.shortTermList.appendChild(button);
  });
}

function renderEarlyGrowth() {
  if (!elements.earlyGrowthList) return;
  const candidates = state.report.earlyGrowthCandidates || [];
  elements.earlyGrowthList.innerHTML = "";

  if (!candidates.length) {
    elements.earlyGrowthList.innerHTML = `<div class="empty-state">저점 성장주 후보가 없습니다.</div>`;
    return;
  }

  candidates.slice(0, 12).forEach((candidate, index) => {
    const stock = state.report.stocks.find((item) => item.ticker === candidate.ticker) || candidate;
    const technicalStock = { ...candidate, technical: stock.technical || candidate.technical };
    const button = document.createElement("button");
    button.className = `stock-card early-growth-card ${candidate.ticker === state.selectedTicker ? "active" : ""}`;
    button.type = "button";
    button.innerHTML = `
      <div class="stock-card-inner">
        <div class="stock-head">
          <h3>${index + 1}. ${escapeHtml(candidate.name)} (${escapeHtml(candidate.ticker)})</h3>
          <span class="score">${formatScore(candidate.score)}</span>
        </div>
        <div class="stock-meta">
          <span class="tag growth">${escapeHtml(candidate.entryLabel)}</span>
          <span class="tag">${escapeHtml(candidate.industry)}</span>
          <span class="tag decision">종합 ${formatScore(candidate.baseScore)}</span>
          <span class="tag risk">리스크 ${escapeHtml(candidate.riskLevel)}</span>
          ${technicalStock.technical ? technicalTopBadge(technicalStock) : ""}
        </div>
        <p class="candidate-reason">${escapeHtml((candidate.reasons || [])[0] || pickPrimaryReason(stock))}</p>
        <div class="stock-scores early-growth-scores">
          ${scoreTile("규모", candidate.sizeScore)}
          ${scoreTile("성장", candidate.growthScore)}
          ${scoreTile("저점", candidate.pullbackScore)}
          ${scoreTile("재무", candidate.qualityAnchorScore)}
          ${scoreTile("밸류", candidate.valuationAnchorScore)}
        </div>
      </div>
    `;
    button.addEventListener("click", () => {
      state.selectedTicker = candidate.ticker;
      renderStocks();
      renderShortTerm();
      renderMediumTerm();
      renderEarlyGrowth();
      window.location.hash = "detail";
    });
    elements.earlyGrowthList.appendChild(button);
  });
}

function renderDetail() {
  const stock = state.report.stocks.find((item) => item.ticker === state.selectedTicker);
  if (!stock) return;

  elements.detailTitle.textContent = `${stock.name} (${stock.ticker})`;
  elements.detailScore.textContent = formatScore(stock.score);
  elements.detailMeta.innerHTML = `
    <span class="tag decision">${escapeHtml(stock.decisionGrade)}</span>
    <span class="tag">${escapeHtml(stock.industry)}</span>
    <span class="tag role">${escapeHtml(stock.role)}</span>
    <span class="tag style">${escapeHtml(stock.analysisStyle || "분석")}</span>
    <span class="tag risk">리스크 ${escapeHtml(stock.riskLevel)}</span>
    <span class="tag valuation">${escapeHtml(stock.valuationLabel)}</span>
    ${stock.shortTerm ? `<span class="tag short-term">${escapeHtml(stock.shortTerm.signalLabel)}</span>` : ""}
    ${stock.mediumTerm ? `<span class="tag medium-term">${escapeHtml(stock.mediumTerm.signalLabel)}</span>` : ""}
    ${stock.earlyGrowth ? `<span class="tag growth">${escapeHtml(stock.earlyGrowth.entryLabel)}</span>` : ""}
  `;
  elements.detailInsight.textContent = pickPrimaryReason(stock);
  elements.detailScoreGrid.innerHTML = `
    ${detailScoreTile("산업", stock.industryScore)}
    ${detailScoreTile("기본적 분석", stock.qualityScore)}
    ${detailScoreTile("밸류에이션", stock.valuationScore)}
    ${detailScoreTile("모멘텀", stock.momentumScore)}
    ${stock.shortTerm ? detailScoreTile("단기", stock.shortTerm.score) : ""}
    ${stock.mediumTerm ? detailScoreTile("중기", stock.mediumTerm.score) : ""}
    ${stock.earlyGrowth ? detailScoreTile("저점 성장", stock.earlyGrowth.score) : ""}
  `;
  renderTechnical(stock);
  elements.metricGrid.innerHTML = metricItems(stock).join("");
  elements.reasonList.innerHTML = [
    `투자 판단: ${stock.decisionGrade}`,
    stock.shortTerm ? `단기 분류: ${stock.shortTerm.signalLabel} (${formatScore(stock.shortTerm.score)}점, ${stock.shortTerm.timeHorizon})` : null,
    stock.mediumTerm ? `중기 분류: ${stock.mediumTerm.signalLabel} (${formatScore(stock.mediumTerm.score)}점, ${stock.mediumTerm.timeHorizon})` : null,
    stock.earlyGrowth ? `저점 성장 분류: ${stock.earlyGrowth.entryLabel} (${formatScore(stock.earlyGrowth.score)}점)` : null,
    `밸류에이션 해석: ${stock.valuationNote || stock.valuationLabel}`,
    `약식 적정 시총 범위: ${formatValuationRange(stock)}`,
    ...(stock.shortTerm?.reasons || []),
    ...(stock.mediumTerm?.reasons || []),
    ...(stock.earlyGrowth?.reasons || []),
    ...stock.reasons,
  ]
    .filter(Boolean)
    .map((reason) => `<li>${escapeHtml(reason)}</li>`)
    .join("");
  elements.analysisCheckList.innerHTML = (stock.analysisChecks || [])
    .map((check) => `<li>${escapeHtml(check)}</li>`)
    .join("");
  elements.secondOrderList.innerHTML = (stock.secondOrderChecks || [])
    .map((check) => `<li>${escapeHtml(check)}</li>`)
    .join("");
  elements.issueList.innerHTML = detailIssues(stock)
    .map((issue) => `<li>${escapeHtml(issue)}</li>`)
    .join("");
  elements.riskList.innerHTML = [
    ...(stock.shortTerm?.cautions || []),
    ...(stock.mediumTerm?.cautions || []),
    ...(stock.earlyGrowth?.cautions || []),
    ...stock.cautions,
  ]
    .map((risk) => `<li>${escapeHtml(risk)}</li>`)
    .join("");
}

function renderNews() {
  const news = state.report.news;
  elements.newsList.innerHTML = "";
  if (!news.length) {
    elements.newsList.innerHTML = `<div class="empty-state">라이브 모드에서 뉴스가 표시됩니다.</div>`;
    return;
  }
  news.forEach((item) => {
    const card = document.createElement(item.url ? "a" : "div");
    card.className = "news-card";
    if (item.url) {
      card.href = item.url;
      card.target = "_blank";
      card.rel = "noreferrer";
    }
    card.innerHTML = `
      <strong>${escapeHtml(item.title)}</strong>
      <span>${escapeHtml(item.source || "News")}</span>
    `;
    elements.newsList.appendChild(card);
  });
}

function renderOverview() {
  if (!state.report) return;

  renderTopRecommendationList(
    elements.usTopList,
    state.report.stocks.filter((stock) => stock.country === "US").slice(0, 3),
    "미국 추천 종목이 없습니다."
  );
  renderTopRecommendationList(
    elements.krTopList,
    state.report.stocks.filter((stock) => stock.country === "KR").slice(0, 3),
    "국내 추천 종목이 없습니다."
  );

  if (state.backtest) {
    elements.overviewBacktest.textContent = `${formatReturn(state.backtest.strategyReturnPct, true)} / 초과 ${formatReturn(
      state.backtest.alphaPct,
      true
    )}`;
  }

  if (state.snapshots) {
    elements.overviewSnapshots.textContent = `${state.snapshots.uniqueDays || 0}일 · ${
      state.snapshots.coverageLabel || "기록 없음"
    }`;
  }
}

function renderTopRecommendationList(container, stocks, emptyText) {
  if (!container) return;
  if (!stocks.length) {
    container.innerHTML = `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
    return;
  }

  container.innerHTML = stocks
    .map(
      (stock, index) => `
        <button class="top-stock-button" type="button" data-ticker="${escapeHtml(stock.ticker)}">
          <span class="rank">${index + 1}</span>
          <span class="top-stock-main">
            <strong>${escapeHtml(stock.name)}</strong>
            <small>${escapeHtml(stock.ticker)} · ${escapeHtml(stock.industry)}</small>
            <small class="top-stock-reason">${escapeHtml(pickPrimaryReason(stock))}</small>
          </span>
          <span class="top-stock-score">
            <strong>${formatScore(stock.score)}</strong>
            <small>${escapeHtml(stock.decisionGrade)}</small>
            ${technicalTopBadge(stock)}
          </span>
        </button>
      `
    )
    .join("");

  container.querySelectorAll(".top-stock-button").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedTicker = button.dataset.ticker;
      renderStocks();
      window.location.hash = "detail";
    });
  });
}

function renderTechnical(stock) {
  if (!elements.technicalTrend || !elements.technicalChart || !elements.technicalMetrics) return;

  const technical = stock.technical || null;
  const trendLabel = technical?.trendLabel || "데이터 부족";
  elements.technicalTrend.className = `trend-pill ${technicalToneClass(trendLabel)}`;
  elements.technicalTrend.textContent = trendLabel;

  if (!hasTechnicalPrices(technical)) {
    elements.technicalChart.innerHTML = `<div class="empty-state">차트 데이터 부족</div>`;
    elements.technicalMetrics.innerHTML = [
      technicalMetric("RSI 14", "N/A", "가격 데이터 부족"),
      technicalMetric("52주 위치", "N/A", "고가/저가 계산 전"),
      technicalMetric("1개월", "N/A"),
      technicalMetric("3개월", "N/A"),
      technicalMetric("6개월", "N/A"),
    ].join("");
    return;
  }

  elements.technicalChart.innerHTML = buildTechnicalSvg(technical);
  elements.technicalMetrics.innerHTML = [
    technicalMetric("RSI 14", formatOneDecimal(technical.rsi14), rsiLabel(technical.rsi14)),
    technicalMetric(
      "52주 위치",
      formatPct(technical.rangePositionPct),
      `${formatPriceValue(technical.fiftyTwoWeekLow)} - ${formatPriceValue(technical.fiftyTwoWeekHigh)}`,
      { position: technical.rangePositionPct }
    ),
    technicalMetric("1개월", formatReturn(technical.oneMonthReturnPct, true)),
    technicalMetric("3개월", formatReturn(technical.threeMonthReturnPct, true)),
    technicalMetric("6개월", formatReturn(technical.sixMonthReturnPct, true)),
  ].join("");
}

function buildTechnicalSvg(technical) {
  const prices = Array.isArray(technical.prices) ? technical.prices : [];
  const ma20 = Array.isArray(technical.ma20) ? technical.ma20 : [];
  const ma60 = Array.isArray(technical.ma60) ? technical.ma60 : [];
  const ma120 = Array.isArray(technical.ma120) ? technical.ma120 : [];
  const values = [
    ...seriesValues(prices, "close"),
    ...seriesValues(ma20, "value"),
    ...seriesValues(ma60, "value"),
    ...seriesValues(ma120, "value"),
  ];
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const padding = Math.max((maxValue - minValue) * 0.08, maxValue * 0.01, 1);
  const scale = {
    width: 720,
    height: 300,
    left: 42,
    right: 24,
    top: 20,
    bottom: 38,
    min: minValue - padding,
    max: maxValue + padding,
    total: prices.length,
  };
  scale.innerWidth = scale.width - scale.left - scale.right;
  scale.innerHeight = scale.height - scale.top - scale.bottom;

  const gridRows = [0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => {
      const y = scale.top + scale.innerHeight * ratio;
      const value = scale.max - (scale.max - scale.min) * ratio;
      return `
        <line class="chart-grid" x1="${scale.left}" y1="${y.toFixed(1)}" x2="${scale.width - scale.right}" y2="${y.toFixed(1)}"></line>
        <text class="chart-axis-label" x="${scale.left - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end">${escapeHtml(
          formatPriceValue(value)
        )}</text>
      `;
    })
    .join("");
  const firstDate = prices[0]?.date || "";
  const lastDate = prices[prices.length - 1]?.date || "";

  return `
    <svg viewBox="0 0 ${scale.width} ${scale.height}" role="img" aria-label="최근 1년 가격 차트">
      <rect class="chart-bg" x="0" y="0" width="${scale.width}" height="${scale.height}" rx="8"></rect>
      ${gridRows}
      <path class="chart-line price" d="${chartPath(prices, "close", scale)}"></path>
      ${chartPathElement(ma20, "ma20", "value", scale)}
      ${chartPathElement(ma60, "ma60", "value", scale)}
      ${chartPathElement(ma120, "ma120", "value", scale)}
      <text class="chart-date-label" x="${scale.left}" y="${scale.height - 10}">${escapeHtml(shortDate(firstDate))}</text>
      <text class="chart-date-label" x="${scale.width - scale.right}" y="${scale.height - 10}" text-anchor="end">${escapeHtml(
        shortDate(lastDate)
      )}</text>
      <g class="chart-legend" transform="translate(${scale.width - 304}, ${scale.top + 4})">
        ${legendItem(0, "price", "종가")}
        ${legendItem(68, "ma20", "MA20")}
        ${legendItem(138, "ma60", "MA60")}
        ${legendItem(208, "ma120", "MA120")}
      </g>
    </svg>
  `;
}

function chartPathElement(points, className, valueKey, scale) {
  const path = chartPath(points, valueKey, scale);
  return path ? `<path class="chart-line ${className}" d="${path}"></path>` : "";
}

function chartPath(points, valueKey, scale) {
  if (!Array.isArray(points) || !points.length) return "";
  let path = "";
  let drawing = false;
  points.forEach((point, index) => {
    const value = numericValue(point?.[valueKey]);
    if (!Number.isFinite(value)) {
      drawing = false;
      return;
    }
    const x = scaleX(index, scale);
    const y = scaleY(value, scale);
    path += `${drawing ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)} `;
    drawing = true;
  });
  return path.trim();
}

function scaleX(index, scale) {
  if (scale.total <= 1) return scale.left + scale.innerWidth / 2;
  return scale.left + (index / (scale.total - 1)) * scale.innerWidth;
}

function scaleY(value, scale) {
  if (scale.max <= scale.min) return scale.top + scale.innerHeight / 2;
  return scale.top + ((scale.max - value) / (scale.max - scale.min)) * scale.innerHeight;
}

function legendItem(x, className, label) {
  return `
    <g transform="translate(${x}, 0)">
      <line class="chart-legend-line ${className}" x1="0" y1="5" x2="18" y2="5"></line>
      <text x="24" y="9">${escapeHtml(label)}</text>
    </g>
  `;
}

function seriesValues(points, valueKey) {
  if (!Array.isArray(points)) return [];
  return points.map((point) => numericValue(point?.[valueKey])).filter((value) => Number.isFinite(value));
}

function numericValue(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function hasTechnicalPrices(technical) {
  return seriesValues(technical?.prices, "close").length >= 2;
}

function technicalMetric(label, value, note = "", options = {}) {
  const position = numericValue(options.position);
  const rangeBar = Number.isFinite(position)
    ? `<span class="range-track"><span style="width:${safeScore(position)}%"></span></span>`
    : "";
  return `
    <div class="technical-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      ${note ? `<small>${escapeHtml(note)}</small>` : ""}
      ${rangeBar}
    </div>
  `;
}

function technicalTopBadge(stock) {
  const technical = stock.technical;
  const label = technicalSummary(technical);
  return `<span class="top-stock-tech ${technicalToneClass(technical?.trendLabel)}">${escapeHtml(label)}</span>`;
}

function technicalSummary(technical) {
  if (!technical) return "기술 데이터 부족";
  const trend = technical.trendLabel || "데이터 부족";
  if (Number.isFinite(technical.rsi14)) {
    return `${trend} · RSI ${technical.rsi14.toFixed(0)}`;
  }
  return trend;
}

function technicalToneClass(label) {
  if (label === "상승 추세") return "trend-up";
  if (label === "하락 추세") return "trend-down";
  if (label === "중립") return "trend-neutral";
  return "trend-missing";
}

function rsiLabel(value) {
  if (!Number.isFinite(value)) return "데이터 부족";
  if (value >= 70) return "과열권";
  if (value <= 30) return "침체권";
  return "중립권";
}

function pickPrimaryReason(stock) {
  const reasons = stock.reasons || [];
  return (
    reasons.find((reason) => {
      return (
        !reason.includes("산업의") &&
        !reason.includes("기본적 분석") &&
        !reason.includes("가격 모멘텀")
      );
    }) ||
    reasons[0] ||
    "점수화 모델 기준 상위 후보로 분류되었습니다."
  );
}

function detailIssues(stock) {
  if (stock.recentIssues?.length) {
    return stock.recentIssues.slice(0, 4);
  }

  const news = state.report?.news || [];
  const ticker = stock.ticker.toLowerCase();
  const name = stock.name.toLowerCase();
  const industry = stock.industry.toLowerCase();
  const matchedNews = news.filter((item) => {
    const title = `${item.title || ""} ${item.source || ""}`.toLowerCase();
    return title.includes(ticker) || title.includes(name) || title.includes(industry);
  });
  if (matchedNews.length) {
    return matchedNews.slice(0, 4).map((item) => `${item.title} - ${item.source || "News"}`);
  }

  const structuralIssues = (stock.reasons || []).filter((reason) => {
    return !reason.includes("기본적 분석") && !reason.includes("가격 모멘텀");
  });
  if (structuralIssues.length) {
    return structuralIssues.slice(0, 4);
  }

  return ["라이브 뉴스가 부족해 현재는 산업/재무/모멘텀 근거를 중심으로 확인합니다."];
}

function showPage(pageId) {
  const nextPage = PAGE_IDS.has(pageId) ? pageId : "home";
  state.currentPage = nextPage;

  elements.pageViews.forEach((view) => {
    const isActive = view.dataset.page === nextPage;
    view.classList.toggle("active", isActive);
    view.setAttribute("aria-hidden", isActive ? "false" : "true");
  });

  elements.pageLinks.forEach((link) => {
    const isActive = link.dataset.pageLink === nextPage;
    link.classList.toggle("active", isActive);
    link.setAttribute("aria-current", isActive ? "page" : "false");
  });

  window.scrollTo({ top: 0, behavior: "smooth" });
}

function pageFromHash() {
  return window.location.hash.replace("#", "") || "home";
}

function renderError(error) {
  elements.stockList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
}

function renderBacktestError(error) {
  elements.backtestSummary.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  elements.backtestBenchmarks.innerHTML = "";
  elements.backtestPeriods.innerHTML = "";
  elements.backtestNotes.innerHTML = "";
}

function renderSnapshotError(error) {
  elements.snapshotSummary.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  elements.snapshotList.innerHTML = "";
}

async function fetchJsonWithFallback(primaryUrl, fallbackUrls, message) {
  const urls = window.STATIC_DATA_ONLY ? fallbackUrls : [primaryUrl, ...fallbackUrls];
  let lastError = null;
  for (const url of urls) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`${url} ${response.status}`);
      }
      return await response.json();
    } catch (error) {
      lastError = error;
    }
  }
  throw new Error(lastError?.message ? `${message} (${lastError.message})` : message);
}

function setLoading(isLoading) {
  elements.loading.classList.toggle("visible", isLoading);
  elements.refreshButton.disabled = isLoading;
}

function setBacktestLoading(isLoading) {
  elements.backtestButton.disabled = isLoading;
  elements.backtestButton.textContent = isLoading ? "계산 중" : "실행";
}

function setSnapshotLoading(isLoading) {
  elements.snapshotRefreshButton.disabled = isLoading;
  elements.snapshotRefreshButton.textContent = isLoading ? "확인 중" : "기록 새로고침";
}

function snapshotMetric(label, value) {
  return `
    <div class="snapshot-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function backtestMetric(label, value, rawValue = null) {
  const tone =
    rawValue === null || !Number.isFinite(rawValue)
      ? ""
      : rawValue > 0
        ? " positive"
        : rawValue < 0
          ? " negative"
          : "";
  return `
    <div class="backtest-metric${tone}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function periodRow(period) {
  return `
    <div class="period-row">
      <span>${escapeHtml(period.startDate)} → ${escapeHtml(period.endDate)}</span>
      <span>${escapeHtml((period.tickers || []).join(", "))}</span>
      <strong>${formatReturn(period.returnPct, true)}</strong>
      <strong>${formatReturn(period.benchmarkReturnPct, true)}</strong>
      <strong class="${period.alphaPct >= 0 ? "positive-text" : "negative-text"}">${formatReturn(period.alphaPct, true)}</strong>
    </div>
  `;
}

function scoreBar(score) {
  return `<div class="score-bar"><div class="score-fill" style="width:${safeScore(score)}%"></div></div>`;
}

function miniBar(label, score) {
  return `
    <div class="mini-bar">
      <span>${label}</span>
      <div class="mini-track"><div class="mini-fill" style="width:${safeScore(score)}%"></div></div>
      <strong>${formatScore(score)}</strong>
    </div>
  `;
}

function scoreTile(label, score) {
  return `<div><span>${label}</span><strong>${formatScore(score)}</strong></div>`;
}

function detailScoreTile(label, score) {
  return `
    <div class="detail-score-tile">
      <span>${escapeHtml(label)}</span>
      <strong>${formatScore(score)}</strong>
      ${scoreBar(score)}
    </div>
  `;
}

function metricItems(stock) {
  const fundamentals = stock.fundamentals;
  const currency = fundamentals.marketCapCurrency || "USD";
  return [
    ["매출성장", formatPct(fundamentals.revenueGrowthPct)],
    ["영업이익률", formatPct(fundamentals.operatingMarginPct)],
    ["ROE", formatPct(fundamentals.roePct)],
    ["부채비율", formatPct(fundamentals.debtToEquityPct)],
    ["유동비율", formatPct(fundamentals.currentRatioPct)],
    ["이자보상", formatMultiple(fundamentals.interestCoverage)],
    ["매출액", formatFinancialAmount(fundamentals.revenue, currency)],
    ["영업이익", formatFinancialAmount(fundamentals.operatingIncome, currency)],
    ["EBITDA", formatFinancialAmount(fundamentals.ebitda, currency)],
    ["순이익", formatFinancialAmount(fundamentals.netIncome, currency)],
    ["영업현금흐름", formatFinancialAmount(fundamentals.operatingCashFlow, currency)],
    ["FCF", formatFinancialAmount(fundamentals.freeCashFlow, currency)],
    ["PER", formatMultiple(fundamentals.pe)],
    ["Forward PER", formatMultiple(fundamentals.forwardPe)],
    ["시가총액", formatMarketCap(fundamentals.marketCapUsd, currency)],
    ["적정 시총 하단", formatFinancialAmount(stock.valuationRange?.marketCapLow, currency)],
    ["적정 시총 상단", formatFinancialAmount(stock.valuationRange?.marketCapHigh, currency)],
    ["상승여력 범위", formatPctRange(stock.valuationRange?.upsideLowPct, stock.valuationRange?.upsideHighPct)],
  ].map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`);
}

function formatScore(value) {
  return Number.isFinite(value) ? value.toFixed(1) : "-";
}

function safeScore(value) {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function formatPct(value) {
  return Number.isFinite(value) ? `${value.toFixed(1)}%` : "N/A";
}

function formatPctRange(low, high) {
  if (!Number.isFinite(low) || !Number.isFinite(high)) return "N/A";
  return `${low.toFixed(1)}%~${high.toFixed(1)}%`;
}

function formatValuationRange(stock) {
  const valuationRange = stock.valuationRange;
  const currency = stock.fundamentals?.marketCapCurrency || "USD";
  if (!valuationRange || !Number.isFinite(valuationRange.marketCapLow) || !Number.isFinite(valuationRange.marketCapHigh)) {
    return valuationRange?.note || "계산 가능한 데이터 부족";
  }
  return `${formatFinancialAmount(valuationRange.marketCapLow, currency)}~${formatFinancialAmount(
    valuationRange.marketCapHigh,
    currency
  )} · ${valuationRange.profitMetric} x ${formatMultiple(valuationRange.multipleLow)}~${formatMultiple(
    valuationRange.multipleHigh
  )} · 여력 ${formatPctRange(valuationRange.upsideLowPct, valuationRange.upsideHighPct)}`;
}

function formatReturn(value, signed = false) {
  if (!Number.isFinite(value)) return "N/A";
  const prefix = signed && value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(1)}%`;
}

function formatOneDecimal(value) {
  return Number.isFinite(value) ? value.toFixed(1) : "N/A";
}

function formatPriceValue(value) {
  if (!Number.isFinite(value)) return "N/A";
  return value.toLocaleString(undefined, {
    maximumFractionDigits: value >= 100 ? 0 : 2,
  });
}

function shortDate(value) {
  if (typeof value !== "string" || value.length < 10) return "";
  return value.slice(5, 10).replace("-", "/");
}

function formatMultiple(value) {
  return Number.isFinite(value) ? `${value.toFixed(1)}x` : "N/A";
}

function formatMarketCap(value, currency = "USD") {
  if (!Number.isFinite(value)) return "N/A";
  if (currency === "KRW") {
    if (value >= 1_000_000_000_000) return `${(value / 1_000_000_000_000).toFixed(1)}조원`;
    return `${(value / 100_000_000).toFixed(0)}억원`;
  }
  if (value >= 1_000_000_000_000) return `$${(value / 1_000_000_000_000).toFixed(2)}T`;
  return `$${(value / 1_000_000_000).toFixed(1)}B`;
}

function formatFinancialAmount(value, currency = "USD") {
  if (!Number.isFinite(value)) return "N/A";
  const sign = value < 0 ? "-" : "";
  const absValue = Math.abs(value);
  if (currency === "KRW") {
    if (absValue >= 1_000_000_000_000) return `${sign}${(absValue / 1_000_000_000_000).toFixed(1)}조원`;
    if (absValue >= 100_000_000) return `${sign}${(absValue / 100_000_000).toFixed(0)}억원`;
    return `${value.toLocaleString()}원`;
  }
  if (absValue >= 1_000_000_000_000) return `${sign}$${(absValue / 1_000_000_000_000).toFixed(2)}T`;
  if (absValue >= 1_000_000_000) return `${sign}$${(absValue / 1_000_000_000).toFixed(1)}B`;
  if (absValue >= 1_000_000) return `${sign}$${(absValue / 1_000_000).toFixed(1)}M`;
  return `${sign}$${absValue.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function formatIndicatorValue(value, unit) {
  if (!Number.isFinite(value)) return "N/A";
  const suffix = unit === "index" ? "" : unit;
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

showPage(pageFromHash());
loadReport();
loadBacktest();
loadSnapshots();
