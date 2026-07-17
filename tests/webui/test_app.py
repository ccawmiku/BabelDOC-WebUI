from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import babeldoc.webui.app as webapp
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_and_manager(tmp_path):
    app = webapp.create_app(tmp_path)
    manager = app.state.manager

    def fake_create(input_path, filename, output_dir, options):
        job = webapp.WebJob(
            id=output_dir.parent.name,
            filename=filename,
            input_path=input_path,
            output_dir=output_dir,
            options=options,
        )
        manager.jobs[job.id] = job
        return job

    manager.create = fake_create
    with TestClient(app) as client:
        yield client, manager
    manager.executor.shutdown(wait=False, cancel_futures=True)


def post_job(client, *, filename="paper.pdf", body=b"%PDF-1.4\n%%EOF", data=None):
    return client.post(
        "/api/jobs",
        files={"pdf": (filename, body, "application/pdf")},
        data=data or {},
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("../../paper.pdf", "paper.pdf"),
        ("测试 sample.pdf", "测试-sample.pdf"),
        (".pdf", "document.pdf"),
        (None, "document.pdf"),
    ],
)
def test_safe_filename_normal_cases(source, expected):
    assert webapp.safe_filename(source) == expected


def test_safe_filename_limits_long_names():
    filename = webapp.safe_filename(f"{'a' * 400}.pdf")
    assert len(filename) <= 120
    assert filename.endswith(".pdf")


@pytest.mark.parametrize("reserved", ["CON.pdf", "nul.pdf", "Lpt1.pdf"])
def test_safe_filename_avoids_windows_reserved_names(reserved):
    assert webapp.safe_filename(reserved).casefold() != reserved.casefold()


def test_job_options_never_expose_api_key():
    options = webapp.JobOptions(api_key="top-secret")
    assert "api_key" not in options.public()
    job = webapp.WebJob(
        id="job",
        filename="paper.pdf",
        input_path=Path("paper.pdf"),
        output_dir=Path("output"),
        options=options,
    )
    assert "top-secret" not in json.dumps(job.public())


def test_job_public_includes_zero_token_usage():
    job = webapp.WebJob(
        id="job",
        filename="paper.pdf",
        input_path=Path("paper.pdf"),
        output_dir=Path("output"),
        options=webapp.JobOptions(api_key="sk"),
    )
    assert job.public()["token_usage"] == webapp.empty_token_usage()
    assert job.public()["glossary"] == webapp.empty_glossary_summary()
    assert job.public()["last_activity_at"] == job.last_activity_at


def test_usage_snapshot_reads_translator_and_term_counters(tmp_path):
    manager = webapp.JobManager(tmp_path)
    translator = SimpleNamespace(
        token_count=SimpleNamespace(value=1234),
        prompt_token_count=SimpleNamespace(value=1000),
        completion_token_count=SimpleNamespace(value=234),
        cache_hit_prompt_token_count=SimpleNamespace(value=456),
    )
    config = SimpleNamespace(
        term_extraction_token_usage={"total_tokens": 321}
    )

    assert manager._usage_snapshot(translator, config) == {
        "total_tokens": 1234,
        "prompt_tokens": 1000,
        "completion_tokens": 234,
        "cache_hit_prompt_tokens": 456,
        "term_extraction_tokens": 321,
    }
    manager.executor.shutdown(wait=False, cancel_futures=True)


@pytest.mark.parametrize(
    ("counter_value", "term_value"),
    [(-1, -2), (None, None), ("invalid", "invalid")],
)
def test_usage_snapshot_clamps_or_ignores_invalid_values(
    tmp_path,
    counter_value,
    term_value,
):
    manager = webapp.JobManager(tmp_path)
    translator = SimpleNamespace(
        token_count=SimpleNamespace(value=counter_value),
    )
    config = SimpleNamespace(
        term_extraction_token_usage={"total_tokens": term_value}
    )
    assert manager._usage_snapshot(translator, config) == webapp.empty_token_usage()
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_settings_round_trip_is_encrypted(tmp_path):
    store = webapp.SettingsStore(tmp_path / "中文设置")
    store.save(
        api_key="sk-secret-on-disk",
        base_url="https://example.test/v1",
        model="model-a",
    )

    assert store.api_key() == "sk-secret-on-disk"
    assert b"sk-secret-on-disk" not in store.path.read_bytes()
    assert store.public() == {
        "api_key_saved": True,
        "base_url": "https://example.test/v1",
        "model": "model-a",
    }


