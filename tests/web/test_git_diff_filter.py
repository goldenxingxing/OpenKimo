"""Tests for the git-diff app-runtime path filter.

``sessions.get_session_git_diff`` feeds the "files changed" panel. That panel is
meant to show what a conversation *produced*, so app-runtime/metadata paths
(session storage, Office/WPS temp locks, editor/bytecode artifacts) must be
excluded instead of mixing 9 sessions' metadata into one conversation's numbers.
"""

from kimo_cli.web.api.sessions import _is_app_runtime_path


class TestIsAppRuntimePath:
    def test_session_data_excluded(self):
        assert _is_app_runtime_path("session-data/abc/context.jsonl")
        assert _is_app_runtime_path("session-data/abc/lease.json")
        assert _is_app_runtime_path("session-data/abc/state.json")
        assert _is_app_runtime_path("session-data/abc/evolution/records.jsonl")
        assert _is_app_runtime_path("session-data/abc/archive/compaction.jsonl")

    def test_office_lock_excluded(self):
        # Plain and git-quoted (non-ASCII -> quoted + octal escapes).
        assert _is_app_runtime_path(
            "output/.~AI\u9a71\u52a8\u95ed\u73af\u80f0\u5c9b\u7d20\u6c55_\u9879\u76ee\u76ee\u6807\u4e0e\u80cc\u666f.pptx"
        )
        assert _is_app_runtime_path('"output/.~AI\\351\\251\\261...pptx"')

    def test_editor_artifacts_excluded(self):
        assert _is_app_runtime_path("output/x.tmp")
        assert _is_app_runtime_path("output/x.swp")
        assert _is_app_runtime_path("output/x.pyc")
        assert _is_app_runtime_path("output/.DS_Store")
        assert _is_app_runtime_path("src/__pycache__/x.pyc")

    def test_user_output_kept(self):
        assert not _is_app_runtime_path("output/ppt_render/01_cover.png")
        assert not _is_app_runtime_path(
            "output/AI\u9a71\u52a8\u95ed\u73af\u80f0\u5c9b\u7d20\u6c55_\u9879\u76ee\u76ee\u6807\u4e0e\u80cc\u666f.pptx"
        )
        assert not _is_app_runtime_path("output/report.md")
        assert not _is_app_runtime_path("output/docs/design_spec.md")

    def test_root_level_kept(self):
        assert not _is_app_runtime_path("README.md")
        assert not _is_app_runtime_path("AGENTS.md")
        assert not _is_app_runtime_path("foo/bar/design.pdf")
