"""Tests for path normalization/collision preflight."""

from protondrive_sync.core.path_safety import scan_path_safety


def test_warns_on_leading_trailing_space(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / " name.txt").write_text("x")

    report = scan_path_safety(root, [])

    assert report.ok
    assert any("leading or trailing spaces" in issue.message for issue in report.issues)


def test_blocks_trim_collision(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "name.txt").write_text("a")
    (root / "name.txt ").write_text("b")

    report = scan_path_safety(root, [])

    assert not report.ok
    assert len(report.blocking_issues) == 1


def test_blocks_cross_platform_case_collision(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "Model.pt").write_text("a")
    (root / "model.pt").write_text("b")

    report = scan_path_safety(root, [], cross_platform=True)

    assert not report.ok


def test_filters_are_respected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    ignored = root / "build"
    ignored.mkdir()
    (ignored / "A.txt").write_text("a")
    (ignored / "a.txt").write_text("b")

    report = scan_path_safety(root, ["- build/**"], cross_platform=True)

    assert report.ok
