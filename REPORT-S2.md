# S2 Pack & Collector — 라운드 게이트·제약·미해결 리포트

- 작성: 2026-09-01 (Asia/Seoul)
- 범위: B-2~B-11. B-1은 출발점에서 고정 계약으로 인계받았고, B-7은 정책 판정 선행
  조건으로 미착수했다.
- 정본: `codex-prompt-s2-batch-2026-09-01.md` >
  `claude-code-prompt-v2-rebuild-2026-08-27.md` §5 > S2 handoff > `REPORT-S1.md`
- 출발점: `4030f69` = 당시 `origin/main`; 기준선 `ruff 0`, `pytest 270 passed`,
  source 20건, 자격증명 0개, HTTP client 없음
- 종료 구성: B 블록 9개 커밋 + 이 리포트 1개 커밋. B-7 커밋은 없음

---

## §A — S2-A 무자격증명 관통

### 1. 라운드 ID·출발 커밋·종료 커밋

- ID: S2-A
- 출발: `4030f69`
- 종료: `a67c0e4`

### 2. 블록별 상태

| 블록 | 상태 | 결과 |
|---|---|---|
| B-2 | ✅ | World Bank로 authority-stats 수집→정규화→SQLite 적재 관통. KOSIS·data.go.kr는 명시적 dataset spec 없이 호출 불가 |
| B-5 | ✅ | HN Firebase story/comment를 공식 item으로 재검증. Algolia는 후보 ID 탐색용으로만 격리 |
| B-6 | ✅ | Codex/Chrome이 확인한 web-primary를 짧은 excerpt·hash·URL 스냅샷으로 저장하는 전용 경로 |

### 3. 커밋 목록

| 해시 | 메시지 |
|---|---|
| `b3e739e` | `feat(collector): authority-stats 수집과 적재 계약 구현` |
| `a74f617` | `feat(collector): Hacker News 공식 원본 검증 수집 구현` |
| `a67c0e4` | `feat(collector): web-primary 짧은 스냅샷 저장 구현` |

세 커밋 모두 작성 직후 `origin/main`에 push했다.

### 4. 게이트 실측

- 라운드 종료: `ruff check .` 0, `pytest` **297/297 passed**
  (기준선 270 대비 +27)
- 기본 pytest: `tests/conftest.py` 소켓 차단 + `httpx.MockTransport` + fixture,
  네트워크 0건
- S2 종합 라이브 스모크(2026-09-01):

| source | 실행 | 상태 | DB Content | Observation | Metric | Snapshot |
|---|---:|---|---:|---:|---:|---:|
| World Bank | collector 1회 / HTTP 1회 | `completed` | 1 | 1 | 1 | 1 |
| Hacker News | collector 1회 / HTTP 1회 | `completed` | 1 | 1 | 2 | 1 |

World Bank는 `SP.POP.TOTL`, `country=KOR`, `mrv=1`; HN은 공식 Firebase item `8863`을
각각 한 페이지·한 item만 요청했다.

### 5. 정본과 어긋난 지점

1. B-1의 `CollectResult` 고정 계약에 snapshot 필드가 없다. 계약을 바꾸지 않고
   `metadata.snapshots` / `observation_snapshot_refs`로 전달했다. 적재는 동작하지만
   base dataclass 자체가 snapshot ref 타입을 검증하지는 못한다.
2. 소스 단위 Guard 통과 후에 dataset selector를 검증하므로, key가 있는 KOSIS·
   data.go.kr 요청의 selector/승인 실패는 기존 8종 `PolicyBlocked`가 아니라
   pre-HTTP `CollectorContractError`(또는 Pack `source_error` gap)다. 이를 바꾸려면
   금지된 base/Guard 계약 확장이 필요하다.
3. KOSIS spec은 식별자·기간 형식은 검증하지만 dataset policy URL·이용 승인·
   저장 허용을 표현하지 못한다. data.go.kr는 runtime spec에 이 정보를 받지만,
   두 소스 모두 dataset 정책 provenance를 레지스트리에 영속하는 구조는 없다.

### 6. 판정 밖에서 스스로 정한 것과 사유

- HTTP runtime은 `httpx>=0.28,<1` 하나만 추가하고 `ria/http.py`에 timeout·User-Agent·
  URL secret 제거·transport 주입을 모았다.
- KOSIS는 `org_id/table_id/object_l1/item_id/period_type`, data.go.kr는 dataset ID·승인된
  HTTPS endpoint·policy URL·field mapping을 호출자가 명시하게 했다. 실제 식별자는
  추정하지 않았다.
- HN Algolia는 후보 ID만 반환하고, 저장 전 공식 Firebase item으로 재확인하도록
  분리했다.
- web-primary는 Core collector network path가 아닌 `store_web_snapshot` 함수로 제한했고,
  excerpt 상한을 config 기본 1,000자로 두었다.

