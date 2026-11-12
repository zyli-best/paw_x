## 工作流硬性约束
| **规则** | **适用范围** | **违反后果** |
| --- | --- | --- |
| 必须有且仅有 **1 个 **`start`**、1 个 **`end` | 主画布 + 每个 `loop`/`loop-pro` 的 `blocks` | 校验失败 |
| `start` 必须有出边；`end` 必须有入边 | 同上 | 校验失败 |
| 非分支终点节点需有入边+出边 | `start`/`end`/分支节点例外 | 校验失败 |
| **禁止有向环** | 主图与循环体内 | 校验失败 |
| `loop-continue`/`loop-terminate` **仅**在 `loop`/`loop-pro` 的 `blocks` 内 | 全局 | 构建失败 |
| 子工作流嵌套深度 ≤ **8** | `workflow` 节点链 | 运行失败 |
| 主画布 `nodes` ≥ 2，`edges` ≥ 1 | 图 schema | 校验失败 |


### 循环体 I/O 映射
```plain
loop / loop-pro（外层）
├── inputs[]     ← 外部传入，驱动循环
├── outputs[]    ← 每次迭代结果聚合成 array 后输出
└── node_param.blocks
    ├── nodes: [ start(别名「loop-开始」), ..., end(别名「loop-结束」) ]
    └── edges: [...]
```

| 映射关系 | 规则 |
| --- | --- |
| 外层 `inputs` → **loop-开始** `outputs` | **名称一致**；`loop_type=array` 时开始节点输出为**元素类型**（非 array） |
| **loop-结束** `inputs` → 外层 `outputs` | **名称一致**；每次迭代写入，最终聚合为数组 |
| loop-开始/结束变量 | **不需要** `description`（与主画布 start/end 不同） |
| 循环体内引用外层循环节点 | 通过循环节点虚拟注入变量池，ref 指向外层 `loop`/`loop-pro` 的 `id` |


### 循环体允许节点
`loop`** 体内**（不可含 continue/terminate）：`llm`、`code`、`message`、`http`、`knowledge-retrieval`、`if-else`、`decision`、`parameter-extractor`、`variable-aggregator`、`variable-update`、`json-serialize`、`json-deserialize`

`loop-pro`** 体内**（在上述基础上增加）：`loop-continue`、`loop-terminate`

**禁止**放入循环体：主画布级 `start`/`end`（须用 loop-开始/loop-结束）、`workflow`、`database-*`、`document-*`、`input`/`form-input`、`plugin`、`agent`（引擎未限制但前端菜单未提供，Agent 勿生成）

### 节点搭配速查表
| **场景** | **必须搭配** | **禁止/注意** |
| --- | --- | --- |
| 任意工作流 | `start` → … → `end` | 不可缺 start/end |
| 循环 | `blocks` 内 `loop-开始` + `loop-结束` | 子节点不可放主画布顶层 |
| 条件分支 | 每个 `case.id` 一条出边 + `intent_chain|{id}` | 无 outputs，不可当数据源 |
| 意图识别 | 每个 `intent_chains[].id` 一条出边 | 输入名固定 `Query` |
| 数据库 CRUD | `database-connection` → 下游 ref `engine_handle` | 禁止字面量伪造 handle |
| 子工作流 | 已发布子流 `flow_id`；I/O 由子流 start/end 定义 | **不可改名**入参/出参 |
| 人工暂停 | `input`/`form-input` 后须能继续到 `end` | 触发 `waiting_input`，需 `/v2/run/action` |
| 流式展示 | `message` 引用 `llm`/`agent`/`sse` 流式字段 | — |
| 文档 RAG | `start.enable_file_upload` → `document-extractor` → `llm` | extractor 输入名 `document` |
| ReAct/工具 | `llm`/`agent` + `mcp_server_urls` 或 `agent_skills` | — |


## start — 开始节点


**功能描述**

工作流入口节点。从 API/运行入参读取全局变量，按 `outputs` 声明的类型做类型转换后写入变量池，供下游引用。无 `inputs`。开始节点的输入变量的描述不能为空。

### Agent 硬性约束
+ 主画布：**有且仅有 1 个**；必须有出边
+ 主画布：`outputs[].name` 非空；`outputs[].schema.description` **不能为空**（loop-开始除外）
+ `enable_file_upload=true` 时须声明 `document` 类输出供附件合并
+ 循环体内使用 `alias_name="loop-开始"` 时：**不需要 description**；`outputs` 与外层 loop `inputs` 名称/类型同步

