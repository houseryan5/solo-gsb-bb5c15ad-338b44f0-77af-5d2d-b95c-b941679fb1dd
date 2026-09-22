"""Pydantic models: request/response schemas and input validation."""
from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import RequestValidationErrors

# Floating point tolerance for balance comparisons.
EPS = 1e-9


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class BucketSpec(BaseModel):
    """令牌桶定义。

    capacity: 桶容量（令牌上限）
    refill_rate: 每秒连续补充的令牌数
    initial: 初始余额，缺省为满桶
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="桶唯一标识")]
    capacity: Annotated[float, Field(description="桶容量，必须为正数")]
    refill_rate: Annotated[float, Field(description="令牌补充速率（令牌/秒），必须非负")]
    initial: Annotated[float | None, Field(default=None, description="初始余额，默认等于容量")] = None

    @field_validator("capacity", "refill_rate", mode="after")
    @classmethod
    def _validate_rate_fields(cls, value, info):
        if info.field_name == "capacity" and value <= 0:
            raise ValueError("capacity 必须为正数")
        if info.field_name == "refill_rate" and value < 0:
            raise ValueError("refill_rate 必须为非负数")
        return value

    @field_validator("initial", mode="before")
    @classmethod
    def _validate_initial(cls, value):
        if value is not None and (not _is_finite_number(value) or value < 0):
            raise ValueError("initial 必须是非负有限数值")
        return value

    @model_validator(mode="after")
    def _check_initial_within_capacity(self):
        if self.initial is not None and self.initial > self.capacity + EPS:
            raise ValueError("initial 不能大于 capacity")
        return self


class SimRequest(BaseModel):
    """单次试算请求。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="请求唯一标识")]
    timestamp: Annotated[float, Field(description="请求时间（秒，任意一致的时间原点）")]
    cost: Annotated[float, Field(gt=0, description="消耗量（从链上每个桶扣减的令牌数）")]
    chain: Annotated[list[str], Field(min_length=1, description="桶链：按顺序引用的桶 ID")]

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value):
        if not _is_finite_number(value):
            raise ValueError("timestamp 必须是有限数值")
        return value

    @field_validator("cost", mode="before")
    @classmethod
    def _validate_cost(cls, value):
        if not _is_finite_number(value):
            raise ValueError("cost 必须是有限数值")
        if value <= 0:
            raise ValueError("cost 必须为正数")
        return value

    @field_validator("chain")
    @classmethod
    def _validate_chain_shape(cls, value: list[str]):
        if not value:
            raise ValueError("chain 至少需要引用一个桶")
        seen: set[str] = set()
        for bucket_id in value:
            if not isinstance(bucket_id, str) or not bucket_id:
                raise ValueError("chain 中的桶 ID 必须是非空字符串")
            if bucket_id in seen:
                raise ValueError(f"chain 中存在重复桶引用: {bucket_id}")
            seen.add(bucket_id)
        return value


def _simulate_cross_validation_errors(
    buckets: list[BucketSpec], requests: list[SimRequest]
) -> list[dict]:
    """/simulate 系请求体的跨字段校验：重复桶、未知桶引用、重复请求 ID。

    /simulate、/plan-initial-balances、/usage-report 与 /fairness-report 共用。
    """
    errors: list[dict] = []

    # 重复桶 ID
    seen: set[str] = set()
    duplicates: set[str] = set()
    for index, bucket in enumerate(buckets):
        if bucket.id in seen:
            duplicates.add(bucket.id)
            errors.append(
                {
                    "type": "duplicate_bucket",
                    "loc": ("buckets", index, "id"),
                    "msg": f"重复的桶 ID: {bucket.id}",
                    "input": bucket.id,
                }
            )
        seen.add(bucket.id)

    if duplicates:
        # 已存在重复定义，引用校验意义不大，直接返回
        return errors

    # 链中未知引用
    for r_index, request in enumerate(requests):
        for c_index, bucket_id in enumerate(request.chain):
            if bucket_id not in seen:
                errors.append(
                    {
                        "type": "unknown_bucket_ref",
                        "loc": ("requests", r_index, "chain", c_index),
                        "msg": f"请求 {request.id!r} 引用了未定义的桶: {bucket_id}",
                        "input": bucket_id,
                    }
                )

    # 重复请求 ID（便于调用方定位模拟结果）
    seen_req: set[str] = set()
    for r_index, request in enumerate(requests):
        if request.id in seen_req:
            errors.append(
                {
                    "type": "duplicate_request",
                    "loc": ("requests", r_index, "id"),
                    "msg": f"重复的请求 ID: {request.id}",
                    "input": request.id,
                }
            )
        seen_req.add(request.id)

    return errors


