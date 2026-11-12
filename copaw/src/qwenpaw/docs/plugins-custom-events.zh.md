# 插件请求级自定义事件

最低支持版本：**本仓库 Paw 2.0.1**，自定义事件 API 版本为 `1`。
这不是对其他来源同名版本的支持声明；插件也可以检查
`qwenpaw.plugins.CUSTOM_EVENT_API_VERSION >= 1`。

此接口只提供通用事件投递能力。检索触发时机、知识库 ID 和统计口径由插件决定，
Paw 不识别知识库，也不会自动生成 `platform_kb_called` 事件。

## 调用方式

在 `on_model_call` 中间件或异步工具的实际检索代码处调用：

```python
from qwenpaw.plugins import emit_custom_event

await emit_custom_event(
    type="platform_kb_called",
    data="知识库ID1,知识库ID2",
)
```

持有 `PluginApi` 时，也可以使用等价的
`await api.emit_custom_event(type="platform_kb_called", data="知识库ID1,知识库ID2")`。

同步工具使用同步接口：

```python
from qwenpaw.plugins import emit_custom_event_sync

emit_custom_event_sync(type="platform_kb_called", data="知识库ID1,知识库ID2")
```

也支持 `api.emit_custom_event_sync(...)`。在 `asyncio.to_thread` 等继承请求
ContextVar 的工作线程内，同步接口会将事件提交回请求的事件循环。
自行创建的线程必须显式传播请求上下文；请求外、启动回调和独立进程不能使用该接口。

中间件示例：

```python
from agentscope.middleware import MiddlewareBase
from qwenpaw.plugins import emit_custom_event


class RetrievalMiddleware(MiddlewareBase):
    async def on_model_call(self, agent, input_kwargs, next_handler):
        # 在现有业务代码实际发起检索的位置发送一次。
        # 此处 knowledge_base_ids 来自插件自己的检索配置。
        knowledge_base_ids = ["知识库ID1", "知识库ID2"]
        await emit_custom_event(
            type="platform_kb_called",
            data=",".join(knowledge_base_ids),
        )
        # 接着执行插件原有的检索、上下文注入和模型调用逻辑。
        return await next_handler(**input_kwargs)
```

不要直接按 `on_model_call` 的调用次数计数：缓存、模型重试、多轮推理不一定意味着
发生了新的检索。每次实际检索发送一次；失败检索、检索自身重试是否计数由业务双方约定。
逗号字符串要求知识库 ID 本身不含逗号。

## SSE 格式与投递语义

插件提交的业务事件仍是：

```json
{"type":"platform_kb_called","data":"知识库ID1,知识库ID2"}
```

Paw 自动附加传输字段，实际 SSE 帧示例：

```text
data: {"object":"custom_event","type":"platform_kb_called","data":"知识库ID1,知识库ID2","response_id":"response_abc","session_id":"session_abc","sequence_number":7}

```

- `response_id` 复用本轮 `object: "response"` 的 `id`，不是另造业务请求标识。
- `sequence_number` 与普通流式消息共用本轮的递增序列，在实际输出时统一分配。
- 事件立即唤醒执行期间的 SSE 输出，不等待工具或模型回答结束；异步执行代码应正常让出事件循环。
- 接口成功返回 `None` 表示已接收进本次请求的待发送队列，不表示客户端已经收到。
- 已接收事件经过 Console 的现有任务缓冲及订阅广播。正常完成、失败、取消时，
  这些事件排在终止响应之前；终止响应发出后拒绝新事件。
- 无当前请求或请求已关闭时抛出 `RuntimeError`；空事件类型抛出 `ValueError`；
  `data` 必须可编码为标准 JSON，不接受任意 Python 对象或 NaN/Infinity。
- 自定义事件不写入模型消息、会话上下文或工具调用结果。Paw 控制台不会把它显示为聊天或工具卡片。
  对接平台直接消费 SSE 中 `object == "custom_event"` 的事件。

## 断线重连

继续使用 `POST /api/console/chat`，保留原来的会话、用户及频道参数，并增加：

```json
{
  "session_id": "session_abc",
  "user_id": "user_abc",
  "input": [],
  "reconnect": true,
  "response_id": "response_abc"
}
```

如最初请求设置了客户端 `run_id`，重连时也可以携带同一个 `run_id`。
`response_id` 和 `run_id` 同时提供时必须同时匹配；未指定标识时接入当前执行，
若当前没有执行则重放最近一次保留的执行。建议始终携带标识，避免接入其他轮次。

重连会重放该执行的完整缓冲，再接收新事件；已结束执行重放完成后关闭连接。
重放使用原始 SSE 字符串，`response_id` 和 `sequence_number` 不变。
消费方应使用 `(response_id, sequence_number)` 去重，避免把重放重复计算为检索。
找不到匹配的执行时返回 HTTP 404，不会启动新的推理。

已结束执行默认在内存保留 **300 秒**，每个工作区最多保留 **128 次**，超过数量时
淘汰最早保留项。活动执行不受此数量限制。服务重启后缓冲消失；本接口不提供持久化统计。

验收应覆盖中间件和工具实时投递、并发请求隔离、完成/失败/取消前收尾、
活动及已结束任务的重连、重放标识不变，以及模型输入和工具展示不受自定义事件影响。

## 排查日志

默认 INFO 日志中搜索 `custom_event`，按 `response_id` 关联同一次请求：

- `accepted`：接口已接收插件事件，记录事件类型、会话和待发送数量。
- `published`：运行时已分配 `sequence_number`，交给输出流。
- `sse_ready`：Console 已序列化 SSE 帧，交给任务缓冲和连接输出；不代表客户端已收到。
- `terminal`：本轮终止时记录 `accepted`、`published`、`pending` 数量；
  若 `accepted=0`，说明本轮没有通过该接口接收插件事件。
- `replay_attached` / `replay_miss`：重连是否找到执行，记录活动或已完成状态、缓冲帧数。
- `rejected`（WARNING）：请求外调用、请求已关闭或事件类型无效。

这些日志记录传输标识和计数，不记录事件 `data` 正文。
