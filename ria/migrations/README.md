# ria/migrations

스키마 변경 SQL 보관 디렉터리다. `ria/core/store.py` 의 `schema_version` 테이블과 연동한다.

## 규칙

- 파일명은 `NNN_요약.sql` (예: `001_initial.sql`). `NNN` 이 곧 `schema_version.version` 이다.
- 이미 배포된 마이그레이션 파일은 수정하지 않는다. 새 번호 파일을 추가한다.
- `store.py` 의 `CREATE TABLE IF NOT EXISTS` 는 신규 DB 부트스트랩용이고,
  기존 DB 의 스키마 변경은 이 디렉터리의 SQL 로 적용한다.
- v1 DB 마이그레이션은 하지 않는다. v1 데이터는 소실됐고 DESIGN §17 에서 개발 데이터로만 취급한다.
