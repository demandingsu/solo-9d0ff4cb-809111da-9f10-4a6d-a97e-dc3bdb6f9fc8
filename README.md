# 纸样厚度计数核对 API

登记一批纸样的总片数，分次提交互可重叠的厚度区间计数（整数微米，区间
`[lo, hi)`，含起点不含终点）。给定合格区间，返回**同时满足全部已接收计数**
时合格片数可达到的最小值和最大值。计算按端点做整数规划，不逐片展开。

补报与已有计数不相容时**整份拒收**（HTTP 409），已有结果保持不变、仍可查询。
409 回执给出需要回查的行清单，以及一条首尾相接、各步上限之和小于零的
可验算矛盾链（依据精确到本次表行号或旧报告号与区间号）。

## 快速开始

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs> 录入与查询。SQLite 数据保存在命名卷
`inspection-data`（容器内 `/data/inspection.db`）中，重启容器不丢失。

本地不用 Docker 时：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/inspections` | 登记总片数（`total_pieces`，可选 `note`） |
| `GET` | `/inspections` | 列出全部登记 |
| `GET` | `/inspections/{id}` | 查看登记及已接收的分次补报 |
| `PATCH` | `/inspections/{id}` | 更正总片数/备注；新总片数与已有计数冲突则拒收 |
| `POST` | `/inspections/{id}/reports` | 提交一批区间计数 `{"segments":[{"lo","hi","count"}]}` |
| `GET` | `/inspections/{id}/reports` | 查看补报历史 |
| `GET` | `/inspections/{id}/bounds?qualified_lo=&qualified_hi=` | 查询合格片数上下界 |
| `GET` | `/health` | 健康检查 |

### 示例

```bash
curl -X POST localhost:8000/inspections \
  -H 'content-type: application/json' -d '{"total_pieces":100}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":70}]}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":50,"hi":70,"count":60}]}'

curl 'localhost:8000/inspections/1/bounds?qualified_lo=50&qualified_hi=60'
# {"inspection_id":1,"total_pieces":100,"qualified_lo":50,"qualified_hi":60,
#  "min_count":30,"max_count":60}

# 冲突补报：整份 409 拒收，上面的结果不受影响
curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":71}]}'
# HTTP 409，回执为结构化对象：
# {"detail":{
#   "message":"补报与已有计数（或本报告内部）不相容，整份拒收；已有结果保持不变",
#   "recheck_rows":[ ... 需要回查的行（本次表行号 / 旧报告号+区间号）... ],
#   "contradiction_chain":{
#     "steps":[{"start":40,"end":60,"upper_bound":70,"basis":{...}}, ...],
#     "bound_sum":-1,"closed_loop":true}}}
```

## 409 拒收回执：回查行与矛盾链

补报（或 `PATCH` 总片数）与已有计数不相容时，整份退回，回执的 `detail`
不再只是一句话，而是给出两部分可复核的证据：

* `recheck_rows`：需要回查的行。每行标明出处——`"本次补报"`（行号即本次
  表内序号，从 1 起，`report_id` 为 `null`）或 `"已接收报告"`（给出旧
  `report_id` 与该报告内的区间行号 `row_number`），并附上 `lo/hi/count`。
* `contradiction_chain`：一条首尾相接的矛盾链。以“厚度严格小于端点 x 的
  累计片数” F(x) 为准（`"+∞"` 端点表示全部片数），每步给出两个端点、
  `F(end) - F(start)` 的上限 `upper_bound`，以及该上限的依据 `basis`：
  - `segment`：某次补报的区间计数，注明本次表行号或旧报告号与区间号；
  - `total`：登记总片数，附数值，对应 F(+∞) - F(0) = 总片数；
  - `monotone`：累计片数单调规则（厚度越大，累计片数不减）。

沿链相加：每步终点接下一步起点，末步回到首步起点，故各步上限之和约束
F(x0) - F(x0)。`bound_sum` 严格小于零即得到 `0 <= 负数` 的矛盾，链本身
可独立验算。证据只使用拒收判定当时已落库的数据与本次表，后续更正不改变
已返回的回执。冲突只退回这一份报告；其他批次的相容补报照常接收。

## 计算方法

把所有区间端点（含查询端点与厚度零点）排序，将厚度轴切成原子区间。落在同一
原子区间内的整数厚度在所有计数约束下等价，因此每个原子只需一个非负整数变量
（片数）。设 F(x) 为“厚度严格小于 x 的累计片数”，F(+∞) 为全部片数：

* 区间 `[lo, hi)` 计数为 `c` ⇔ `F(hi) - F(lo) = c`；
* 总片数 `N` ⇔ `F(+∞) - F(0) = N`；
* 原子片数非负、累计片数单调 ⇔ 相邻端点 `a < b` 满足 `F(a) <= F(b)`。

每条约束写成差分约束边 `u -> v`，权重 `w`，含义 `F(v) <= F(u) + w`，即
“F(v) 减 F(u) 的上限为 w”。边同时记录依据（本次表行号 / 旧报告号与区间号 /
登记总片数 / 单调规则）。

目标“合格区间片数”为 `F(hi) - F(lo)`。其最大值等于差分约束图上 `lo → hi`
的最短路，最小值等于反向最短路取负。约束矩阵为连续一矩阵（全幺模），最短路
给出的整数解即原整数规划的精确最优解。负环 ⇔ 计数互相矛盾。

发现负环时，沿 Bellman-Ford 前驱边链把环上的边取出，即得到一条首尾相接、
各步上限之和严格小于零的**矛盾链**，作为 409 回执的可验算证据。

复杂度只取决于端点数量与图规模，与总片数无关；实现使用 SPFA 快速判定负环，
触发时用 Bellman-Ford 复核并抽取负环，避免误判。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

* `tests/test_solver.py`：重叠区间、相邻不重叠区间、零计数、冲突检测、
  大数量不展开，以及 40 组与逐片枚举对拍。
* `tests/test_contradiction.py`：矛盾链的闭环性、负的上限和、依据类型与
  端点/权重自洽、行号与报告号归属，以及 60 组“枚举无解 ⇔ 存在负环”对拍。
* `tests/test_api.py`：登记、补报、跨批次重叠、跨批次冲突整份拒收、
  单份报告内部冲突拒收、拒收后原结果可查、`PATCH` 冲突拒收、参数校验、
  409 回执结构与证据快照语义、`/docs`。
* `tests/test_concurrency.py`：并发冲突补报只有一个成功；并发相容补报
  全部接收；一个批次冲突不阻塞其他批次。
