from o2gateway.operations.logging import truncate_log_file


def test_truncate_log_file_preserves_file_and_clears_content(tmp_path):
    log_file = tmp_path / "gateway.log"
    log_file.write_text("first line\nsecond line\n", encoding="utf-8")

    assert truncate_log_file(str(log_file)) is True
    assert log_file.exists()
    assert log_file.read_text(encoding="utf-8") == ""


def test_truncate_log_file_returns_false_when_file_does_not_exist(tmp_path):
    assert truncate_log_file(str(tmp_path / "missing.log")) is False


def test_truncate_log_file_returns_false_for_directory(tmp_path):
    assert truncate_log_file(str(tmp_path)) is False
