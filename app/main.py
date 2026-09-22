"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import RequestValidationErrors
from app.fairness import fairness_report
from app.forecast import forecast
from app.models import (
    FairnessReportRequest,
    FairnessReportResponse,
    ForecastRequest,
    ForecastResponse,
    PlanInitialBalancesResponse,
    ReservationSimulateRequest,
    ReservationSimulateResponse,
    RouteSimulateRequest,
    RouteSimulateResponse,
    SimulateRequest,
    SimulateResponse,
    UsageReportRequest,
    UsageReportResponse,
)
from app.planner import plan_initial_balances
from app.reservation import reservation_simulate
from app.routing import route_simulate
from app.simulation import simulate
from app.usage import usage_report

app = FastAPI(
    title="多层配额原子试算 API",
    description=(
        "接收若干令牌桶与带时间戳的请求序列，按时间顺序模拟连续补充；"
        "仅当请求桶链上所有桶余额充足时原子扣减，否则全部保持不变。"
        "另提供 /forecast：在不扣减状态的前提下，预测桶链首次同时满足成本所需的等待时间；"
        "/plan-initial-balances：以全部请求放行为目标，反推各桶所需的最低初始余额；"
        "/usage-report：按时间窗口统计各桶的放行消耗、拒绝次数与峰值占用；"
        "/fairness-report：按 principal 统计放行消耗与拒绝次数，给出加权 Jain "
        "公平指数、最受限主体及其首个短缺桶（全局与逐窗口）；"
        "以及 /reservation-simulate：按 start 升序对区间预留做容量受限的链上原子试算，"
        "返回逐项结果与每桶合并后的占用区间、峰值并发；"
        "以及 /route-simulate：在共享连续补充令牌桶上为携带多条候选桶链的请求选路——"
        "仅候选链全部桶有余额时可选，按扣减后链上最大占用率最小（并列按候选顺序）"
        "确定选中链并仅扣该链，无候选可行时全部桶原子不变并给出各候选首个不足桶。"
    ),
    version="1.0.0",
)


@app.exception_handler(RequestValidationErrors)
async def cross_validation_exception_handler(request: Request, exc: RequestValidationErrors):
    return JSONResponse(status_code=422, content={"detail": exc.errors})


@app.exception_handler(RequestValidationError)
async def pydantic_validation_exception_handler(request: Request, exc: RequestValidationError):
    # jsonable_encoder：校验器抛出的 ValueError 会进入 ctx.error，需转成可序列化形式
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.get("/", tags=["meta"])
def root():
    return {
        "service": app.title,
        "docs": "/docs",
        "health": "/health",
        "simulate": "/simulate",
        "forecast": "/forecast",
        "plan_initial_balances": "/plan-initial-balances",
        "usage_report": "/usage-report",
        "fairness_report": "/fairness-report",
        "reservation_simulate": "/reservation-simulate",
        "route_simulate": "/route-simulate",
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post("/simulate", response_model=SimulateResponse, tags=["simulation"])
def simulate_endpoint(payload: SimulateRequest) -> SimulateResponse:
    return simulate(payload)


@app.post("/forecast", response_model=ForecastResponse, tags=["forecast"])
def forecast_endpoint(payload: ForecastRequest) -> ForecastResponse:
    return forecast(payload)


@app.post("/plan-initial-balances", response_model=PlanInitialBalancesResponse, tags=["planner"])
def plan_initial_balances_endpoint(payload: SimulateRequest) -> PlanInitialBalancesResponse:
    return plan_initial_balances(payload)


@app.post("/usage-report", response_model=UsageReportResponse, tags=["usage"])
def usage_report_endpoint(payload: UsageReportRequest) -> UsageReportResponse:
    return usage_report(payload)


@app.post("/fairness-report", response_model=FairnessReportResponse, tags=["fairness"])
def fairness_report_endpoint(payload: FairnessReportRequest) -> FairnessReportResponse:
    return fairness_report(payload)


@app.post(
    "/reservation-simulate",
    response_model=ReservationSimulateResponse,
    tags=["reservation"],
)
def reservation_simulate_endpoint(
    payload: ReservationSimulateRequest,
) -> ReservationSimulateResponse:
    return reservation_simulate(payload)


@app.post(
    "/route-simulate",
    response_model=RouteSimulateResponse,
    tags=["routing"],
)
def route_simulate_endpoint(payload: RouteSimulateRequest) -> RouteSimulateResponse:
    return route_simulate(payload)
