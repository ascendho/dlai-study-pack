from study.crawler import _code_group_for_lesson, TranscriptCrawler
from study.parser import LessonInfo


def test_existing_lesson_path_prefers_transcripts_directory(tmp_path):
    course_dir = tmp_path / "course"
    transcripts_dir = course_dir / "transcripts"
    transcripts_dir.mkdir(parents=True)
    legacy_path = course_dir / "01-introduction.md"
    new_path = transcripts_dir / "01-introduction.md"
    legacy_path.write_text("legacy", encoding="utf-8")
    new_path.write_text("new", encoding="utf-8")

    crawler = TranscriptCrawler(output_root=tmp_path)

    assert crawler._existing_lesson_path("course", 1) == new_path


def test_existing_lesson_path_falls_back_to_legacy_root_file(tmp_path):
    course_dir = tmp_path / "course"
    course_dir.mkdir()
    legacy_path = course_dir / "01-introduction.md"
    legacy_path.write_text("legacy", encoding="utf-8")

    crawler = TranscriptCrawler(output_root=tmp_path)

    assert crawler._existing_lesson_path("course", 1) == legacy_path


def test_code_group_for_lesson_splits_lesson_and_project_code():
    assert (
        _code_group_for_lesson(
            LessonInfo(
                1,
                "Tool Execution Environments",
                "https://example.test/code",
                kind="code",
            )
        )
        == "lessons"
    )
    assert (
        _code_group_for_lesson(
            LessonInfo(
                2,
                "Hands-On Project",
                "https://example.test/project",
                kind="lesson",
            )
        )
        == "project"
    )
    assert (
        _code_group_for_lesson(
            LessonInfo(
                3,
                "Graded",
                "https://example.test/graded",
                kind="assignment",
            )
        )
        == "project"
    )
    assert (
        _code_group_for_lesson(
            LessonInfo(
                4,
                "Conclusion",
                "https://example.test/conclusion",
                kind="video",
            )
        )
        is None
    )
