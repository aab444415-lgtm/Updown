import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowUpDown,
  BarChart3,
  Gauge,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  TrendingUp,
  Trophy,
} from "lucide-react";

type LegendKey = "lynch" | "oneil" | "greenblatt" | "fisher";
type HorizonKey = "overall" | "short" | "medium" | "long";

declare global {
  interface Window {
    STATIC_DATA_ONLY?: boolean;
  }
}

type LegendScores = Record<LegendKey, number>;

type Fundamentals = {
  revenueGrowthPct: number | null;
  operatingMarginPct: number | null;
  roePct: number | null;
  debtToEquityPct: number | null;
  pe: number | null;
  forwardPe: number | null;
  marketCap: number | null;
  marketCapCurrency: string;
  freeCashFlow: number | null;
  operatingIncome: number | null;
};

type Stock = {
  ticker: string;
  name: string;
  industry: string;
  role: string;
  score: number;
  country: string;
  currency: string;
  decisionGrade: string;
  riskLevel: string;
  valuationLabel: string;
  analysisStyle: string;
  fundamentals: Fundamentals;
  legendScores: LegendScores | null;
  legendCompositeScore: number | null;
  legendReasons: string[];
  legendWarnings: string[];
  reasons: string[];
  cautions: string[];
  technical?: TechnicalSnapshot | null;
  shortTerm?: TermCandidate | null;
  mediumTerm?: TermCandidate | null;
  longTerm?: TermCandidate | null;
};

type TechnicalSnapshot = {
  trendLabel?: string;
  rsi14: number | null;
  ma20DistancePct: number | null;
  ma60DistancePct: number | null;
  volumeRatio: number | null;
  twentyDayBreakoutPct: number | null;
};

type TermCandidate = {
  ticker: string;
  name: string;
  industry: string;
  score: number;
  baseScore: number;
  signalLabel: string;
  setupLabel?: string;
  timeHorizon: string;
  newsScore: number;
  marketScore: number;
  chartScore: number;
  volumeScore?: number;
  companyScore: number;
  confidenceScore?: number;
  confidenceLabel?: string;
  decisionGrade: string;
  riskLevel: string;
  reasons: string[];
  cautions: string[];
};

type DataQuality = {
  liveNews: boolean;
  liveMarketData: boolean;
  liveFundamentals: boolean;
  liveMacro: boolean;
  liveKoreaFundamentals: boolean;
  warnings: string[];
};

type Report = {
  createdAtDisplay: string;
  macroContext: string;
  dataQuality: DataQuality;
  stocks: Stock[];
  shortTermCandidates: TermCandidate[];
  mediumTermCandidates: TermCandidate[];
  longTermCandidates: TermCandidate[];
  legendCandidates: Array<{
    ticker: string;
    name: string;
    legendCompositeScore: number;
    legendScores: LegendScores;
  }>;
};

type BacktestResult = {
  horizon: HorizonKey;
  months: number;
  topN: number;
  benchmarkTicker: string;
  strategyReturnPct: number | null;
  benchmarkReturnPct: number | null;
  alphaPct: number | null;
  winRatePct: number | null;
  maxDrawdownPct: number | null;
  dataCoveragePct: number | null;
  snapshotCoveragePct: number | null;
  warnings: string[];
  assumptions?: string[];
};

type WeightedStock = Stock & { weightedLegendScore: number };

const STRATEGIES: Array<{ key: LegendKey; label: string; short: string; tone: string }> = [
  { key: "lynch", label: "피터 린치", short: "PEG 성장", tone: "violet" },
  { key: "oneil", label: "오닐 CANSLIM", short: "성장 모멘텀", tone: "teal" },
  { key: "greenblatt", label: "그린블라트", short: "수익률 가치", tone: "amber" },
  { key: "fisher", label: "필립 피셔", short: "장기 품질", tone: "rose" },
];

const DEFAULT_WEIGHTS: Record<LegendKey, number> = {
  lynch: 25,
  oneil: 35,
  greenblatt: 25,
  fisher: 15,
};