def test_settings_updates_preserve_saved_key(tmp_path):
    store = webapp.SettingsStore(tmp_path)
    store.save(api_key="sk-preserved", base_url="https://one.test/v1")
    store.save(base_url="https://two.test/v1", model="model-b")
    assert store.api_key() == "sk-preserved"
    assert store.public()["base_url"] == "https://two.test/v1"
    assert store.public()["model"] == "model-b"


def test_settings_clear_removes_key(tmp_path):
    store = webapp.SettingsStore(tmp_path)
    store.save(api_key="sk-remove")
    public = store.clear_api_key()
    assert public["api_key_saved"] is False
    assert store.api_key() == ""


def test_corrupt_settings_fall_back_to_defaults(tmp_path):
    store = webapp.SettingsStore(tmp_path)
    store.path.write_text("not-json", encoding="utf-8")
    assert store.public() == {
        "api_key_saved": False,
        "base_url": "",
        "model": "gpt-4o-mini",
    }


def test_invalid_encrypted_key_is_not_reported_as_saved(tmp_path):
    store = webapp.SettingsStore(tmp_path)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "api_key": {"scheme": "unknown", "value": "invalid"},
            }
        ),
        encoding="utf-8",
    )
    assert store.api_key() == ""
    assert store.public()["api_key_saved"] is False


def test_root_health_and_default_controls(client_and_manager):
    client, _manager = client_and_manager
    assert client.get("/api/health").json()["status"] == "ok"
    html = client.get("/").text
    assert 'id="model"' in html
    assert "推荐 4" in html
    assert '<option value="no_watermark" selected>' in html
    assert 'aria-label="实时 Token 统计"' in html
    assert 'aria-label="本次 Token 统计"' in html
    assert 'id="glossary-summary"' in html
    assert 'id="glossary-dialog"' in html
    assert 'id="activity-hint"' in html
    assert '<option value="" selected>关闭 · 速度优先</option>' in html
    assert "自动提取术语 <b>高消耗</b>" in html
    assert 'name="auto_extract_glossary" type="checkbox" value="true" checked' not in html


def test_stylesheet_uses_warm_gold_theme(client_and_manager):
    client, _manager = client_and_manager
    css = client.get("/static/styles.css").text
    assert "--gold: #f4c84b" in css
    assert "--gold-bright: #ffdb69" in css
    assert "#c6ff4a" not in css
    assert "#9078ff" not in css


def test_settings_api_does_not_return_key(client_and_manager):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-never-return", model="model-a")
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["api_key_saved"] is True
    assert "sk-never-return" not in response.text


def test_models_requires_api_key(client_and_manager):
    client, _manager = client_and_manager
    response = client.post("/api/models", data={})
    assert response.status_code == 400


def test_models_are_sorted_deduplicated_and_settings_saved(
    client_and_manager,
    monkeypatch,
):
    client, manager = client_and_manager

    class FakeOpenAI:
        def __init__(self, api_key, base_url):
            assert api_key == "sk-models"
            assert base_url == "https://models.test/v1"
            self.models = SimpleNamespace(
                list=lambda: SimpleNamespace(
                    data=[
                        SimpleNamespace(id="z-model"),
                        SimpleNamespace(id="a-model"),
                        SimpleNamespace(id="z-model"),
                    ]
                )
            )

    monkeypatch.setattr(webapp, "OpenAI", FakeOpenAI)
    response = client.post(
        "/api/models",
        data={
            "api_key": "sk-models",
            "base_url": "https://models.test/v1",
        },
    )
    assert response.status_code == 200
    assert response.json()["models"] == ["a-model", "z-model"]
    assert manager.settings.api_key() == "sk-models"
    assert b"sk-models" not in manager.settings.path.read_bytes()


def test_models_maps_provider_failure_to_502(client_and_manager, monkeypatch):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")

    class FailingModels:
        @staticmethod
        def list():
            raise RuntimeError("provider unavailable")

    class FailingOpenAI:
        def __init__(self, **_kwargs):
            self.models = FailingModels()

    monkeypatch.setattr(webapp, "OpenAI", FailingOpenAI)
    response = client.post("/api/models", data={})
    assert response.status_code == 502
    assert "provider unavailable" not in response.text


