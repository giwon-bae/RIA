# S1 Foundation — 발견한 제약 · 미해결 리포트

- 작성: 2026-08-27
- 범위: A-1 ~ A-12 (지시서 `claude-code-prompt-v2-rebuild-2026-08-27.md` §4)
- 정본: `~/nia/resources/ideas/ria/DESIGN.md` v2.1 · `~/nia/.codex/config.toml`

---

## 0. 종료 게이트 결과

| 게이트 | 결과 |
|---|---|
| `ruff check` | 0 |
| `ruff format --check` | 33 파일 정렬됨 |
| `pytest` | 263 passed |
| `python -m ria.cli source list` | **20건** |
| `python -m ria.cli source check reddit` | **BLOCKED** / `access_status_not_allowed` (실호출 0) |
| 항목별 커밋 | 12건 (A-1 ~ A-12 각 1건) |
| `git log origin/main..HEAD` 비어 있음 | ❌ **미충족 — 아래 §1 참조** |
| `git tag s1-foundation` | 로컬 생성 완료, **push 미완** |

실호출은 0건이다. HTTP 클라이언트 자체가 의존성에 없다 (`requests`·`httpx`·`urllib3`·
`aiohttp` 미설치). 단위 테스트는 `tests/conftest.py` 에서 소켓 연결을 차단하고,
전역 config 를 자격증명 0개로 갈아끼워 `.env` 유무와 무관하게 통과한다.

`openai` · Responses API 클라이언트 · Agents SDK 는 설치하지도 않았다.

---

## 1. 🔴 push 미완 — 가장 중요한 미해결

**12개 커밋 전부 로컬에만 있다. 원격은 여전히 커밋 0 이다.**

경위는 이렇다. A-1 을 커밋한 직후 `git push -u origin main` 이 실패했다.

```
fatal: could not read Password for 'https://giwon-bae@github.com': Device not configured
```

- origin 이 HTTPS(`https://giwon-bae@github.com/giwon-bae/RIA.git`)인데 osxkeychain 에
  해당 자격증명이 없다.
- `gh` CLI 는 설치돼 있지 않고 `GH_TOKEN`·`GITHUB_TOKEN` 환경변수도 없다.
- `~/.ssh/giwon-bae-GitHub` 키로 **SSH 인증은 정상 동작한다** (`ssh -T git@github.com` →
  `Hi giwon-bae!`).

지시서 §9.4 대로 그 자리에서 멈추고 보고했고, **기원님이 "push는 내가 할게 commit만 해줘"로
결정**했다. 이후 항목은 커밋만 하고 진행했다.

### 기원님이 실행할 것

```bash
cd ~/GitHub/Personal_Project/RIA

# 방법 A — SSH 로 전환 (검증됨)
git remote set-url origin git@github.com:giwon-bae/RIA.git

# 방법 B — HTTPS 유지. 키체인에 PAT 를 넣은 뒤 그대로 push

git push -u origin main
git push origin s1-foundation
```

push 가 끝나면 `git log origin/main..HEAD` 가 비고 S1 게이트가 전부 충족된다.
**이것이 끝나기 전에는 이번 작업물이 2026-07·08 과 같은 소실 위험에 그대로 노출돼 있다.**

---

## 2. 지시서 §10 이 요구한 4개 항목

### 2.1 Reddit rate limit 실측값

**채울 수 없다. 승인 전이라 실호출을 하지 않았고, 하지 않는 것이 설계다.**

Reddit 공식 문서에는 rate limit 수치가 기재돼 있지 않고, 응답 헤더(`X-Ratelimit-Used` ·
`X-Ratelimit-Remaining` · `X-Ratelimit-Reset`)로만 알 수 있다. 헤더는 실제 응답을 받아야
관측되므로 Data Access Request 승인 **이후에만** 채울 수 있다.

현재 상태를 코드로는 이렇게 표현해 뒀다.