class SimulateRequest(BaseModel):
    """POST /simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[BucketSpec], Field(min_length=1, description="令牌桶集合")]
    requests: Annotated[list[SimRequest], Field(min_length=1, description="按时间戳模拟的请求序列")]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors = _simulate_cross_validation_errors(self.buckets, self.requests)
        if errors:
            raise RequestValidationErrors(errors)
        return self


class ForecastBucket(BaseModel):
    """预测时刻的桶状态快照。

    capacity: 桶容量（令牌上限）
    refill_rate: 每秒连续补充的令牌数
    balance: 当前余额（now 时刻持有）
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="桶唯一标识")]
    capacity: Annotated[float, Field(description="桶容量，必须为正数")]
    refill_rate: Annotated[float, Field(description="令牌补充速率（令牌/秒），必须非负")]
    balance: Annotated[float, Field(description="当前余额，要求 0 ≤ balance ≤ capacity")]

    @field_validator("capacity", "refill_rate", mode="after")
    @classmethod
    def _validate_rate_fields(cls, value, info):
        if info.field_name == "capacity" and value <= 0:
            raise ValueError("capacity 必须为正数")
        if info.field_name == "refill_rate" and value < 0:
            raise ValueError("refill_rate 必须为非负数")
        return value

    @field_validator("balance", mode="before")
    @classmethod
    def _validate_balance(cls, value):
        if not _is_finite_number(value) or value < 0:
            raise ValueError("balance 必须是非负有限数值")
        return value

    @model_validator(mode="after")
    def _check_balance_within_capacity(self):
        if self.balance > self.capacity + EPS:
            raise ValueError("balance 不能大于 capacity")
        return self


class ForecastRequest(BaseModel):
    """POST /forecast 请求体。"""

    model_config = ConfigDict(extra="forbid")

    now: Annotated[float, Field(description="当前时刻（秒，任意一致的时间原点）")]
    cost: Annotated[float, Field(gt=0, description="请求成本（链上每个桶都需持有的令牌数）")]
    chain: Annotated[list[str], Field(min_length=1, description="桶链：按顺序引用的桶 ID")]
    buckets: Annotated[list[ForecastBucket], Field(min_length=1, description="桶状态快照集合")]

    @field_validator("now", mode="before")
    @classmethod
    def _validate_now(cls, value):
        if not _is_finite_number(value):
            raise ValueError("now 必须是有限数值")
        return value

    @field_validator("cost", mode="before")
    @classmethod
    def _validate_cost(cls, value):
        if not _is_finite_number(value):
            raise ValueError("cost 必须是有限数值")
        if value <= 0:
            raise ValueError("cost 必须为正数")
        return value

    @field_validator("chain")
    @classmethod
    def _validate_chain_shape(cls, value: list[str]):
        if not value:
            raise ValueError("chain 至少需要引用一个桶")
        seen: set[str] = set()
        for bucket_id in value:
            if not isinstance(bucket_id, str) or not bucket_id:
                raise ValueError("chain 中的桶 ID 必须是非空字符串")
            if bucket_id in seen:
                raise ValueError(f"chain 中存在重复桶引用: {bucket_id}")
            seen.add(bucket_id)
        return value

    @model_validator(mode="after")
    def _cross_validate(self):
        errors: list[dict] = []

        # 重复桶 ID
        seen: set[str] = set()
        duplicates = False
        for index, bucket in enumerate(self.buckets):
            if bucket.id in seen:
                duplicates = True
                errors.append(
                    {
                        "type": "duplicate_bucket",
                        "loc": ("buckets", index, "id"),
                        "msg": f"重复的桶 ID: {bucket.id}",
                        "input": bucket.id,
                    }
                )
            seen.add(bucket.id)

        if duplicates:
            # 已存在重复定义，引用校验意义不大，直接返回
            raise RequestValidationErrors(errors)

        # 链中未知引用
        for c_index, bucket_id in enumerate(self.chain):
            if bucket_id not in seen:
                errors.append(
                    {
                        "type": "unknown_bucket_ref",
                        "loc": ("chain", c_index),
                        "msg": f"桶链引用了未定义的桶: {bucket_id}",
                        "input": bucket_id,
                    }
                )

        if errors:
            raise RequestValidationErrors(errors)
        return self


