"""URL 정규화 — ContentItem 연결 전용 (DESIGN §17 "URL dedup" 행).

**플랫폼 Observation 병합에는 쓰지 않는다.** 같은 외부 URL 이 HN 과 Reddit 에 올라오면
ContentItem 은 1건으로 묶이지만 Observation 은 2건으로 남아야 한다 (DESIGN §10.2).
정규화 결과를 Observation 키로 쓰면 그 구분이 무너진다.

정규화 범위는 지시서 A-10 이 정한 세 가지 — 트래킹 파라미터 제거 · 호스트 소문자 ·
후행 슬래시 — 에 더해, 같은 문서를 다른 문자열로 만들 뿐인 조각(fragment)과
질의 순서를 정리한다. 그 이상은 하지 않는다. `www.` 를 떼거나 http 를 https 로
바꾸는 것은 다른 문서를 같은 것으로 만들 수 있어서 하지 않는다.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 접두사로 판단하는 트래킹 파라미터.
TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "_hs", "pk_", "mtm_", "matomo_")

# 이름이 정확히 일치할 때 제거하는 트래킹 파라미터.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "twclid",
        "ttclid",
        "igshid",
        "igsh",
        "mc_cid",
        "mc_eid",
        "s_kwcid",
        "vero_id",
        "vero_conv",
        "ref_src",
        "ref_url",
        "spm",
    }
)

# 스킴별 기본 포트. 명시돼 있으면 떼어낸다.
_DEFAULT_PORTS = {"http": "80", "https": "443"}

# 정규화 대상 스킴. mailto·javascript 등은 ContentItem 이 아니다.
_SUPPORTED_SCHEMES = frozenset({"http", "https"})


def is_tracking_param(name: str) -> bool:
    """트래킹 파라미터인가."""
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def normalize_url(url: str | None) -> str | None:
    """ContentItem 연결에 쓸 정규화 URL. 정규화할 수 없으면 None.

    None 을 돌려준다는 것은 "이 URL 로는 다른 플랫폼의 같은 문서를 묶을 수 없다"는
    뜻이다. 그때는 묶지 말아야지 억지로 키를 만들면 안 된다.
    """
    if url is None:
        return None

    candidate = url.strip()
    if not candidate:
        return None

    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in _SUPPORTED_SCHEMES or not parts.hostname:
        return None

    host = parts.hostname.lower()
    port = parts.port
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"

    path = parts.path
    if len(path) > 1:
        path = path.rstrip("/")
    elif path == "":
        path = "/"

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_param(k)
    ]
    query = urlencode(sorted(kept))

    # fragment 는 버린다. 같은 문서의 다른 위치일 뿐이다.
    return urlunsplit((scheme, host, path, query, ""))


def url_key(url: str | None) -> str | None:
    """`content_items.url_key` 에 저장할 값. `normalize_url` 과 같다.

    이름을 따로 둔 이유는 호출부에서 "이건 ContentItem 키다"가 드러나게 하기 위해서다.
    Observation 키로 쓰지 않는다.
    """
    return normalize_url(url)


def same_content(left: str | None, right: str | None) -> bool:
    """두 URL 이 같은 ContentItem 인가. 정규화할 수 없으면 묶지 않는다."""
    left_key = normalize_url(left)
    right_key = normalize_url(right)
    if left_key is None or right_key is None:
        return False
    return left_key == right_key
