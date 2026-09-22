"""严格 FIFO 排队试算：队首等待连续补充至可执行或到期，后继请求不得越过队首。

与 /simulate 共用同一套连续补充令牌桶语义（惰性补充、封顶 capacity、
链上原子扣减），区别在于请求不再到达即判定，而是按到达时间及输入顺序
排入严格 FIFO 队列：

- 仅队首可执行。队首的决断时刻 =
  min(链上各桶连续补充后全部达到 cost 的最早时刻, max(deadline, 成为队首的时刻))；
- 决断时刻链上余额充足（含恰在 deadline 才补足）→ 链上每个桶原子扣减
  cost，状态 executed；否则不扣任何桶，状态 expired；
- 成本超过容量或零补充导致永远不可达的队首，同样等待至 deadline 再判定；
- 队首离场（执行或过期）后，下一个请求才能成为队首——到达事件只入队、
  不改变桶状态，因此到达请求永远不会越过队首；
- 前一队首离场晚于下一请求的 deadline 时，下一请求在成为队首的时刻立即
  决断：余额充足仍执行，否则过期。
"""
from __future__ import annotations

import math

from app.models import (
    EPS,
    BucketBalance,
    QueueSimResult,
    QueueSimulateRequest,
    QueueSimulateResponse,
)
from app.simulation import _round


def queue_simulate(payload: QueueSimulateRequest) -> QueueSimulateResponse:
    if not payload.buckets:
        raise ValueError("buckets 不能为空")
    if not payload.requests:
        raise ValueError("requests 不能为空")

    # 与 /simulate 一致：所有桶在最早请求时刻持有初始余额，之后按
    # (now - last) * rate 连续补充，上限为容量；时间戳可用任意一致原点。
    t_start = min(request.timestamp for request in payload.requests)

    capacity = {b.id: float(b.capacity) for b in payload.buckets}
    refill_rate = {b.id: float(b.refill_rate) for b in payload.buckets}
    balance = {
        b.id: float(b.capacity if b.initial is None else b.initial) for b in payload.buckets
    }
    last_time = {b.id: t_start for b in payload.buckets}

    def refill(bucket_id: str, now: float) -> float:
        """把某个桶补充到时刻 now，返回补充后的余额。"""
        elapsed = now - last_time[bucket_id]
        if elapsed > 0:
            balance[bucket_id] = min(
                capacity[bucket_id], balance[bucket_id] + refill_rate[bucket_id] * elapsed
            )
        last_time[bucket_id] = now
        return balance[bucket_id]

    # 严格 FIFO：按到达时间（同刻按输入顺序）依次成为队首；
    # 只有前一个队首离场（执行或过期）后，下一个请求才能成为队首。
    order = sorted(
        range(len(payload.requests)),
        key=lambda i: (payload.requests[i].timestamp, i),
    )

    results_by_index: dict[int, QueueSimResult] = {}
    now = t_start  # 上一个队首的离场时刻

    for i in order:
        request = payload.requests[i]
        cost = float(request.cost)
        deadline = float(request.deadline)

        # 成为队首的时刻 = max(自己的到达时间, 上一个队首的离场时刻)
        head_since = max(now, float(request.timestamp))

        # 先对链上所有桶补充到队首时刻（无论结果如何，时间都会推进）
        head_balances = {bucket_id: refill(bucket_id, head_since) for bucket_id in request.chain}

        # 各桶自队首时刻起连续补充到达 cost 所需的等待；不可达则等待无穷
        waits: dict[str, float] = {}
        reachable = True
        for bucket_id in request.chain:
            if cost > capacity[bucket_id] + EPS:
                reachable = False  # cost_exceeds_capacity
            elif head_balances[bucket_id] + EPS >= cost:
                waits[bucket_id] = 0.0
            elif refill_rate[bucket_id] <= 0:
                reachable = False  # zero_refill_rate
            else:
                waits[bucket_id] = (cost - head_balances[bucket_id]) / refill_rate[bucket_id]

        # 队首的最早可执行时刻；不可达（成本超容量或零补充）时为无穷，
        # 此时队首同样等待至 deadline
        t_ready = head_since + max(waits.values()) if reachable else math.inf

        # 决断时刻 = min(最早可执行时刻, deadline)；deadline 已过（排队期间
        # 错过 deadline）时立即在成为队首的时刻决断
        decisive = min(t_ready, max(deadline, head_since))

        # 补充到决断时刻并判定：余额充足则原子扣减（executed），否则 expired
        refilled = {bucket_id: refill(bucket_id, decisive) for bucket_id in request.chain}
        short = [bucket_id for bucket_id in request.chain if refilled[bucket_id] + EPS < cost]

        executed = not short
        balances_view: dict[str, BucketBalance] = {}
        for bucket_id in request.chain:
            before = refilled[bucket_id]
            if executed:
                balance[bucket_id] = before - cost
                after = balance[bucket_id]
            else:
                # 原子性：过期不扣减，链上所有桶保持补充后的余额
                after = before
            balances_view[bucket_id] = BucketBalance(before=_round(before), after=_round(after))

        # 限制桶：执行时为等待最久的桶（立即可执行时为 null）；
        # 过期时为决断时刻链上第一个余额不足的桶
        limiting_bucket: str | None = None
        if executed:
            max_wait = max(waits.values())
            if _round(max_wait) > 0:
                limiting_bucket = next(
                    bucket_id for bucket_id in request.chain if waits[bucket_id] == max_wait
                )
        else:
            limiting_bucket = short[0]

        results_by_index[i] = QueueSimResult(
            request_id=request.id,
            index=i,
            timestamp=_round(float(request.timestamp)),
            deadline=_round(deadline),
            status="executed" if executed else "expired",
            executed_at=_round(decisive) if executed else None,
            expired_at=None if executed else _round(decisive),
            wait_seconds=_round(decisive - float(request.timestamp)),
            limiting_bucket=limiting_bucket,
            balances=balances_view,
        )

        now = decisive  # 队首离场，后继请求才能成为队首

    # 把所有桶补充到模拟结束时刻（最后一个队首的离场时刻），给出终态余额
    final_balances = {
        bucket_id: _round(
            min(
                capacity[bucket_id],
                balance[bucket_id]
                + refill_rate[bucket_id] * max(0.0, now - last_time[bucket_id]),
            )
        )
        for bucket_id in balance
    }

    results = [results_by_index[i] for i in range(len(payload.requests))]
    executed_count = sum(1 for r in results if r.status == "executed")

    return QueueSimulateResponse(
        executed_count=executed_count,
        expired_count=len(results) - executed_count,
        results=results,
        final_balances=final_balances,
    )
