from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import ctypes
import json
import logging
import re
import sys
import threading
import uuid
import webbrowser
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Annotated
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from babeldoc.format.pdf import high_level
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.translation_config import WatermarkOutputMode
from babeldoc.translator.translator import OpenAITranslator
from babeldoc.translator.translator import set_translate_rate_limiter

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_DATA_DIR = Path.cwd() / ".babeldoc-web"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_GLOSSARY_ROWS = 20_000
REASONING_EFFORTS = {"", "low", "medium", "high"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def empty_token_usage() -> dict[str, int]:
    return {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_prompt_tokens": 0,
        "term_extraction_tokens": 0,
    }


def empty_glossary_summary() -> dict[str, Any]:
    return {
        "available": False,
        "count": 0,
        "top_terms": [],
        "url": None,
    }


def safe_filename(filename: str | None) -> str:
    name = Path(filename or "document.pdf").name
    source_stem = "" if name.casefold() == ".pdf" else Path(name).stem
    stem = re.sub(
        r"[^0-9A-Za-z\u4e00-\u9fff._-]+",
        "-",
        source_stem,
    ).strip(".-")
    stem = stem[:116].rstrip(".-")
    reserved = {
        "aux",
        "clock$",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if stem.casefold() in reserved:
        stem = f"_{stem}"
    return f"{stem or 'document'}.pdf"


class SettingsStore:
    """Persist local settings while protecting API keys with the OS account."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "settings.json"
        self.key_path = data_dir / "settings.key"
        self.lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("Unable to read BabelDOC web settings")
            return {"version": 1}

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _dpapi(value: bytes, *, protect: bool) -> bytes:
        class DataBlob(ctypes.Structure):
            _fields_ = [
                ("size", ctypes.c_ulong),
                ("data", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        input_blob = DataBlob(len(value), buffer)
        output_blob = DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if protect:
            succeeded = crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "BabelDOC Web API Key",
                None,
                None,
                None,
                0x1,
                ctypes.byref(output_blob),
            )
        else:
            succeeded = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                0x1,
                ctypes.byref(output_blob),
            )
        if not succeeded:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.data, output_blob.size)
        finally:
            kernel32.LocalFree(output_blob.data)

    def _fallback_cipher(self):
        from cryptography.fernet import Fernet

        if not self.key_path.exists():
            self.key_path.write_bytes(Fernet.generate_key())
            try:
                self.key_path.chmod(0o600)
            except OSError:
                pass
        return Fernet(self.key_path.read_bytes())

    def _protect(self, value: str) -> dict[str, str]:
        raw = value.encode("utf-8")
        if sys.platform == "win32":
            encrypted = self._dpapi(raw, protect=True)
            return {
                "scheme": "windows-dpapi",
                "value": base64.b64encode(encrypted).decode("ascii"),
            }
        encrypted = self._fallback_cipher().encrypt(raw)
        return {"scheme": "fernet", "value": encrypted.decode("ascii")}

    def _unprotect(self, value: dict[str, str]) -> str:
        scheme = value.get("scheme")
        encrypted = value.get("value", "")
        if scheme == "windows-dpapi":
            raw = self._dpapi(base64.b64decode(encrypted), protect=False)
        elif scheme == "fernet":
            raw = self._fallback_cipher().decrypt(encrypted.encode("ascii"))
        else:
            return ""
        return raw.decode("utf-8")

    def api_key(self) -> str:
        with self.lock:
            protected = self._read().get("api_key")
            if not isinstance(protected, dict):
                return ""
            try:
                return self._unprotect(protected)
            except Exception:
                logger.exception("Unable to decrypt saved API key")
                return ""

    def public(self) -> dict[str, Any]:
        with self.lock:
            value = self._read()
            return {
                "api_key_saved": bool(self.api_key()),
                "base_url": value.get("base_url", ""),
                "model": value.get("model", "gpt-4o-mini"),
            }

    def save(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            value = self._read()
            value["version"] = 1
            if api_key:
                value["api_key"] = self._protect(api_key)
            if base_url is not None:
                value["base_url"] = base_url
            if model is not None:
                value["model"] = model
            self._write(value)
            return self.public()

    def clear_api_key(self) -> dict[str, Any]:
        with self.lock:
            value = self._read()
            value.pop("api_key", None)
            self._write(value)
            return self.public()


@dataclass
class JobOptions:
    api_key: str
    base_url: str | None = None
    model: str = "gpt-4o-mini"
    lang_in: str = "en"
    lang_out: str = "zh"
    pages: str | None = None
    qps: int = 4
    reasoning: str | None = None
    output_mode: str = "both"
    watermark_mode: str = "no_watermark"
    skip_scanned_detection: bool = False
    enhance_compatibility: bool = False
    ocr_workaround: bool = False
    use_alternating_pages_dual: bool = False
    disable_rich_text_translate: bool = False
    auto_extract_glossary: bool = False

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("api_key", None)
        return value


@dataclass
class WebJob:
    id: str
    filename: str
    input_path: Path
    output_dir: Path
    options: JobOptions
    status: str = "queued"
    progress: float = 0.0
    stage: str = "等待处理"
    stage_progress: float = 0.0
    message: str = "任务已加入队列"
    outputs: list[dict[str, str]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=empty_token_usage)
    glossary: dict[str, Any] = field(default_factory=empty_glossary_summary)
    error: str | None = None
    created_at: str = field(default_factory=utc_now)
    last_activity_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    config: TranslationConfig | None = field(default=None, repr=False)
    glossary_path: Path | None = field(default=None, repr=False)
    future: Future | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "progress": round(self.progress, 2),
            "stage": self.stage,
            "stage_progress": round(self.stage_progress, 2),
            "message": self.message,
            "outputs": self.outputs,
            "token_usage": dict(self.token_usage),
            "glossary": dict(self.glossary),
            "error": self.error,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
            "options": self.options.public(),
        }


class JobManager:
    """Run one BabelDOC translation at a time and expose thread-safe state."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.jobs_dir = data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.settings = SettingsStore(data_dir)
        self.jobs: dict[str, WebJob] = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="babeldoc-web",
        )
        self.doc_layout_model = None
        high_level.init()

    def create(
        self,
        input_path: Path,
        filename: str,
        output_dir: Path,
        options: JobOptions,
    ) -> WebJob:
        job = WebJob(
            id=output_dir.parent.name,
            filename=filename,
            input_path=input_path,
            output_dir=output_dir,
            options=options,
        )
        with self.lock:
            self.jobs[job.id] = job
            job.future = self.executor.submit(self._run, job.id)
        return job

    def get(self, job_id: str) -> WebJob:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return job

    def list_recent(self) -> list[dict[str, Any]]:
        with self.lock:
            jobs = sorted(
                self.jobs.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )
            return [job.public() for job in jobs[:20]]

    def cancel(self, job_id: str) -> WebJob:
        with self.lock:
            job = self.get(job_id)
            if job.status in {"completed", "failed", "cancelled"}:
                return job
            job.cancel_requested = True
            job.message = "正在取消任务…"
            if job.status == "queued" and job.future and job.future.cancel():
                job.status = "cancelled"
                job.finished_at = utc_now()
                job.message = "任务已取消"
            elif job.config is not None:
                job.config.cancel_translation()
            return job

    def _update(self, job: WebJob, **values: Any) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(job, key, value)
            if "last_activity_at" not in values:
                job.last_activity_at = utc_now()

    @staticmethod
    def _usage_snapshot(
        translator: OpenAITranslator,
        config: TranslationConfig,
    ) -> dict[str, int]:
        def counter_value(name: str) -> int:
            counter = getattr(translator, name, None)
            try:
                return max(0, int(getattr(counter, "value", 0) or 0))
            except (TypeError, ValueError):
                return 0

        term_usage = getattr(config, "term_extraction_token_usage", {}) or {}
        try:
            term_tokens = max(0, int(term_usage.get("total_tokens", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            term_tokens = 0

        return {
            "total_tokens": counter_value("token_count"),
            "prompt_tokens": counter_value("prompt_token_count"),
            "completion_tokens": counter_value("completion_token_count"),
            "cache_hit_prompt_tokens": counter_value(
                "cache_hit_prompt_token_count"
            ),
            "term_extraction_tokens": term_tokens,
        }

    def _load_doc_layout_model(self):
        if self.doc_layout_model is None:
            from babeldoc.docvision.doclayout import DocLayoutModel

            self.doc_layout_model = DocLayoutModel.load_onnx()
        return self.doc_layout_model

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        self._update(
            job,
            status="running",
            started_at=utc_now(),
            stage="准备模型",
            message="正在加载版面分析模型，首次运行可能需要下载资源",
        )
        try:
            asyncio.run(self._translate(job))
        except asyncio.CancelledError:
            self._update(
                job,
                status="cancelled",
                finished_at=utc_now(),
                message="任务已取消",
            )
        except Exception as exc:
            logger.exception("BabelDOC web job %s failed", job.id)
            self._update(
                job,
                status="failed",
                finished_at=utc_now(),
                error=str(exc),
                message="翻译失败，请检查接口参数或服务日志",
            )
        finally:
            with self.lock:
                job.config = None
                job.options.api_key = ""

    async def _translate(self, job: WebJob) -> None:
        options = job.options
        set_translate_rate_limiter(options.qps)
        translator = OpenAITranslator(
            lang_in=options.lang_in,
            lang_out=options.lang_out,
            model=options.model,
            base_url=options.base_url or None,
            api_key=options.api_key,
            reasoning=options.reasoning,
        )
        layout_model = self._load_doc_layout_model()
        watermark = WatermarkOutputMode(options.watermark_mode)

        config = TranslationConfig(
            input_file=job.input_path,
            output_dir=job.output_dir,
            translator=translator,
            lang_in=options.lang_in,
            lang_out=options.lang_out,
            pages=options.pages or None,
            qps=options.qps,
            no_dual=options.output_mode == "mono",
            no_mono=options.output_mode == "dual",
            watermark_output_mode=watermark,
            doc_layout_model=layout_model,
            skip_scanned_detection=options.skip_scanned_detection,
            enhance_compatibility=options.enhance_compatibility,
            ocr_workaround=options.ocr_workaround,
            use_alternating_pages_dual=options.use_alternating_pages_dual,
            disable_rich_text_translate=options.disable_rich_text_translate,
            auto_extract_glossary=options.auto_extract_glossary,
            report_interval=0.2,
            use_rich_pbar=False,
        )
        with self.lock:
            job.config = config
        getattr(layout_model, "init_font_mapper", lambda _config: None)(config)

        async for event in high_level.async_translate(config):
            event_type = event.get("type")
            token_usage = self._usage_snapshot(translator, config)
            if event_type in {"progress_start", "progress_update", "progress_end"}:
                stage = str(event.get("stage", "处理中"))
                self._update(
                    job,
                    stage=stage,
                    stage_progress=float(event.get("stage_progress", 0.0)),
                    progress=float(event.get("overall_progress", job.progress)),
                    message=f"正在处理：{stage}",
                    token_usage=token_usage,
                )
            elif event_type == "error":
                self._update(job, token_usage=token_usage)
                raise RuntimeError(str(event.get("error", "未知翻译错误")))
            elif event_type == "finish":
                result = event["translate_result"]
                glossary, glossary_path = self._collect_glossary(job, result)
                self._update(
                    job,
                    status="completed",
                    progress=100.0,
                    stage_progress=100.0,
                    stage="处理完成",
                    message="翻译完成，可以下载结果",
                    outputs=self._collect_outputs(job, result),
                    token_usage=token_usage,
                    glossary=glossary,
                    glossary_path=glossary_path,
                    finished_at=utc_now(),
                )
                return

        if job.cancel_requested:
            raise asyncio.CancelledError
        raise RuntimeError("翻译进程意外结束，未返回结果")

    def _collect_outputs(self, job: WebJob, result: Any) -> list[dict[str, str]]:
        labels = {
            "mono_pdf_path": "纯译文 PDF",
            "dual_pdf_path": "双语对照 PDF",
            "no_watermark_mono_pdf_path": "无水印纯译文 PDF",
            "no_watermark_dual_pdf_path": "无水印双语对照 PDF",
        }
        seen: set[Path] = set()
        outputs: list[dict[str, str]] = []
        root = job.output_dir.resolve()
        for attr, label in labels.items():
            value = getattr(result, attr, None)
            if not value:
                continue
            path = Path(value).resolve()
            if path in seen or not path.is_file() or root not in path.parents:
                continue
            seen.add(path)
            outputs.append(
                {
                    "name": path.name,
                    "label": label,
                    "url": f"/api/jobs/{job.id}/downloads/{path.name}",
                }
            )
        return outputs

    @staticmethod
    def _read_glossary(
        path: Path,
        limit: int = MAX_GLOSSARY_ROWS,
    ) -> tuple[int, list[dict[str, str]], bool]:
        count = 0
        entries: list[dict[str, str]] = []
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                source = str(row.get("source", "") or "").strip()
                target = str(row.get("target", "") or "").strip()
                language = str(row.get("tgt_lng", "") or "").strip()
                if not source and not target:
                    continue
                count += 1
                if len(entries) < limit:
                    entries.append(
                        {
                            "source": source,
                            "target": target,
                            "language": language,
                        }
                    )
        return count, entries, count > len(entries)

    def _collect_glossary(
        self,
        job: WebJob,
        result: Any,
    ) -> tuple[dict[str, Any], Path | None]:
        value = getattr(result, "auto_extracted_glossary_path", None)
        if not value:
            return empty_glossary_summary(), None
        path = Path(value).resolve()
        root = job.output_dir.resolve()
        if path.suffix.lower() != ".csv" or root not in path.parents or not path.is_file():
            return empty_glossary_summary(), None
        try:
            count, top_terms, _truncated = self._read_glossary(path, limit=10)
        except (OSError, csv.Error):
            logger.warning("Unable to read glossary for job %s", job.id)
            return empty_glossary_summary(), None
        return (
            {
                "available": True,
                "count": count,
                "top_terms": top_terms,
                "url": f"/api/jobs/{job.id}/glossary",
            },
            path,
        )


def create_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = (data_dir or DEFAULT_DATA_DIR).resolve()
    manager = JobManager(data_dir)
    app = FastAPI(title="BabelDOC Web", version="0.1.0")
    app.state.manager = manager

    def completed_output_dir(job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        output_dir = (manager.jobs_dir / job_id / "output").resolve()
        if manager.jobs_dir.resolve() not in output_dir.parents or not output_dir.is_dir():
            raise HTTPException(status_code=404, detail="任务不存在")
        return output_dir

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "BabelDOC Web"}

    @app.get("/api/jobs")
    async def jobs():
        return manager.list_recent()

    @app.get("/api/settings")
    async def settings():
        return manager.settings.public()

    @app.delete("/api/settings/api-key")
    async def clear_saved_api_key():
        return manager.settings.clear_api_key()

    @app.post("/api/models")
    async def models(
        api_key: Annotated[str, Form()] = "",
        base_url: Annotated[str, Form()] = "",
    ):
        resolved_api_key = api_key.strip() or manager.settings.api_key()
        if not resolved_api_key:
            raise HTTPException(status_code=400, detail="请先填写 API Key")
        saved = manager.settings.public()
        resolved_base_url = base_url.strip() or saved["base_url"] or None

        def fetch_models() -> list[str]:
            client = OpenAI(
                api_key=resolved_api_key,
                base_url=resolved_base_url,
            )
            return sorted({item.id for item in client.models.list().data})

        try:
            available_models = await asyncio.to_thread(fetch_models)
        except Exception as exc:
            logger.warning("Unable to list models: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="无法读取模型列表，请检查接口地址和 API Key",
            ) from exc
        if not available_models:
            raise HTTPException(status_code=502, detail="接口没有返回可用模型")
        manager.settings.save(
            api_key=api_key.strip() or None,
            base_url=resolved_base_url or "",
        )
        return {"models": available_models, **manager.settings.public()}

    @app.post("/api/jobs", status_code=202)
    async def create_job(
        pdf: Annotated[UploadFile, File()],
        api_key: Annotated[str, Form()] = "",
        base_url: Annotated[str, Form()] = "",
        model: Annotated[str, Form()] = "",
        lang_in: Annotated[str, Form()] = "en",
        lang_out: Annotated[str, Form()] = "zh",
        pages: Annotated[str, Form()] = "",
        qps: Annotated[int, Form()] = 4,
        reasoning: Annotated[str, Form()] = "",
        output_mode: Annotated[str, Form()] = "both",
        watermark_mode: Annotated[str, Form()] = "no_watermark",
        skip_scanned_detection: Annotated[bool, Form()] = False,
        enhance_compatibility: Annotated[bool, Form()] = False,
        ocr_workaround: Annotated[bool, Form()] = False,
        use_alternating_pages_dual: Annotated[bool, Form()] = False,
        disable_rich_text_translate: Annotated[bool, Form()] = False,
        auto_extract_glossary: Annotated[bool, Form()] = False,
    ):
        if not (pdf.filename or "").lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="只支持 PDF 文件")
        saved = manager.settings.public()
        resolved_api_key = api_key.strip() or manager.settings.api_key()
        resolved_base_url = base_url.strip() or saved["base_url"] or None
        resolved_model = model.strip() or saved["model"] or "gpt-4o-mini"
        if not resolved_api_key:
            raise HTTPException(status_code=400, detail="请填写并保存 API Key")
        if not resolved_model:
            raise HTTPException(status_code=400, detail="请填写模型名称")
        if not 1 <= qps <= 32:
            raise HTTPException(status_code=400, detail="QPS 必须在 1 到 32 之间")
        reasoning = reasoning.strip().lower()
        if reasoning not in REASONING_EFFORTS:
            raise HTTPException(status_code=400, detail="无效的 Reasoning 强度")
        if output_mode not in {"both", "mono", "dual"}:
            raise HTTPException(status_code=400, detail="无效的输出模式")
        if watermark_mode not in {item.value for item in WatermarkOutputMode}:
            raise HTTPException(status_code=400, detail="无效的水印模式")

        job_id = uuid.uuid4().hex[:12]
        filename = safe_filename(pdf.filename)
        job_dir = manager.jobs_dir / job_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        input_dir.mkdir(parents=True, exist_ok=False)
        output_dir.mkdir(parents=True, exist_ok=False)
        input_path = input_dir / filename
        total = 0
        try:
            with input_path.open("wb") as target:
                while chunk := await pdf.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="PDF 不能超过 500 MB",
                        )
                    target.write(chunk)
        except Exception:
            input_path.unlink(missing_ok=True)
            raise
        finally:
            await pdf.close()

        options = JobOptions(
            api_key=resolved_api_key,
            base_url=resolved_base_url,
            model=resolved_model,
            lang_in=lang_in.strip() or "en",
            lang_out=lang_out.strip() or "zh",
            pages=pages.strip() or None,
            qps=qps,
            reasoning=reasoning or None,
            output_mode=output_mode,
            watermark_mode=watermark_mode,
            skip_scanned_detection=skip_scanned_detection,
            enhance_compatibility=enhance_compatibility,
            ocr_workaround=ocr_workaround,
            use_alternating_pages_dual=use_alternating_pages_dual,
            disable_rich_text_translate=disable_rich_text_translate,
            auto_extract_glossary=auto_extract_glossary,
        )
        manager.settings.save(
            api_key=api_key.strip() or None,
            base_url=resolved_base_url or "",
            model=resolved_model,
        )
        job = manager.create(input_path, filename, output_dir, options)
        return job.public()

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        try:
            return manager.get(job_id).public()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        try:
            return manager.cancel(job_id).public()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @app.get("/api/jobs/{job_id}/glossary")
    async def glossary(job_id: str):
        try:
            job = manager.get(job_id)
            output_dir = job.output_dir.resolve()
            path = job.glossary_path
        except KeyError:
            output_dir = completed_output_dir(job_id)
            matches = sorted(
                output_dir.glob("*.glossary.csv"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            path = matches[0] if matches else None
        if path is None:
            raise HTTPException(status_code=404, detail="此任务没有术语表")
        path = path.resolve()
        if (
            path.suffix.lower() != ".csv"
            or output_dir not in path.parents
            or not path.is_file()
        ):
            raise HTTPException(status_code=404, detail="术语表不存在")
        try:
            count, entries, truncated = manager._read_glossary(path)
        except (OSError, csv.Error) as exc:
            raise HTTPException(status_code=422, detail="术语表无法读取") from exc
        return {
            "count": count,
            "entries": entries,
            "truncated": truncated,
            "limit": MAX_GLOSSARY_ROWS,
        }

    @app.get("/api/jobs/{job_id}/downloads/{filename}")
    async def download(job_id: str, filename: str):
        try:
            job = manager.get(job_id)
            output_dir = job.output_dir.resolve()
        except KeyError:
            output_dir = completed_output_dir(job_id)
        if Path(filename).name != filename:
            raise HTTPException(status_code=400, detail="无效文件名")
        path = (output_dir / filename).resolve()
        if (
            path.suffix.lower() != ".pdf"
            or output_dir not in path.parents
            or not path.is_file()
        ):
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(path, filename=path.name, media_type="application/pdf")

    return app


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Run the local BabelDOC web interface",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.no_browser:
        threading.Timer(
            1.2,
            webbrowser.open,
            args=(f"http://{args.host}:{args.port}",),
        ).start()
    uvicorn.run(create_app(args.data_dir), host=args.host, port=args.port)


if __name__ == "__main__":
    cli()