def test_models_rejects_empty_provider_result(client_and_manager, monkeypatch):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")

    class EmptyOpenAI:
        def __init__(self, **_kwargs):
            self.models = SimpleNamespace(list=lambda: SimpleNamespace(data=[]))

    monkeypatch.setattr(webapp, "OpenAI", EmptyOpenAI)
    assert client.post("/api/models", data={}).status_code == 502


def test_job_uses_saved_key_model_and_no_watermark_default(client_and_manager):
    client, manager = client_and_manager
    manager.settings.save(
        api_key="sk-saved",
        base_url="https://saved.test/v1",
        model="saved-model",
    )
    response = post_job(client)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["options"]["base_url"] == "https://saved.test/v1"
    assert payload["options"]["model"] == "saved-model"
    assert payload["options"]["watermark_mode"] == "no_watermark"
    assert payload["options"]["reasoning"] is None
    assert payload["options"]["auto_extract_glossary"] is False
    assert "sk-saved" not in response.text


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [("", None), ("low", "low"), ("medium", "medium"), ("high", "high")],
)
def test_job_accepts_reasoning_efforts(client_and_manager, reasoning, expected):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")
    response = post_job(client, data={"reasoning": reasoning})
    assert response.status_code == 202
    assert response.json()["options"]["reasoning"] == expected


@pytest.mark.parametrize("reasoning", ["minimal", "extreme", "enabled"])
def test_job_rejects_unknown_reasoning_effort(client_and_manager, reasoning):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")
    assert post_job(client, data={"reasoning": reasoning}).status_code == 400


def test_new_job_settings_are_persisted(client_and_manager):
    client, manager = client_and_manager
    response = post_job(
        client,
        data={
            "api_key": "sk-new",
            "base_url": "https://new.test/v1",
            "model": "new-model",
        },
    )
    assert response.status_code == 202
    assert manager.settings.api_key() == "sk-new"
    assert manager.settings.public()["model"] == "new-model"


def test_job_requires_new_or_saved_key(client_and_manager):
    client, _manager = client_and_manager
    response = post_job(client)
    assert response.status_code == 400


def test_job_rejects_non_pdf_extension(client_and_manager):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")
    response = post_job(client, filename="paper.txt")
    assert response.status_code == 400


@pytest.mark.parametrize("qps", [0, 33])
def test_job_rejects_qps_outside_boundaries(client_and_manager, qps):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")
    response = post_job(client, data={"qps": str(qps)})
    assert response.status_code == 400


@pytest.mark.parametrize("qps", [1, 32])
def test_job_accepts_qps_boundaries(client_and_manager, qps):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")
    response = post_job(client, data={"qps": str(qps)})
    assert response.status_code == 202
    assert response.json()["options"]["qps"] == qps


def test_job_rejects_non_numeric_qps(client_and_manager):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")
    assert post_job(client, data={"qps": "fast"}).status_code == 422


@pytest.mark.parametrize(
    "data",
    [
        {"output_mode": "invalid"},
        {"watermark_mode": "invalid"},
    ],
)
def test_job_rejects_invalid_choice_values(client_and_manager, data):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")
    assert post_job(client, data=data).status_code == 400


def test_upload_size_limit_removes_partial_file(client_and_manager, monkeypatch):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")
    monkeypatch.setattr(webapp, "MAX_UPLOAD_BYTES", 8)
    response = post_job(client, body=b"%PDF-more-than-eight-bytes")
    assert response.status_code == 413
    assert not any(path.is_file() for path in manager.jobs_dir.rglob("*"))


def test_uploaded_filename_is_sanitized(client_and_manager):
    client, manager = client_and_manager
    manager.settings.save(api_key="sk-saved")
    response = post_job(client, filename="../测试 sample.pdf")
    assert response.status_code == 202
    job = manager.get(response.json()["id"])
    assert job.filename == "测试-sample.pdf"
    assert job.input_path.parent.name == "input"


def test_cancel_missing_job_returns_404(client_and_manager):
    client, _manager = client_and_manager
    assert client.post("/api/jobs/missing/cancel").status_code == 404


