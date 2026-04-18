from datetime import datetime

from reporting import report_paths


def test_unique_report_path_adds_counter_when_name_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(report_paths, "CLAIM_RESOLUTION_REPORT_DIR", tmp_path)
    when = datetime(2026, 4, 17, 13, 1, 2, 123456)

    first = report_paths.unique_report_path("Classification Dry Runs", "classification", ".md", when=when)
    first.write_text("first")
    second = report_paths.unique_report_path("Classification Dry Runs", "classification", ".md", when=when)

    assert first.name == "classification_20260417_130102_123456.md"
    assert second.name == "classification_20260417_130102_123456_001.md"
    assert second.parent == tmp_path / "Classification_Dry_Runs"


def test_sync_report_file_uploads_when_not_in_local_dropbox(tmp_path, monkeypatch):
    monkeypatch.setattr(report_paths, "CLAIM_RESOLUTION_REPORT_DIR", tmp_path / "DropboxRoot")
    calls = []

    def fake_upload(local_path, dropbox_path):
        calls.append((local_path, dropbox_path))
        return True, ""

    monkeypatch.setattr(report_paths, "upload_file_to_dropbox", fake_upload)

    report = tmp_path / "fallback" / "report.md"
    report.parent.mkdir()
    report.write_text("hello")

    result = report_paths.sync_report_file(report, "Classification Dry Runs")

    assert result.uploaded_to_dropbox is True
    assert result.dropbox_path.endswith("/Classification_Dry_Runs/report.md")
    assert calls == [(report, result.dropbox_path)]


def test_sync_report_file_skips_api_when_inside_local_dropbox(tmp_path, monkeypatch):
    monkeypatch.setattr(report_paths, "CLAIM_RESOLUTION_REPORT_DIR", tmp_path / "DropboxRoot")

    def fake_upload(local_path, dropbox_path):
        raise AssertionError("Local Dropbox files should be left for Dropbox sync")

    monkeypatch.setattr(report_paths, "upload_file_to_dropbox", fake_upload)

    report = tmp_path / "DropboxRoot" / "Classification_Dry_Runs" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("hello")

    result = report_paths.sync_report_file(report, "Classification Dry Runs")

    assert result.uploaded_to_dropbox is False
    assert result.local_path == report