### 7. 범위 밖 변경

없음. `httpx` 1종·적재 adapter·fixture/tests는 B-2 허용 범위이고, AI SDK·S3
경로는 추가하지 않았다.

---

## §B — S2-B 키 소스 fixture 전용

### 1. 라운드 ID·출발 커밋·종료 커밋

- ID: S2-B
- 출발: `a67c0e4`
- 종료: `4f58b15`

### 2. 블록별 상태

| 블록 | 상태 | 결과 |
|---|---|---|
| B-3 | ✅ | OpenDART 공시 목록·재무·문서 원본을 모드별로 정규화·적재 |
| B-4 | ✅ | Naver API Hub 검색·DataLab·쇼핑인사이트 3종. 상대 지수는 `relative` / `relative_index_0_100`로만 저장 |
| B-9 | ✅ | YouTube `search.list`→`videos.list` 공식 재검증, raw 카운트 지표, snapshot·Observation payload·Metric 30일 retention |

### 3. 커밋 목록

| 해시 | 메시지 |
|---|---|
| `b251d2f` | `feat(collector): OpenDART 공시와 재무 fixture 수집 구현` |
| `7449a9d` | `feat(collector): Naver API Hub 상대지수 수집 구현` |
| `4f58b15` | `feat(collector): YouTube 수집과 30일 retention 구현` |

세 커밋 모두 작성 직후 `origin/main`에 push했다.

### 4. 게이트 실측

- 라운드 종료: `ruff check .` 0, `pytest` **320/320 passed**
  (S2-A 297 대비 +23, 기준선 270 대비 +50)
- OpenDART·Naver·YouTube는 자격증명 0개 환경에서 fixture·MockTransport로만 검증했다.
  실호출 0건은 요청된 승인 경계다.
- YouTube retention: 29일에는 유지, 31일에 snapshot body 제거·Observation payload
  `NULL`·연결 Metric 0행으로 정리되며 HN 행은 영향 0건임을 테스트했다.

### 5. 정본과 어긋난 지점

1. YouTube 지표별 quota unit을 모델링하지 못했다. 현재 estimator는 페이지당
   HTTP 2회로 계산하고, 레지스트리에 일일 quota 수치가 없어 Guard 기본 호출
   상한 50을 쓴다. `search.list`와 `videos.list`의 서로 다른 비용은 반영되지 않는다.
2. 30일 후 raw body·Observation payload·Metric은 정리하지만 공유 ContentItem의
   title·publisher·published_at과 Observation trace 행은 남긴다. 소스별 expiry/
   ownership가 없는 고정 Content 모델에서 안전하게 전건 삭제할 수 없었다.

### 6. 판정 밖에서 스스로 정한 것과 사유

- OpenDART는 하나의 collector에 `disclosures` / `financials` / `document` 모드를 두어
  조사 의도를 명시하고, 재무 수치는 절대 지표로 저장했다.
- Naver 상대 지수의 단위를 `relative_index_0_100`로 고정하고 raw ratio를 절대
  검색량·클릭량으로 변환하는 경로를 열지 않았다.
- YouTube는 후보 검색 응답을 저장 근거로 쓰지 않고 `videos.list` item을 관측·지표의
  공식 원본으로 재조회했다.
- retention은 snapshot linkage를 기준으로 YouTube 행만 정리하고, 다른 소스는
  건드리지 않게 했다. 자동 스케줄링은 S3 Job 범위로 남겼다.

### 7. 범위 밖 변경

없음. `ria/core/snapshots.py`는 B-9의 YouTube retention 확장만 수정했고, 나머지는
지정 collector·fixture·test에 한정했다.

---

## §C — S2-C 게이트 소스·오케스트레이션

### 1. 라운드 ID·출발 커밋·종료 커밋

- ID: S2-C
- 출발: `4f58b15`
- 구현 종료: `2f55c24`
- 리포트·README 종료 커밋: 이 파일을 포함하는 `docs(s2): REPORT-S2`
  (`HEAD`; 커밋 해시를 자기 내용으로 고정할 수 없어 메시지로 식별)

### 2. 블록별 상태

| 블록 | 상태 | 결과 |
|---|---|---|
| B-8 | ✅ | Reddit·Threads Guard 차단과 `core` 전환 후 fixture 진입, Reddit 헤더 기반 rate 상태, Threads 0건 미차감 카운터 |
| B-10 | ✅ | Core Pack 모듈 9개, Lane 8종 매핑, source 실패 격리, query audit, 최신 `source_registry` 20행 동기화 |
| B-11 | ✅ | `collect <pack\|source>`·`query observations/metrics`·`snapshot get`, secret 마스킹, README S2·Privacy 링크 |
| B-7 | **미착수** | 등록 소스가 data.go.kr 1개뿐이고 국가법령정보·정부 부처의 §7 판정·등록이 없음. B-10은 비호출 `not_attempted` 선언만 생성 |