export default function App() {
  const [report, setReport] = useState<Report | null>(null);
  const [selectedTicker, setSelectedTicker] = useState("");
  const [weights, setWeights] = useState(DEFAULT_WEIGHTS);
  const [query, setQuery] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [backtestHorizon, setBacktestHorizon] = useState<HorizonKey>("short");
  const [backtest, setBacktest] = useState<BacktestResult | null>(null);
  const [isBacktestLoading, setIsBacktestLoading] = useState(false);

  const loadReport = async () => {
    setIsLoading(true);
    setError("");
    try {
      const nextReport = await fetchReport();
      setReport(nextReport);
      setSelectedTicker((current) => current || nextReport.stocks[0]?.ticker || "");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "리포트를 불러오지 못했습니다.");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void loadReport();
  }, []);

  const runBacktest = async () => {
    setIsBacktestLoading(true);
    try {
      setBacktest(await fetchBacktest(backtestHorizon));
    } catch (caught) {
      setBacktest({
        horizon: backtestHorizon,
        months: 12,
        topN: 5,
        benchmarkTicker: "SPY",
        strategyReturnPct: null,
        benchmarkReturnPct: null,
        alphaPct: null,
        winRatePct: null,
        maxDrawdownPct: null,
        dataCoveragePct: null,
        snapshotCoveragePct: null,
        warnings: [caught instanceof Error ? caught.message : "백테스트를 불러오지 못했습니다."],
      });
    } finally {
      setIsBacktestLoading(false);
    }
  };

  const rankedStocks = useMemo(() => {
    if (!report) return [];
    return report.stocks
      .map((stock) => ({
        ...stock,
        weightedLegendScore: weightedScore(stock.legendScores, stock.legendCompositeScore, weights),
      }))
      .filter((stock) => {
        const text = `${stock.ticker} ${stock.name} ${stock.industry}`.toLowerCase();
        return text.includes(query.trim().toLowerCase());
      })
      .sort((a, b) => b.weightedLegendScore - a.weightedLegendScore);
  }, [report, query, weights]);

  const selectedStock =
    rankedStocks.find((stock) => stock.ticker === selectedTicker) || rankedStocks[0] || null;

  useEffect(() => {
    if (rankedStocks.length && !rankedStocks.some((stock) => stock.ticker === selectedTicker)) {
      setSelectedTicker(rankedStocks[0].ticker);
    }
  }, [rankedStocks, selectedTicker]);

  if (isLoading && !report) {
    return <LoadingView />;
  }

  if (error && !report) {
    return (
      <main className="app-shell centered">
        <section className="empty-panel">
          <AlertTriangle aria-hidden="true" />
          <h1>리포트 로드 실패</h1>
          <p>{error}</p>
          <button className="icon-button primary" type="button" onClick={loadReport}>
            <RefreshCw size={17} aria-hidden="true" />
            다시 시도
          </button>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Legend Quant Screener</p>
          <h1>투자 전설 전략 스크리너</h1>
        </div>
        <div className="topbar-actions">
          <StatusPill label="뉴스" active={report?.dataQuality.liveNews} />
          <StatusPill label="시장" active={report?.dataQuality.liveMarketData} />
          <StatusPill label="재무" active={report?.dataQuality.liveFundamentals} />
          <button className="icon-button" type="button" onClick={loadReport} disabled={isLoading}>
            <RefreshCw size={17} aria-hidden="true" />
            새로고침
          </button>
        </div>
      </header>

      {report && (
        <>
          <section className="summary-grid" aria-label="전략별 상위 후보">
            <CombinedLeader stock={rankedStocks[0]} createdAt={report.createdAtDisplay} />
            {STRATEGIES.map((strategy) => (
              <StrategyLeader
                key={strategy.key}
                strategy={strategy}
                stock={topByStrategy(report.stocks, strategy.key)}
              />
            ))}
          </section>

          <TermDashboard report={report} onSelect={setSelectedTicker} />

          <BacktestPanel
            horizon={backtestHorizon}
            result={backtest}
            isLoading={isBacktestLoading}
            onHorizonChange={setBacktestHorizon}
            onRun={runBacktest}
          />

          <section className="workspace-grid">
            <aside className="control-column">
              <WeightPanel weights={weights} onChange={setWeights} />
              <RankingPanel
                stocks={rankedStocks}
                selectedTicker={selectedStock?.ticker || ""}
                query={query}
                onQueryChange={setQuery}
                onSelect={setSelectedTicker}
              />
            </aside>

            <section className="detail-column">
              {selectedStock ? (
                <>
                  <StockDetail stock={selectedStock} />
                  <StrategyMatrix stocks={rankedStocks.slice(0, 8)} />
                </>
              ) : (
                <section className="empty-panel">
                  <Search aria-hidden="true" />
                  <h2>검색 결과 없음</h2>
                </section>
              )}
            </section>
          </section>
        </>
      )}
    </main>
  );
}

