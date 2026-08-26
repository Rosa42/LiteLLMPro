"""Data dir layout: host plugins/ vs Docker /app/shared_quota_router."""

from pathlib import Path

from shared_quota_router.data_paths import default_data_dir


def test_host_plugin_layout(tmp_path: Path) -> None:
    here = tmp_path / "plugins" / "shared_quota_router"
    here.mkdir(parents=True)
    assert default_data_dir("vision-cache", here=here) == tmp_path / "data" / "vision-cache"
    assert (
        default_data_dir("gateway-memory", here=here)
        == tmp_path / "data" / "gateway-memory"
    )


def test_docker_app_layout(tmp_path: Path) -> None:
    here = tmp_path / "shared_quota_router"
    here.mkdir()
    got = default_data_dir("vision-cache", here=here)
    assert got == tmp_path / "data" / "vision-cache"
    assert got != tmp_path.parent / "data" / "vision-cache"