- `sources.yaml` → `reddit.rate_limit_model: server_headers`
- `ria/config.py` → `SOURCE_QUOTAS["reddit"] = SourceQuota(limit=None, window_hours=None,
  scope="per_client", note="공식 수치 미기재 — X-Ratelimit-* 응답 헤더 기준.")`
  추측값을 넣지 않았다. `limit=None` 이 "모른다"는 뜻이다.
- Policy Guard 는 `server_headers` 소스에 사전 상한(`config.default_max_calls_per_run`, 50)을
  두되, 허용 판정에 "rate limit 수치가 공식 문서에 없다 — 응답 헤더를 읽어 처리해야 한다"는
  note 를 함께 돌려준다.

**승인 후 할 일**: 첫 성공 호출의 응답 헤더 3종을 그대로 이 문서에 기록하고,
`SOURCE_QUOTAS["reddit"]` 을 관측값으로 갱신한다.

### 2.2 Threads 쿼터에서 0건 응답이 실제로 차감되지 않는가

**공식 문서의 기술은 확인했으나, 실측 검증은 하지 못했다.**

DESIGN §7 Threads 행이 "0건 응답은 미차감"이라고 적고 있고 근거로
<https://developers.facebook.com/docs/threads/keyword-search/> 를 든다. 이 문장은
정본 문서를 통해 확인된 사실이지만, **실제로 카운터가 움직이지 않는지는 App Review 승인
후 연속 호출로 관측해야만 확인된다.** 승인 전 실호출 금지이므로 이번 스테이지에서는 검증
불가다.

현재 상태:

- `sources.yaml` → `threads.quota: {limit: 2200, window_hours: 24, scope: per_user,
  note: "앱 간 합산. 0건 응답은 미차감. limit 파라미터 최대 100."}`
- `ria/config.py` 의 기본값과 이 선언이 어긋나면 테스트가 실패하도록 불변식을 걸어 뒀다
  (`tests/test_sources_yaml.py::test_declared_quota_agrees_with_config_default`).
- 쿼터 **카운터 구현 자체는 아직 없다.** B-8 에서 collector 와 함께 만들어야 하고,
  그때 "0건 응답은 카운터에서 제외"를 코드로 넣어야 한다. S1 은 선언까지다.

**승인 후 할 일**: 0건을 돌려주는 질의를 반복하고 쿼터 잔량이 줄지 않는지 관측한 뒤
결과를 이 문서에 기록한다.

### 2.3 KOSIS · 공공데이터 데이터셋 식별자를 어떻게 확보해야 하는가

**확보 경로를 확인하지 못했다. 그래서 아무것도 하드코딩하지 않았다.**

지시서 B-2 가 "추측한 테이블 ID 를 하드코딩하지 않는다"라고 못 박았고, 정본 문서(DESIGN
§7.1)가 주는 것은 KOSIS OpenAPI 매뉴얼 PDF 링크와 data.go.kr 포털 주소뿐이다. 식별자
체계·발급 절차를 DESIGN 이 서술하지 않으므로 **여기서 추정해 적으면 그것이 곧 오염이다.**

확실한 것만 적는다.

- KOSIS 는 통계표 단위로 식별자가 붙는다. `sources.yaml` 의 `rate_limit_model:
  dataset_specific` · `verify_before_use: true` 가 그 사실을 반영한다.
- 공공데이터포털은 **데이터셋별로 이용허락·트래픽·활용신청 조건이 다르다**(DESIGN §7).
  즉 "포털 키 하나 = 전체 접근"이 아니다. 데이터셋마다 활용신청 승인이 선행돼야 한다.
- 따라서 collector 는 데이터셋 식별자를 **인자로 받아야 하고**, 레지스트리는 데이터셋
  단위 정책 검사를 할 수 있어야 한다. S1 의 소스 단위 레지스트리로는 부족하다.

**S2 착수 전에 해결해야 할 것** (기원님 확인 필요):

1. 조사에 실제로 쓸 KOSIS 통계표를 특정하고 그 식별자를 받아 온다. 매뉴얼 PDF 로
   식별자 체계를 먼저 확인한다.
