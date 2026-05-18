# 주식 추천 리서치 MVP

세계 뉴스, 거시경제 지표, 산업 모멘텀, 기업 기본 지표를 합쳐 관심 산업과 종목 후보를 정리하는 Python CLI/웹 대시보드 프로그램입니다.

> 투자 판단을 대신하는 자동 매수/매도 도구가 아니라, 리서치 후보를 빠르게 좁히는 보조 도구입니다.
> 기본 종합 점수는 3개월~2년 정도의 중단기~중장기 후보 선별용이며, 단기 매매 후보는 별도 점수로 분리합니다.

## 빠른 실행

```bash
python3 -m stock_recommender.cli
```

웹 대시보드로 보려면:

```bash
python3 -m stock_recommender.web
```

브라우저에서 `http://127.0.0.1:8765`를 열면 됩니다.

Cloudflare Pages 자동 업데이트는 [docs/cloudflare-auto-update.md](docs/cloudflare-auto-update.md)를 참고하세요.

라이브 데이터 수집을 시도하려면:

```bash
python3 -m stock_recommender.cli --live
```

`--live`는 Google News/Yahoo Finance 공개 데이터, SEC EDGAR, OpenDART, FRED, ECOS 갱신을 시도합니다.

특정 산업 개수와 종목 개수를 조정하려면:

```bash
python3 -m stock_recommender.cli --top-industries 5 --top-stocks 4
```

리포트를 파일로 저장하려면:

```bash
python3 -m stock_recommender.cli --output reports/today.md
```

## 검증

```bash
python3 -m unittest
```

API 키 연결 상태를 확인하려면:

```bash
python3 -m stock_recommender.sources_status
```

추천 모델 백테스트를 실행하려면:

```bash
python3 -m stock_recommender.backtest_cli --months 12 --top 5 --benchmark SPY --output reports/backtest_12m.md
```

오늘의 추천 결과를 포인트인타임 스냅샷으로 저장하려면:

```bash
python3 -m stock_recommender.snapshot_cli --live
```

리포트 생성과 동시에 저장하려면:

```bash
python3 -m stock_recommender.cli --live --save-snapshot --output reports/today.md
```

## 1단계 데이터 연결 설정

먼저 예시 환경 파일을 복사합니다.

```bash
cp .env.example .env
```

그 다음 `.env`에서 필요한 키를 채웁니다.

```bash
SEC_USER_AGENT="stock-recommender/0.1 your-email@example.com"
OPENDART_API_KEY=""
FRED_API_KEY=""
ECOS_API_KEY=""
```

- SEC EDGAR: 미국 재무제표용입니다. API 키는 필요 없지만, `SEC_USER_AGENT`에 연락 가능한 이메일을 넣는 것이 좋습니다.
- OpenDART: 한국 재무제표/공시용입니다. 인증키가 필요합니다.
- FRED: 미국 거시경제 지표용입니다. 무료 계정/API 키가 필요합니다.
- ECOS: 한국은행 거시경제 지표용입니다. 인증키가 필요합니다.

현재 구현된 것:

- SEC EDGAR 티커/CIK 매핑 수집
- SEC `companyfacts` 기반 매출성장률, 영업이익률, ROE, 부채비율 갱신
- OpenDART 기반 한국 상장사 연간 재무제표 갱신
- SQLite 캐시 저장소 `data/cache.sqlite`
- OpenDART/FRED/ECOS 클라이언트 기본 구조
- OpenDART/FRED/ECOS 키 응답 상태 진단
- FRED 금리/물가/고용/달러 지표와 ECOS 원달러 환율을 산업 점수에 반영
- 웹/리포트의 데이터 품질 표시

비용 기준:

- SEC EDGAR: 무료
- OpenDART: 원칙적으로 무료
- FRED: 무료 API 키
- ECOS: 일반적으로 무료 인증키
- 안정적인 가격 데이터/뉴스 API: 무료 플랜으로 시작 가능하지만 운영용은 유료 가능

## 2단계 점수화/투자 판단 모델

현재 종목 점수는 산업 점수, 기본적 분석, 밸류에이션, 가격 모멘텀, 핵심/부가 기업 역할을 합산합니다.

- 산업 점수: 거시 테마, 실제 거시지표, 뉴스 언급 강도, 산업 내 가격 모멘텀
- 기본적 분석: 매출 성장률, 영업이익률, ROE, 부채비율, 유동비율, 이자보상비율, 영업현금흐름, FCF
- 확장 재무 데이터: 매출액, 영업이익, EBITDA, 당기순이익, 영업현금흐름, CAPEX, FCF를 SEC EDGAR/OpenDART에서 가능한 범위로 수집
- 밸류에이션: PER/Forward PER을 성장성, 수익성, 부채 부담과 함께 해석한 상대 점수
- 약식 적정가치 범위: 기준 이익과 멀티플 범위를 조합해 적정 시가총액과 현재 시총 대비 여력을 계산
- 분석 프레임: 성장주/가치주/경기민감 저PER/사이클 회복 등 종목별 분석 스타일과 체크리스트
- 단기 매매 후보: 뉴스/이슈 30%, 시장 데이터 35%, 차트 분석 25%, 기업 데이터 10%로 당일~2주 후보를 별도 랭킹
- 저점 성장주 후보: 소형/중소형 규모, 매출 성장, 재무 버팀목, 고점 대비 조정과 단기 반등 여부를 별도 랭킹으로 계산
- 2차적 사고 체크: 산업 성장 지속성, 미래 이익 범위, 적용 멀티플, 선두/후발 경쟁 구도 검토
- 투자 판단 등급: `매수 후보`, `관심`, `관망`, `제외`
- 리스크 등급: `낮음`, `중간`, `높음`

