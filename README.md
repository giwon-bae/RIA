# RIA — 근거 수집 전문 에이전트 (RIA Core)

RIA 는 여러 사이트의 검색 결과를 모으는 범용 크롤러가 아니다.
**추적 가능하고 재현 가능한 근거 패키지(Evidence Pack)를 만드는 자료수집 전문 에이전트**다.

이 레포는 그중 **RIA Core** — 결정론적 엔진 — 을 담는다.

> 설계 정본: `~/nia/resources/ideas/ria/DESIGN.md` (v2.1)
> 이 README 는 설계를 요약하지 않는다. 설치·운영 방법만 적는다.

## 핵심 원칙

- **RIA Core 는 AI 모델을 호출하지 않는다** (DESIGN §3.4). 모델 실행은 Codex/ChatGPT 구독 계층의 몫이다.
  따라서 `openai` · Responses API 클라이언트 · Agents SDK 같은 과금형 AI SDK 를 의존성에 넣지 않는다.
- 플랫폼 정책은 코드 상수가 아니라 `ria/policy/sources.yaml` (Source Registry) 로 관리한다.
  접근 승인 여부는 `access_status` 필드 하나로 전환된다.
- 접근이 막힌 소스를 비공식 스크래퍼로 대체하지 않는다. `gap` 으로 남긴다.

## 구현 진행 상태

| 스테이지 | 범위 | 상태 |
|---|---|---|
| S1 Foundation | 계약 · 정책 · 저장 · CLI 뼈대 | 완료 (`s1-foundation`) |
| S2 Pack & Collector | 수집 → 정규화 → 저장 관통 | 완료 (`s2-collectors`; B-7 정책 판정 대기) |
| S3 MCP · Job · 검증 | MCP 도구 12종 · Job · 품질 게이트 | 미착수 |

S1 에서 발견한 제약과 DESIGN 과의 불일치는 [`REPORT-S1.md`](REPORT-S1.md) 에 전건 기록했다.
S2 라운드별 게이트·제약·미해결은 [`REPORT-S2.md`](REPORT-S2.md) 에 기록했다.

## 요구 사항

- Python 3.11 이상
- 가상환경 경로는 **`RIA/.venv` 로 고정**한다. `~/nia/.codex/config.toml` 의 MCP 서버 `command` 가
  `/Users/zero/GitHub/Personal_Project/RIA/.venv/bin/python` 을 직접 가리키므로 다른 이름을 쓰면 MCP 가 뜨지 않는다.

## 설치

```bash
cd ~/GitHub/Personal_Project/RIA
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## `.env` 설정

API 키는 **`.env` 에서만** 로드한다. `.env` 는 커밋하지 않고 `.env.example` 만 커밋한다.

```bash
cp .env.example .env
# 편집기로 .env 를 열어 필요한 키를 채운다
```

키가 **불필요한** 소스: Hacker News, World Bank.
Reddit · Threads 는 키가 있어도 **승인 전에는 Policy Guard 가 차단한다. 이는 버그가 아니라 설계다.**

키 목록과 발급처는 `.env.example` 을 정본으로 본다.

개인정보 처리 방침: [`PRIVACY.md`](PRIVACY.md)

## CLI 사용 예시

`source list/check`·`query`·`snapshot` 명령은 네트워크 없이 로컬 정책·SQLite만 다룬다. `collect`는
Policy Guard 통과 후에만 공식 API를 호출하고, 차단 소스는 실호출 없이 gap을 남긴다.

```bash
# 등록된 소스 20건을 표로 본다
.venv/bin/python -m ria.cli source list

# Pack·상태로 거른다
.venv/bin/python -m ria.cli source list --pack community-signal
.venv/bin/python -m ria.cli source list --status blocked

# Policy Guard 6단 검사를 돌린다. 차단이면 어느 단계에서 왜 막혔는지 나온다.
.venv/bin/python -m ria.cli source check reddit
.venv/bin/python -m ria.cli source check hacker_news

