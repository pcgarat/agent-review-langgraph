from __future__ import annotations

import pytest

from agent_review.pr_url import PullRequestUrlError, parse_pull_request_url


@pytest.mark.parametrize(
    ("url", "repo", "pr_id"),
    [
        ("https://github.com/acme/widgets/pull/12", "acme/widgets", 12),
        ("https://github.com/acme/widgets/pull/12/", "acme/widgets", 12),
        ("https://github.com/acme/widgets/pull/12/files?w=1#r", "acme/widgets", 12),
        ("http://github.example.com/acme/widgets/pull/3/commits", "acme/widgets", 3),
        ("https://bitbucket.org/cqm/emotivoo-v2/pull-requests/430", "cqm/emotivoo-v2", 430),
        (
            "https://bitbucket.org/cqm/emotivoo-v2/pull-requests/430/diff",
            "cqm/emotivoo-v2",
            430,
        ),
        (
            "https://bitbucket.corp/projects/CQM/repos/emotivoo-v2/pull-requests/8/overview",
            "CQM/emotivoo-v2",
            8,
        ),
        ("<https://github.com/acme/widgets/pull/9>", "acme/widgets", 9),
    ],
)
def test_parsea_la_url_de_un_pull_request(url: str, repo: str, pr_id: int) -> None:
    parsed = parse_pull_request_url(url)
    assert parsed.repo == repo
    assert parsed.pr_id == pr_id


@pytest.mark.parametrize(
    "url",
    [
        "",
        "no-es-una-url",
        "https://github.com/acme/widgets",
        "https://github.com/acme/widgets/pull/0",
        "https://github.com/acme/widgets/issues/12",
        "ftp://github.com/acme/widgets/pull/12",
        "https://bitbucket.org/cqm/emotivoo-v2/commits/abc",
    ],
)
def test_rechaza_lo_que_no_es_un_pull_request(url: str) -> None:
    with pytest.raises(PullRequestUrlError):
        parse_pull_request_url(url)