2. data.go.kr 에서 필요한 데이터셋의 활용신청을 하고, 승인 후 상세 페이지에 표시되는
   엔드포인트와 요청 규격을 그대로 넘겨받는다.
3. 위 둘 중 어느 것도 확정되기 전에는 `authority-stats` Pack 의 end-to-end 관통을
   **World Bank(무인증)로만** 한다. 지시서 §11 의 작업 순서도 그렇게 잡혀 있다.

### 2.4 DESIGN 과 구현이 어긋난 지점 — 전건

아래 §3 에 전건 기록했다.

---

## 3. DESIGN ↔ 구현 불일치 전건

### 3.1 지시서가 이미 해소한 것 (근거 기록용)

| # | DESIGN | 구현 | 근거 |
|---|---|---|---|
| 1 | §16.1 트리가 `mcp_server.py` · `cli.py` 를 레포 루트에 그린다 | `ria/` 패키지 안에 둔다 | `.codex/config.toml` 의 `-m ria.mcp_server` 가 실제 계약. 지시서 §2.3 |
| 2 | §12.2 "MCP 도구 후보" 6개 | config.toml 의 12개가 정본 (S3 구현) | 지시서 §2.4 |
| 3 | §18 로드맵의 `[x]` 표시 | 현재 코드베이스 상태가 아님 (소실 전 이력) | DESIGN 머리말이 직접 경고 |

### 3.2 정본이 침묵해서 구현이 정한 것

| # | 지점 | 구현이 정한 것 | 왜 |
|---|---|---|---|
| 4 | `constraints.personal_data` | DESIGN §4.1 은 `exclude` 만 적는다. `Literal["exclude", "minimal"]` 로 뒀고 기본값은 `exclude` | §15 의 "조사 목적상 필수일 때만 제한적으로 보존" 경로를 표현할 값이 필요했다. 단일 값 Literal 은 계약이 아니다 |
| 5 | EvidencePack 의 `sources` · `conflicts` · `gaps` · `query_log` · `policy_log` | §4.2 는 빈 배열로만 적는다. 필드 구성을 §9.4(보존 목록) · §13.5~13.9 · §14(실패 처리 표)에서 끌어왔다 | 구조 없이는 §13 품질 게이트를 검사할 수 없다 |
| 6 | `rate_limit_model` 어휘 | §8.1 예시가 `server_headers` 하나만 준다. `none` · `daily_quota` · `dataset_specific` · `paid_tier` · `unknown` 을 추가 정의했다 | 정본이 침묵한 소스에는 **`unknown`** 을 쓴다. 추측한 수치를 넣지 않기 위한 값이다 |
| 7 | `access_method` · `commercial_use` · `storage_policy` · `deletion_policy` 어휘 | 마찬가지로 §8.1 예시값을 기준으로 최소 어휘를 정의하고 Literal 로 강제 | 오타 하나가 정책 판정을 조용히 통과시키는 것을 막는다 |
| 8 | 상업 이용 판정 규칙 | `commercial_use ∈ {separate_agreement_required, unclear}` 는 `commercial_context=true` 에서 막되, **`access_status=core` 면 통과**시킨다 | DESIGN 은 이 규칙을 적지 않는다. 지시서 A-6 의 필수 검증("access_status 를 core 로 바꾸면 통과")에서 유도했다. "별도 합의 확보"를 표현하는 수단이 곧 access_status 전환이기 때문이다 |
| 9 | `query_runs` 의 "비용" 컬럼 | `cost_note TEXT` 로 뒀다 | §10.1 이 "비용"이라고만 적고 단위·의미를 정의하지 않는다. 숫자 컬럼으로 만들면 무엇의 비용인지 모른 채 집계될 위험이 있다 |
| 10 | `source_registry` 테이블의 용도 | "사용 시점 정책 스냅샷"으로 해석했다. 정본은 `sources.yaml` 이다 | §10.1 이 테이블 이름만 준다. yaml 과 표가 둘 다 정본이면 어긋날 때 판단 불가다. **아직 이 표를 채우는 코드는 없다 — S2·S3 과제** |
| 11 | `schema_version` | DESIGN 에 없다. 지시서 A-7 요구로 추가 | — |

