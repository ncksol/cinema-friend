"""BFI URL validation and Tessitura query assembly."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import SplitResult, parse_qs, quote, unquote, urlsplit

from cinema_friend.domain.errors import InputError

BASE_URL: Final = "https://whatson.bfi.org.uk/imax/Online/"
DEFAULT_PATH: Final = "/imax/Online/default.asp"
ARTICLE_PREFIX: Final = "/imax/Online/article/"
MAP_SELECT_PATH: Final = "/imax/Online/mapSelect.asp"
PERMALINK_PARAM: Final = "BOparam::WScontent::loadArticle::permalink"
PAGE_PARAM: Final = "BOset::WScontent::SearchResultsInfo::current_page"
GET_PAGE_PARAM: Final = "BOparam::WScontent::getPage::article_id"
DO_WORK_PARAM: Final = "doWork::WScontent::getPage"
MAP_PARAM: Final = "BOparam::WSmap::loadMap::performance_ids"
SLUG_RE: Final = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PERFORMANCE_ID_RE: Final = re.compile(
    r"^[0-9A-Fa-f]{8}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{12}$"
)


@dataclass(frozen=True, slots=True)
class ArticleRef:
    """Canonical BFI article identity."""

    slug: str
    canonical_url: str


def _literal_query(pairs: Sequence[tuple[str, str]]) -> str:
    return "&".join(f"{name}={quote(value, safe='')}" for name, value in pairs)


def _validate_host(raw_url: str) -> SplitResult:
    split = urlsplit(raw_url)
    if split.scheme != "https":
        raise InputError("BFI URLs must use https")
    if split.username is not None or split.password is not None:
        raise InputError("BFI URLs must not include credentials")
    if split.fragment:
        raise InputError("BFI URLs must not include a fragment")
    try:
        port = split.port
    except ValueError as exc:
        raise InputError("BFI URLs must not include a port") from exc
    if port is not None:
        raise InputError("BFI URLs must not include a port")
    if split.hostname != "whatson.bfi.org.uk":
        raise InputError("BFI URLs must target whatson.bfi.org.uk")
    return split


def _validate_slug(slug: str) -> str:
    slug = unquote(slug)
    if not SLUG_RE.fullmatch(slug):
        raise InputError("BFI article slugs must contain lowercase letters, digits, and hyphens")
    return slug


def _validate_guid(guid: str) -> str:
    if not PERFORMANCE_ID_RE.fullmatch(guid):
        raise InputError("value must be a GUID")
    return guid


def _query_values(query: str, name: str) -> list[str] | None:
    values = parse_qs(query, keep_blank_values=True).get(name)
    if values is None:
        return None
    if len(values) != 1 or values[0] == "":
        raise InputError(f"{name} must appear exactly once")
    return values


def film_page_url(slug: str) -> str:
    slug = _validate_slug(slug)
    return BASE_URL + "default.asp?" + _literal_query([(PERMALINK_PARAM, slug)])


def pagination_url(s_token: str, page: int, article_id: str) -> str:
    if page < 1:
        raise InputError("page must be at least 1")
    article_id = _validate_guid(article_id)
    return BASE_URL + "default.asp?" + _literal_query(
        [
            ("sToken", s_token),
            (PAGE_PARAM, str(page)),
            (DO_WORK_PARAM, ""),
            (GET_PAGE_PARAM, article_id),
        ]
    )


def seat_map_url(performance_id: str) -> str:
    performance_id = _validate_guid(performance_id)
    return BASE_URL + "mapSelect.asp?" + _literal_query([(MAP_PARAM, performance_id)])


def parse_article_url(raw_url: str) -> ArticleRef:
    split = _validate_host(raw_url)
    if split.path == DEFAULT_PATH:
        permalink = _query_values(split.query, PERMALINK_PARAM)
        if permalink is None:
            raise InputError("BFI article URLs require a permalink")
        slug = _validate_slug(permalink[0])
        return ArticleRef(slug=slug, canonical_url=film_page_url(slug))
    if split.path.startswith(ARTICLE_PREFIX):
        slug = _validate_slug(split.path[len(ARTICLE_PREFIX) :])
        permalink = _query_values(split.query, PERMALINK_PARAM)
        if permalink is not None and permalink[0] != slug:
            raise InputError("permalink must match the article slug")
        return ArticleRef(slug=slug, canonical_url=film_page_url(slug))
    raise InputError("BFI article URLs must use /imax/Online/article/<slug> or default.asp")


def validate_redirect_target(raw_url: str) -> str:
    split = _validate_host(raw_url)
    if split.path == DEFAULT_PATH or split.path.startswith(ARTICLE_PREFIX):
        return parse_article_url(raw_url).canonical_url
    raise InputError("redirect target must stay on an allowed BFI route")
