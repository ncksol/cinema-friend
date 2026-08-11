"""BFI URL construction and validation tests."""

from __future__ import annotations

import pytest

from cinema_friend.domain.errors import InputError


def test_parse_article_url_rebuilds_canonical_url():
    from cinema_friend.bfi.urls import parse_article_url

    ref = parse_article_url(
        "https://whatson.bfi.org.uk/imax/Online/default.asp"
        "?utm_source=x&BOparam::WScontent::loadArticle::permalink=dog-stars"
    )
    assert ref.slug == "dog-stars"
    assert ref.canonical_url == (
        "https://whatson.bfi.org.uk/imax/Online/default.asp"
        "?BOparam::WScontent::loadArticle::permalink=dog-stars"
    )


def test_parse_article_url_accepts_article_path():
    from cinema_friend.bfi.urls import parse_article_url

    ref = parse_article_url("https://whatson.bfi.org.uk/imax/Online/article/dog-stars?utm_source=x")
    assert ref.slug == "dog-stars"
    assert ref.canonical_url == (
        "https://whatson.bfi.org.uk/imax/Online/default.asp"
        "?BOparam::WScontent::loadArticle::permalink=dog-stars"
    )


def test_pagination_keeps_parameter_names_literal_and_encodes_token():
    from cinema_friend.bfi.urls import pagination_url

    url = pagination_url("1,a/b+=", 2, "2152D1E8-CFF7-419F-BE57-F51C1E490F24")
    assert "BOset::WScontent::SearchResultsInfo::current_page=2" in url
    assert "sToken=1%2Ca%2Fb%2B%3D" in url
    assert "%3A%3A" not in url


def test_film_and_seat_map_urls_are_literal():
    from cinema_friend.bfi.urls import film_page_url, seat_map_url

    assert (
        film_page_url("dog-stars")
        == "https://whatson.bfi.org.uk/imax/Online/default.asp"
        "?BOparam::WScontent::loadArticle::permalink=dog-stars"
    )
    assert (
        seat_map_url("2152D1E8-CFF7-419F-BE57-F51C1E490F24")
        == "https://whatson.bfi.org.uk/imax/Online/mapSelect.asp"
        "?BOparam::WSmap::loadMap::performance_ids=2152D1E8-CFF7-419F-BE57-F51C1E490F24"
    )


def test_validate_redirect_target_canonicalizes_allowed_urls():
    from cinema_friend.bfi.urls import validate_redirect_target

    assert (
        validate_redirect_target("https://whatson.bfi.org.uk/imax/Online/article/dog-stars")
        == "https://whatson.bfi.org.uk/imax/Online/default.asp"
        "?BOparam::WScontent::loadArticle::permalink=dog-stars"
    )


def test_validate_redirect_target_rejects_map_select():
    from cinema_friend.bfi.urls import validate_redirect_target

    with pytest.raises(InputError):
        validate_redirect_target(
            "https://whatson.bfi.org.uk/imax/Online/mapSelect.asp"
            "?BOparam::WSmap::loadMap::performance_ids=2152D1E8-CFF7-419F-BE57-F51C1E490F24"
        )


def test_parse_article_url_rejects_duplicate_permalink():
    from cinema_friend.bfi.urls import parse_article_url

    with pytest.raises(InputError):
        parse_article_url(
            "https://whatson.bfi.org.uk/imax/Online/default.asp"
            "?BOparam::WScontent::loadArticle::permalink=dog-stars"
            "&BOparam::WScontent::loadArticle::permalink=dog-stars"
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://whatson.bfi.org.uk/imax/Online/article/dog-stars",
        "https://evil.example/imax/Online/article/dog-stars",
        "https://whatson.bfi.org.uk:444/imax/Online/article/dog-stars",
        "https://user@whatson.bfi.org.uk/imax/Online/article/dog-stars",
    ],
)
def test_rejects_unsafe_urls(url: str):
    from cinema_friend.bfi.urls import parse_article_url

    with pytest.raises(InputError):
        parse_article_url(url)
