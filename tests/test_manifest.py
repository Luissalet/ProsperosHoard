from pathlib import Path

from faustus_manifest import check_repo

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_manifest_passes_faustus_checks():
    data = check_repo(REPO_ROOT)
    assert data["id"] == "prospero"
    assert data["app"]["health"]["expect"]["service"] == "prosperos-hoard"