def test_cancel_queued_job_marks_it_cancelled(tmp_path):
    manager = webapp.JobManager(tmp_path)
    job = webapp.WebJob(
        id="queued",
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=tmp_path / "output",
        options=webapp.JobOptions(api_key="sk"),
    )

    class CancellableFuture:
        @staticmethod
        def cancel():
            return True

    job.future = CancellableFuture()
    manager.jobs[job.id] = job
    assert manager.cancel(job.id).status == "cancelled"
    assert job.finished_at is not None
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_cancel_running_job_calls_translation_config(tmp_path):
    manager = webapp.JobManager(tmp_path)
    job = webapp.WebJob(
        id="running",
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=tmp_path / "output",
        options=webapp.JobOptions(api_key="sk"),
        status="running",
    )
    config = SimpleNamespace(cancelled=False)
    config.cancel_translation = lambda: setattr(config, "cancelled", True)
    job.config = config
    manager.jobs[job.id] = job
    manager.cancel(job.id)
    assert job.cancel_requested is True
    assert config.cancelled is True
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_download_serves_only_job_output(client_and_manager, tmp_path):
    client, manager = client_and_manager
    output_dir = tmp_path / "jobs" / "download" / "output"
    output_dir.mkdir(parents=True)
    result_file = output_dir / "translated.pdf"
    result_file.write_bytes(b"translated")
    glossary_file = output_dir / "glossary.csv"
    glossary_file.write_text("source,target\nterm,术语\n", encoding="utf-8")
    job = webapp.WebJob(
        id="download",
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=output_dir,
        options=webapp.JobOptions(api_key="sk"),
    )
    manager.jobs[job.id] = job

    response = client.get("/api/jobs/download/downloads/translated.pdf")
    assert response.status_code == 200
    assert response.content == b"translated"
    assert client.get("/api/jobs/download/downloads/missing.pdf").status_code == 404
    assert client.get("/api/jobs/download/downloads/..%5Csecret.pdf").status_code == 400
    assert client.get("/api/jobs/download/downloads/glossary.csv").status_code == 404


def test_completed_files_remain_available_after_service_restart(client_and_manager):
    client, manager = client_and_manager
    job_id = "abcdef123456"
    output_dir = manager.jobs_dir / job_id / "output"
    output_dir.mkdir(parents=True)
    pdf = output_dir / "translated.pdf"
    pdf.write_bytes(b"translated-after-restart")
    glossary = output_dir / "translated.glossary.csv"
    glossary.write_text(
        "source,target,tgt_lng\nterm,术语,zh\n",
        encoding="utf-8",
    )

    download = client.get(f"/api/jobs/{job_id}/downloads/{pdf.name}")
    assert download.status_code == 200
    assert download.content == b"translated-after-restart"
    terms = client.get(f"/api/jobs/{job_id}/glossary")
    assert terms.status_code == 200
    assert terms.json()["entries"][0]["target"] == "术语"


def test_disk_result_fallback_rejects_invalid_job_id(client_and_manager):
    client, _manager = client_and_manager
    assert client.get("/api/jobs/not-a-job/downloads/result.pdf").status_code == 404
    assert client.get("/api/jobs/not-a-job/glossary").status_code == 404


def test_glossary_api_returns_entries_without_exposing_file(
    client_and_manager,
    tmp_path,
):
    client, manager = client_and_manager
    output_dir = tmp_path / "jobs" / "terms" / "output"
    output_dir.mkdir(parents=True)
    glossary_file = output_dir / "terms.csv"
    glossary_file.write_text(
        "\ufeffsource,target,tgt_lng\n"
        "StoryScope,StoryScope,zh\n"
        "\n"
        "AI fiction,AI小说,zh\n",
        encoding="utf-8",
    )
    job = webapp.WebJob(
        id="terms",
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=output_dir,
        options=webapp.JobOptions(api_key="sk"),
        glossary_path=glossary_file,
    )
    manager.jobs[job.id] = job

    response = client.get("/api/jobs/terms/glossary")
    assert response.status_code == 200
    assert response.json() == {
        "count": 2,
        "entries": [
            {"source": "StoryScope", "target": "StoryScope", "language": "zh"},
            {"source": "AI fiction", "target": "AI小说", "language": "zh"},
        ],
        "truncated": False,
        "limit": webapp.MAX_GLOSSARY_ROWS,
    }
    assert client.get("/api/jobs/terms/downloads/terms.csv").status_code == 404


