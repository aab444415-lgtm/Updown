# Cloudflare 자동 업데이트 설정

이 저장소는 Cloudflare Pages가 빌드할 때 `python3 scripts/export_cloudflare_static.py`를 실행해서 최신 추천 JSON을 생성합니다.

GitHub Actions는 매일 한국시간 06:30에 추천 스냅샷을 `snapshot_store/recommendation_snapshots.json`에 저장해 커밋한 뒤 Cloudflare Pages Deploy Hook을 호출합니다. 그러면 Cloudflare가 다시 빌드하고 사이트의 미국주식 Top3, 국내주식 Top3, 스냅샷 기록, 백테스트 JSON을 새로 만듭니다.

## 1. Cloudflare Deploy Hook 만들기

Cloudflare Dashboard에서:

1. Workers & Pages
2. 해당 Pages 프로젝트 선택
3. Settings
4. Builds and deployments
5. Deploy hooks
6. Add deploy hook
7. 이름 예시: `daily-stock-refresh`
8. Branch: `main`
9. 생성된 URL 복사

## 2. GitHub Secret 등록

GitHub 저장소에서:

1. Settings
2. Secrets and variables
3. Actions
4. New repository secret
5. Name: `CLOUDFLARE_PAGES_DEPLOY_HOOK`
6. Secret: Cloudflare에서 복사한 Deploy Hook URL

스냅샷 생성 품질을 높이려면 같은 위치에 아래 secret도 추가합니다.

```text
SEC_USER_AGENT=stock-recommender/0.1 your-email@example.com
OPENDART_API_KEY=발급받은키
FRED_API_KEY=발급받은키
ECOS_API_KEY=발급받은키
KRX_AUTH_KEY=발급받은키
POLYGON_API_KEY=발급받은키
```

위 값은 GitHub Actions의 일일 스냅샷 생성에 직접 쓰입니다. Secret에 등록하지 않은 키는 Cloudflare 환경변수에 있어도 매일 커밋되는 스냅샷 품질에는 반영되지 않습니다.
`STOCK_RECOMMENDER_POLYGON_FRESH_LIMIT`는 repository variable로 등록하지 않으면 기본값 `4`를 사용합니다.

## 3. Cloudflare 환경변수 확인

Cloudflare Pages 프로젝트의 Environment variables에 아래 값을 넣으면 라이브 데이터 품질이 좋아집니다.

```text
SEC_USER_AGENT=stock-recommender/0.1 your-email@example.com
STOCK_RECOMMENDER_TIMEZONE=Asia/Seoul
STOCK_RECOMMENDER_SNAPSHOT_STORE_PATH=snapshot_store/recommendation_snapshots.json
OPENDART_API_KEY=발급받은키
FRED_API_KEY=발급받은키
ECOS_API_KEY=발급받은키
KRX_AUTH_KEY=발급받은키
POLYGON_API_KEY=발급받은키
```

`POLYGON_API_KEY`, `KRX_AUTH_KEY`, `NEWS_API_KEY`는 아직 필수는 아닙니다.

## 4. 수동 실행 테스트

GitHub 저장소에서:

1. Actions
2. Daily Cloudflare Pages redeploy
3. Run workflow

성공하면 Cloudflare Pages의 Deployments에 새 배포가 생깁니다.

## 일정

- 매일 06:30 KST 자동 실행
- GitHub cron 기준: `30 21 * * *` UTC