class ForecastBucketResult(BaseModel):
    """链上单个桶的可达性预测。"""

    id: str
    balance_now: Annotated[float, Field(description="now 时刻的余额")]
    reachable: Annotated[bool, Field(description="该桶单独是否可达成本")]
    reason: Annotated[
        str | None,
        Field(default=None, description="不可达原因：cost_exceeds_capacity / zero_refill_rate"),
    ] = None
    wait_seconds: Annotated[
        float | None, Field(default=None, description="该桶单独满足成本所需等待秒数；不可达为 null")
    ] = None
    projected_balance: Annotated[
        float | None,
        Field(default=None, description="预计可通过时刻的余额（不扣减）；链不可达时为 null"),
    ] = None


class ForecastResponse(BaseModel):
    reachable: Annotated[bool, Field(description="链上全部桶是否都能达到成本")]
    wait_seconds: Annotated[
        float | None, Field(description="全部桶首次同时满足成本所需等待秒数；不可达为 null")
    ]
    pass_at: Annotated[
        float | None, Field(description="预计可通过时刻 = now + wait_seconds；不可达为 null")
    ]
    limiting_bucket: Annotated[
        str | None,
        Field(description="等待最长的限制桶（并列取链上首个）；立即可通过或不可达时为 null"),
    ]
    buckets: Annotated[list[ForecastBucketResult], Field(description="链上各桶的预测，按链顺序")]


class BucketBalance(BaseModel):
    """链上单个桶在某次扣减判定时的余额视图。"""

    before: Annotated[float, Field(description="判定时刻（补充之后、扣减之前）的余额")]
    after: Annotated[float, Field(description="扣减之后的余额；被拒绝时与 before 相同")]


class SimResult(BaseModel):
    """单项请求的模拟结果。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    allowed: bool
    rejected_by: Annotated[str | None, Field(default=None, description="导致拒绝的第一个余额不足的桶 ID")]
    balances: Annotated[dict[str, BucketBalance], Field(description="链上各桶扣减前后余额")]


class SimulateResponse(BaseModel):
    allowed_count: int
    rejected_count: int
    results: list[SimResult]
    final_balances: Annotated[dict[str, float], Field(description="模拟结束时所有桶的余额")]


class PlanBucketResult(BaseModel):
    """单个桶的初始余额规划结果。"""

    id: str
    required_initial: Annotated[
        float, Field(description="满足该桶全部链上消耗所需的最低初始余额（[0, capacity] 内）")
    ]
    requested_initial: Annotated[
        float, Field(description="请求中给出的 initial；缺省按 capacity 计")
    ]
    delta: Annotated[
        float,
        Field(description="required_initial - requested_initial；为正表示请求初始余额不足"),
    ]


class PlanFailure(BaseModel):
    """满桶起步仍不可行时，按处理顺序首个无法满足的请求与桶。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    bucket_id: Annotated[str, Field(description="链上首个余额不足的桶 ID")]
    cost: Annotated[float, Field(description="该请求的消耗量")]
    available: Annotated[
        float, Field(description="满桶起步回放中，该桶在判定时刻（补充之后、扣减之前）的余额")
    ]


class PlanInitialBalancesResponse(BaseModel):
    reachable: Annotated[bool, Field(description="满桶起步是否能让全部请求放行")]
    buckets: Annotated[
        list[PlanBucketResult] | None,
        Field(description="各桶规划结果，按 buckets 输入顺序；不可行为 null"),
    ]
    final_balances: Annotated[
        dict[str, float] | None,
        Field(description="用规划值完整回放得到的终态余额；不可行为 null"),
    ]
    first_failure: Annotated[
        PlanFailure | None,
        Field(description="不可行时按处理顺序首个无法满足的请求与桶；可行时为 null"),
    ]


class UsageReportRequest(SimulateRequest):
    """POST /usage-report 请求体：/simulate 请求体 + 统计窗口宽度。

    桶与请求的字段、跨字段校验（重复桶、未知引用、重复请求 ID）全部沿用
    SimulateRequest。
    """

    window_seconds: Annotated[float, Field(description="统计窗口宽度（秒），必须为正的有限数值")]

    @field_validator("window_seconds", mode="before")
    @classmethod
    def _validate_window_seconds(cls, value):
        if not _is_finite_number(value):
            raise ValueError("window_seconds 必须是有限数值")
        if value <= 0:
            raise ValueError("window_seconds 必须为正数")
        return value


class WindowRange(BaseModel):
    """半开窗口区间 [start, end)。"""

    index: Annotated[int, Field(description="窗口序号（从 0 开始）")]
    start: Annotated[float, Field(description="窗口起点（含）")]
    end: Annotated[float, Field(description="窗口终点（不含）")]


