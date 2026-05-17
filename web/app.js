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
  overviewTopStocks: document.querySelector("#overviewTopStocks"),
  overviewTopIndustries: document.querySelector("#overviewTopIndustries"),
  overviewBacktest: document.querySelector("#overviewBacktest"),
  overviewSnapshots: document.querySelector("#overviewSnapshots"),
  macroContext: document.querySelector("#macroContext"),
  macroSnapshot: document.querySelector("#macroSnapshot"),
  warnings: document.querySelector("#warnings"),
  industryList: document.querySelector("#industryList"),
  stockList: document.querySelector("#stockList"),
  industryFilter: document.querySelector("#industryFilter"),
  detailTitle: document.querySelector("#detailTitle"),
  detailScore: document.querySelector("#detailScore"),
  metricGrid: document.querySelector("#metricGrid"),
  reasonList: document.querySelector("#reasonList"),
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

const PAGE_IDS = new Set(["home", "backtest", "snapshots", "macro", "industries", "stocks", "detail", "news"]);

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
          <span class="tag risk">리스크 ${escapeHtml(stock.riskLevel)}</span>
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

function renderDetail() {
  const stock = state.report.stocks.find((item) => item.ticker === state.selectedTicker);
  if (!stock) return;

  elements.detailTitle.textContent = `${stock.name} (${stock.ticker})`;
  elements.detailScore.textContent = `${stock.decisionGrade} ${formatScore(stock.score)}`;
  elements.metricGrid.innerHTML = metricItems(stock.fundamentals).join("");
  elements.reasonList.innerHTML = [
    `투자 판단 ${stock.decisionGrade}, 리스크 ${stock.riskLevel}, 밸류에이션 ${stock.valuationLabel}`,
    ...stock.reasons,
  ]
    .map((reason) => `<li>${escapeHtml(reason)}</li>`)
    .join("");
  elements.riskList.innerHTML = stock.cautions.map((risk) => `<li>${escapeHtml(risk)}</li>`).join("");
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

  const topStocks = state.report.stocks
    .slice(0, 3)
    .map((stock) => `${stock.ticker} ${formatScore(stock.score)}`)
    .join(" · ");
  const topIndustries = state.report.industries
    .slice(0, 2)
    .map((industry) => industry.name)
    .join(" · ");

  elements.overviewTopStocks.textContent = topStocks || "-";
  elements.overviewTopIndustries.textContent = topIndustries || "-";

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

function metricItems(fundamentals) {
  return [
    ["매출성장", formatPct(fundamentals.revenueGrowthPct)],
    ["영업이익률", formatPct(fundamentals.operatingMarginPct)],
    ["ROE", formatPct(fundamentals.roePct)],
    ["부채비율", formatPct(fundamentals.debtToEquityPct)],
    ["PER", formatMultiple(fundamentals.pe)],
    ["Forward PER", formatMultiple(fundamentals.forwardPe)],
    ["시가총액", formatMarketCap(fundamentals.marketCapUsd, fundamentals.marketCapCurrency)],
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

function formatReturn(value, signed = false) {
  if (!Number.isFinite(value)) return "N/A";
  const prefix = signed && value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(1)}%`;
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