### 输入变量（inputs）
无。

### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `usr_input` | string | 推荐 | 主画布必填 | 用户主输入 |
| `document` | object | 可选 | 主画布必填 | `enable_file_upload=true` 时自动/手动声明 |
| 自定义 | 多种 | 可选 | 主画布必填 | 工作流全局入参 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `enable_file_upload` | boolean | 否 | `false` | 开启后运行可携带顶层 `attachments`，合并为 `document` 变量供下游引用 |
| `file_output_name` | string | 否 | `document` | 已废弃，固定为 `document`，仅兼容旧 DSL |


---

## end — 结束节点
**功能描述**

工作流出口节点。支持两种输出模式：变量直出（`output_mode=0`）或模板聚合（`output_mode=1`）。无论哪种模式，结果中均包含 `output_text`。结束节点的描述不能为空。

### Agent 硬性约束
+ 主画布：**有且仅有 1 个**；必须有入边
+ 主画布：`inputs[].name` 非空、引用或值非空；`inputs[].schema.description` **不能为空**（loop-结束除外）
+ `output_mode=1` 时 `template` **必填**
+ 循环体内 `alias_name="loop-结束"`：`inputs` 与外层 loop `outputs` **名称一致**；不需要 description

### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `output_mode` | integer | 否 | `0` | `0`：变量输出；`1`：按 `template` 聚合为文本 |
| `template` | string | 否 | `""` | 聚合模板，支持 `{{变量名}}`；`output_mode=1` 时使用 |
| `stream` | boolean | 否 | `true` | 是否流式输出（继承 BaseNode） |


---

## llm — 大模型节点
**功能描述**

调用大语言模型，支持文本/JSON/Markdown 输出、对话历史、视觉多模态、MCP 工具与 ReAct 智能体模式。模板中的 `{{变`

`量名}}` 由 inputs 填充。

### Agent 硬性约束
+ `node_param.model`、`node_param.template` **必填**
+ 所有 inputs：`name` 非空、值非空
+ 所有 outputs：`name` 非空
+ `output_type=json` 时 outputs 宜结构化定义
+ 可流式（`stream=true`），供 `message` 引用

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `input` | string | 推荐 | 无硬性 | 主提示上下文；名须与 template 中 `{{}}` 一致 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `text` | string | 是 | 可选 | 模型生成文本 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `model` | string | **是** | `Qwen3.6-35B-A3B` | 模型名称；用户未指定时默认使用 `Qwen3.6-35B-A3B` |
| `template` | string | 是 | — | 用户提示词模板 |
| `max_tokens` | integer | 否 | `4096` | 最大生成 token |
| `api_base` | string | 否 | `https://aaas.lcfuturecenter.com/prod-api/aibase/model` | API 地址；用户未指定时默认使用此地址 |
| `temperature` | number | 否 | `0.1` | 采样温度 |
| `app_id` | string | 否 | `appid14` | 应用 ID |
| `api_key` | string | 否 | `""` | API Key（敏感，勿硬编码）；平台自动获取，无需手动填写 |
| `api_secret` | string | 否 | `""` | API Secret |
| `uid` | string | 否 | `uid14` | 用户 ID |
| `markdown_template` | string | 否 | Markdown 格式要求 | `output_type=markdown` 时附加 |
| `enable_chat_history` | boolean | 否 | `false` | 是否启用对话历史 |
| `history_rounds` | integer | 否 | `10` | 历史轮数 |
| `output_type` | string | 否 | `text` | `text` / `json` / `markdown` |
| `system_prompt` | string | 否 | `""` | 系统提示词 |
| `enable_vision` | boolean | 否 | `false` | 视觉多模态 |
| `mcp_server_urls` | string[] | 否 | `[]` | MCP 服务地址列表 |
| `mcp_servers` | object[] | 否 | `[]` | 平台 MCP 配置 |
| `enable_react` | boolean | 否 | `true` | ReAct 工具调用模式 |
| `max_agent_steps` | integer | 否 | `10` | 智能体最大步数 |
| `agent_skills` | string[] | 否 | `[]` | MCP 工具名称列表 |
| `enable_function_call` | boolean | 否 | `false` | 函数调用模式 |
| `memory_max_size` | integer | 否 | `50` | 记忆条数上限 |
| `enable_think_content` | boolean | 否 | `false` | 输出是否含思考内容 |
| `stream` | boolean | 否 | `false` | 流式输出；可被消息节点引用 |