class UsageWindowStats(BaseModel):
    """单个桶在单个窗口内的使用统计。"""

    index: Annotated[int, Field(description="窗口序号")]
    allowed_consumption: Annotated[
        float, Field(description="窗口内放行请求对该桶的消耗合计")
    ]
    rejected_count: Annotated[
        int, Field(description="窗口内引用该桶且被原子拒绝的请求数（无论限制桶是否为该桶）")
    ]
    peak_occupancy: Annotated[
        float, Field(description="窗口内峰值占用 = capacity - 窗口内最低余额")
    ]


class UsageBucketReport(BaseModel):
    """单个桶的逐窗统计与峰值信息。"""

    id: str
    allowed_consumption: Annotated[float, Field(description="全部窗口放行消耗合计")]
    rejected_count: Annotated[int, Field(description="全部窗口拒绝次数合计")]
    peak_occupancy: Annotated[float, Field(description="全部窗口中的最高峰值占用")]
    peak_window: Annotated[
        WindowRange, Field(description="达到峰值占用的窗口起止时间（并列取最早窗口）")
    ]
    windows: Annotated[
        list[UsageWindowStats], Field(description="逐窗口统计，按窗口序号升序")
    ]


class BusiestWindow(WindowRange):
    """全局最忙窗口：跨桶放行消耗合计最大的窗口（并列取最早）。"""

    allowed_consumption: Annotated[float, Field(description="该窗口全部桶放行消耗之和")]
    allowed_count: Annotated[int, Field(description="该窗口放行的请求数")]
    rejected_count: Annotated[int, Field(description="该窗口被拒的请求数")]


class UsageReportResponse(BaseModel):
    window_seconds: Annotated[float, Field(description="统计窗口宽度（秒）")]
    window_count: Annotated[int, Field(description="覆盖请求时间跨度的窗口总数")]
    windows: Annotated[
        list[WindowRange], Field(description="全部窗口的起止时间，按窗口序号升序")
    ]
    buckets: Annotated[
        list[UsageBucketReport], Field(description="各桶统计，按 buckets 输入顺序")
    ]
    busiest_window: Annotated[BusiestWindow, Field(description="全局最忙窗口")]