def test_glossary_api_missing_job_or_file_returns_404(client_and_manager, tmp_path):
    client, manager = client_and_manager
    assert client.get("/api/jobs/missing/glossary").status_code == 404
    job = webapp.WebJob(
        id="no-terms",
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=tmp_path / "output",
        options=webapp.JobOptions(api_key="sk"),
    )
    manager.jobs[job.id] = job
    assert client.get("/api/jobs/no-terms/glossary").status_code == 404


def test_collect_outputs_deduplicates_and_blocks_outside_files(tmp_path):
    manager = webapp.JobManager(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    valid = output_dir / "translated.pdf"
    valid.write_bytes(b"pdf")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    job = webapp.WebJob(
        id="result",
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=output_dir,
        options=webapp.JobOptions(api_key="sk"),
    )
    result = SimpleNamespace(
        mono_pdf_path=valid,
        dual_pdf_path=valid,
        no_watermark_mono_pdf_path=outside,
        no_watermark_dual_pdf_path=None,
        auto_extracted_glossary_path=None,
    )
    outputs = manager._collect_outputs(job, result)
    assert [item["name"] for item in outputs] == ["translated.pdf"]
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_collect_glossary_builds_count_top_ten_and_web_url(tmp_path):
    manager = webapp.JobManager(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    glossary_file = output_dir / "terms.csv"
    rows = [f"source-{index},译文-{index},zh" for index in range(12)]
    glossary_file.write_text(
        "source,target,tgt_lng\n" + "\n".join(rows),
        encoding="utf-8",
    )
    job = webapp.WebJob(
        id="glossary",
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=output_dir,
        options=webapp.JobOptions(api_key="sk"),
    )
    summary, path = manager._collect_glossary(
        job,
        SimpleNamespace(auto_extracted_glossary_path=glossary_file),
    )
    assert path == glossary_file.resolve()
    assert summary["available"] is True
    assert summary["count"] == 12
    assert len(summary["top_terms"]) == 10
    assert summary["top_terms"][0] == {
        "source": "source-0",
        "target": "译文-0",
        "language": "zh",
    }
    assert summary["url"] == "/api/jobs/glossary/glossary"
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_read_glossary_reports_truncation_and_skips_blank_rows(tmp_path):
    glossary_file = tmp_path / "terms.csv"
    glossary_file.write_text(
        "source,target,tgt_lng\n"
        "one,一,zh\n"
        ",,\n"
        "two,二,zh\n"
        "three,三,zh\n",
        encoding="utf-8",
    )
    count, entries, truncated = webapp.JobManager._read_glossary(
        glossary_file,
        limit=2,
    )
    assert count == 3
    assert [entry["source"] for entry in entries] == ["one", "two"]
    assert truncated is True


def test_collect_glossary_blocks_files_outside_job_output(tmp_path):
    manager = webapp.JobManager(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    outside = tmp_path / "terms.csv"
    outside.write_text("source,target\nterm,术语\n", encoding="utf-8")
    job = webapp.WebJob(
        id="glossary",
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=output_dir,
        options=webapp.JobOptions(api_key="sk"),
    )
    summary, path = manager._collect_glossary(
        job,
        SimpleNamespace(auto_extracted_glossary_path=outside),
    )
    assert summary == webapp.empty_glossary_summary()
    assert path is None
    manager.executor.shutdown(wait=False, cancel_futures=True)


def make_manager_job(manager, tmp_path, job_id="run"):
    job = webapp.WebJob(
        id=job_id,
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=tmp_path / "output",
        options=webapp.JobOptions(api_key="sk-transient"),
    )
    manager.jobs[job.id] = job
    return job


def test_manager_update_refreshes_last_activity(tmp_path, monkeypatch):
    manager = webapp.JobManager(tmp_path)
    job = make_manager_job(manager, tmp_path)
    job.last_activity_at = "before"
    monkeypatch.setattr(webapp, "utc_now", lambda: "after")
    manager._update(job, progress=12.34)
    assert job.progress == 12.34
    assert job.last_activity_at == "after"
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_translate_forwards_reasoning_to_openai_translator(tmp_path, monkeypatch):
    manager = webapp.JobManager(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    job = webapp.WebJob(
        id="reasoning",
        filename="paper.pdf",
        input_path=tmp_path / "paper.pdf",
        output_dir=output_dir,
        options=webapp.JobOptions(api_key="sk", reasoning="medium"),
    )
    captured = {}

    class FakeTranslator:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.token_count = SimpleNamespace(value=0)
            self.prompt_token_count = SimpleNamespace(value=0)
            self.completion_token_count = SimpleNamespace(value=0)
            self.cache_hit_prompt_token_count = SimpleNamespace(value=0)

    class FakeConfig:
        def __init__(self, **kwargs):
            self.term_extraction_token_usage = {"total_tokens": 0}
            self.kwargs = kwargs

    class FakeLayout:
        @staticmethod
        def init_font_mapper(_config):
            return None

    async def fake_translate(_config):
        yield {
            "type": "finish",
            "translate_result": SimpleNamespace(
                mono_pdf_path=None,
                dual_pdf_path=None,
                no_watermark_mono_pdf_path=None,
                no_watermark_dual_pdf_path=None,
                auto_extracted_glossary_path=None,
            ),
        }

    monkeypatch.setattr(webapp, "OpenAITranslator", FakeTranslator)
    monkeypatch.setattr(webapp, "TranslationConfig", FakeConfig)
    monkeypatch.setattr(manager, "_load_doc_layout_model", lambda: FakeLayout())
    monkeypatch.setattr(webapp.high_level, "async_translate", fake_translate)
    monkeypatch.setattr(webapp, "set_translate_rate_limiter", lambda _qps: None)

    asyncio.run(manager._translate(job))
    assert captured["reasoning"] == "medium"
    assert job.status == "completed"
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_run_clears_transient_key_after_success(tmp_path, monkeypatch):
    manager = webapp.JobManager(tmp_path)
    job = make_manager_job(manager, tmp_path)

    async def succeed(current_job):
        manager._update(current_job, status="completed")

    monkeypatch.setattr(manager, "_translate", succeed)
    manager._run(job.id)
    assert job.status == "completed"
    assert job.options.api_key == ""
    assert job.config is None
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_run_converts_exception_to_failed_job(tmp_path, monkeypatch):
    manager = webapp.JobManager(tmp_path)
    job = make_manager_job(manager, tmp_path)

    async def fail(_job):
        raise RuntimeError("translation failed")

    monkeypatch.setattr(manager, "_translate", fail)
    manager._run(job.id)
    assert job.status == "failed"
    assert job.error == "translation failed"
    assert job.finished_at is not None
    assert job.options.api_key == ""
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_run_converts_async_cancellation_to_cancelled_job(tmp_path, monkeypatch):
    manager = webapp.JobManager(tmp_path)
    job = make_manager_job(manager, tmp_path)

    async def cancel(_job):
        raise asyncio.CancelledError

    monkeypatch.setattr(manager, "_translate", cancel)
    manager._run(job.id)
    assert job.status == "cancelled"
    assert job.finished_at is not None
    assert job.options.api_key == ""
    manager.executor.shutdown(wait=False, cancel_futures=True)


def test_cli_defaults_to_lan_and_opens_loopback_url(tmp_path, monkeypatch):
    timer_calls = []
    uvicorn_calls = []

    class FakeTimer:
        def __init__(self, interval, function, args):
            timer_calls.append((interval, function, args))

        def start(self):
            timer_calls.append("started")

    monkeypatch.setattr(webapp.argparse.ArgumentParser, "parse_args", lambda _self: SimpleNamespace(
        host=webapp.DEFAULT_HOST,
        port=8787,
        data_dir=tmp_path,
        no_browser=False,
    ))
    monkeypatch.setattr(webapp.threading, "Timer", FakeTimer)
    monkeypatch.setattr(webapp.uvicorn, "run", lambda app, host, port: uvicorn_calls.append((app, host, port)))

    webapp.cli()

    assert timer_calls[0][2] == ("http://127.0.0.1:8787",)
    assert timer_calls[1] == "started"
    assert uvicorn_calls[0][1:] == (webapp.DEFAULT_HOST, 8787)