---

## code — 代码节点
**功能描述**

在沙箱代码服务中执行 Python 或 JavaScript。inputs 按名称映射为 `main()` 参数，返回值 dict 的 key 映射到 outputs。

### Agent 硬性约束
+ `node_param.code` **必填**
+ 有 `schema.value` 的 inputs：name 与值非空
+ outputs `name` 非空

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| 自定义 | 多种 | 条件 | — | 与 `main()` 参数名一致 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `text` | string | 推荐 | 可选 | 默认输出 |
| 自定义 | 多种 | 可选 | 可选 | 对应 return dict 的 key |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `code` | string | **是** | — | 源代码 |
| `language` | string | 否 | `python` | `python` 或 `javascript` |


---

## knowledge-retrieval — 知识库节点（平台）
**功能描述**

从多个知识库并发检索，经 `KNOWLEDGE_API_BASE` 调用检索服务，支持语义/混合/全文策略、改写、重排与标签过滤。

### Agent 硬性约束
+ 输入名 `Query`** 必填且值非空**
+ `and`/`or` 为 ref 类型时值非空
+ 须配置 `knowledge_list` 或等效检索目标

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `Query` | string | **是** | 无硬性 | 检索查询（固定名，非 `input`） |
| `and` | string/array | 否 | — | 标签 AND 过滤 |
| `or` | string/array | 否 | — | 标签 OR 过滤 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `text` | string | 推荐 | 可选 | 检索结果文本（UI 默认） |
| `output_list` | array | 可选 | 可选 | 结构化片段列表 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `strategy` | integer | 否 | `1` | `1` 语义；`2` 混合；`3` 全文 |
| `top_k` | integer | 否 | `10` | 最大召回数（1–20） |
| `min_score` | float | 否 | `0.3` | 最小匹配度（0.01–0.99） |
| `use_rewrite` | boolean | 否 | `false` | 查询改写 |
| `use_rerank` | boolean | 否 | `false` | 结果重排 |
| `knowledge_list` | array | 否 | — | `[{knowledge_uuid, knowledge_name}]` |
| `tags` | object | 否 | `{and:[], or:[]}` | 标签过滤；支持 literal 或 ref |


---

## http — HTTP 请求节点
**功能描述**

异步 HTTP 请求，含 SSRF 防护、重试、多种 body 类型与模板变量填充。