class FairnessSimRequest(BaseModel):
    """公平性试算请求：在 /simulate 请求字段上增加 principal 与 weight。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="请求唯一标识")]
    timestamp: Annotated[float, Field(description="请求时间（秒，任意一致的时间原点）")]
    cost: Annotated[float, Field(gt=0, description="消耗量（从链上每个桶扣减的令牌数）")]
    chain: Annotated[list[str], Field(min_length=1, description="桶链：按顺序引用的桶 ID")]
    principal: Annotated[
        str, Field(min_length=1, description="请求归属主体（租户/用户/调用方 ID）")
    ]
    weight: Annotated[
        float,
        Field(default=1.0, description="主体权重，必须为正的有限数值；缺省为 1.0"),
    ] = 1.0

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value):
        if not _is_finite_number(value):
            raise ValueError("timestamp 必须是有限数值")
        return value

    @field_validator("cost", mode="before")
    @classmethod
    def _validate_cost(cls, value):
        if not _is_finite_number(value):
            raise ValueError("cost 必须是有限数值")
        if value <= 0:
            raise ValueError("cost 必须为正数")
        return value

    @field_validator("principal", mode="before")
    @classmethod
    def _validate_principal(cls, value):
        if not isinstance(value, str) or not value:
            raise ValueError("principal 必须是非空字符串")
        return value

    @field_validator("weight", mode="before")
    @classmethod
    def _validate_weight(cls, value):
        if not _is_finite_number(value):
            raise ValueError("weight 必须是有限数值")
        if value <= 0:
            raise ValueError("weight 必须为正数")
        return value

    @field_validator("chain")
    @classmethod
    def _validate_chain_shape(cls, value: list[str]):
        if not value:
            raise ValueError("chain 至少需要引用一个桶")
        seen: set[str] = set()
        for bucket_id in value:
            if not isinstance(bucket_id, str) or not bucket_id:
                raise ValueError("chain 中的桶 ID 必须是非空字符串")
            if bucket_id in seen:
                raise ValueError(f"chain 中存在重复桶引用: {bucket_id}")
            seen.add(bucket_id)
        return value


class FairnessReportRequest(BaseModel):
    """POST /fairness-report 请求体：带 principal/weight 的请求序列 + 统计窗口宽度。

    桶与请求的字段、跨字段校验（重复桶、未知引用、重复请求 ID）沿用
    SimulateRequest 的同一套规则。
    """

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[BucketSpec], Field(min_length=1, description="令牌桶集合")]
    requests: Annotated[
        list[FairnessSimRequest], Field(min_length=1, description="按时间戳模拟的请求序列")
    ]
    window_seconds: Annotated[float, Field(description="统计窗口宽度（秒），必须为正的有限数值")]

    @field_validator("window_seconds", mode="before")
    @classmethod
    def _validate_window_seconds(cls, value):
        if not _is_finite_number(value):
            raise ValueError("window_seconds 必须是有限数值")
        if value <= 0:
            raise ValueError("window_seconds 必须为正数")
        return value

    @model_validator(mode="after")
    def _cross_validate(self):
        errors = _simulate_cross_validation_errors(self.buckets, self.requests)

        # 同一 principal 的权重必须一致：加权 Jain 指数以主体为单位计算
        principal_weight: dict[str, float] = {}
        for r_index, request in enumerate(self.requests):
            previous = principal_weight.get(request.principal)
            if previous is None:
                principal_weight[request.principal] = float(request.weight)
            elif abs(previous - float(request.weight)) > EPS:
                errors.append(
                    {
                        "type": "inconsistent_principal_weight",
                        "loc": ("requests", r_index, "weight"),
                        "msg": (
                            f"principal {request.principal!r} 的权重不一致："
                            f"{previous} 与 {float(request.weight)}"
                        ),
                        "input": request.weight,
                    }
                )

        if errors:
            raise RequestValidationErrors(errors)
        return self


class FairnessPrincipalRow(BaseModel):
    """单个主体在某个统计范围（全局或单个窗口）内的公平性数据。"""

    principal: Annotated[str, Field(description="主体标识")]
    weight: Annotated[float, Field(description="主体权重")]
    request_count: Annotated[int, Field(description="该范围内该主体的请求总数")]
    allowed_count: Annotated[int, Field(description="放行请求数")]
    rejected_count: Annotated[int, Field(description="被原子拒绝的请求数")]
    allowed_consumption: Annotated[
        float, Field(description="放行请求的 cost 合计（该主体的实际获得量）")
    ]
    theoretical_share: Annotated[
        float,
        Field(description="理论权重份额 = 该范围全部放行消耗 × weight / 权重合计"),
    ]
    deficit: Annotated[
        float, Field(description="短缺量 = max(0, theoretical_share - allowed_consumption)")
    ]
    deficit_ratio: Annotated[
        float,
        Field(description="短缺比例 = deficit / theoretical_share；理论份额为 0 时为 0"),
    ]


class FirstShortfall(BaseModel):
    """最受限主体的短缺证据：按处理顺序首个造成其短缺的拒绝。"""

    request_id: Annotated[str, Field(description="被拒绝的请求 ID")]
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: Annotated[float, Field(description="该请求的时间戳")]
    bucket_id: Annotated[str, Field(description="导致原子拒绝的第一个余额不足的桶 ID")]
    cost: Annotated[float, Field(description="该请求的消耗量")]
    available: Annotated[
        float, Field(description="判定时刻（补充之后、扣减之前）限制桶的余额")
    ]


class FairnessScopeReport(BaseModel):
    """一个统计范围（全局或单个窗口）的公平性报告。"""

    principal_count: Annotated[int, Field(description="参与该范围统计的主体数")]
    request_count: Annotated[int, Field(description="该范围内的请求总数")]
    allowed_count: Annotated[int, Field(description="放行请求总数")]
    rejected_count: Annotated[int, Field(description="拒绝请求总数")]
    allowed_consumption: Annotated[
        float, Field(description="全部主体放行消耗（实际获得量）合计")
    ]
    weighted_jain_index: Annotated[
        float,
        Field(
            description=(
                "加权 Jain 公平指数 = (Σ x)² / (Σ w · Σ x²/w)，"
                "x 为主体实际获得量、w 为权重；取值 (0, 1]，越大越公平"
            )
        ),
    ]
    most_constrained_principal: Annotated[
        str | None,
        Field(
            description=(
                "短缺比例最高且有被拒请求的主体（并列取首次拒绝最早者）；"
                "不存在有短缺的被拒主体时为 null"
            )
        ),
    ]
    first_shortfall: Annotated[
        FirstShortfall | None,
        Field(
            description="最受限主体按处理顺序的首个短缺拒绝；无最受限主体时为 null"
        ),
    ]
    principals: Annotated[
        list[FairnessPrincipalRow],
        Field(description="各主体统计，按主体首次请求的处理顺序"),
    ]


class FairnessWindowReport(FairnessScopeReport):
    """单个窗口的公平性报告：范围数据 + 窗口起止。"""

    index: Annotated[int, Field(description="窗口序号")]
    start: Annotated[float, Field(description="窗口起点（含）")]
    end: Annotated[float, Field(description="窗口终点（不含）")]


class FairnessReportResponse(BaseModel):
    window_seconds: Annotated[float, Field(description="统计窗口宽度（秒）")]
    window_count: Annotated[int, Field(description="覆盖请求时间跨度的窗口总数")]
    windows: Annotated[
        list[WindowRange], Field(description="全部窗口的起止时间，按窗口序号升序")
    ]
    global_report: Annotated[FairnessScopeReport, Field(description="跨全部窗口的全局报告")]
    window_reports: Annotated[
        list[FairnessWindowReport], Field(description="逐窗口报告，按窗口序号升序")
    ]


class ReservationBucket(BaseModel):
    """预留试算桶定义：只有容量上限，没有补充概念。

    capacity: 桶容量，即任意时刻允许的并发占用上限
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="桶唯一标识")]
    capacity: Annotated[float, Field(description="桶容量（并发占用上限），必须为正数")]

    @field_validator("capacity", mode="before")
    @classmethod
    def _validate_capacity(cls, value):
        if not _is_finite_number(value):
            raise ValueError("capacity 必须是有限数值")
        if value <= 0:
            raise ValueError("capacity 必须为正数")
        return value


