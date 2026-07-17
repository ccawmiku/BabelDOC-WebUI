from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tools import windows_launcher


def test_find_project_root_walks_up_from_executable_directory(tmp_path):
    project = tmp_path / "project"
    nested = project / "dist" / "windows"
    nested.mkdir(parents=True)
    (project / "start-web.bat").write_text("@echo off", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
    assert windows_launcher.find_project_root([nested]) == project


def test_find_project_root_requires_launcher_and_project_file(tmp_path):
    (tmp_path / "start-web.bat").write_text("@echo off", encoding="utf-8")
    assert windows_launcher.find_project_root([tmp_path]) is None


def test_main_opens_browser_when_service_is_already_ready(monkeypatch):
    opened = []
    monkeypatch.setattr(windows_launcher, "service_is_ready", lambda: True)
    monkeypatch.setattr(windows_launcher.webbrowser, "open", opened.append)
    assert windows_launcher.main() == 0
    assert opened == [windows_launcher.APP_URL]


def test_main_reports_missing_project(monkeypatch):
    errors = []
    monkeypatch.setattr(windows_launcher, "service_is_ready", lambda: False)
    monkeypatch.setattr(windows_launcher, "find_project_root", lambda: None)
    monkeypatch.setattr(windows_launcher, "show_error", errors.append)
    assert windows_launcher.main() == 1
    assert "start-web.bat" in errors[0]


def test_wait_for_service_stops_when_process_exits(monkeypatch):
    process = SimpleNamespace(poll=lambda: 1)
    monkeypatch.setattr(windows_launcher, "service_is_ready", lambda: False)
    assert windows_launcher.wait_for_service(process) is False


def test_candidate_directories_are_absolute():
    assert all(isinstance(path, Path) and path.is_absolute() for path in windows_launcher.candidate_directories())
