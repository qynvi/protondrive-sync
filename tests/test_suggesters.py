"""Tests for path suggesters."""

from pathlib import Path

from protondrive_sync.core.suggesters import LocalPathSuggester


class TestLocalPathSuggester:
    """Test the synchronous path suggestion logic (calling _suggest_path directly)."""

    def test_suggest_in_home(self, tmp_path):
        """Typing a partial dir name suggests the first matching child."""
        # Create some dirs
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        (tmp_path / "gamma").mkdir()

        s = LocalPathSuggester()
        # Partial match for "a" inside tmp_path
        result = s._suggest_path(str(tmp_path / "a"))
        assert result == str(tmp_path / "alpha") + "/"

    def test_suggest_trailing_slash(self, tmp_path):
        """Path ending in / suggests first child directory."""
        (tmp_path / "sub1").mkdir()
        (tmp_path / "sub2").mkdir()

        s = LocalPathSuggester()
        result = s._suggest_path(str(tmp_path) + "/")
        assert result == str(tmp_path / "sub1") + "/"

    def test_no_match(self, tmp_path):
        """Returns None when no directory matches."""
        (tmp_path / "alpha").mkdir()

        s = LocalPathSuggester()
        result = s._suggest_path(str(tmp_path / "zzz"))
        assert result is None

    def test_nonexistent_parent(self):
        """Returns None for a path with nonexistent parent."""
        s = LocalPathSuggester()
        result = s._suggest_path("/definitely/nonexistent/path")
        assert result is None

    def test_hidden_dirs_not_suggested_unless_typed(self, tmp_path):
        """Hidden dirs (dot-prefixed) not suggested unless user starts with dot."""
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "visible").mkdir()

        s = LocalPathSuggester()
        # Without dot prefix: should suggest 'visible', not '.hidden'
        result = s._suggest_path(str(tmp_path) + "/")
        assert "visible" in result

        # With dot prefix: should suggest '.hidden'
        # Use string concat, not Path /, because Path normalizes "." away
        result = s._suggest_path(str(tmp_path) + "/.")
        assert ".hidden" in result

    def test_files_not_suggested(self, tmp_path):
        """Files are not suggested, only directories."""
        (tmp_path / "file.txt").write_text("x")
        (tmp_path / "adir").mkdir()

        s = LocalPathSuggester()
        result = s._suggest_path(str(tmp_path) + "/")
        assert result == str(tmp_path / "adir") + "/"

    def test_empty_dir(self, tmp_path):
        """Returns None for an empty directory."""
        empty = tmp_path / "empty"
        empty.mkdir()

        s = LocalPathSuggester()
        result = s._suggest_path(str(empty) + "/")
        assert result is None

    def test_empty_value(self):
        """Returns None for empty input."""
        s = LocalPathSuggester()
        assert s._suggest_path("") is None

    def test_relative_path_not_suggested(self):
        """Returns None for relative paths (must start with / or ~)."""
        s = LocalPathSuggester()
        assert s._suggest_path("some/relative") is None