class ReservationRequest(BaseModel):
    """单次预留请求：在半开区间 [start, end) 内占用链上每个桶的 cost 容量。"""

    model_config = ConfigDict(extra="forbid")

    start: Annotated[float, Field(description="预留起始时刻（含），秒，任意一致的时间原点")]
    end: Annotated[float, Field(description="预留结束时刻（不含），必须严格大于 start")]
    cost: Annotated[float, Field(gt=0, description="预留期间对链上每个桶的并发占用量")]
    chain: Annotated[list[str], Field(min_length=1, description="桶链：按顺序引用的桶 ID")]

    @field_validator("start", "end", mode="before")
    @classmethod
    def _validate_time(cls, value, info):
        if not _is_finite_number(value):
            raise ValueError(f"{info.field_name} 必须是有限数值")
        return value

    @field_validator("cost", mode="before")
    @classmethod
    def _validate_cost(cls, value):
        if not _is_finite_number(value):
            raise ValueError("cost 必须是有限数值")
        if value <= 0:
            raise ValueError("cost 必须为正数")
        return value

    @field_validator("chain")
    @classmethod
    def _validate_chain_shape(cls, value: list[str]):
        if not value:
            raise ValueError("chain 至少需要引用一个桶")
        seen: set[str] = set()
        for bucket_id in value:
            if not isinstance(bucket_id, str) or not bucket_id:
                raise ValueError("chain 中的桶 ID 必须是非空字符串")
            if bucket_id in seen:
                raise ValueError(f"chain 中存在重复桶引用: {bucket_id}")
            seen.add(bucket_id)
        return value

    @model_validator(mode="after")
    def _check_interval(self):
        if self.end <= self.start:
            raise ValueError("end 必须严格大于 start")
        return self


