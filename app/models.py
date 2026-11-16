"""请求与响应的 Pydantic 模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _non_empty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("字符串不能为空")
    return value


# ---------- 请求模型 ----------

class ObservationIn(BaseModel):
    code: str = Field(..., min_length=1, description="观测唯一编号")
    name: str = Field(..., min_length=1, description="野外记录的物种名（可能是旧名/同义名）")

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _non_empty(v)


class BatchCreate(BaseModel):
    observations: list[ObservationIn] = Field(
        ..., min_length=1, max_length=100, description="一批观测，至多 100 条"
    )
    note: str | None = Field(None, description="备注")


class EdgeIn(BaseModel):
    synonym: str = Field(..., min_length=1, description="同义名（旧名）")
    target: str = Field(..., min_length=1, description="指向的名称（接受名或另一同义名）")

    @field_validator("synonym", "target")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _non_empty(v)


class TaxonomyCreate(BaseModel):
    accepted_names: list[str] = Field(..., min_length=1, description="接受名列表")
    synonym_edges: list[EdgeIn] = Field(
        default_factory=list, max_length=40, description="同义名指向关系，至多 40 条，允许分叉"
    )
    note: str | None = None

    @field_validator("accepted_names")
    @classmethod
    def _strip_names(cls, values: list[str]) -> list[str]:
        cleaned = [_non_empty(v) for v in values]
        return cleaned


class ResultCreate(BaseModel):
    batch_id: int = Field(..., description="观测批次 ID")
    revision_id: int | None = Field(None, description="分类表修订 ID，缺省使用当前最新修订")


class AcquisitionCreate(BaseModel):
    source_url: str | None = Field(
        None,
        description="分类中心分页源地址，缺省使用 ECO_SOURCE_URL 环境变量",
    )
    note: str | None = Field(None, description="备注")

    @field_validator("source_url")
    @classmethod
    def _strip_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if not v:
            raise ValueError("source_url 不能为空")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("source_url 必须是 http(s) 地址")
        return v


# ---------- 分页源模型 ----------

class SourceEdge(BaseModel):
    synonym: str = Field(..., min_length=1)
    target: str = Field(..., min_length=1)

    @field_validator("synonym", "target")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _non_empty(v)


class SourcePage(BaseModel):
    """HTTP 分页源单页返回：版号 + 接受名 + 同义关系 + 下一页游标。"""

    version: str = Field(..., min_length=1, description="整版版号，跨页必须一致")
    accepted_names: list[str] = Field(default_factory=list)
    synonym_edges: list[SourceEdge] = Field(default_factory=list)
    next_cursor: str | None = Field(None, description="下一页游标；null 表示最后一页")

    @field_validator("version")
    @classmethod
    def _strip_version(cls, v: str) -> str:
        return _non_empty(v)

    @field_validator("accepted_names")
    @classmethod
    def _strip_names(cls, values: list[str]) -> list[str]:
        return [_non_empty(v) for v in values]

    @field_validator("next_cursor")
    @classmethod
    def _strip_cursor(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


# ---------- 响应模型 ----------

class ObservationOut(BaseModel):
    line_no: int
    code: str
    name: str


class BatchOut(BaseModel):
    id: int
    note: str | None
    created_at: str
    observation_count: int
    observations: list[ObservationOut]


class BatchSummaryOut(BaseModel):
    id: int
    note: str | None
    created_at: str
    observation_count: int


class EdgeOut(BaseModel):
    synonym: str
    target: str


class TaxonomyOut(BaseModel):
    revision_id: int
    note: str | None
    created_at: str
    superseded_at: str | None
    accepted_count: int
    edge_count: int
    accepted_names: list[str]
    synonym_edges: list[EdgeOut]


class TaxonomySummaryOut(BaseModel):
    revision_id: int
    note: str | None
    created_at: str
    superseded_at: str | None
    accepted_count: int
    edge_count: int


class ResultItemOut(BaseModel):
    line_no: int
    observation_code: str
    input_name: str
    item_status: str
    accepted_name: str | None
    reason_code: str | None
    reason: str | None


class ResultSummaryOut(BaseModel):
    result_id: int
    batch_id: int
    revision_id: int
    status: str
    created_at: str
    included_count: int
    pending_count: int


class ResultDetailOut(ResultSummaryOut):
    items: list[ResultItemOut]


class AcquisitionPageOut(BaseModel):
    page_no: int
    request_cursor: str | None
    next_cursor: str | None
    version: str
    accepted_count: int
    edge_count: int
    confirmed_at: str


class AcquisitionSummaryOut(BaseModel):
    acquisition_id: int
    source_url: str
    note: str | None
    status: str
    base_revision_id: int | None
    version: str | None
    next_cursor: str | None
    pages_confirmed: int
    accepted_count: int
    edge_count: int
    published_revision_id: int | None
    error_code: str | None
    error_detail: str | None
    created_at: str
    updated_at: str


class AcquisitionOut(AcquisitionSummaryOut):
    pages: list[AcquisitionPageOut]
