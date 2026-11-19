"""Cursor adapters with bounded asynchronous I/O and snapshot validation."""

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from time import perf_counter
from typing import Generic, Literal

import httpx
from pydantic import ValidationError

from .contracts import Coverage, FeedPage, Inspection, MarketplaceReview, Row, ShipmentTrace, Source


PAGE_SIZE = 50
MAX_PAGES = 64
MAX_PAGE_BYTES = 1024 * 1024
MAX_ATTEMPTS = 3
Scenario = Literal["normal", "trace_unavailable"]


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceStatus:
    source: Source
    status: Literal["pending", "running", "succeeded", "failed"]
    rows: int = 0
    pages: int = 0
    retries: int = 0
    duplicates: int = 0
    elapsed_ms: int = 0
    snapshot_id: str | None = None
    sha256: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class FeedResult(Generic[Row]):
    status: SourceStatus
    items: tuple[Row, ...]
    coverage: Coverage


class SourceFailure(RuntimeError):
    def __init__(self, status: SourceStatus):
        super().__init__(status.error)
        self.status = status


class _Retryable(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedAdapter(Generic[Row]):
    source: Source
    url: str
    page_model: type[FeedPage[Row]]
    key_field: str
    token: str = field(default="", repr=False)

    async def read(self, client: httpx.AsyncClient, *, fault: bool = False) -> FeedResult[Row]:
        started = perf_counter()
        items: dict[str, Row] = {}
        pagination: set[str] = set()
        cursor = None
        snapshot_id = None
        coverage: Coverage = "records"
        pages = retries = duplicates = 0
        expected_coverage = {"all_ratings", "partial_ratings"} if self.source is Source.REVIEWS else {"records"}

        def status(value, error=None, fingerprint=None) -> SourceStatus:
            return SourceStatus(self.source, value, len(items), pages, retries, duplicates,
                                round((perf_counter() - started) * 1000), snapshot_id, fingerprint, error)

        try:
            while pages < MAX_PAGES:
                params: dict[str, str | int] = {"limit": PAGE_SIZE}
                if cursor is not None:
                    params["cursor"] = cursor
                    params["snapshot"] = snapshot_id
                if fault:
                    params["fault"] = "unavailable"
                for attempt in range(MAX_ATTEMPTS):
                    try:
                        raw = await self._get(client, params)
                        break
                    except _Retryable as exc:
                        if attempt == MAX_ATTEMPTS - 1:
                            raise RuntimeError(str(exc)) from None
                        retries += 1
                        await asyncio.sleep(0.12 * 2 ** attempt)

                try:
                    page = self.page_model.model_validate_json(raw)
                except ValidationError:
                    raise RuntimeError("响应未通过 v1 数据契约校验") from None
                pages += 1
                if pages == 1:
                    snapshot_id, coverage = page.snapshot_id, page.coverage
                if page.snapshot_id != snapshot_id or page.coverage != coverage:
                    raise RuntimeError("分页期间上游快照或统计口径发生变化")
                if coverage not in expected_coverage:
                    raise RuntimeError("数据源声明的 coverage 与契约不一致")
                for item in page.items:
                    key = getattr(item, self.key_field)
                    if key not in items:
                        items[key] = item
                    elif items[key] == item:
                        duplicates += 1
                    else:
                        raise RuntimeError("同一源记录 ID 对应不同内容")

                cursor = page.next_cursor
                if cursor is None:
                    ordered = tuple(items[key] for key in sorted(items))
                    digest = content_hash({
                        "coverage": coverage, "rows": [item.model_dump(mode="json") for item in ordered],
                    })
                    return FeedResult(status("succeeded", fingerprint=digest), ordered, coverage)
                if cursor in pagination:
                    raise RuntimeError("上游游标循环")
                pagination.add(cursor)
            raise RuntimeError(f"分页达到 {MAX_PAGES} 页上限")
        except RuntimeError as exc:
            raise SourceFailure(status("failed", str(exc))) from None

    async def _get(self, client: httpx.AsyncClient, params: dict) -> bytes:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with client.stream("GET", self.url, params=params, headers=headers) as response:
                code = response.status_code
                if code == 429 or 500 <= code < 600:
                    raise _Retryable(f"上游 HTTP {code}")
                if code != 200:
                    raise RuntimeError(f"上游 HTTP {code}")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > MAX_PAGE_BYTES:
                        raise RuntimeError("上游单页超出 1 MiB")
                    payload.extend(chunk)
                return bytes(payload)
        except httpx.RequestError:
            raise _Retryable("上游连接或读取超时") from None


def configured_adapters(port: int, public_demo: bool) -> tuple[tuple[FeedAdapter, ...], bool]:
    types = (
        (Source.REVIEWS, MarketplaceReview, "review_id"),
        (Source.SHIPMENTS, ShipmentTrace, "trace_ref"),
        (Source.INSPECTIONS, Inspection, "inspection_id"),
    )
    overrides = [os.getenv(f"SENTINEL_{source.value.upper()}_URL", "").strip() for source, _, _ in types]
    if any(overrides) and (public_demo or not all(overrides)):
        raise ValueError("企业源接入须配置三条服务端 URL，并使用本地模式。")

    adapters = []
    for (source, row_model, key_field), configured in zip(types, overrides):
        url = configured or f"http://127.0.0.1:{port}/demo-source/v1/{source.value}"
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            raise ValueError(f"{source.value} 的源 URL 无效") from None
        if parsed.scheme not in {"https", "http"} or not parsed.host or parsed.userinfo or parsed.fragment:
            raise ValueError(f"{source.value} 须使用无内嵌凭据的 HTTP(S) URL")
        if parsed.scheme == "http" and parsed.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(f"{source.value} 的远程源 URL 需使用 HTTPS")
        token = os.getenv(f"SENTINEL_{source.value.upper()}_TOKEN", "").strip() if configured else ""
        adapters.append(FeedAdapter(source, url, FeedPage[row_model], key_field, token))
    return tuple(adapters), not any(overrides)