class ReservationSimulateRequest(BaseModel):
    """POST /reservation-simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[ReservationBucket], Field(min_length=1, description="预留桶集合")]
    reservations: Annotated[
        list[ReservationRequest], Field(min_length=1, description="按 start 升序试算的预留序列")
    ]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors: list[dict] = []

        # 重复桶 ID
        seen: set[str] = set()
        duplicates: set[str] = set()
        for index, bucket in enumerate(self.buckets):
            if bucket.id in seen:
                duplicates.add(bucket.id)
                errors.append(
                    {
                        "type": "duplicate_bucket",
                        "loc": ("buckets", index, "id"),
                        "msg": f"重复的桶 ID: {bucket.id}",
                        "input": bucket.id,
                    }
                )
            seen.add(bucket.id)

        if duplicates:
            # 已存在重复定义，引用校验意义不大，直接返回
            raise RequestValidationErrors(errors)

        # 链中未知引用
        for r_index, reservation in enumerate(self.reservations):
            for c_index, bucket_id in enumerate(reservation.chain):
                if bucket_id not in seen:
                    errors.append(
                        {
                            "type": "unknown_bucket_ref",
                            "loc": ("reservations", r_index, "chain", c_index),
                            "msg": f"预留 {r_index} 引用了未定义的桶: {bucket_id}",
                            "input": bucket_id,
                        }
                    )

        if errors:
            raise RequestValidationErrors(errors)
        return self


class OccupancyInterval(BaseModel):
    """单个桶上并发占用量恒定的合并区间 [start, end)。"""

    start: Annotated[float, Field(description="区间起点（含）")]
    end: Annotated[float, Field(description="区间终点（不含）")]
    occupancy: Annotated[float, Field(description="该区间内的并发占用总量")]


class ReservationResult(BaseModel):
    """单项预留的试算结果。"""

    index: Annotated[int, Field(description="该预留在原始 reservations 数组中的下标")]
    start: float
    end: float
    cost: float
    allowed: Annotated[bool, Field(description="是否原子接受")]
    rejected_by: Annotated[
        str | None,
        Field(default=None, description="导致拒绝的第一个放不下的桶 ID；接受时为 null"),
    ] = None


class ReservationBucketOccupancy(BaseModel):
    """单个桶在全部接受预留叠加后的占用视图。"""

    id: str
    capacity: Annotated[float, Field(description="桶容量（并发占用上限）")]
    peak_occupancy: Annotated[float, Field(description="整个时间轴上的峰值并发占用")]
    intervals: Annotated[
        list[OccupancyInterval],
        Field(description="合并后的非零占用区间，按起点升序、相邻同占用相接区间已合并"),
    ]


class ReservationSimulateResponse(BaseModel):
    allowed_count: int
    rejected_count: int
    results: Annotated[
        list[ReservationResult], Field(description="逐项预留结果，按原始输入索引升序")
    ]
    buckets: Annotated[
        list[ReservationBucketOccupancy],
        Field(description="各桶合并后的占用区间与峰值并发，按 buckets 输入顺序"),
    ]


class RouteSimRequest(BaseModel):
    """单次路由试算请求：携带多条候选桶链，由服务挑选一条放行。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="请求唯一标识")]
    timestamp: Annotated[float, Field(description="请求时间（秒，任意一致的时间原点）")]
    cost: Annotated[float, Field(gt=0, description="消耗量（从选中链上每个桶扣减的令牌数）")]
    chains: Annotated[
        list[list[str]],
        Field(min_length=1, description="候选桶链列表：至少一条，每条链按顺序引用桶 ID"),
    ]

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value):
        if not _is_finite_number(value):
            raise ValueError("timestamp 必须是有限数值")
        return value

    @field_validator("cost", mode="before")
    @classmethod
    def _validate_cost(cls, value):
        if not _is_finite_number(value):
            raise ValueError("cost 必须是有限数值")
        if value <= 0:
            raise ValueError("cost 必须为正数")
        return value

    @field_validator("chains")
    @classmethod
    def _validate_chains_shape(cls, value: list[list[str]]):
        if not value:
            raise ValueError("chains 至少需要一条候选链")
        for chain in value:
            if not chain:
                raise ValueError("候选链至少需要引用一个桶")
            seen: set[str] = set()
            for bucket_id in chain:
                if not isinstance(bucket_id, str) or not bucket_id:
                    raise ValueError("候选链中的桶 ID 必须是非空字符串")
                if bucket_id in seen:
                    raise ValueError(f"候选链中存在重复桶引用: {bucket_id}")
                seen.add(bucket_id)
        return value


