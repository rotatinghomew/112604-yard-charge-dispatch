# 候装车充电作业、整组保电与计费

共享供电容量下编排整组交车，按桩回执兑现作业和计费。

当前资料覆盖现场和业务对端使用的交换记录。数据均为合成，`samples/` 保存原始样本和字段约定，《交换资料约定》说明同一对象的编号、时间及来源。

仓库现已提供纯 Python 标准库实现的场端服务：核心规则在 `yard_service/service.py`，一分钟粒度的容量排程在 `yard_service/planner.py`，HTTP 入口为 `yard_service/server.py`，并附带一个无需构建的值班页面。

## 运行环境

使用 Python 3.11.2。资料检查和服务本身只使用标准库，无需下载安装包。由项目根目录运行以下命令，可以检查合成资料能否解析以及引用能否对应：

```sh
python3 -m unittest discover -s tests -v
```

启动场端服务（默认读取 `samples/`，固定到样本回放时刻）：

```sh
python3 app.py --data samples --now 2026-11-27T01:42:00Z --port 8088
```

浏览器打开 <http://127.0.0.1:8088/> 可使用调度、充电值班、交车和财务工作台；JSON 接口索引见 <http://127.0.0.1:8088/api>。

如需同时对接本地桩端录制，先在一个终端运行：

```sh
python3 peer_replay.py
```

再启动场端服务：

```sh
python3 app.py --peer http://127.0.0.1:8087 \
  --data samples --now 2026-11-27T01:42:00Z --port 8088
```

## 业务规则与接口

服务对象可直接嵌入 Python 测试：

```py
from yard_service import YardService

yard = YardService("samples", now="2026-11-27T01:42:00Z")
yard.capacity()
yard.evaluate_bookings(["C-9"], {"at": "2026-11-27T01:42:00Z"})
yard.billing_view()
yard.handoff_view()
```

主要 HTTP 接口：

- 车队/端口资料：`POST /api/fleet-reports`、`GET /api/fleets`、`GET /api/ports`
- 急单与容量：`POST /api/urgent-orders`、`POST /api/capacity/assess`、`POST /api/urgent-orders/{id}/accept`
- 换档：`POST /api/changes`、`POST /api/changes/{id}/confirm`、`POST /api/split-proposals/{id}/confirm`
- 计划与派工：`POST /api/plans/evaluate`、`POST /api/plans/commit`、`POST /api/dispatch`、`POST /api/dispatch/reassign`
- 桩端消息：`POST /api/pile/sessions`、`POST /api/pile/sessions/{id}/stop`、`POST|GET /api/pile/sessions/{id}/poll`、`POST /api/pile/sessions/{id}/stop-receipt`、`POST /api/pile/messages`、`POST /api/meter`
- 交车：`GET /api/handoff`、`POST /api/handoff/collect`
- 财务：`GET /api/billing`、`POST /api/payments`、`POST /api/orders/{vin}/pay`、`POST /api/billing/settle`
- 调度复核：`GET /api/reviews/dispatch`、`POST /api/reviews/dispatch/{id}/decision`
- 总览/状态：`GET /api/dashboard`、`GET /api/state`、`POST /api/action`

关键约束：

1. 默认按 `booking` 整车队承诺和交车；只有部分车辆达到 `minimum_wh` 时，交车接口仍返回 `whole_group_not_ready`。
2. 容量评估同时考虑接口型号、端口上限、供电组上限、已承诺作业和有效会话。容量不足时输出 `original_plan_kept/available`、`split_requires_buyer`、`handoff_time_change_required` 三类结论。
3. 停机命令无应答或桩端状态为 `unknown` 时，VIN、端口和 `authorized_kw` 仍占用；只有收到可核对的 `stopped` 状态/回执后，才从证明的停机时刻释放容量。重复接桩会返回 `vin_already_plugged` 或 `port_occupied`。
4. `total_wh` 是会话累积实送电量，不能把计量事件逐条相加；`supersedes` 指向的更正会取代旧读数。迟到的更正可在结算后生成补款/退差，历史收款记录保持不变。
5. 计费只按有效会话的累积实送 Wh 和车辆有效费率计算；已排班但没有实送/计量的功率不产生费用。
6. 停机不等于结算完成：缺少有效计量不能最终结算；实际电量不足时，即使订单已标记付款，也会生成 `under_delivery` 调度复核，列出未达标车辆和剩余窗口。

## 本地对端

运行 `python3 peer_replay.py` 可在 `127.0.0.1:8087` 重放 `samples/peer.json` 中的 HTTP 录制。`--recording` 可读取另一份同格式录制，`--port` 可更改端口。每条录制依次写请求方法、路径、JSON 请求体和响应序列。没有请求体时写 null。每种请求独立推进响应，最后一项可重复读取，状态码 0 代表连接断开且未收到应答。重启重放进程会重新开始读取录制，不代表现实中的远端状态被重置。
