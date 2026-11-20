"""API 请求/响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentIn(BaseModel):
    lo: int = Field(..., ge=0, description="区间起点，包含，整数微米")
    hi: int = Field(..., description="区间终点，不包含，整数微米，必须大于 lo")
    count: int = Field(..., ge=0, description="该厚度区间内的片数")

    @model_validator(mode="after")
    def _validate_range(self) -> "SegmentIn":
        if self.hi <= self.lo:
            raise ValueError("hi 必须大于 lo（区间含起点、不含终点）")
        return self


class CountReportIn(BaseModel):
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="本次补报的全部厚度区间计数；同一报告内或与已有报告可以重叠。"
        "任一区间与已有计数不相容时，整份报告拒收。",
    )


class InspectionCreate(BaseModel):
    total_pieces: int = Field(..., ge=0, description="纸样总片数")
    note: str | None = Field(None, max_length=500, description="备注，可选")


class InspectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_pieces: int | None = Field(None, ge=0, description="新的总片数，可选")
    note: str | None = Field(None, max_length=500, description="新的备注；传 null 可清空")

    @model_validator(mode="after")
    def _validate_payload(self) -> "InspectionUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供 total_pieces 或 note 中的一项")
        return self


class SegmentOut(BaseModel):
    id: int
    lo: int
    hi: int
    count: int


class CountReportOut(BaseModel):
    id: int
    inspection_id: int
    created_at: str
    segments: list[SegmentOut]


class InspectionSummary(BaseModel):
    id: int
    total_pieces: int
    note: str | None
    created_at: str
    report_count: int = Field(..., description="已接收的补报批次数")
    segment_count: int = Field(..., description="已接收的厚度区间条数")


class InspectionDetail(InspectionSummary):
    reports: list[CountReportOut]


class BoundsOut(BaseModel):
    inspection_id: int
    total_pieces: int
    qualified_lo: int
    qualified_hi: int
    min_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最小值")
    max_count: int = Field(..., description="同时满足全部计数时，合格片数可达到的最大值")


class RecheckRow(BaseModel):
    """矛盾链要求回查的一行计数。"""

    source: str = Field(..., description="本次补报 或 已接收报告")
    report_id: int | None = Field(
        None, description="已接收报告编号；本次补报的行该字段为 null"
    )
    row_number: int = Field(..., description="区间在其所属报告内的行号，从 1 起")
    lo: int
    hi: int
    count: int


class ChainBasis(BaseModel):
    """矛盾链单步上限的依据。"""

    kind: str = Field(
        ..., description="segment=区间计数；total=登记总片数；monotone=累计片数单调规则"
    )
    source: str | None = Field(None, description="本次补报 或 已接收报告（segment 时）")
    report_id: int | None = Field(None, description="报告编号；本次补报为 null")
    row_number: int | None = Field(None, description="报告内区间行号，从 1 起")
    lo: int | None = None
    hi: int | None = None
    count: int | None = None
    total_pieces: int | None = Field(None, description="登记总片数数值（total 时）")
    rule: str | None = Field(None, description="规则文字说明（monotone 时）")


class ChainStepOut(BaseModel):
    """矛盾链一步：F(end) - F(start) <= upper_bound。"""

    start: int | str = Field(..., description="起点端点厚度；'+∞' 表示全部片数端点")
    end: int | str = Field(..., description="终点端点厚度；'+∞' 表示全部片数端点")
    upper_bound: int = Field(..., description="终点累计片数减起点累计片数的上限")
    basis: ChainBasis


class ContradictionChainOut(BaseModel):
    steps: list[ChainStepOut]
    bound_sum: int = Field(
        ..., description="各步上限之和；首尾相接且该值严格小于零即 0 <= 负数 的矛盾"
    )
    closed_loop: bool = Field(..., description="是否首尾相接成环")


class RejectionDetail(BaseModel):
    """409 整份拒收回执：回查行清单 + 可验算矛盾链。"""

    message: str
    recheck_rows: list[RecheckRow]
    contradiction_chain: ContradictionChainOut