class RouteSimulateRequest(BaseModel):
    """POST /route-simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[BucketSpec], Field(min_length=1, description="令牌桶集合")]
    requests: Annotated[
        list[RouteSimRequest], Field(min_length=1, description="按时间戳路由试算的请求序列")
    ]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors: list[dict] = []

        # 重复桶 ID
        seen: set[str] = set()
        duplicates: set[str] = set()
        for index, bucket in enumerate(self.buckets):
            if bucket.id in seen:
                duplicates.add(bucket.id)
                errors.append(
                    {
                        "type": "duplicate_bucket",
                        "loc": ("buckets", index, "id"),
                        "msg": f"重复的桶 ID: {bucket.id}",
                        "input": bucket.id,
                    }
                )
            seen.add(bucket.id)

        if duplicates:
            # 已存在重复定义，引用校验意义不大，直接返回
            raise RequestValidationErrors(errors)

        # 候选链中未知引用
        for r_index, request in enumerate(self.requests):
            for c_index, chain in enumerate(request.chains):
                for b_index, bucket_id in enumerate(chain):
                    if bucket_id not in seen:
                        errors.append(
                            {
                                "type": "unknown_bucket_ref",
                                "loc": ("requests", r_index, "chains", c_index, b_index),
                                "msg": (
                                    f"请求 {request.id!r} 的候选链 {c_index} "
                                    f"引用了未定义的桶: {bucket_id}"
                                ),
                                "input": bucket_id,
                            }
                        )

        # 重复请求 ID（便于调用方定位模拟结果）
        seen_req: set[str] = set()
        for r_index, request in enumerate(self.requests):
            if request.id in seen_req:
                errors.append(
                    {
                        "type": "duplicate_request",
                        "loc": ("requests", r_index, "id"),
                        "msg": f"重复的请求 ID: {request.id}",
                        "input": request.id,
                    }
                )
            seen_req.add(request.id)

        if errors:
            raise RequestValidationErrors(errors)
        return self


class RouteCandidateRejection(BaseModel):
    """候选链的拒绝原因：该链上第一个余额不足的桶。"""

    chain_index: Annotated[int, Field(description="候选链在 chains 中的下标")]
    rejected_by: Annotated[str, Field(description="该候选链上第一个余额不足的桶 ID")]
    available: Annotated[
        float, Field(description="判定时刻（补充之后、扣减之前）该桶的余额")
    ]


class RouteSimResult(BaseModel):
    """单项路由请求的试算结果。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    allowed: bool
    selected_chain_index: Annotated[
        int | None,
        Field(default=None, description="选中候选链在 chains 中的下标；被拒绝时为 null"),
    ] = None
    selected_chain: Annotated[
        list[str] | None,
        Field(default=None, description="选中的桶链；被拒绝时为 null"),
    ] = None
    balances: Annotated[
        dict[str, BucketBalance],
        Field(description="本请求触及的所有桶（全部候选链的并集）扣减前后余额"),
    ]
    rejections: Annotated[
        list[RouteCandidateRejection] | None,
        Field(default=None, description="被拒绝时逐候选链的首个不足桶；放行时为 null"),
    ] = None


class RouteSimulateResponse(BaseModel):
    allowed_count: int
    rejected_count: int
    results: Annotated[
        list[RouteSimResult], Field(description="逐项路由结果，按原始输入索引升序")
    ]
    final_balances: Annotated[dict[str, float], Field(description="模拟结束时所有桶的余额")]


class QueueSimRequest(SimRequest):
    """单次排队试算请求：/simulate 请求字段 + 不早于 timestamp 的 deadline。

    deadline: 最晚可执行时刻（秒）；队首在 deadline 前（含）余额充足即原子扣减，
    否则等待至 deadline 标记 expired 出队。
    """

    deadline: Annotated[
        float, Field(description="最晚可执行时刻（秒），必须为有限数值且不早于 timestamp")
    ]

    @field_validator("deadline", mode="before")
    @classmethod
    def _validate_deadline(cls, value):
        if not _is_finite_number(value):
            raise ValueError("deadline 必须是有限数值")
        return value

    @model_validator(mode="after")
    def _check_deadline_not_before_timestamp(self):
        if self.deadline < self.timestamp:
            raise ValueError("deadline 必须不早于 timestamp")
        return self


class QueueSimulateRequest(BaseModel):
    """POST /queue-simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[BucketSpec], Field(min_length=1, description="令牌桶集合")]
    requests: Annotated[
        list[QueueSimRequest],
        Field(min_length=1, description="按（到达时间, 输入顺序）排入严格 FIFO 队列的请求序列"),
    ]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors = _simulate_cross_validation_errors(self.buckets, self.requests)
        if errors:
            raise RequestValidationErrors(errors)
        return self


class QueueSimResult(BaseModel):
    """单项排队请求的结果。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    deadline: float
    status: Annotated[
        Literal["executed", "expired"],
        Field(
            description="executed=在 deadline 前（含）原子扣减放行；expired=等待至 deadline 仍不可执行"
        ),
    ]
    executed_at: Annotated[
        float | None, Field(default=None, description="实际执行时刻；expired 时为 null")
    ] = None
    expired_at: Annotated[
        float | None, Field(default=None, description="过期出队时刻；executed 时为 null")
    ] = None
    wait_seconds: Annotated[float, Field(description="从到达到执行/过期的等待秒数")]
    limiting_bucket: Annotated[
        str | None,
        Field(
            default=None,
            description="就绪最晚的限制桶（并列取链上首个）；成为队首即可执行时为 null",
        ),
    ] = None
    balances: Annotated[
        dict[str, BucketBalance],
        Field(description="链上各桶在判定时刻（补充之后、扣减之前/之后）的余额"),
    ]


class QueueSimulateResponse(BaseModel):
    executed_count: int
    expired_count: int
    results: Annotated[
        list[QueueSimResult], Field(description="逐项排队结果，按原始输入索引升序")
    ]
    final_balances: Annotated[
        dict[str, float], Field(description="最后一个请求出队时刻所有桶的余额")
    ]