function LoadingView() {
  return (
    <main className="app-shell">
      <header className="topbar skeleton-head">
        <div>
          <div className="skeleton-line short" />
          <div className="skeleton-line title" />
        </div>
        <div className="skeleton-line button" />
      </header>
      <section className="summary-grid">
        {Array.from({ length: 5 }, (_, index) => (
          <div className="summary-card skeleton-card" key={index}>
            <div className="skeleton-line short" />
            <div className="skeleton-line title" />
            <div className="skeleton-line" />
          </div>
        ))}
      </section>
    </main>
  );
}

function StatusPill({ label, active }: { label: string; active?: boolean }) {
  return (
    <span className={`status-pill ${active ? "active" : ""}`}>
      {label}
      <strong>{active ? "연결" : "보조"}</strong>
    </span>
  );
}

function TermDashboard({ report, onSelect }: { report: Report; onSelect: (ticker: string) => void }) {
  return (
    <section className="term-dashboard" aria-label="기간별 추천 후보">
      <CandidateColumn
        title="단기 스윙 후보"
        eyebrow="Short"
        candidates={report.shortTermCandidates || []}
        onSelect={onSelect}
      />
      <CandidateColumn
        title="중기 추세 후보"
        eyebrow="Medium"
        candidates={report.mediumTermCandidates || []}
        onSelect={onSelect}
      />
      <CandidateColumn
        title="장기 투자 후보"
        eyebrow="Long"
        candidates={report.longTermCandidates || []}
        onSelect={onSelect}
      />
    </section>
  );
}