### 3. 커밋 목록

| 해시 | 메시지 |
|---|---|
| `ffc8d13` | `feat(collector): Reddit·Threads 게이트 차단 collector + 쿼터 카운터` |
| `c81afe4` | `feat(packs): Pack 오케스트레이션 + source_registry 스냅샷` |
| `2f55c24` | `feat(cli): collect/pack 명령 + README S2 갱신` |
| `HEAD` | `docs(s2): REPORT-S2` |

앞의 세 커밋은 항목별로 push했고, 마지막 행은 이 리포트를 포함한다.

### 4. 게이트 실측

- 라운드·종합: `ruff check .` **0**, `ruff format --check .` **70 files formatted**,
  `pytest` **362/362 passed** (기준선 270 대비 **+92**)
- B-8 종료 329 passed → B-10 종료 343 passed → B-11 종료 362 passed
- Reddit: `status=blocked`, `access_status_not_allowed`, gap 1건, C/O/M/S `0/0/0/0`,
  소스 HTTP 0건
- Threads: `status=blocked`, `commercial_use_not_permitted`, gap 1건, C/O/M/S
  `0/0/0/0`, 소스 HTTP 0건
- `.env` 없음, 실 credential 0개. `.env.example` Reddit User-Agent는
  `python:ria-core:2.1.0 (by /u/Ambitious-Debt-8876)`
- Core Pack 모듈 9개, `web_primary.py` Pack 모듈 0개, source registry 동기 20행

### 5. 정본과 어긋난 지점

1. Threads 2,200/24h/user 카운터는 프로세스 메모리 dict다. 재시작·다중
   프로세스·다른 앱의 사용량을 합산하지 못하므로 운영상 전역 한도를
   지속 집행하지 못한다.
2. Threads token refresh로 받은 새 token은 현재 수집에만 쓰고 안전한 저장소에
   갱신하지 않는다. `.env` 자동 쓰기는 보안·범위 밖이라 열지 않았다.
3. Threads conditional 상태에서는 `_collect()` 전에 차단되고, `core`로 바꾸면
   representativeness warning이 `None`이다. 따라서 미승인 표본 경고를 수집 결과
   metadata에 넣는 요구와 승인 전 실호출 0 게이트를 현 고정 계약에서 동시에
   표현하지 못했다.
4. DB `source_registry` 는 YAML의 최신 20행을 `source_id` PK upsert한다. `quota`·
   `access_status_note`가 없고 이전 상태를 덮어써서, 특정 query 시점의 정책·쿼터를
   완전히 복원할 수 없다.
5. `fallback_sources` 자동 재귀 라우팅을 구현하지 않았다. source ID와 Pack ID
   (`web-primary`)가 섞이고 Reddit↔Threads·KOSIS↔data.go.kr/World Bank 순환이 있어,
   현 정본으로 자동화하면 중복·순환 호출 위험이 있다. Pack의 다음 ordered
   source 계속 실행만 구현했다.

### 6. 판정 밖에서 스스로 정한 것과 사유

- Threads는 단일 설치·단일 configured user를 기본으로 보고 token과 무관한
  stable subject hash를 쓴다. 다중 사용자는 collector 생성 시 subject를 주입하게 했다.
- Reddit은 쿼터를 하드코딩하지 않고 `X-Ratelimit-Used/Remaining/Reset` 헤더를
  metadata로 남긴다. `remaining<=0`이면 pagination을 끝내고 reset 초를 남기지만,
  sleep/retry는 자동 실행하지 않는다.
- Pack은 단일 source 차단·실패 후 다음 declared source를 계속하고 gap/query audit를
  남긴다. 잘못된 Pack dataset JSON은 source 실행 실패가 아닌 요청 전체 인자
  오류로 fail-fast한다.
- Core Pack은 9개 모듈만 두고 `web-primary`는 모듈을 만들지 않았다. CLI도
  `web-primary`를 실행하지 않고 `store_web_snapshot`을 안내한다.
- CLI는 source와 Pack 옵션 JSON을 분리하고, credential은 옵션으로 받지 않는다.
  조회는 raw SQL을 열지 않고 안전한 필터만 두며 snapshot body는 `--include-body`
  opt-in으로 한정했다. 차단은 exit 0, 실제 source/Pack 실패는 non-zero다.

### 7. 범위 밖 변경

없음. `base.py`·`guard.py`·`registry.py`·`sources.yaml`·`PRIVACY.md`는 변경하지
않았다. `naver_shopping_search` collector·Google Trends 실행·App/Play/Coupang 실행·
`ria.mcp_server`·Job·Validator·AI SDK는 추가하지 않았다.