# 기계 판독용
.venv/bin/python -m ria.cli source list --json
.venv/bin/python -m ria.cli source check reddit --json

# 상업 조사 맥락을 끄거나, 호출 예정량·기준일을 바꿔서 검사한다
.venv/bin/python -m ria.cli source check hn_algolia --non-commercial
.venv/bin/python -m ria.cli source check threads --calls 2500
.venv/bin/python -m ria.cli source check hacker_news --as-of 2027-01-01

# 허용된 소스를 수집해 정규화·적재한다
.venv/bin/python -m ria.cli collect world_bank SP.POP.TOTL \
  --options-json '{"country":"KOR","mrv":1,"per_page":1}' --json
.venv/bin/python -m ria.cli collect hacker_news launch \
  --options-json '{"item_ids":[8863]}' --json

# Pack은 source별 옵션을 하나의 JSON object로 받는다
.venv/bin/python -m ria.cli collect authority-stats population \
  --source-options-json '{"world_bank":{"country":"KOR","mrv":1}}' --json

# Reddit·Threads는 승인 전에 status=blocked/conditional이므로 호출 없이 gap 1건
.venv/bin/python -m ria.cli collect reddit demand --json
.venv/bin/python -m ria.cli collect threads demand --json

# 적재된 관측·지표와 스냅샷 메타데이터를 조회한다
.venv/bin/python -m ria.cli query observations --source hacker_news --limit 20 --json
.venv/bin/python -m ria.cli query metrics hn_score --platform hacker_news --json
.venv/bin/python -m ria.cli snapshot get snap_xxx --json
# 원본 body는 명시적으로 요청할 때만 출력한다
.venv/bin/python -m ria.cli snapshot get snap_xxx --include-body --json
```

`pip install -e .` 를 했다면 `ria source list` 로도 부를 수 있다.

`source check`와 `collect`는 판정·실행 경로가 정상적으로 끝나면 차단이어도 종료
코드 0이다. 허용·차단 여부는 `allowed`·`status`·`gaps`에 담긴다. 잘못된 인자·미등록
대상·수집 실패·DB 오류는 0이 아닌 종료 코드로 반환한다.

## 검사

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest
```

기본 pytest는 fixture + 소켓 차단으로 네트워크 0건을 보장한다. `.env` 값이 있든 없든
동일하게 통과해야 하며, config는 자격증명 0개의 임시 값으로 교체한다.

## 디렉터리

```text
ria/
├── contracts/    ResearchBrief · EvidencePack 스키마 (DESIGN §4)
├── policy/       Source Registry(sources.yaml) · Policy Guard (DESIGN §8)
├── core/         저장·정규화·스냅샷 (DESIGN §10)
├── packs/        Source Pack 오케스트레이션 (DESIGN §5)
├── collectors/   플랫폼별 결정론적 수집기
├── migrations/   스키마 변경 SQL
├── config.py     .env 로드 · 경로 · TTL · 쿼터 기본값
├── cli.py        운영·디버깅 CLI
└── mcp_server.py MCP 서버 진입점 (S3)
```

> DESIGN §16.1 의 트리는 `mcp_server.py` · `cli.py` 를 레포 루트에 그렸으나,
> `~/nia/.codex/config.toml` 의 `-m ria.mcp_server` 가 실제 계약이므로 **`ria/` 패키지 안**에 둔다.
> 이 불일치는 config.toml 을 정본으로 해소했다.

## MCP 등록

`~/nia/.codex/config.toml` 에 이미 등록돼 있다. **이 값은 계약이므로 변경하지 않는다.**

```toml
[mcp_servers.ria_core]
command = "/Users/zero/GitHub/Personal_Project/RIA/.venv/bin/python"
args = ["-m", "ria.mcp_server", "--db", "/Users/zero/GitHub/Personal_Project/RIA/ria.db"]
cwd = "/Users/zero/GitHub/Personal_Project/RIA"
```

MCP 서버 구현은 S3 범위다.