이 등급은 바로 매수하라는 뜻이 아니라, 더 깊게 볼 종목을 추리는 필터입니다. 실제 투자 판단용으로 쓰려면 재무 원천 검증, 가격 데이터 품질, 백테스트, 포트폴리오 규칙, 손절/리밸런싱 규칙이 추가로 필요합니다.

## 3단계 백테스트 실험실

현재는 월말 리밸런싱 기준으로 과거 가격 데이터를 가져와 Top N 동일비중 성과를 계산합니다.

- 기간: 최근 6개월, 12개월, 24개월
- 보유 종목 수: Top 3, Top 5, Top 10
- 비교 대상: SPY, QQQ, KOSPI
- 지표: 전략 누적수익률, 벤치마크 수익률, 초과수익, 월 승률, 최대낙폭, 연율화 변동성

주의할 점:

- 과거 시점의 뉴스/재무 스냅샷이 아직 저장되어 있지 않아, 현재 기본 지표와 과거 가격 모멘텀을 함께 사용하는 검증입니다.
- 거래비용, 세금, 환율 환산, 슬리피지는 아직 반영하지 않습니다.
- 실제 투자 판단용으로 올리려면 매일 추천 점수 스냅샷을 저장한 뒤 그 기록으로 다시 백테스트해야 합니다.

## 4단계 포인트인타임 스냅샷

추천 결과를 `data/cache.sqlite`의 `recommendation_snapshots` 테이블에 일별로 저장합니다. 같은 날짜와 같은 모드의 스냅샷은 최신 결과로 갱신됩니다.

저장되는 항목:

- 날짜와 생성 시각
- 종목별 점수, 투자 판단 등급, 리스크 등급
- 산업 점수와 세부 점수
- 사용한 데이터 소스와 데이터 품질
- 추천 근거, 주의 리스크, 참고 뉴스

웹 대시보드의 `추천 스냅샷 기록` 영역에서 누적 기록일, 준비도, 최근 Top 5, 데이터 커버리지를 확인할 수 있습니다.

## 현재 MVP가 하는 일

- 세계 뉴스 RSS 제목/요약에서 산업 키워드 빈도를 계산합니다.
- Yahoo Finance 공개 quote/chart API에서 가격 모멘텀과 일부 투자 지표를 가져오려고 시도합니다.
- SEC EDGAR와 OpenDART에서 미국/한국 기업 기본 재무를 갱신합니다.
- FRED와 ECOS에서 금리, 물가, 고용, 달러, 원/달러 환율을 가져와 산업 점수에 반영합니다.
- 과거 가격 데이터로 월별 리밸런싱 백테스트를 실행합니다.
- 추천 점수와 근거를 일별 스냅샷으로 저장합니다.
- 네트워크나 API가 실패하면 내장 샘플 지표로 계속 실행됩니다.
- 산업별 핵심 기업과 부가 기업을 분리해서 추천 후보를 만듭니다.
- 중소형/초기 성장 후보를 별도 유니버스와 랭킹으로 추적합니다.
- 단기 매매 후보를 뉴스, 시장 모멘텀, 차트 위치, 기업 데이터 기준으로 따로 정렬합니다.
- 매출 성장, 영업이익률, ROE, 부채비율, 밸류에이션, 가격 모멘텀, 최근 이슈를 점수화합니다.
- 추천 이유, 투자 판단 등급, 주의할 리스크를 한국어 Markdown 리포트와 웹 화면으로 출력합니다.

## 구조

```text
stock_recommender/
  cli.py          # 실행 진입점
  config.py       # .env/API 키 설정
  storage.py      # SQLite 캐시 저장소
  data_sources.py # 뉴스/가격/quote 데이터 수집
  models.py       # 데이터 모델
  official_sources.py # OpenDART/FRED/ECOS 클라이언트
  pipeline.py     # CLI/웹 공통 리포트 생성 흐름
  report.py       # Markdown 리포트 생성
  scoring.py      # 산업/종목 점수화 로직
  sec_edgar.py    # SEC EDGAR 재무제표 수집/파싱
  universe.py     # 기본 산업/종목 유니버스
  web.py          # 웹 대시보드 서버
web/
  index.html      # 대시보드 화면
  styles.css      # 대시보드 스타일
  app.js          # API 호출과 화면 렌더링
```

## 다음에 붙이면 좋은 것

- 한국/미국/일본/유럽 등 시장별 유니버스 확장
- World Bank, OECD, IMF 같은 추가 거시경제 지표 연결
- 분기 재무제표와 컨센서스 추정치 연결
- 뉴스 원문 요약, 감성 분석, 일회성/구조적 이슈 분류
- 저장된 스냅샷 기반의 더 정확한 포인트인타임 백테스트
- 포트폴리오 비중, 손절/리밸런싱 규칙
- 사용자 관심 종목 저장, 알림, 모바일 앱 UI