---

## §종합

### 8. 정책 재확인 기록 + S1 이월 5건 처리 결과

#### 정책 재확인

- 실행일은 2026-09-01이고 가장 빠른 만료일은 2026-09-10이다. 만료 소스가
  없어 `SourceRegistry.set_access_status()`로 날짜를 미는 재확인은 **0건**이다.
- 정책 URL을 읽지 않고 `last_verified_at`을 바꾼 소스는 0건이다.
- 2026-09-10 이후에는 World Bank·HN·HN Algolia·Naver 계열 등의 공식 정책을
  실제로 재확인하고, 빈 `policy_urls`는 공식 약관 URL까지 채운기 전에는
  Guard 만료 판정을 우회하지 않는다.

#### S1 이월 5건

| # | 이월 항목 | 상태 | 결과 |
|---:|---|---|---|
| 1 | YouTube Observation·Metric retention | ✅ | 30일 만료 시 raw body·payload·연결 metric 정리. 자동 스케줄링은 S3 Job 범위 |
| 2 | Threads 쿼터 카운터·0건 미차감 | **부분** | 코드·fixture로 2,200/24h/user·token 회전 후 합산·0건 미차감 통과. 실측·영속성·타 앱 합산 미확인 |
| 3 | `source_registry` 동기화 | **부분** | Pack/source 실행 직전 최신 20행 upsert. 이력·quota·status note·query binding 없음 |
| 4 | dataset 단위 정책 검사 | **부분** | KOSIS/data.go.kr 실제 식별자 없이 호출 불가. 완전한 registry/provenance·KOSIS policy 검증은 없음 |
| 5 | Naver API Hub 이관 note | ✅ | note 유지, `naverapihub.apigw.ntruss.com` 3종 collector, 상대 지수 제약 fixture 검증 |

### 9. 미해결·기원님 확인 필요

현재 미해결 결정 그룹은 **8건**이다.

1. **B-7:** 국가법령정보·정부 부처 source의 DESIGN §7 판정과 Registry 등록이
   선행돼야 한다. 그전에는 `regulation-policy` 실호출 경로를 열지 않는다.
2. **KOSIS:** 실제 사용할 통계표와 `org_id/table_id/object_l1/item_id/period_type`,
   해당 표의 이용·저장 조건을 확정해야 한다.
3. **data.go.kr:** 데이터셋 선택·활용신청 승인 후 dataset ID·공식 endpoint·
   policy URL·응답 field mapping을 인자로 넘겨야 한다.
4. **Reddit:** key 발급만으로는 부족하다. Data Access 승인·credential 설정·
   `access_status=core` 전환이 모두 필요하다. 첫 승인 호출 후
   `X-Ratelimit-Used/Remaining/Reset` 실측값을 기록해야 한다.
5. **Threads:** Meta App Review·`threads_basic`/`threads_keyword_search`·credential·`core`
   전환이 필요하다. 승인 후 0건 미차감을 실측하고 persistent quota store·
   stable user ID·refresh token 안전 갱신 방식을 결정해야 한다.
6. **Coupang Partners·Google Trends:** 실호출 경로 전에 지시서 §8 키 표와
   `.env.example` 키명을 확정해야 한다.
7. **미등록 우선 소스:** OECD, 기업 IR·공식 보도자료, GitHub 공식 자료,
   국가법령정보·정부 부처는 §7 판정·Registry 등록 전에 실행할 수 없다.
8. **S3 전 결정 후보:** source policy 이력/쿼터를 별도 스키마로 남길지,
   혼합·순환 fallback을 어떻게 비순환 규칙으로 정규화할지, YouTube
   ContentItem까지 30일 정리할지 확인이 필요하다.

2026-09-10에 도달하면 위 8건과 별도로 정책 TTL 재확인이 실행 게이트로 추가된다.

### 10. push 상태·태그

- B 블록 9개는 각 커밋 직후 push했다. B-11 push 후 `HEAD = origin/main = 2f55c24`,
  `git log origin/main..HEAD` 출력은 빈 값이었다.
- 이 리포트는 `docs(s2): REPORT-S2`로 push한 뒤 다시 빈 출력을 확인한다.
- 원격 `s1-foundation`: 존재 확인(`dfd2abf`).
- `s2-collectors`: 이 리포트 커밋·push·동기화 재확인이 성공한 후에만
  `HEAD`에 생성·push한다. 작성 시점에 미존재인 태그를 이미 있는 것으로 적지 않는다.

---

## 종료 판정

- S2-A: **PASS**
- S2-B: **PASS**
- S2-C: **PASS** (B-7은 상위 보강문서가 명시한 정책 선행 대기로 미착수)
- S2 종료 게이트: 리포트 push·원격 태그 확인 전까지 **진행 중**
