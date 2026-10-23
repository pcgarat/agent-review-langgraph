"""URL de un pull request, reducida a repo y numero.

El panel no llama al VCS: solo reconoce la URL. El commit lo lee el worker, que
ya tiene el adaptador. Sirve para GitHub (`/pull/123`) y Bitbucket Cloud o
Server (`/pull-requests/123`), con o sin el sufijo de pestaña (`/files`,
`/diff`, `/overview`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_GITHUB = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<pr>[1-9]\d*)(?:/.*)?$")
_BITBUCKET = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull-requests/(?P<pr>[1-9]\d*)(?:/.*)?$"
)
_BITBUCKET_SERVER = re.compile(
    r"^/projects/(?P<owner>[^/]+)/repos/(?P<repo>[^/]+)/pull-requests/(?P<pr>[1-9]\d*)(?:/.*)?$"
)


class PullRequestUrlError(ValueError):
    """La URL no es un pull request de GitHub ni de Bitbucket."""


@dataclass(frozen=True)
class PullRequestRef:
    repo: str
    pr_id: int


def parse_pull_request_url(raw: str) -> PullRequestRef:
    """`owner/repo` y el numero del PR, o `PullRequestUrlError`."""
    text = raw.strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise PullRequestUrlError("La URL tiene que ser http(s) y apuntar a un pull request")
    path = parts.path.rstrip("/") or "/"
    for pattern in (_GITHUB, _BITBUCKET, _BITBUCKET_SERVER):
        match = pattern.fullmatch(path)
        if match is None:
            continue
        owner = _segment(match.group("owner"))
        repo = _segment(match.group("repo"))
        return PullRequestRef(repo=f"{owner}/{repo}", pr_id=int(match.group("pr")))
    raise PullRequestUrlError(
        "No reconozco esa URL. Vale una de GitHub (/pull/123) o de Bitbucket (/pull-requests/123)."
    )


def _segment(value: str) -> str:
    if not value or value in {".", ".."} or any(char.isspace() for char in value):
        raise PullRequestUrlError("La URL no trae un repositorio reconocible")
    return value