function CandidateColumn({
  title,
  eyebrow,
  candidates,
  onSelect,
}: {
  title: string;
  eyebrow: string;
  candidates: TermCandidate[];
  onSelect: (ticker: string) => void;
}) {
  return (
    <section className="panel term-panel">
      <div className="panel-title">
        <TrendingUp size={18} aria-hidden="true" />
        <h2>{title}</h2>
        <span>{eyebrow}</span>
      </div>
      <div className="term-list">
        {candidates.slice(0, 3).map((candidate, index) => (
          <button
            className="term-card"
            key={candidate.ticker}
            type="button"
            onClick={() => onSelect(candidate.ticker)}
          >
            <span className="rank-number">{index + 1}</span>
            <span className="term-main">
              <strong>
                {candidate.ticker} · {candidate.name}
              </strong>
              <small>{candidate.signalLabel}</small>
            </span>
            <strong className="rank-score">{formatScore(candidate.score)}</strong>
            <span className="term-tags">
              {candidate.setupLabel ? <em>{candidate.setupLabel}</em> : null}
              {candidate.confidenceLabel ? <em>신뢰도 {candidate.confidenceLabel}</em> : null}
            </span>
            <span className="term-score-grid">
              <ScoreMini label="차트" value={candidate.chartScore} />
              {candidate.volumeScore === undefined ? null : (
                <ScoreMini label="거래량" value={candidate.volumeScore} />
              )}
              <ScoreMini label="시장" value={candidate.marketScore} />
              <ScoreMini label="뉴스" value={candidate.newsScore} />
              <ScoreMini label="기업" value={candidate.companyScore} />
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

function ScoreMini({ label, value }: { label: string; value: number }) {
  return (
    <span>
      {label}
      <strong>{formatScore(value)}</strong>
    </span>
  );
}

function BacktestPanel({
  horizon,
  result,
  isLoading,
  onHorizonChange,
  onRun,
}: {
  horizon: HorizonKey;
  result: BacktestResult | null;
  isLoading: boolean;
  onHorizonChange: (horizon: HorizonKey) => void;
  onRun: () => void;
}) {
  return (
    <section className="panel backtest-panel">
      <div className="panel-title">
        <BarChart3 size={18} aria-hidden="true" />
        <h2>기간별 모델 검증</h2>
        <span>{horizonLabel(horizon)}</span>
      </div>
      <div className="backtest-controls">
        <label>
          <span>검증 대상</span>
          <select value={horizon} onChange={(event) => onHorizonChange(event.target.value as HorizonKey)}>
            <option value="overall">종합</option>
            <option value="short">단기</option>
            <option value="medium">중기</option>
            <option value="long">장기</option>
          </select>
        </label>
        <button className="icon-button primary" type="button" disabled={isLoading} onClick={onRun}>
          <RefreshCw size={17} aria-hidden="true" />
          {isLoading ? "계산 중" : "백테스트"}
        </button>
      </div>
      <div className="backtest-metrics">
        <Metric label="전략 누적" value={formatPct(result?.strategyReturnPct ?? null)} icon="trend" />
        <Metric label="초과수익" value={formatPct(result?.alphaPct ?? null)} icon="bar" />
        <Metric label="월 승률" value={formatPct(result?.winRatePct ?? null)} icon="shield" />
        <Metric label="최대낙폭" value={formatPct(result?.maxDrawdownPct ?? null)} icon="alert" />
        <Metric label="데이터" value={formatPct(result?.dataCoveragePct ?? null)} icon="gauge" />
        <Metric label="스냅샷" value={formatPct(result?.snapshotCoveragePct ?? null)} icon="bar" />
      </div>
      {result?.warnings?.length ? (
        <div className="warning-strip">
          {result.warnings.slice(0, 3).map((warning) => (
            <span key={warning}>{warning}</span>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function CombinedLeader({ stock, createdAt }: { stock?: WeightedStock; createdAt: string }) {
  return (
    <article className="summary-card combined-card">
      <div className="card-heading">
        <Trophy size={19} aria-hidden="true" />
        <span>가중 종합 1위</span>
      </div>
      <div className="leader-row">
        <div>
          <h2>{stock ? stock.ticker : "-"}</h2>
          <p>{stock ? stock.name : "후보 없음"}</p>
        </div>
        <strong className="score-badge">{formatScore(stock?.weightedLegendScore)}</strong>
      </div>
      <div className="meta-row">
        <span>{stock?.industry || "-"}</span>
        <span>{createdAt}</span>
      </div>
    </article>
  );
}

function StrategyLeader({
  strategy,
  stock,
}: {
  strategy: (typeof STRATEGIES)[number];
  stock?: Stock;
}) {
  const score = stock?.legendScores?.[strategy.key] ?? null;
  return (
    <article className={`summary-card strategy-${strategy.tone}`}>
      <div className="card-heading">
        <Gauge size={18} aria-hidden="true" />
        <span>{strategy.label}</span>
      </div>
      <div className="leader-row compact">
        <div>
          <h2>{stock?.ticker || "-"}</h2>
          <p>{strategy.short}</p>
        </div>
        <strong className="score-badge">{formatScore(score)}</strong>
      </div>
      <ScoreBar value={score ?? 0} />
    </article>
  );
}

function WeightPanel({
  weights,
  onChange,
}: {
  weights: Record<LegendKey, number>;
  onChange: (weights: Record<LegendKey, number>) => void;
}) {
  const total = Object.values(weights).reduce((sum, value) => sum + value, 0);
  return (
    <section className="panel">
      <div className="panel-title">
        <SlidersHorizontal size={18} aria-hidden="true" />
        <h2>전략 가중치</h2>
        <span>{total}%</span>
      </div>
      <div className="slider-list">
        {STRATEGIES.map((strategy) => (
          <label className="slider-row" key={strategy.key}>
            <span>{strategy.label}</span>
            <input
              type="range"
              min="0"
              max="60"
              step="5"
              value={weights[strategy.key]}
              onChange={(event) =>
                onChange({ ...weights, [strategy.key]: Number(event.target.value) })
              }
            />
            <strong>{weights[strategy.key]}%</strong>
          </label>
        ))}
      </div>
    </section>
  );
}

function RankingPanel({
  stocks,
  selectedTicker,
  query,
  onQueryChange,
  onSelect,
}: {
  stocks: WeightedStock[];
  selectedTicker: string;
  query: string;
  onQueryChange: (query: string) => void;
  onSelect: (ticker: string) => void;
}) {
  return (
    <section className="panel ranking-panel">
      <div className="panel-title">
        <ArrowUpDown size={18} aria-hidden="true" />
        <h2>랭킹 비교</h2>
      </div>
      <label className="search-box">
        <Search size={16} aria-hidden="true" />
        <input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="티커, 기업, 산업"
        />
      </label>
      <div className="ranking-list">
        {stocks.map((stock, index) => (
          <button
            className={`ranking-row ${stock.ticker === selectedTicker ? "selected" : ""}`}
            key={stock.ticker}
            type="button"
            onClick={() => onSelect(stock.ticker)}
          >
            <span className="rank-number">{index + 1}</span>
            <span className="rank-main">
              <strong>{stock.ticker}</strong>
              <small>{stock.name}</small>
            </span>
            <span className="rank-score">{formatScore(stock.weightedLegendScore)}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function StockDetail({ stock }: { stock: WeightedStock }) {
  return (
    <article className="detail-panel">
      <div className="detail-head">
        <div>
          <p className="eyebrow">
            {countryLabel(stock.country)} · {stock.industry}
          </p>
          <h2>
            {stock.ticker} <span>{stock.name}</span>
          </h2>
        </div>
        <div className="detail-score">
          <span>가중 점수</span>
          <strong>{formatScore(stock.weightedLegendScore)}</strong>
        </div>
      </div>

      <div className="tag-row">
        <span>{stock.decisionGrade}</span>
        <span>{stock.riskLevel}</span>
        <span>{stock.valuationLabel}</span>
        <span>{stock.analysisStyle}</span>
        {stock.shortTerm?.setupLabel ? <span>{stock.shortTerm.setupLabel}</span> : null}
        {stock.shortTerm?.confidenceLabel ? <span>단기 신뢰도 {stock.shortTerm.confidenceLabel}</span> : null}
      </div>

      <div className="strategy-score-grid">
        {STRATEGIES.map((strategy) => (
          <div className="strategy-tile" key={strategy.key}>
            <span>{strategy.label}</span>
            <strong>{formatScore(stock.legendScores?.[strategy.key])}</strong>
            <ScoreBar value={stock.legendScores?.[strategy.key] ?? 0} />
          </div>
        ))}
      </div>

      <div className="metric-grid">
        <Metric label="매출 성장" value={formatPct(stock.fundamentals.revenueGrowthPct)} icon="trend" />
        <Metric label="영업이익률" value={formatPct(stock.fundamentals.operatingMarginPct)} icon="bar" />
        <Metric label="ROE" value={formatPct(stock.fundamentals.roePct)} icon="shield" />
        <Metric label="부채비율" value={formatPct(stock.fundamentals.debtToEquityPct)} icon="alert" />
        <Metric label="Forward PER" value={formatMultiple(stock.fundamentals.forwardPe)} icon="gauge" />
        <Metric label="시가총액" value={formatMarketCap(stock.fundamentals.marketCap, stock.fundamentals.marketCapCurrency)} icon="bar" />
        <Metric label="RSI 14" value={formatNumber(stock.technical?.rsi14 ?? null)} icon="gauge" />
        <Metric label="거래량" value={formatRatio(stock.technical?.volumeRatio ?? null)} icon="bar" />
        <Metric label="MA20 이격" value={formatPct(stock.technical?.ma20DistancePct ?? null)} icon="trend" />
        <Metric label="20일 돌파" value={formatPct(stock.technical?.twentyDayBreakoutPct ?? null)} icon="trend" />
      </div>

      <div className="insight-grid">
        <section>
          <h3>전략 근거</h3>
          <ul>
            {stock.legendReasons.slice(0, 4).map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </section>
        <section>
          <h3>데이터 한계</h3>
          <ul>
            {stock.legendWarnings.slice(0, 4).map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </section>
      </div>
    </article>
  );
}

function Metric({ label, value, icon }: { label: string; value: string; icon: string }) {
  const Icon =
    icon === "trend"
      ? TrendingUp
      : icon === "shield"
        ? ShieldCheck
        : icon === "alert"
          ? AlertTriangle
          : icon === "gauge"
            ? Gauge
            : BarChart3;
  return (
    <div className="metric-tile">
      <Icon size={17} aria-hidden="true" />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function StrategyMatrix({ stocks }: { stocks: WeightedStock[] }) {
  return (
    <section className="panel matrix-panel">
      <div className="panel-title">
        <BarChart3 size={18} aria-hidden="true" />
        <h2>전략 매트릭스</h2>
      </div>
      <div className="matrix-table">
        <div className="matrix-head">
          <span>종목</span>
          {STRATEGIES.map((strategy) => (
            <span key={strategy.key}>{strategy.short}</span>
          ))}
        </div>
        {stocks.map((stock) => (
          <div className="matrix-row" key={stock.ticker}>
            <strong>{stock.ticker}</strong>
            {STRATEGIES.map((strategy) => (
              <div className="matrix-bar" key={strategy.key}>
                <span style={{ width: `${safeScore(stock.legendScores?.[strategy.key])}%` }} />
              </div>
            ))}
          </div>
        ))}
      </div>
    </section>
  );
}

function ScoreBar({ value }: { value: number }) {
  return (
    <div className="score-track">
      <span style={{ width: `${safeScore(value)}%` }} />
    </div>
  );
}

async function fetchReport(): Promise<Report> {
  const endpoints = window.STATIC_DATA_ONLY
    ? ["/data/report-live.json"]
    : ["/api/report", "/data/report-live.json"];
  let lastError: Error | null = null;
  for (const endpoint of endpoints) {
    try {
      const response = await fetch(endpoint, { cache: "no-store" });
      if (!response.ok) throw new Error(`${endpoint} ${response.status}`);
      return (await response.json()) as Report;
    } catch (caught) {
      lastError = caught instanceof Error ? caught : new Error(String(caught));
    }
  }
  throw lastError || new Error("리포트를 불러오지 못했습니다.");
}

async function fetchBacktest(horizon: HorizonKey): Promise<BacktestResult> {
  const params = new URLSearchParams({
    months: "12",
    top: "5",
    benchmark: "SPY",
    method: "snapshot",
    horizon,
  });
  const endpoints = window.STATIC_DATA_ONLY
    ? [`/data/backtest-12-5-SPY-${horizon}.json`, "/data/backtest-12-5-SPY.json"]
    : [`/api/backtest?${params.toString()}`, `/data/backtest-12-5-SPY-${horizon}.json`];
  let payload: BacktestResult | null = null;
  let lastError: Error | null = null;
  for (const endpoint of endpoints) {
    try {
      const response = await fetch(endpoint, { cache: "no-store" });
      if (!response.ok) throw new Error(`${endpoint} ${response.status}`);
      payload = (await response.json()) as BacktestResult;
      break;
    } catch (caught) {
      lastError = caught instanceof Error ? caught : new Error(String(caught));
    }
  }
  if (!payload) throw lastError || new Error("백테스트를 불러오지 못했습니다.");
  return {
    horizon: payload.horizon ?? horizon,
    months: payload.months ?? 12,
    topN: payload.topN ?? 5,
    benchmarkTicker: payload.benchmarkTicker ?? "SPY",
    strategyReturnPct: payload.strategyReturnPct ?? null,
    benchmarkReturnPct: payload.benchmarkReturnPct ?? null,
    alphaPct: payload.alphaPct ?? null,
    winRatePct: payload.winRatePct ?? null,
    maxDrawdownPct: payload.maxDrawdownPct ?? null,
    dataCoveragePct: payload.dataCoveragePct ?? null,
    snapshotCoveragePct: payload.snapshotCoveragePct ?? null,
    warnings: [...(payload.warnings ?? []), ...(payload.assumptions ?? [])],
  };
}

function weightedScore(
  scores: LegendScores | null,
  fallback: number | null,
  weights: Record<LegendKey, number>,
) {
  if (!scores) return fallback ?? 0;
  const total = Object.values(weights).reduce((sum, value) => sum + value, 0);
  if (total <= 0) return fallback ?? 0;
  return STRATEGIES.reduce((sum, strategy) => {
    return sum + (scores[strategy.key] || 0) * (weights[strategy.key] / total);
  }, 0);
}

function topByStrategy(stocks: Stock[], key: LegendKey) {
  return [...stocks].sort((a, b) => (b.legendScores?.[key] ?? 0) - (a.legendScores?.[key] ?? 0))[0];
}

function safeScore(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function formatScore(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return `${Math.round(value)}점`;
}

function formatPct(value: number | null) {
  return value === null || Number.isNaN(value) ? "-" : `${value.toFixed(1)}%`;
}

function formatReturn(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

function formatNumber(value: number | null) {
  return value === null || Number.isNaN(value) ? "-" : value.toFixed(1);
}

function formatRatio(value: number | null) {
  return value === null || Number.isNaN(value) ? "-" : `${value.toFixed(2)}배`;
}

function formatMultiple(value: number | null) {
  return value === null || Number.isNaN(value) ? "-" : `${value.toFixed(1)}배`;
}

function formatMarketCap(value: number | null, currency: string) {
  if (value === null || Number.isNaN(value)) return "-";
  if (currency === "KRW") {
    return value >= 1_000_000_000_000
      ? `${(value / 1_000_000_000_000).toFixed(1)}조원`
      : `${Math.round(value / 100_000_000)}억원`;
  }
  return value >= 1_000_000_000_000
    ? `$${(value / 1_000_000_000_000).toFixed(2)}T`
    : `$${(value / 1_000_000_000).toFixed(1)}B`;
}

function countryLabel(country: string) {
  if (country === "KR") return "한국";
  if (country === "US") return "미국";
  return country;
}

function horizonLabel(horizon: HorizonKey) {
  if (horizon === "short") return "단기";
  if (horizon === "medium") return "중기";
  if (horizon === "long") return "장기";
  return "종합";
}