### Agent 硬性约束
+ `url`** 必填**（input 或 node_param）
+ 自定义 inputs：name 非空、有 value 则非空
+ outputs `name` 非空

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `url` | string | **是** | — | 请求 URL |
| `headers` | object | 否 | — | 可覆盖 node_param |
| `auth` | object | 否 | — | 认证信息 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `body` | object/string | 是 | 可选 | 响应体 |
| `status_code` | integer | 是 | 可选 | HTTP 状态码 |
| `headers` | object | 是 | 可选 | 响应头 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `method` | string | 否 | `GET` | GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS |
| `headers` | object/array/string | 否 | — | 请求头；支持 `{{变量}}` |
| `params` | object/array/string | 否 | — | 查询参数 |
| `body` | object/string/array | 否 | — | 请求体（POST/PUT/PATCH） |
| `body_type` | string | 否 | `raw/json` | none/form-data/x-www-form-urlencoded/raw/* 等 |
| `timeout` | integer | 否 | `10` | 超时秒数，上限 86400 |
| `retry_times` | integer | 否 | `3` | 重试次数 |
| `retry_interval` | integer | 否 | `1` | 重试间隔（秒） |
| `ignore_error` | boolean | 否 | `false` | 失败时返回空输出而非 FAILED |
| `allow_private_network` | boolean | 否 | `false` | 跳过 SSRF（仅内网/开发） |
| `body_slot_typing` | boolean | 否 | `true` | raw/json 时保留 JSON 类型占位 |


---

## sse — SSE 请求节点
**功能描述**

消费 Server-Sent Events 流，将分块数据通过 callback 流式推送，最终拼接为完整 `content`。

### Agent 硬性约束
+ `url`** 必填**
+ 推荐输出名 `sse_content`

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `url` | string | **是** | — | SSE 端点 |
| `headers` | object | 否 | — | 可选 |
| `auth` | object | 否 | — | 认证 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `sse_content` | string/object | 推荐 | 可选 | 完整 SSE 内容 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `method` | string | 否 | `GET` | HTTP 方法 |
| `body_type` | string | 否 | `raw/json` | 请求体类型 |
| `timeout` | integer | 否 | `1800` | 长连接超时（秒），上限 86400 |
| `allow_private_network` | boolean | 否 | `false` | 跳过 SSRF |
| `headers` | object/array/string | 否 | 请求头 |  |
| `params` | object/array/string | 否 | 查询参数 |  |
| `body` | object/string/array | 否 | 请求体 |  |
| `parse_json` | boolean | 否 | `true` | 将 SSE data 解析为 JSON 对象 |
| `body_slot_typing` | boolean | 否 | `true` | 同 HTTP 节点 |
| `stream` | boolean | 否 | `true` | 默认流式 |


---

## image-understand — 图片理解节点
**功能描述**

多模态 VL 模型：图片（file_id、路径或 URL）+ 文本提示，输出理解结果。

### Agent 硬性约束
+ `model`、`api_base`、`template` **必填**（前端校验）
+ inputs/outputs `name` 非空；有值 inputs 非空

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `image` | string | **是** | 无硬性 | file_id、路径或 URL |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `text` | string | 是 | 可选 | 理解结果 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `model` | string | **是** | — | VL 模型名称 |
| `max_tokens` | integer | 否 | `4096` | 最大 token |
| `api_base` | string | 否 | `LLM_API_BASE` | API 地址 |
| `temperature` | number | 否 | `0.1` | 温度 |
| `api_key` | string | 否 | `""` | API Key |
| `template` | string | 否 | `""` | 用户提示模板 |
| `system_prompt` | string | 否 | `""` | 系统提示 |
| `output_type` | string | 否 | `text` | `text` 或 `json`（多字段） |


---

## document-extractor — 文档解析节点
**功能描述**

从 txt/docx/pptx/pdf/xlsx 等提取纯文本与元数据。

### Agent 硬性约束
+ 输入名固定 `document`，值非空
+ outputs `name` 非空

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `document` | string/object | **是** | — | file_id、`{file_id, file_origin_name}` 或路径 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `text` | string | 是 | 可选 | 纯文本 |
| `metadata` | object | 是 | 可选 | 文件信息与统计 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `max_chars` | integer | 否 | `200000` | 最大字符数 |
| `include_metadata` | boolean | 否 | `true` | 是否输出 metadata |


---

## document-converter — 文档格式转换节点
**功能描述**

V1：Markdown 转 Excel（.xlsx），上传后返回下载链接。（当前只支持md转excel）

### Agent 硬性约束
+ 输入名固定 `markdown`，值非空
+ outputs `name` 非空

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `markdown` | string | **是** | — | Markdown 文本 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `download_url` | string | 是 | 可选 | 下载链接 |
| `file_id` | string | 是 | 可选 | 文件 ID |
| `document` | object | 是 | 可选 | 文件对象 |
| `metadata` | object | 是 | 可选 | 转换元数据 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `source_format` | string | 否 | `md` | 源格式 |
| `target_format` | string | 否 | `xlsx` | 目标格式 |
| `sheet_strategy` | string | 否 | `multi_sheet` | `multi_sheet` / `single_sheet` |
| `output_filename` | string | 否 | `""` | 输出文件名 |
| `max_chars` | integer | 否 | `200000` | 输入 Markdown 最大长度 |


---

## decision — 意图识别节点
**功能描述**

基于 LLM 对用户输入进行意图分类，通过 `edge_source_handle` 路由到对应分支。

### Agent 硬性约束
+ `intent_chains` **必填**；`model` **必填**
+ 输入名固定 `Query`，值非空
+ 非默认意图（`intent_type≠1`）：`name` 与 `description` **均非空**
+ **每个** `intent_chains[].id` 须有一条出边，`source_handle=intent_chain|{id}`
+ 有 outputs 但主要用于路由；`edge_source_handle` 为运行时路由结果

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `Query` | string | **是** | 无硬性 | 待分类文本（固定名） |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `class_name` | string | 是 | 可选 | 命中意图 name |
| `output` | string | 可选 | 可选 | LLM 原始输出 |
| `llm_stream_res` | string | 可选 | 可选 | 流式内容 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `model` | string | **是** | `gpt-4o` | 模型 |
| `temperature` | number | 否 | `0.5` | 温度 |
| `api_base` | string | 否 | `LLM_API_BASE` | API 地址 |
| `api_key` | string | 否 | — | API Key |
| `max_tokens` | integer | 否 | `1024` | 最大 token |
| `prompt_prefix` | string | 否 |  | 系统提示词 |
| `app_id` | string | 否 | `""` | 应用 ID |
| `uid` | string | 否 | `""` | 用户 ID |
| `enable_chat_history` | boolean | 否 | `false` | 对话历史 |
| `intent_chains` | array | 是 | — | `[{id, name, description, intent_type}]` |
| `use_function_call` | boolean | 否 | `false` | 函数调用分类 |
| `enable_think_content` | boolean | 否 | `false` | 含思考内容 |
| `enable_llm_content` | boolean | 否 | `false` | 将回答放入 `llm_stream_res` |


---

## if-else — 条件分支节点
**功能描述**

按 `cases` 条件列表求值，设置 `edge_source_handle` 为命中分支的 `case.id`，不产出业务数据 outputs。

### Agent 硬性约束
+ `cases` **必填**；每项含 `id`、`level`、`logical_operator`、`conditions`
+ 每个 condition 必填：`left_var_index`、`right_var_index`、`compare_operator`
+ **每个** `case.id` 须有一条出边，`source_handle=intent_chain|{id}`
+ `level=999` 表示默认分支（else）
+ compare_operator 枚举：`eq`,`ne`,`gt`,`ge`,`lt`,`le`,`contains`,`not_contains`,`empty`,`not_empty`,`is`,`is_not`,`start_with`,`end_with`,`null`,`not_null`
+ `outputs` 为空或不用作下游数据源

### 输入变量（inputs）
由 `cases[].conditions` 自动同步；left/right 对应 inputs 中的变量引用。

### 输出变量（outputs）
无（仅路由）。

### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `cases` | array | **是** | — | 分支列表，每项含 `id`、`level`、`logical_operator`、`conditions` |


**case 结构**：`{ id, level, logical_operator: "and"|"or", conditions: [{ left_var_index, right_var_index, compare_operator }] }`

---

## loop — 循环节点
**功能描述**

子工作流循环：`array`（列表）。在 `blocks` 内执行内部 DSL，按迭代聚合 outputs 为数组。  
循环 Pro 节点需要有开始和结束节点，对应映射循环 Pro 节点的输入变量和输出变量，用于将节点的输入输出变量映射到对应的循环体工作流当中。

### Agent 硬性约束
+ `loop_type` **必填**：`for` | `array` | `while`
+ `array` 模式：外层 input 须为 **array** 类型
+ `while` 模式：须配置 `conditions`、`logical_operator`、`max_iterations`
+ `blocks` 内：**1 个 loop-开始 + 1 个 loop-结束**，满足 §1.1 映射
+ `blocks` 内递归适用全部图级约束（无环、连通）
+ 循环节点自身须有主图入边+出边

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `input` | array/string/… | 推荐 | 无硬性 | `array` 模式为待遍历列表 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `output` | array | 推荐 | 无硬性 | 迭代结果数组 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `loop_type` | string | **是** | `for` | `for` / `array` / `while` |
| `loop_count` | integer | 否 | `3` | `for` 模式循环次数 |
| `blocks` | object | 否 | `{}` | 内部 `{nodes, edges}` |
| `conditions` | array | 否 | `[]` | `while` 模式条件 |
| `logical_operator` | string | 否 | `and` | 多条件逻辑 |
| `max_iterations` | integer | 否 | `100` | 最大迭代防死循环 |


### 搭配建议
+ 列表批处理：`start(array)` → `loop` → `end`
+ 体内典型链：loop-开始 → `llm`/`code` → loop-结束
+ **不可**在普通 `loop` 体内使用 `loop-continue`/`loop-terminate`（仅 loop-pro）
+ 常见错误：blocks 缺 start/end；loop-结束 input 名与外层 output 不一致

---

## loop-pro — 循环(Pro)节点
**功能描述**

支持条件循环和次数循环， `while` 需在 `blocks` 内配合 `loop-continue` / `loop-terminate` 控制。

循环 Pro 节点需要有开始和结束节点，对应映射循环 Pro 节点的输入变量和输出变量，用于将节点的输入输出变量映射到对应的循环体工作流当中。

### Agent 硬性约束
+ `blocks` **必填**
+ 体内须含 **loop-开始 + loop-结束**（同 §1.1）
+ `max_iterations` 范围 **1–50**（默认 10）
+ `while` 模式：须 `loop-continue` 或 `loop-terminate` 或到达 loop-结束
+ 可使用 `loop-continue`/`loop-terminate`（仅 loop-pro）

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `input` | array/string/… | 推荐 | 无硬性 | `array` 模式为待遍历列表 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `output` | array | 推荐 | 无硬性 | 迭代结果数组 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `blocks` | object | **是** | — | 内部工作流 DSL |
| `loop_type` | string | 否 | `for` | `array` / `for` / `while` |
| `loop_count` | integer | 否 | `3` | `for` 次数 |
| `max_iterations` | integer | 否 | `10` | `while` 上限（最大 50） |


### 搭配建议
+ 复杂循环：体内 `if-else` → continue（下一轮）/ terminate（提前退出）
+ 无控制节点到达 loop-结束视为 continue
+ 常见错误：while 无 continue/terminate 导致逻辑不清；max_iterations>50

---

## loop-continue — 继续循环节点
**功能描述**

**仅**在 `loop-pro` 的 `blocks` 内使用。抛出 `LoopContinueSignal`，将 inputs 作为下一轮输入。

### **Agent 硬性约束**
+ **禁止****出现在主画布或普通 **`**loop**`** 体外（build 拒绝）**
+ **仅推荐在 **`**loop-pro**`** 体内使用**
+ **inputs 定义传递变量；****无 outputs**
+ **须有入边；出边可选（信号节点）**

---

## loop-terminate — 终止循环节点
**功能描述**

**仅**在 `loop-pro` 的 `blocks` 内使用。抛出 `LoopTerminateSignal`，将 inputs 作为循环最终结果。

### Agent 硬性约束
+ 同 `loop-continue` 作用域限制
+ inputs 作为提前退出时的循环输出
+ **无 outputs**

---

## parameter-extractor — 参数提取节点
**功能描述**

用 LLM 从用户文本提取结构化参数（Function Call 或对话模式）。

### Agent 硬性约束
+ `node_param.model` **必填**
+ **所有** inputs 值非空
+ outputs：`name` 非空；`schema.description` 非空（`llm_stream_res` 除外）

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `input` | string | 推荐 | 无硬性 | 待提取文本 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| 自定义字段 | 多种 | 是 | **必填** | 提取目标字段 |
| `llm_stream_res` | string | 可选 | 豁免 | 流式原始输出 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `model` | string | **是** | — | 模型 |
| `max_tokens` | integer | 否 | — | 最大 token |
| `temperature` | number | 否 | `0.1` | 温度 |
| `api_base` | string | 否 | `LLM_API_BASE` | API 地址 |
| `api_key` | string | 否 | `""` | API Key |
| `tool_name` | string | 否 | `parameter-extractor` | 工具名 |
| `tool_description` | string | 否 | 参数提取说明 | 工具描述 |
| `prompt_prefix` | string | 否 | 参数提取专家 | 提示前缀 |
| `use_function_call` | boolean | 否 | `false` | 函数调用模式 |
| `enable_think_content` | boolean | 否 | `false` | 含思考内容 |
| `enable_llm_content` | boolean | 否 | `false` | 写入 `llm_stream_res` |
| `stream` | boolean | 否 | — | 流式 |


---

## variable-aggregator — 变量聚合节点
**功能描述**

将多个 input 按 `output_message` 模板合并为单个字符串（类似消息节点）。

**别名**：`variable-assigner`（与 `variable-aggregator` 同一实现）

### Agent 硬性约束
+ `output_message` **必填**
+ 所有 inputs：`name` 非空、值非空

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| 自定义 | 多种 | 是 | 无硬性 | 模板 `{{name}}` 对应 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `text` | string | 是 | 可选 | 聚合结果 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `output_message` | string | 否 | — | 模板，支持 `{{变量名}}` |


---

## variable-update — 变量更新节点
**功能描述**

单次运行的工作流中，在变量池中**原地修改**指定节点的输出值，不产生新的下游变量（侧效应节点）。

### Agent 硬性约束
+ `updates` 每项：`target_node_id`、`target_name`、有效 `schema.value` 必填
+ ref 类型须指向有效节点输出；literal 须类型匹配

### 输入变量（inputs）
无（更新目标在 `node_param.updates` 中声明）。

### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `update_results` | object | 是 | 可选 | 更新结果摘要 |


### 节点参数（node_param
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `updates` | array | 是 | `[]` | 更新项列表 |
| `updates[].target_node_id` | string | 是 | — | 目标节点 ID |
| `updates[].target_name` | string | 是 | — | 目标输出名 |
| `updates[].schema.value` | object | 是 | — | `{type: ref|literal, content}` |


---

## json-serialize — JSON 序列化节点
**功能描述**

将所有 inputs 合并为一个对象后序列化为 JSON 字符串。

### Agent 硬性约束
+ inputs `name` 非空；有 value 的须非空
+ outputs `name` 非空；推荐 `json_text`

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| 自定义 | 多种 | 是 | — | 各 name 作为 JSON key；推荐 `input` |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `json_text` | string | 推荐 | 可选 | JSON 字符串 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `indent` | integer/null | 否 | null | 缩进空格；null 为紧凑单行 |
| `ensure_ascii` | boolean | 否 | — | 非 ASCII 转 `\uXXXX` |


---

## json-deserialize — JSON 反序列化节点
**功能描述**

将 JSON 字符串（或已是 dict/list）解析为值。

### Agent 硬性约束
+ 首个 input 值非空
+ outputs `name` 非空；推荐 `output`

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `input` | string | **是** | — | JSON 字符串 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `output` | object/array | 推荐 | 可选 | 解析结果 |


### 节点参数（node_param）
无。

---

## database-connection — 数据库连接节点
**功能描述**

创建异步 MySQL 连接池，输出单次运行有效的 `engine_handle` 供下游 DB 节点使用。

### Agent 硬性约束
+ 输入 `host`**、**`port`**、**`database`**、**`username`**、**`password` 均为**字面量**且非空
+ `port` 须为 1–65535 整数
+ 当前仅支持 `dialect=mysql`
+ 下游 CRUD **必须** ref 本节点 `engine_handle`

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `host` | string | **是** | — | 主机（字面量） |
| `port` | integer | **是** | — | 端口 1–65535 |
| `database` | string | **是** | — | 库名 |
| `username` | string | **是** | — | 用户名（**非** `user`） |
| `password` | string | **是** | — | 密码（敏感） |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `engine_handle` | string | 是 | 可选 | 连接句柄 token |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `dialect` | string | 否 | `mysql` | 当前仅支持 mysql |
| `charset` | string | 否 | `utf8mb4` | 字符集 |
| `pool_size` | integer | 否 | `5` | 连接池大小 |
| `pool_timeout` | integer | 否 | `30` | 取连接超时（秒） |
| `allow_private_network` | boolean | 否 | `false` | 允许内网地址 |


---

## database-query — 数据库查询节点
**功能描述**

只读 SQL（SELECT/WITH/SHOW/EXPLAIN），支持 SQL 模板与结构化构建器。

### Agent 硬性约束
+ `engine_handle` **必填**，且 **仅允许** ref `database-connection` 节点的 `engine_handle`
+ `sql`：input 或 `node_param.sql` **至少一项**非空
+ 输出须含 `rows`

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `engine_handle` | string | **是** | — | ref connection 输出 |
| `sql` | string | 条件 | — | SQL 或模板 |
| 模板变量 | 多种 | 可选 | — | SQL 中 `{{var}}` 对应 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `rows` | array | **是** | 可选 | 查询结果行 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `sql` | string | 否 | — | SQL 模板，支持 `{{变量}}` |
| `timeout` | integer | 否 | `5` | SQL 执行超时（秒） |
| `max_rows` | integer | 否 | `1000` | 最大行数 |
| `max_cell_bytes` | integer | 否 | `262144` | 单元格最大字节 |
| `omit_columns` | string[] | 否 | `[]` | 排除列名 |
| `query_sql_builder` | object | 否 | — | 结构化 SELECT 构建器 |


---

## database-insert — 数据库新增节点
**功能描述**

执行 INSERT/REPLACE SQL。

### Agent 硬性约束
+ `engine_handle` **必填**，且 **仅允许** ref `database-connection` 节点的 `engine_handle`
+ `sql`：input 或 `node_param.sql` **至少一项**非空
+ 输出须含 `affected_rows`

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `engine_handle` | string | **是** | — | ref connection |
| `sql` | string | 条件 | — | INSERT 模板 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `affected_rows` | integer | **是** | 可选 | 影响行数 |
| `last_insert_id` | integer | 可选 | 可选 | 自增 ID |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `sql` | string | 否 | — | INSERT 模板 |
| `timeout` | integer | 否 | `60` | 超时（秒） |
| `insert_sql_builder` | object | 否 | — | 结构化 INSERT 构建器 |


---

## database-update — 数据库更新节点
**功能描述**

执行 UPDATE SQL（须含有效 SET 与 WHERE）。

### Agent 硬性约束
+ `engine_handle` **必填**，且 **仅允许** ref `database-connection` 节点的 `engine_handle`
+ `sql`：input 或 `node_param.sql` **至少一项**非空
+ 输出须含 `affected_rows`

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `engine_handle` | string | **是** | — | ref connection |
| `sql` | string | 条件 | — | UPDATE 模板 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `affected_rows` | integer | **是** | 可选 | 影响行数 |
| `last_insert_id` | integer | 可选 | 可选 | 自增 ID |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `sql` | string | 否 | — | UPDATE 模板 |
| `timeout` | integer | 否 | `60` | 超时（秒） |
| `update_sql_builder` | object | 否 | — | 结构化 UPDATE 构建器 |


---

## database-delete — 数据库删除节点
**功能描述**

执行 DELETE SQL（**必须**含 WHERE 条件）。

### Agent 硬性约束
+ `engine_handle` **必填**，且 **仅允许** ref `database-connection` 节点的 `engine_handle`
+ `sql`：input 或 `node_param.sql` **至少一项**非空
+ 输出须含 `affected_rows`

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `engine_handle` | string | **是** | — | ref connection |
| `sql` | string | 条件 | — | DELETE 模板 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `affected_rows` | integer | **是** | 可选 | 影响行数 |
| `last_insert_id` | integer | 可选 | 可选 | 自增 ID |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `sql` | string | 否 | — | DELETE 模板 |
| `timeout` | integer | 否 | `60` | 超时（秒） |
| `delete_sql_builder` | object | 否 | — | 结构化 DELETE 构建器 |


---

## message — 消息节点
**功能描述**

按 `output_message` 模板渲染消息，支持流式；可引用 LLM/Agent 的流式输出实时展示。

### Agent 硬性约束
+ `node_param.output_message` **不能为空**
+ 所有 inputs：`name` 非空、值非空
+ 须有入边（可以没用出边）

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| 自定义 | 多种 | 是 | 无硬性 | 模板 `{{name}}` 须与此处 name 对应 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| `text` | string | 是 | 可选 | 渲染后的消息文本 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `output_message` | string | 否 | — | 消息模板 |
| `stream` | boolean | 否 | `true` | 流式输出 |


---

## input — 人工介入节点（原输入节点）
**功能描述**

暂停工作流，向前端发送 `on_event_node_waiting_input`，等待用户通过 `/action` 提交后继续。

### Agent 硬性约束
+ `outputs` 定义用户提交字段，`name` 非空
+ 有值的 `inputs`（上游预填）须 name 与值非空
+ 运行时会阻塞直至 action 或工作流超时

---

## form-input — 表单输入节点
**功能描述**

多字段表单暂停节点，推送完整 schema 与 `instruction_text` 给前端。

### Agent 硬性约束
+ `inputs` 通常为空；`outputs` 各字段 `name` 非空（表单字段定义）
+ 输出 schema 可含 `x-widget`、`x-options` 等扩展
+ 同样触发 `waiting_input`，需 action 唤醒

### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `instruction_text` | string | 否 | `""` | 表单说明文字 |


---

## workflow — 工作流节点
**功能描述**

同进程内嵌执行已发布子工作流（`flow_id`），共享父 session/callback，支持嵌套深度限制。

### Agent 硬性约束
+ `flow_id` **必填**；子工作流须已发布
+ **inputs 与 outputs 不可擅自改名**：由子工作流 start/end 固定
+ inputs 对齐子流 **start.outputs**；outputs 对齐子流 **end.inputs**
+ 嵌套深度 ≤ 8
+ 有 value 的 inputs 须非空

### 输入变量（inputs）
| **name** | **type** | **必填** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| 子流 start 输出名 | 多种 | 按子流 | — | 名称类型由子流定义 |


### 输出变量（outputs）
| **name** | **type** | **默认** | **描述要求** | **说明** |
| --- | --- | --- | --- | --- |
| 子流 end 输入名 | 多种 | 按子流 | — | 名称类型由子流定义 |


### 节点参数（node_param）
| **参数名** | **类型** | **必填** | **默认值** | **说明** |
| --- | --- | --- | --- | --- |
| `flow_id` | string | **是** | — | 子工作流 ID |
| `app_id` | string | 否 | `appid` | 应用 ID |
| `uid` | string | 否 | `uid` | 用户 ID |
| `name` | string | 否 | `""` | 展示名称 |
| `description` | string | 否 | `""` | 描述 |


---