### 3.3 구현이 DESIGN 보다 **엄격**하게 간 것

| # | 지점 | DESIGN | 구현 | 왜 |
|---|---|---|---|---|
| 12 | Google Trends `access_status` | §7 은 `conditional/experimental` 두 값을 병기 | **`experimental` 단일 값** | 지시서 §7 이 "공식 endpoint 문서 확보 전까지 실행 차단"을 요구한다. Guard 는 `experimental` 을 호출 불가로 본다 |
| 13 | metrics 필드 | §13.2 "적용 가능한 필드를 가진다" | 단위·분모·지역·기간·모집단·측정방법·출처를 **전부 필드로, 생략 불가**(nullable 은 허용) | 지시서 A-3 요구. 필드가 통째로 빠지면 그 수치가 무엇에 대한 것인지 사후 복원이 불가능하다 |
| 14 | `metrics.index_type` | §6.3 은 원칙만 서술 | DB 스키마에 `NOT NULL CHECK (index_type IN ('absolute','relative'))` | 상대 지수를 절대 수치로 표현하는 경로를 스키마 수준에서 막는다 |
| 15 | 상대 지수 소스의 `blocked_data_types` | 정본에 없음 | naver_datalab · naver_shopping_insight · google_trends 에 `absolute_search_volume` / `absolute_click_volume` 을 명시적으로 차단 등록 | 같은 이유 |
| 16 | `signals.representativeness_warning` | §4.2 는 필드만 나열 | **빈 문자열 불가** | §13.4 가 대표성 한계 포함을 요구한다. 비울 수 있으면 비워진다 |
| 17 | `source_observations` UNIQUE 제약 | 명시 없음 | **일부러 두지 않았다** | §10.2 "절대 덮어쓰지 않는다". UNIQUE 가 있으면 재수집이 갱신이 된다 |
| 18 | observations · metrics 모듈의 갱신 함수 | 명시 없음 | **만들지 않았다.** 부재를 테스트로 고정 | 같은 이유 |

### 3.4 구현이 DESIGN 에 **없는 검사**를 추가한 것

| # | 추가 | 왜 |
|---|---|---|
| 19 | Policy Guard 의 `brief_blocklist` 단계 (6단 검사 이전) | §8.2 의 6단에는 없다. 그러나 §4.1 의 `constraints.blocked_sources` 가 어디서도 쓰이지 않으면 죽은 필드가 된다. 정책이 허용해도 조사 요청이 막으면 호출하지 않는다 |
| 20 | `storage_policy=approved_use_only` 소스는 승인(`core`) 전 원본 body 를 저장하지 않음 | §10.4 "정책상 원본 저장이 제한되면 메타데이터·URL·해시만 저장"을 소스 단위 규칙으로 구체화했다 |

### 3.5 레지스트리에 **없는 소스** — S2 의 실제 장애물

DESIGN §5.1 의 Pack 표는 §7 판정표 20행에 없는 소스를 우선 소스로 지목한다.
20행 전건을 시드했으므로 아래는 **레지스트리에 등록되지 않았고, 지금 상태로는 호출할 수 없다.**

| Pack | DESIGN §5.1 이 지목한 소스 | 레지스트리 | 영향 |
|---|---|---|---|
| `authority-stats` | **OECD** | 없음 | S2 에서 쓰려면 §7 판정을 먼저 받아야 한다 |
| `company-market` | 기업 IR, 공식 보도자료 | 없음 | `web-primary` 경로로 흡수되는 것으로 보이나 정본에 명시 없음 |
| `tech-launch` | GitHub 공식 자료 | 없음 | 같음 |
| `regulation-policy` | **국가법령정보**, 정부 부처 | 없음 | 🔴 **B-7 이 지금 상태로는 착수 불가.** 이 Pack 에 등록된 소스는 `data_go_kr` 하나뿐이다 |
| `web-primary` | Web Search, Chrome | 없음 | **의도된 비대칭.** 수집 주체가 Codex 이고 Core 는 저장만 한다 (지시서 B-10 이 명시) |

