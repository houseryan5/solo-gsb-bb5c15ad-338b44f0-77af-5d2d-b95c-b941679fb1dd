"""严格 FIFO 排队试算（/queue-simulate）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import QueueSimulateRequest
from app.queueing import queue_simulate

client = TestClient(app)


def _run(buckets, requests):
    return queue_simulate(
        QueueSimulateRequest.model_validate({"buckets": buckets, "requests": requests})
    )


# ---------- 队首等待与执行 ----------

def test_immediate_execution_no_wait():
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 10}],
        [{"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"], "deadline": 5}],
    )
    r = resp.results[0]
    assert r.status == "executed"
    assert r.executed_at == 0
    assert r.expired_at is None
    assert r.wait_seconds == 0
    assert r.limiting_bucket is None  # 立即可执行，无限制桶
    assert r.balances["A"].before == 10 and r.balances["A"].after == 6
    assert resp.executed_count == 1 and resp.expired_count == 0
    assert resp.final_balances == {"A": 6}


def test_waits_for_refill_and_executes_before_deadline():
    # 余额 0、速率 1/s、cost 5：队首等到 t=5 补满后执行
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 10}],
    )
    r = resp.results[0]
    assert r.status == "executed"
    assert r.executed_at == 5
    assert r.wait_seconds == 5
    assert r.limiting_bucket == "A"
    assert r.balances["A"].before == 5 and r.balances["A"].after == 0
    assert resp.final_balances == {"A": 0}


def test_deadline_exactly_sufficient_still_executes():
    # 恰在 deadline 补足：仍应原子扣减并成功
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 5}],
    )
    r = resp.results[0]
    assert r.status == "executed"
    assert r.executed_at == 5
    assert r.balances["A"].before == 5 and r.balances["A"].after == 0


def test_insufficient_at_deadline_expires_without_deduction():
    # t_ready = 5 > deadline 3：到期余额 3 不足，过期且不扣减
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 3}],
    )
    r = resp.results[0]
    assert r.status == "expired"
    assert r.expired_at == 3
    assert r.executed_at is None
    assert r.wait_seconds == 3
    assert r.limiting_bucket == "A"
    assert r.balances["A"].before == 3 and r.balances["A"].after == 3
    assert resp.executed_count == 0 and resp.expired_count == 1
    assert resp.final_balances == {"A": 3}


def test_cost_exceeds_capacity_waits_until_deadline():
    # cost 5 > capacity 4：永远不可达，仍等待至 deadline 再过期
    resp = _run(
        [{"id": "A", "capacity": 4, "refill_rate": 1, "initial": 4}],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 10}],
    )
    r = resp.results[0]
    assert r.status == "expired"
    assert r.expired_at == 10
    assert r.wait_seconds == 10
    assert r.limiting_bucket == "A"
    assert r.balances["A"].before == 4 and r.balances["A"].after == 4
    assert resp.final_balances == {"A": 4}


def test_zero_refill_unreachable_waits_until_deadline():
    # 零补充且余额不足：不可达，等待至 deadline 过期
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0, "initial": 3}],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 6}],
    )
    r = resp.results[0]
    assert r.status == "expired"
    assert r.expired_at == 6
    assert r.balances["A"].before == 3 and r.balances["A"].after == 3


def test_expired_does_not_deduct_any_bucket_on_chain():
    # 链上 B 不足导致过期：A 虽充足也保持不扣（原子性）
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0, "initial": 10},
            {"id": "B", "capacity": 10, "refill_rate": 0, "initial": 2},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A", "B"], "deadline": 4}],
    )
    r = resp.results[0]
    assert r.status == "expired"
    assert r.limiting_bucket == "B"
    assert r.balances["A"].before == 10 and r.balances["A"].after == 10
    assert r.balances["B"].before == 2 and r.balances["B"].after == 2
    assert resp.final_balances == {"A": 10, "B": 2}


def test_limiting_bucket_is_slowest_on_chain():
    # A 等 1s、B 等 4s、C 已满足 -> t=4 执行，限制桶为 B
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 2, "initial": 4},
            {"id": "B", "capacity": 10, "refill_rate": 1, "initial": 2},
            {"id": "C", "capacity": 10, "refill_rate": 5, "initial": 9},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A", "B", "C"], "deadline": 100}],
    )
    r = resp.results[0]
    assert r.status == "executed"
    assert r.executed_at == 4
    assert r.limiting_bucket == "B"
    # 决断时刻各桶补充后（封顶 capacity）原子扣减 6
    assert r.balances["A"].before == 10 and r.balances["A"].after == 4
    assert r.balances["B"].before == 6 and r.balances["B"].after == 0
    assert r.balances["C"].before == 10 and r.balances["C"].after == 4
    assert resp.final_balances == {"A": 4, "B": 0, "C": 4}


# ---------- 严格 FIFO：不得越过队首 ----------

def test_arrival_cannot_overtake_head():
    # r2 的桶 B 一直充足，若可越过队首会在 t=1 执行；
    # 严格 FIFO 下必须等 r1 在 t=5 离场后才能执行
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0},
            {"id": "B", "capacity": 10, "refill_rate": 10, "initial": 10},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 8},
            {"id": "r2", "timestamp": 1, "cost": 5, "chain": ["B"], "deadline": 20},
        ],
    )
    assert resp.results[0].status == "executed" and resp.results[0].executed_at == 5
    r2 = resp.results[1]
    assert r2.status == "executed"
    assert r2.executed_at == 5  # 成为队首的时刻，而非到达时刻 1
    assert r2.wait_seconds == 4  # 含排队等待
    assert r2.limiting_bucket is None
    assert r2.balances["B"].before == 10 and r2.balances["B"].after == 5
    assert resp.final_balances == {"A": 0, "B": 5}


def test_queue_cascade_after_head_expires():
    # r1 等不到 8 在 t=3 过期；r2 在 t=3 成为队首等到 t=5 执行；r3 再等到 t=10
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 8, "chain": ["A"], "deadline": 3},
            {"id": "r2", "timestamp": 1, "cost": 5, "chain": ["A"], "deadline": 10},
            {"id": "r3", "timestamp": 2, "cost": 5, "chain": ["A"], "deadline": 12},
        ],
    )
    assert [r.status for r in resp.results] == ["expired", "executed", "executed"]
    assert resp.results[0].expired_at == 3
    assert resp.results[1].executed_at == 5
    assert resp.results[1].balances["A"].before == 5
    assert resp.results[2].executed_at == 10
    assert resp.results[2].wait_seconds == 8
    assert resp.final_balances == {"A": 0}


def test_deadline_passed_while_queued_executes_if_sufficient():
    # r2 的 deadline 2 在排队期间已过；成为队首时（t=5）余额充足仍执行
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0, "initial": 10},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 8},
            {"id": "r2", "timestamp": 1, "cost": 3, "chain": ["B"], "deadline": 2},
        ],
    )
    r2 = resp.results[1]
    assert r2.status == "executed"
    assert r2.executed_at == 5
    assert r2.wait_seconds == 4
    assert r2.balances["B"].before == 10 and r2.balances["B"].after == 7


def test_deadline_passed_while_queued_expires_if_insufficient():
    # r2 的 deadline 2 在排队期间已过；成为队首时（t=5）A 余额 0 不足 -> 过期
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 8},
            {"id": "r2", "timestamp": 1, "cost": 3, "chain": ["A"], "deadline": 2},
        ],
    )
    r2 = resp.results[1]
    assert r2.status == "expired"
    assert r2.expired_at == 5
    assert r2.limiting_bucket == "A"
    assert r2.balances["A"].before == 0 and r2.balances["A"].after == 0


# ---------- 回放顺序与终态 ----------

def test_out_of_order_timestamps_processed_chronologically():
    # 输入乱序，必须按 (timestamp, 输入顺序) 排队；结果仍按原始下标返回
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r_late", "timestamp": 5, "cost": 3, "chain": ["A"], "deadline": 20},
            {"id": "r_early", "timestamp": 0, "cost": 4, "chain": ["A"], "deadline": 20},
        ],
    )
    assert resp.results[0].request_id == "r_late"
    assert resp.results[0].executed_at == 7  # t=5 成为队首时余额 1，再等 2s
    assert resp.results[1].request_id == "r_early"
    assert resp.results[1].executed_at == 4
    assert [r.index for r in resp.results] == [0, 1]
    assert resp.final_balances == {"A": 0}


def test_same_timestamp_requests_keep_input_order():
    resp = _run(
        [{"id": "A", "capacity": 1, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 5},
            {"id": "r2", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 5},
        ],
    )
    assert resp.results[0].status == "executed" and resp.results[0].executed_at == 0
    # 零补充不可达：r2 等待至 deadline 过期
    assert resp.results[1].status == "expired" and resp.results[1].expired_at == 5


def test_final_balances_use_last_departure_time():
    # 终态时刻 = 最后一个队首离场时刻（t=5），未被引用的桶也推进到该时刻
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0},
            {"id": "B", "capacity": 10, "refill_rate": 1, "initial": 3},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 8}],
    )
    assert resp.results[0].executed_at == 5
    assert resp.final_balances == {"A": 0, "B": 8}


def test_readme_example_scenario():
    # 与 examples/queue-simulate.json 相同的完整场景
    resp = _run(
        [
            {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
            {"id": "api_qps", "capacity": 5, "refill_rate": 1, "initial": 1},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"], "deadline": 10},
            {"id": "r2", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"], "deadline": 6},
            {"id": "r3", "timestamp": 2, "cost": 4, "chain": ["tenant_quota", "api_qps"], "deadline": 12},
            {"id": "r4", "timestamp": 8, "cost": 2, "chain": ["api_qps"], "deadline": 20},
        ],
    )
    assert [r.status for r in resp.results] == ["executed", "expired", "executed", "executed"]
    assert [r.executed_at for r in resp.results] == [3, None, 7, 9]
    assert [r.expired_at for r in resp.results] == [None, 6, None, None]
    assert [r.wait_seconds for r in resp.results] == [3, 5, 5, 1]
    assert [r.limiting_bucket for r in resp.results] == ["api_qps"] * 4
    assert resp.executed_count == 3 and resp.expired_count == 1
    assert resp.final_balances == {"tenant_quota": 100, "api_qps": 0}


# ---------- 校验 ----------

def test_deadline_before_timestamp_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QueueSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 5, "cost": 1, "chain": ["A"], "deadline": 4}
                ],
            }
        )


def test_deadline_equal_timestamp_allowed():
    resp = _run(
        [{"id": "A", "capacity": 1, "refill_rate": 0}],
        [{"id": "r", "timestamp": 5, "cost": 1, "chain": ["A"], "deadline": 5}],
    )
    # deadline == timestamp：立即决断，余额充足即执行
    assert resp.results[0].status == "executed"
    assert resp.results[0].executed_at == 5
    assert resp.results[0].wait_seconds == 0


def test_non_finite_deadline_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QueueSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": "inf"}
                ],
            }
        )


def test_missing_deadline_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QueueSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
            }
        )


def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        QueueSimulateRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_bucket_ids_rejected():
    errors = _validate_error(
        {
            "buckets": [
                {"id": "A", "capacity": 1, "refill_rate": 0},
                {"id": "A", "capacity": 2, "refill_rate": 0},
            ],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 1}
            ],
        }
    )
    assert all(e["type"] == "duplicate_bucket" for e in errors)


def test_unknown_bucket_reference_rejected():
    errors = _validate_error(
        {
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A", "X"], "deadline": 1}
            ],
        }
    )
    assert any(e["type"] == "unknown_bucket_ref" and e["input"] == "X" for e in errors)


def test_duplicate_request_ids_rejected():
    errors = _validate_error(
        {
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 1},
                {"id": "r", "timestamp": 1, "cost": 1, "chain": ["A"], "deadline": 2},
            ],
        }
    )
    assert any(e["type"] == "duplicate_request" for e in errors)


def test_extra_field_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QueueSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {
                        "id": "r",
                        "timestamp": 0,
                        "cost": 1,
                        "chain": ["A"],
                        "deadline": 1,
                        "ttl": 1,
                    }
                ],
            }
        )


# ---------- HTTP ----------

def test_http_queue_simulate_ok():
    response = client.post(
        "/queue-simulate",
        json={
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 10},
                {"id": "r2", "timestamp": 1, "cost": 6, "chain": ["A"], "deadline": 4},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    # r1：t=5 补足后执行
    assert data["results"][0]["status"] == "executed"
    assert data["results"][0]["executed_at"] == 5.0
    assert data["results"][0]["expired_at"] is None
    assert data["results"][0]["wait_seconds"] == 5.0
    assert data["results"][0]["limiting_bucket"] == "A"
    assert data["results"][0]["balances"]["A"] == {"before": 5.0, "after": 0.0}
    # r2：deadline 4 在排队期间已过，t=5 成为队首时余额 0 不足 -> 过期
    assert data["results"][1]["status"] == "expired"
    assert data["results"][1]["expired_at"] == 5.0
    assert data["results"][1]["executed_at"] is None
    assert data["results"][1]["balances"]["A"] == {"before": 0.0, "after": 0.0}
    assert data["executed_count"] == 1 and data["expired_count"] == 1
    assert data["final_balances"] == {"A": 0.0}


def test_http_deadline_before_timestamp_is_422():
    response = client.post(
        "/queue-simulate",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 5, "cost": 1, "chain": ["A"], "deadline": 4}
            ],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail


def test_http_unknown_bucket_reference_is_422():
    response = client.post(
        "/queue-simulate",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["GHOST"], "deadline": 1}
            ],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)
