from __future__ import annotations

import os
import subprocess

import pytest

from agent_review.adapters.diff_parser import parse_unified_diff
from agent_review.adapters.twg_vcs import TwgVcsAdapter

pytestmark = pytest.mark.live

WORKSPACE = os.environ.get("BITBUCKET_WORKSPACE", "cqm")
REPO = os.environ.get("BITBUCKET_REPO", "emotivoo-v2")
PR_ID = os.environ.get("REVIEWS_LIVE_PR")


def _twg_ready() -> bool:
    try:
        return (
            subprocess.run(
                ["twg", "whoami"], capture_output=True, text=True, check=False, timeout=20
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not PR_ID, reason="define REVIEWS_LIVE_PR para el smoke real")
@pytest.mark.skipif(not _twg_ready(), reason="twg no autenticado")
def test_adaptador_contra_pr_real() -> None:
    """Lee un PR real y comprueba que diff y anclas casan.

    Solo lectura: no publica ningun comentario.
    """
    adapter = TwgVcsAdapter(workspace=WORKSPACE, repo=REPO)
    diff = adapter.get_diff(f"{WORKSPACE}/{REPO}", int(PR_ID))
    files = parse_unified_diff(diff)
    assert files, "el PR no tiene cambios parseables"

    stats = adapter.get_diffstat(f"{WORKSPACE}/{REPO}", int(PR_ID))
    assert stats, "diffstat vacio"

    path, added = next(((p, fd.added) for p, fd in files.items() if fd.added), (None, {}))
    assert path and added
    line_no = sorted(added)[0]
    needle = added[line_no].strip()[:40]
    anchor = adapter.resolve_inline_anchor(
        f"{WORKSPACE}/{REPO}", int(PR_ID), path=path, text=needle
    )
    assert anchor is not None, "diff-line no encontro la linea anadida"
    assert anchor.line in added