또한 `fallback_sources` 가 `web-primary` 라는 **pack id 를 source id 자리에** 쓴다.
DESIGN §8.1 의 예시(`fallback_sources: [threads, web-primary]`) 자체가 그렇게 섞여 있어
그대로 따랐고, 레지스트리 로더는 source_id 와 pack_id 둘 다 허용한다. 정리하려면 정본을
먼저 고쳐야 한다.

### 3.6 정본이 링크를 주지 않아 비워 둔 것

`policy_urls` 는 DESIGN §7.1 에 링크가 있는 소스만 채웠다. 아래 6개는 **빈 배열**이다.
추측한 URL 을 넣지 않았다.

`data_go_kr` · `world_bank` · `hn_algolia` · `coupang_seller` · `coupang_partners` · `x_twitter`

참고로 DESIGN §8.1 의 스키마 예시는 `reddit` 의 `policy_urls` 를 `[]` 로 적지만
§7.1 에는 Reddit 정책 URL 이 4개 있다. §7.1 을 채택했다.

### 3.7 지시서 §8 키 표에 없어 자격증명을 등록하지 못한 소스

| 소스 | 레지스트리 상태 | 문제 |
|---|---|---|
| `coupang_partners` | `conditional` / `auth_type: api_key` | §8 키 표에 Coupang 키가 없다. `.env.example` 에도 없다 |
| `google_trends` | `experimental` / `auth_type: api_key` | 같음 |

둘 다 지시서가 "등록만 하고 실호출 경로를 열지 않는다"라고 정한 소스라 지금은 문제가
되지 않는다. **실호출 경로를 열려면 키를 먼저 §8 표와 `.env.example` 에 추가해야 한다.**
테스트에 이 예외를 명시적으로 적어 뒀다 (`tests/test_sources_yaml.py::CREDENTIAL_EXEMPT`).

---

## 4. S1 에서 만들지 않은 것 (S2 로 넘어가는 과제)

1. **관측·지표 수준의 retention.** A-9 은 `raw_snapshots` 만 다룬다.
   `source_observations.payload_json` 에 복사된 플랫폼 응답과 `metrics` 행의 YouTube 30일
   이행은 B-9 에서 collector 와 함께 다뤄야 한다. 지금 YouTube 를 호출하면 스냅샷은
   30일에 정리되지만 관측 payload 는 남는다.
2. **Threads 쿼터 카운터.** 선언(2,200/24h/user)만 있고 카운팅 구현이 없다. B-8 과제.
   0건 응답 미차감 규칙도 그때 코드로 들어가야 한다.
3. **`source_registry` 테이블을 채우는 코드.** 표는 있으나 동기화 함수가 없다.
4. **데이터셋 단위 정책 검사.** 공공데이터포털은 데이터셋마다 조건이 다른데 현재
   레지스트리는 소스 단위다. §2.3 참조.
5. **Naver API Hub 이관.** 기존 개발자센터 API 는 2027-06-30 종료 예정이다.
   `sources.yaml` 의 `notes` 에 기록만 해 뒀다.

---

## 5. 환경 관련 기록

- 이 맥에는 Python **3.14.7**(homebrew)과 3.9.6(시스템)만 있다. `.venv` 는 3.14.7 로
  만들었다. `requires-python = ">=3.11"` 이고 ruff `target-version = "py311"` 이라
  3.12+ 문법(PEP 695 타입 파라미터 등)은 쓰지 않았다.
- `.codex/config.toml` 이 요구하는 `.venv/bin/python` 경로는 그대로 만족한다.
  다만 **`ria.mcp_server` 는 S3 범위라 아직 없다.** 지금 MCP 서버를 띄우면 실패한다.
  config.toml 의 `required = false` 덕분에 Codex 기동 자체는 막히지 않는다.
