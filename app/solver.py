"""不逐片展开的合格片数上下界计算，以及不相容时的矛盾链提取。

模型
----
把所有补报区间的端点（以及查询用的合格区间端点）排序，把厚度轴切成
若干“原子区间”。同一个原子区间内的整数厚度，在所有计数约束看来完全
等价，因此只需用一个非负整数变量记录落在该原子内的片数。

设 F(x) 表示“厚度严格小于 x 的累计片数”，F(+∞) 为全部片数，则：

* 任意补报区间 [lo, hi) 计数为 c ⇔ F(hi) - F(lo) = c；
* 登记总片数 N ⇔ F(+∞) - F(0) = N；
* 累计片数单调不减 ⇔ 对相邻端点 a < b 有 F(b) - F(a) >= 0，
  即 F(a) - F(b) <= 0。

这些都是关于 F 的差分约束。每条约束是一条有向边 u -> v，权重 w，
含义为 F(v) <= F(u) + w，即“F(v) 减 F(u) 的上限为 w”。

目标“合格区间 [q_lo, q_hi) 片数”就是 F(q_hi) - F(q_lo)。其最大可
达到值等于图上 q_lo -> q_hi 的最短路；最小可达到值等于反向最短路
取负。约束矩阵是连续一矩阵，全幺模，因此最短路给出的整数解就是原
整数问题的精确最优解。图上存在负环 ⇔ 计数互相矛盾。

矛盾链（负环）
--------------
负环本身就是一条首尾相接的矛盾链：沿环把每一步的“上限”相加，得到
F(x0) - F(x0) <= 负数，即 0 <= 负数，矛盾。每条边都带着可回查的
依据（本次补报行号 / 旧报告号与区间号 / 登记总片数 / 单调规则），
因此质检员可以逐行复核。

算法不枚举片，也不展开原子内部的具体厚度分布；复杂度只与端点数量有关，
与总片数无关。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, NamedTuple, Sequence

INF = 10**30

Constraint = tuple[int, int, int]  # (lo, hi, count)，区间含 lo 不含 hi

# 依据类型：
#   segment   某次补报中的一个区间计数
#   total     登记的总片数（F(+∞) - F(0) = N）
#   monotone  累计片数单调规则（相邻端点间 F 不减）
EVIDENCE_SEGMENT = "segment"
EVIDENCE_TOTAL = "total"
EVIDENCE_MONOTONE = "monotone"

INF_ENDPOINT = "+∞"  # 开放 API 中表示“全部片数”端点的字符串


class Evidence(NamedTuple):
    """一条差分约束边的依据，可直接序列化进 409 回执。"""

    kind: str
    # segment 依据：来源报告与行号。current=True 表示来自被拒收的本次表。
    report_id: int | None = None
    row_number: int | None = None  # 区间在其所属报告内的序号（从 1 起）
    current: bool = False
    lo: int | None = None
    hi: int | None = None
    count: int | None = None
    # total 依据：登记总片数数值
    total_pieces: int | None = None

    def to_dict(self) -> dict:
        data: dict = {"kind": self.kind}
        if self.kind == EVIDENCE_SEGMENT:
            data.update(
                {
                    "source": "本次补报" if self.current else "已接收报告",
                    "report_id": self.report_id,
                    "row_number": self.row_number,
                    "lo": self.lo,
                    "hi": self.hi,
                    "count": self.count,
                }
            )
        elif self.kind == EVIDENCE_TOTAL:
            data.update({"total_pieces": self.total_pieces})
        return data


@dataclass(frozen=True)
class LabeledConstraint:
    """带出处的区间计数。

    ``report_id`` 为 None 表示它来自本次被拒收的补报（行号即该表内序号）；
    否则为已接收报告的编号，``row_number`` 为区间在该报告内的序号。
    """

    lo: int
    hi: int
    count: int
    report_id: int | None = None
    row_number: int = 1

    @property
    def current(self) -> bool:
        return self.report_id is None


@dataclass(frozen=True)
class Edge:
    """差分约束边：F(v) <= F(u) + weight，``evidence`` 为该约束的依据。"""

    u: int
    v: int
    weight: int
    evidence: Evidence


@dataclass
class _Graph:
    """端点空间上的差分约束图。

    节点 0..m-1 对应有限端点（排序后的厚度值），节点 m 为 +∞ 端点
    （F(+∞) 即全部片数）。
    """

    endpoints: list[int]
    node_count: int
    adj: list[list[Edge]] = field(default_factory=list)
    inf_node: int = 0

    def endpoint_label(self, node: int) -> int | str:
        return INF_ENDPOINT if node == self.inf_node else self.endpoints[node]


class InconsistentCounts(Exception):
    """给定计数约束无解（互相矛盾，或与总片数矛盾）。"""

    def __init__(self, message: str = "计数约束互相矛盾", chain: "ContradictionChain | None" = None):
        super().__init__(message)
        self.chain = chain


@dataclass(frozen=True)
class ChainStep:
    """矛盾链中的一步：F(end) - F(start) <= upper_bound。"""

    start: int | str
    end: int | str
    upper_bound: int
    evidence: Evidence

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "upper_bound": self.upper_bound,
            "basis": self.evidence.to_dict(),
        }


@dataclass(frozen=True)
class ContradictionChain:
    """首尾相接的矛盾链；各步上限之和严格小于零即构成 0 <= 负数。"""

    steps: tuple[ChainStep, ...]
    bound_sum: int

    def to_dict(self) -> dict:
        return {
            "steps": [step.to_dict() for step in self.steps],
            "bound_sum": self.bound_sum,
            "closed_loop": True,
        }


def _as_labeled(
    constraints: Sequence[Constraint] | Sequence[LabeledConstraint],
) -> list[LabeledConstraint]:
    labeled: list[LabeledConstraint] = []
    for position, item in enumerate(constraints, start=1):
        if isinstance(item, LabeledConstraint):
            labeled.append(item)
        else:
            lo, hi, count = item
            labeled.append(
                LabeledConstraint(lo=lo, hi=hi, count=count, report_id=None, row_number=position)
            )
    return labeled


def _build_graph(
    total_count: int,
    constraints: Sequence[Constraint] | Sequence[LabeledConstraint],
    extra_endpoints: Iterable[int] = (),
) -> _Graph:
    """构造带依据的差分约束图（节点即累计片数端点，含 +∞ 节点）。"""
    labeled = _as_labeled(constraints)

    endpoint_set = set(extra_endpoints)
    endpoint_set.add(0)
    for item in labeled:
        if item.lo < 0:
            raise ValueError("厚度端点必须非负")
        endpoint_set.add(item.lo)
        endpoint_set.add(item.hi)

    endpoints = sorted(endpoint_set)
    index = {value: i for i, value in enumerate(endpoints)}
    inf_node = len(endpoints)
    node_count = inf_node + 1
    adj: list[list[Edge]] = [[] for _ in range(node_count)]

    def add_edge(u: int, v: int, weight: int, evidence: Evidence) -> None:
        adj[u].append(Edge(u=u, v=v, weight=weight, evidence=evidence))

    # 单调性：相邻有限端点 a < b 满足 F(a) <= F(b)，边 b -> a，权重 0。
    # 最后一个有限端点到 +∞ 同样单调，保证图始终强连通。
    monotone_evidence = Evidence(kind=EVIDENCE_MONOTONE)
    for k in range(inf_node):
        add_edge(k + 1, k, 0, monotone_evidence)

    # 登记总片数：F(+∞) - F(0) = N，写成一对方向相反的边。
    total_evidence = Evidence(kind=EVIDENCE_TOTAL, total_pieces=total_count)
    add_edge(0, inf_node, total_count, total_evidence)       # F(+∞) <= F(0) + N
    add_edge(inf_node, 0, -total_count, total_evidence)      # F(0) <= F(+∞) - N

    # 每个补报区间 [lo, hi) 计数 c：F(hi) - F(lo) = c。
    for item in labeled:
        i = index[item.lo]
        j = index[item.hi]
        evidence = Evidence(
            kind=EVIDENCE_SEGMENT,
            report_id=item.report_id,
            row_number=item.row_number,
            current=item.current,
            lo=item.lo,
            hi=item.hi,
            count=item.count,
        )
        add_edge(i, j, item.count, evidence)
        add_edge(j, i, -item.count, evidence)

    return _Graph(
        endpoints=endpoints,
        node_count=node_count,
        adj=adj,
        inf_node=inf_node,
    )


def _extract_negative_cycle(
    graph: _Graph,
    relaxed_node: int,
    predecessor_edge: list[Edge | None],
) -> list[Edge]:
    """Bellman-Ford 第 n 轮仍可松弛时，沿前驱边链取出负环。

    直接保存前驱边（而不只是前驱节点），因为同一对端点之间可能存在
    多条依据不同的边，回执必须指出环实际使用的是哪一条。
    """
    # relaxed_node 要么在负环上，要么能走到负环；先走 n 步确保落到环上。
    node = relaxed_node
    for _ in range(graph.node_count):
        edge = predecessor_edge[node]
        assert edge is not None
        node = edge.u

    # 此时 node 在环上；沿前驱边收集，直到重新回到 node。
    edges: list[Edge] = []
    cursor = node
    while True:
        edge = predecessor_edge[cursor]
        assert edge is not None
        edges.append(edge)
        cursor = edge.u
        if cursor == node:
            break
    # 边是沿前驱反向收集的，反转后按环方向（终点接下一步起点）排列。
    edges.reverse()
    return edges


def _bellman_ford_cycle(graph: _Graph) -> list[Edge] | None:
    """Bellman-Ford 判定负环并返回环上的边（按环顺序），无负环返回 None。"""
    n = graph.node_count
    dist = [0] * n  # 全零初始化等价于添加一个到各点距离为 0 的虚拟源
    predecessor_edge: list[Edge | None] = [None] * n

    for _ in range(n):
        changed_node: int | None = None
        for u in range(n):
            du = dist[u]
            for edge in graph.adj[u]:
                candidate = du + edge.weight
                if candidate < dist[edge.v]:
                    dist[edge.v] = candidate
                    predecessor_edge[edge.v] = edge
                    changed_node = edge.v
        if changed_node is None:
            return None

    # 第 n 轮仍有松弛 => 存在负环，抽取之。
    return _extract_negative_cycle(graph, changed_node, predecessor_edge)  # type: ignore[arg-type]


def _has_negative_cycle_spfa(graph: _Graph) -> bool:
    """SPFA 快速判定是否存在负环（不负责取环）。

    维护“当前最短路径的边数”，达到顶点数即高度疑似负环；由于该计数在
    松弛过程中并非严格不变量，触发时交给 Bellman-Ford 做权威复核。
    """
    n = graph.node_count
    dist = [0] * n
    path_edges = [0] * n
    in_queue = [True] * n
    queue: deque[int] = deque(range(n))

    while queue:
        u = queue.popleft()
        in_queue[u] = False
        du = dist[u]
        for edge in graph.adj[u]:
            v = edge.v
            candidate = du + edge.weight
            if candidate < dist[v]:
                dist[v] = candidate
                path_edges[v] = path_edges[u] + 1
                if path_edges[v] >= n:
                    return True
                if not in_queue[v]:
                    in_queue[v] = True
                    queue.append(v)
    return False


def _chain_from_graph(graph: _Graph) -> ContradictionChain | None:
    """在已构造的图上判定负环并构造矛盾链；相容返回 None。"""
    if not _has_negative_cycle_spfa(graph):
        return None
    cycle_edges = _bellman_ford_cycle(graph)
    if cycle_edges is None:  # SPFA 误报，Bellman-Ford 复核为相容
        return None

    steps = tuple(
        ChainStep(
            start=graph.endpoint_label(edge.u),
            end=graph.endpoint_label(edge.v),
            upper_bound=edge.weight,
            evidence=edge.evidence,
        )
        for edge in cycle_edges
    )
    return ContradictionChain(
        steps=steps, bound_sum=sum(edge.weight for edge in cycle_edges)
    )


def find_contradiction(
    total_count: int,
    constraints: Sequence[Constraint] | Sequence[LabeledConstraint],
) -> ContradictionChain | None:
    """返回一条可验算的矛盾链；约束相容时返回 None。"""
    graph = _build_graph(total_count, constraints)
    return _chain_from_graph(graph)


def check_consistent(
    total_count: int, constraints: Sequence[Constraint] | Sequence[LabeledConstraint]
) -> None:
    """检查整组计数是否存在可行分布，不计算合格片数上下界。

    无解时抛出 :class:`InconsistentCounts`，并在 ``chain`` 属性上附带
    一条可验算的矛盾链。
    """
    chain = find_contradiction(total_count, constraints)
    if chain is not None:
        raise InconsistentCounts("计数约束互相矛盾", chain=chain)


def _shortest_paths(graph: _Graph, source: int) -> list[int]:
    """单源最短路（SPFA）；调用方须已确认图中不存在负环。"""
    n = graph.node_count
    dist = [INF] * n
    path_edges = [0] * n
    in_queue = [False] * n

    dist[source] = 0
    in_queue[source] = True
    queue: deque[int] = deque([source])

    while queue:
        u = queue.popleft()
        in_queue[u] = False
        du = dist[u]
        for edge in graph.adj[u]:
            v = edge.v
            candidate = du + edge.weight
            if candidate < dist[v]:
                dist[v] = candidate
                path_edges[v] = path_edges[u] + 1
                if path_edges[v] >= n:  # 防御：理论上相容数据不会走到这里
                    raise InconsistentCounts("计数约束互相矛盾")
                if not in_queue[v]:
                    in_queue[v] = True
                    queue.append(v)
    return dist


def compute_bounds(
    total_count: int,
    constraints: Sequence[Constraint] | Sequence[LabeledConstraint],
    qualified_lo: int,
    qualified_hi: int,
) -> tuple[int, int]:
    """返回同时满足全部计数时，合格区间 [qualified_lo, qualified_hi) 内
    片数可同时达到的最小值和最大值。

    约束无解时抛出 :class:`InconsistentCounts`。
    """
    if qualified_hi <= qualified_lo:
        raise ValueError("合格区间必须满足 qualified_hi > qualified_lo")

    graph = _build_graph(
        total_count, constraints, (qualified_lo, qualified_hi)
    )

    # 先排除负环；顺带在异常时提供矛盾链。
    chain = _chain_from_graph(graph)
    if chain is not None:
        raise InconsistentCounts("计数约束互相矛盾", chain=chain)

    index = {value: i for i, value in enumerate(graph.endpoints)}
    lo_node = index[qualified_lo]
    hi_node = index[qualified_hi]

    from_lo = _shortest_paths(graph, lo_node)
    from_hi = _shortest_paths(graph, hi_node)

    # max(F(hi) - F(lo)) = dist(lo -> hi)
    # min(F(hi) - F(lo)) = -dist(hi -> lo)
    max_qualified = from_lo[hi_node]
    min_qualified = -from_hi[lo_node]
    if max_qualified >= INF or min_qualified <= -INF:
        # 图强连通且已排除负环，正常情况下不会发生。
        raise RuntimeError("最短路不可达，约束图构造异常")  # pragma: no cover
    return int(min_qualified), int(max_qualified)
