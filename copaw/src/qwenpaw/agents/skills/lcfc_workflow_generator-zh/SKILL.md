---
name: lcfc_workflow_generator
description: 根据用户需求生成联宝（LCFC）定制工作流协议（Workflow Engine JSON），并可选择注册到工作流平台及运行测试。当用户需要设计、创建或生成工作流流程、工作流协议、工作流JSON、workflow编排、流水线设计、节点连线、AI工作流时请务必使用本技能。适用于用户提到"工作流"、"workflow"、"协议"、"流程编排"、"节点"、"边"、"流水线"、"注册工作流"、"发布工作流"、"运行工作流"、"测试工作流"等场景，包括需要生成 start→llm→end 等节点连接的工作流协议。也适用于用户需要参考 LCFC Workflow Engine 协议文档来构建工作流场景。
---

# LCFC Workflow Generator

生成符合联宝（LCFC）Workflow Engine 标准协议的工作流 JSON 文件。

## 核心理念

**先规划，再构建。** 每次生成工作流前必须先做好 Plan，明确预期结果，再动手写 JSON。

**工作流硬性约束：**

> 🚨 **铁律第一条：文件输出路径锁定** — 所有工作流 JSON 文件的**生成、保存、引用**，必须使用 **当前agent工作区**下的 `workflow_result/{当前日期}/`。**严禁保存到任何其他目录**（包括但不限于：临时目录、其他 Skill 目录、`media/`、`references/`、`scripts/`、用户桌面、项目根目录等）。违反此规则即视为**执行流程错误**，须立即将文件移到正确路径后重试，不得将错就错继续流程。

> 🚨 **铁律第二条：禁止无谓生成中间脚本/文件 — 再三思考，能不建就不建**
>
> **🔴 再三思考：你真的需要创建临时文件/验证脚本吗？**
>
> 在动手创建任何临时文件、中间脚本、验证脚本或调试文件之前，**必须再三思考**——反复问自己三个问题：
> 1. **真的需要吗？** 所有验证已由 `scripts/workflow_cli.py verify` 子命令完成，为什么还需要额外文件？
> 2. **有没有替代方案？** 能否在不创建新文件的情况下达成目的？（例如：用 CLI 参数传递、内存中处理、复用已有脚本）
> 3. **没有它会怎样？** 如果答案是不会导致任务失败——**那就不要创建**。
>
> **🔴 能不建就不建。** 所有验证通过 `scripts/workflow_cli.py verify` 子命令完成，不得为此额外生成独立的验证脚本、测试脚本或临时执行文件。不得以"备查""调试方便""以防万一""先留着"等理由创建不必要的临时文件。
>
> **🔴 确需创建时（再三思考后的结论仍是"需要"）**，**必须且只能**放到当前agent工作区下的 `workflow_result/{当前日期}_tmp/` 目录。
>
> **🔴 不需要时，严禁创建。** 如果再三思考后认为不需要临时文件，就**严禁创建**。少一个临时文件就少一分混乱。
>
> **❌ 严禁放到任何其他路径**，包括但不限于：
> - `workflow_result/{当前日期}/`（这是正式产出物目录，不可混入临时文件）
> - `scripts/`、`references/`、`media/`、项目根目录、用户桌面等
>
> **违反后果：**视为**执行流程严重错误**，必须立即将文件移至 `workflow_result/{当前日期}_tmp/` 后重试。

| **规则** | **适用范围** | **违反后果** |
| --- | --- | --- |
| 必须有且仅有 **1 个 **`start`**、1 个 **`end` | 主画布 + 每个 `loop`/`loop-pro` 的 `blocks` | 校验失败 |
| `start` 必须有出边；`end` 必须有入边 | 同上 | 校验失败 |
| 非分支终点节点需有入边+出边 | `start`/`end`/分支节点例外 | 校验失败 |
| **禁止有向环** | 主图与循环体内 | 校验失败 |
| `loop-continue`/`loop-terminate` **仅**在 `loop`/`loop-pro` 的 `blocks` 内 | 全局 | 构建失败 |
| 子工作流嵌套深度 ≤ **8** | `workflow` 节点链 | 运行失败 |
| 主画布 `nodes` ≥ 2，`edges` ≥ 1 | 图 schema | 校验失败 |
| `start`/`end`（主画布）的 `schema.description` **不能为空** | 主画布 | 校验失败 |
| `loop-开始`/`loop-结束` **不需要** description | 循环体内 | 不强制 |

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

**禁止**放入循环体：主画布级 `start`/`end`（须用 loop-开始/loop-结束）、`workflow`、`database-*`、`document-*`、`input`/`form-input`、`plugin`、`agent`

### 节点搭配速查

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

## 工作流程

当用户提出工作流构建需求时，按以下步骤执行：

### Step 1: 分析用户需求
仔细理解用户想要实现的工作流场景，明确：
- 工作流的业务目标是什么
- 需要哪些节点类型（llm、message、code、http、if-else、decision、knowledge-retrieval 等）
- 节点之间的数据流向和连接关系
- 是否需要分支/并行/循环

### Step 2: 制定 Plan
向用户呈现构建方案，包含：
- **预期工作流图**：用文字描述节点连接顺序
- **节点清单**：列出所有节点类型及其职责
- **数据流向说明**：inputs/outputs 的引用关系
- **预期输出结果**：工作流执行后的产出（**必须明确**，后续若需注册/运行验证时将以此为依据）

用户确认 plan 后再进入下一步。

### Step 3: 构建 Workflow 协议
严格按照协议结构构建 JSON。

> ⚙️ **LLM 节点默认模型配置**：当用户**未指定**大模型时，构建 LLM 节点时默认使用以下配置：
> - `"model": "Qwen3.6-35B-A3B"`
> - `"api_base": "https://aaas.lcfuturecenter.com/prod-api/aibase/model"`
> - `api_key` 由平台自动获取，**无需填写**
> 
> 若用户明确指定了模型/地址/密钥，则优先使用用户指定的值。

> 💡 **node_param 入参引用规范（所有节点通用）**：在任何节点的 `node_param` 中，如需引用 inputs 中声明的变量值（如 start 节点输出的 `usr_input`），**必须使用 `{{变量名}}` 格式**，引擎运行时将自动填充对应的变量值。此规则适用于**所有节点类型**的 `node_param`，包括但不限于：
> - **llm 节点**：`template`、`system_prompt`
> - **code 节点**：`code` 脚本内容
> - **http 节点**：`body`、`headers`（值部分）
> - **message 节点**：`output_message`
> - **end 节点**：`template`
> - **parameter-extractor 节点**：`tool_name`、`description`
> - **variable-update 节点**：`updates[].value`
> 
> 示例（数学解题工作流 — llm 节点）：
> ```json
> {
>   "node_param": {
>     "template": "请解决以下数学问题：\n问题：{{usr_input}}\n\n请给出详细的解题步骤和最终答案。",
>     "system_prompt": "你是一位专业的数学解题助手。请按以下格式输出：\n1. 问题分析\n2. 解题思路\n3. 详细步骤\n4. 最终答案"
>   }
> }
> ```
> 
> **注意：** `{{变量名}}` 中的变量名必须与节点 `inputs` 中通过 `ref` 引用的变量名称**完全一致**，否则运行时无法正确填充。

### Step 4: 保存到日期目录（当前agent工作区 — **唯一路径，不得违抗**）

> 🚨 **铁律：** 所有生成的工作流 JSON 文件**必须且只能**保存到 **当前agent工作区**下的 `workflow_result/{当前日期}/`。
> **❌ 严禁保存到任何其他路径**，包括但不限于：`media/`、`references/`、`scripts/`、`skills/` 子目录、用户桌面、项目根目录、临时目录、自定义路径。
>
> **🔴 再三思考：是否需要创建临时文件/验证脚本？**
> - **能不建就不建** — 所有验证已由 `scripts/workflow_cli.py verify` 完成，除非确有必要，否则不要创建任何临时文件
> - **不需要则严禁创建** — 不得以"备查""调试""以防万一"为由创建多余文件
> - **确需创建时**，必须放到 `workflow_result/{当前日期}_tmp/`，**严禁**放入正式产出目录 `workflow_result/{当前日期}/` 或任何其他路径
>
> **违反后果：** 视为**执行流程严重错误**，必须立即停止后续操作，将文件移动到正确路径后重试，**不得将错就错**继续后续步骤（如注册/运行等）。

具体步骤：
1. 获取当前日期（格式 `YYYY-MM-DD`），目标目录为 `workflow_result/{当前日期}/`
2. 每次生成前确保该目录存在（若不存在则创建）
3. 文件名统一为 `{工作流名称}_workflow.json`
4. 将生成的工作流协议 JSON 文件写入 `workflow_result/{当前日期}/{工作流名称}_workflow.json`
5. 写入后**立即验证文件确实存在于 `workflow_result/{当前日期}/` 下**，若不在则立即纠正

> 后续注册/运行/修改操作中引用该文件时，务必使用 `workflow_result/{当前日期}/` 作为 `--data` 参数值，确保路径指向当前agent工作区下的对应文件。**禁止使用绝对路径、相对跳转路径（如 `../`）或其他非 `workflow_result/{当前日期}/` 的路径。**

### Step 5: 询问用户是否注册到平台
Step 4 完成后，**主动询问用户**是否需要将该工作流注册到工作流平台。

- **用户同意注册** → 进入「平台操作」章节
- **用户拒绝或暂不注册** → 交付已完成（DSL 协议已生成保存），告知用户后续如需注册可随时提出

---

## 平台操作（可选）

以下操作为可选步骤，仅在用户明确同意注册到工作流平台后执行，或用户直接提出平台相关操作需求时执行。

> 🚨 **铁律：所有平台操作必须通过 CLI 脚本执行**
>
> 对工作流平台的 **增、删、改、查、运行、验证** 等所有操作，**只能且必须**通过 `scripts/workflow_cli.py` CLI 脚本完成。
>
> **严禁：**
> - ❌ 直接调用 HTTP 接口（`requests.post`、`curl` 等）
> - ❌ 使用其他工具（浏览器、Postman 等）
> - ❌ 在代码中直接拼装 API 请求调用平台
>
> **违反后果：** 视为**执行流程严重错误**，须立即停止并改用 CLI 脚本重试。

### CLI 快速参考

`scripts/workflow_cli.py` 是本 skill 操作工作流平台的核心工具，支持完整的增删改查与运行验证。

**查看所有命令及参数：**
```bash
python scripts/workflow_cli.py --help
```

**常用 CRUD 示例：**

```bash
# 1️⃣ 查询（get）— 按 workflow_uuid 获取工作流详情
python scripts/workflow_cli.py get 112

# 2️⃣ 查询（list）— 列出所有工作流
python scripts/workflow_cli.py list

# 3️⃣ 修改（update）— 按 workflow_uuid 更新工作流名称/DSL/描述等
python scripts/workflow_cli.py update 112 \
    --name "新名称" \
    --data "workflow_result/2026-06-10/新协议.json" \
    --update-by "李四"

# 4️⃣ 删除（delete）— 按 workflow_uuid 删除工作流
python scripts/workflow_cli.py delete 112
```

> 所有命令均支持自动签名登录，无需手动传 `--token`。各子命令的详细参数请通过 `--help` 查看。

### 获取鉴权（获取平台 Token）

所有平台操作均默认自动签名登录，无需手动获取 token。本 skill 所有接口请求均使用统一地址 `https://misqas.lcfuturecenter.com`。

> 脚本内置自动签名登录（对应 Java `RemoteRequestUtils`），**所有命令**（create / update / delete / get / list / run）
> 不传 `--token` 时均会自动调用签名登录，无需额外操作。
> 仅以下场景建议独立使用 `login` 子命令：
> - 调试或确认认证是否正常
> - 需要 token 给其他工具/脚本使用

#### 方式一：CLI 命令获取
```bash
# 获取完整 token 信息（JSON 格式）
python scripts/workflow_cli.py login

# 仅获取裸 token（便于脚本拼接）
python scripts/workflow_cli.py login --raw

# 使用自定义认证地址/密钥（默认 https://misqas.lcfuturecenter.com）
python scripts/workflow_cli.py login \
    --auth-base-url "https://misqas.lcfuturecenter.com" \
    --client-id "872f1e1bbf5181cf43918edd7ad56550" \
    --sign-secret-key "visitclientkey"
```

输出示例（非 `--raw`）：
```json
{
  "token": "Bearer eyJhbGciOiJ...",
  "client_id": "872f1e1bbf5181cf43918edd7ad56550",
  "auth_base_url": "https://misqas.lcfuturecenter.com"
}
```

#### 方式二：其他命令自动获取
所有命令（`create` / `update` / `delete` / `get` / `list` / `run`）不传 `--token` 时，均会自动调用签名登录获取 token 并用于请求。无需手动关心鉴权细节。

### 注册工作流（含自动运行验证）

注册工作流到平台，并自动运行验证结果是否符合预期。

> **未发布 vs 已发布：** 刚注册的工作流默认是**未发布**状态，运行/验证时须用 `--workflow-uuid`；
> 已发布的须用 `--flow-id`。以下操作中，注册后的首次验证默认使用 `workflow_uuid`。

> **鉴权说明：** **所有**平台命令（create / update / delete / get / list / run）均支持自动签名登录，
> `--token`、`--client-id`、`--sign-secret-key` 均为可选参数，不传则自动签名登录完成鉴权。
> 请求地址统一使用 `https://misqas.lcfuturecenter.com`。

#### 5.1 执行注册
1. 获取用户提供的必填参数：`--name`（全局唯一）、`--uid`（创建人用户 ID）
2. 获取可选参数：`--create-by`（创建人姓名）、`--create-dep`（创建部门）、`--flow-mode`（1 非对话/2 对话）、`--base-url`（服务端地址）
3. 使用 `workflow_cli.py` 将 Step 4 保存的 JSON 文件注册到平台（**🚨 `--data` 路径必须且只能指向当前agent工作区下的 `workflow_result/{当前日期}/`，禁止使用任何其他路径**）：
   ```bash
   python scripts/workflow_cli.py create \
       --name "工作流名称" \
       --uid "user_001" \
       --create-by "张三" \
       --create-dep "技术部" \
       --flow-mode 2 \
       --data "workflow_result/2026-06-04/工作流名称_workflow.json"
   ```
4. 注册成功后，记录返回的 `workflow_uuid` 和 `flow_id`，向用户展示结果。
   > ⚠️ **刚注册的版本默认未发布**，后续运行/验证时**必须使用 `workflow_uuid`**，而非 `flow_id`。

#### 5.2 自动运行验证
注册成功后，**必须**运行该工作流进行验证。

> **验证理念：** 成功≠跑通。必须基于业务设计验证方案，对比实际输出与预期结果是否一致，一致才算验证通过。

##### 5.2.1 设计验证方案（由 Agent 自主完成）
根据 Step 2 的 Plan 中明确的预期输出结果，设计验证方案：

1. **确定测试输入**：与 start 节点入参对应，多少个入参就传多少个 `--param`
2. **明确预期输出**：将 Step 2 的预期输出转化为具体的验证项（如关键信息字段、内容格式、特定关键词等）
3. **预期必须可验证**：预期输出中至少包含 1–3 个可被实际输出包含的关键词（如特定名词、数值、结论句等）

##### 5.2.2 执行验证
使用 `verify` 子命令（封装了运行 + 关键词匹配）执行验证：

```bash
python scripts/workflow_cli.py verify \
    --workflow-uuid "{workflow_uuid}" \
    --expected "关键词1,关键词2,关键词3" \
    --param key1="value1" \
    --param key2="value2"
```

> ⚠️ 刚注册的版本**未发布**，必须用 `--workflow-uuid` 而非 `--flow-id`。
> 若工作流已发布，则改用 `--flow-id`。

`verify` 命令会：
- 静默运行工作流，获取完整输出
- 检查所有 `--expected` 关键词是否均出现在输出中
- 输出 JSON 格式结果（含 `success`、`actual_output`、`match_details`）

##### 5.2.3 结果判定（由 Agent 自主判断，不询问用户）
根据 `verify` 返回的 JSON 结果 + Agent 自身 LLM 判断：

- **基础检查**（脚本层）：`verify` 的关键词匹配结果
- **语义判断**（Agent 层）：Agent 用自己的 LLM 能力，将实际输出与预期输出进行语义对比，判断业务逻辑是否正确、结果是否合理

**判定标准**（二者同时满足才算通过）：
1. `verify` 返回 `success: true`（所有关键词均出现）
2. Agent 判断实际输出在语义上符合预期业务目标

- ✅ **两者均满足** → 验证通过，注册完成，交付
- ❌ **任一不满足** → 验证不通过，分析失败原因，进入重试流程

##### 5.2.4 重试流程
验证不通过时：
1. **Agent 分析差异**：对比预期 vs 实际输出，列出具体差异点
2. **定位根因推测**：分析可能的原因（DSL 结构、节点参数、数据流向、LLM 输出不稳定等）
3. **回到 Step 3**：修改工作流协议，修复问题后重新从 5.1 注册+验证

**循环上限：** 从首次注册开始，**「注册 → 验证 → 不符 → 修改协议 → 重新注册」的完整循环最多执行 8 次**。
- 每次 5.2.3 判定不通过时递增循环计数器，Agent 须在上下文中维护该计数
- 第 8 次运行验证完成后若仍不符合预期 → **立即停止循环**

##### 5.2.5 第 8 次失败后的处理（人工介入）
停止循环后，必须向用户输出完整报告：

1. **失败原因分析**（包含但不限于）：
   - 预期输出 vs 实际输出的逐项对比
   - 推测的可能问题方向：DSL 结构错误、节点参数配置不当、数据流向不正确、LLM 输出不稳定等
   - 已尝试的修改方案及其效果

2. **后续计划**（Agent 的下步打算）：
   - 建议人工检查的具体方向
   - 是否需要调整业务预期
   - 是否需更换工作流模式或节点方案
   - 人工介入后如需继续帮助，随时可以重新启动流程

#### 5.3 降级处理
若自动签名登录失败（如认证服务不可达、密钥不匹配等），则：
1. 完成注册后明确告知用户：**自动运行验证失败（原因见报错），请手动登录后台测试该工作流**
2. 记录此为风险点，由用户后续自行验证

> **地址说明：** 所有平台请求默认使用 `https://misqas.lcfuturecenter.com`，也可通过环境变量 `WORKFLOW_BASE_URL`/`WORKFLOW_RUN_URL`/`WORKFLOW_AUTH_BASE_URL` 覆盖。

### 修改工作流（含自动运行验证）

当用户需要对平台上已有的工作流进行修改时执行此步骤。

#### 6.1 确认当前状态
1. 查询当前工作流信息：
   ```bash
   python scripts/workflow_cli.py get <workflow_uuid>
   ```
2. 与用户确认要修改的内容（名称、DSL、描述等）

#### 6.2 执行修改
1. 获取用户提供的修改参数
2. 若修改 DSL（data），先按 Step 3 构建新的协议 JSON 并保存到 `workflow_result/{当前日期}/`
3. 执行修改（**🚨 `--data` 路径必须且只能指向当前agent工作区下的 `workflow_result/{当前日期}/`，禁止使用任何其他路径**）：
   ```bash
   python scripts/workflow_cli.py update <workflow_uuid> \
       --name "新名称" \
       --data "workflow_result/2026-06-04/新协议.json" \
       --update-by "李四"
   ```

#### 6.3 自动运行验证
修改成功后，**必须**运行该工作流验证修改结果是否正确。

> **验证理念：** 成功≠跑通。必须基于业务设计验证方案，对比实际输出与预期结果是否一致，一致才算验证通过。

##### 6.3.1 设计验证方案（由 Agent 自主完成）
根据 Step 2 的 Plan 中的预期输出结果（或双方重新确认的预期），设计验证方案：

1. **确定测试输入**：与 start 节点入参对应
2. **明确预期输出**：转化为具体的验证项和关键词
3. **预期必须可验证**：至少包含 1–3 个可被实际输出包含的关键词

##### 6.3.2 执行验证
使用 `verify` 子命令执行验证：

```bash
# 未发布的工作流用 --workflow-uuid
python scripts/workflow_cli.py verify \
    --workflow-uuid "{workflow_uuid}" \
    --expected "关键词1,关键词2" \
    --param key1="value1"
# 已发布的工作流用 --flow-id
# python scripts/workflow_cli.py verify \
#     --flow-id "{flow_id}" \
#     --expected "关键词1,关键词2" \
#     --param key1="value1"
```

##### 6.3.3 结果判定（由 Agent 自主判断）
- **基础检查**：`verify` 返回 `success: true`
- **语义判断**：Agent 用 LLM 对比实际输出与预期，判断业务逻辑正确性

- ✅ **两者均满足** → 修改完成，交付
- ❌ **任一不满足** → 分析差异，进入重试

##### 6.3.4 重试流程
验证不通过时：
1. **Agent 分析差异并定位根因**
2. **回到 6.2 重新修改**（仅调参），或回到 Step 3 调整协议后经 6.2 修改重试

**循环上限：** 从首次修改开始，**「修改 → 验证 → 不符 → 重新修改/调整协议 → 再次修改」的完整循环最多执行 8 次**。
- 每次 6.3.3 判定不通过时递增循环计数器，Agent 须在上下文中维护该计数
- 第 8 次仍不符合预期 → **立即停止循环**

##### 6.3.5 第 8 次失败后的处理（人工介入）
停止循环后，向用户输出完整报告：

1. **失败原因分析**（包含但不限于）：
   - 预期 vs 实际输出的逐项对比
   - 推测的可能问题方向（DSL 结构、节点参数、数据流向、LLM 输出不稳定等）
   - 已尝试的修改方案及其效果

2. **后续计划**（Agent 的下步打算）：
   - 建议人工检查的具体方向
   - 是否需要调整业务预期
   - 人工介入后如需继续帮助，随时可以重新启动流程

#### 6.4 降级处理
若自动签名登录失败，同上 — 告知用户手动验证。

### 工作流独立运行
若用户**仅需运行已有工作流进行测试**（不涉及注册/修改），直接使用 `run` 子命令。

**方式一：已知 flow_id（已发布）或 workflow_uuid（未发布）直接指定**
```bash
# 已发布工作流 — 用 flow_id
python scripts/workflow_cli.py run \
    --flow-id "2061650160211070976" \
    --param usr_input="你在吃什么"

# 未发布工作流（刚注册）— 用 workflow_uuid
python scripts/workflow_cli.py run \
    --workflow-uuid "110" \
    --param usr_input="你在吃什么"
```

**方式二：交互选择（推荐，什么都不用记）**
不传 `--flow-id`，脚本自动查询当前工作区所有工作流（已发布 & 未发布），列出名称、描述、flow_id 和 workflow_uuid，让用户选择：
```bash
python scripts/workflow_cli.py run --param usr_input="你好"
```
→ 自动查询并展示列表：
```
📋 当前工作区共 5 个工作流：

  1. 客服流程
     描述: 用户咨询自动应答工作流
     flow_id: 2061650160211070976

  2. 知识库问答
     描述: 基于知识库的智能问答
     flow_id: 2061650160211070978

请选择工作流（输入 1-5 或直接输入 flow_id）:
```
输入数字或 flow_id 即可选中并自动运行。

## 协议结构规范

### 顶层结构
```json
{
  "id": "唯一ID（数字字符串）",
  "name": "工作流名称",
  "description": "工作流描述",
  "version": "v1.0.0",
  "data": {
    "edges": [...],
    "nodes": [...]
  }
}
```

### 节点（Node）
节点ID格式：`节点类型::uuid32位hex`（如 `llm::db379de3dc374bf6a47f103a1375a64f`）

使用脚本 `scripts/generate_uuid.py` 快速生成合法节点ID：
```bash
python scripts/generate_uuid.py llm      # 生成1个 llm 节点ID
python scripts/generate_uuid.py start 2  # 生成2个 start 节点ID
python scripts/generate_uuid.py --uuid   # 生成纯UUID
```

所有对工作流平台的增删改查与运行操作，**只能且必须**通过 CLI 脚本 `scripts/workflow_cli.py` 执行（详见 Step 5）。**严禁通过任何其他方式**（包括直接 HTTP 调用、浏览器操作、其他脚本等）操作平台接口。

#### 节点结构
```json
{
  "id": "type::uuidhex",
  "data": {
    "node_meta": {
      "node_type": "基础节点|分支节点|工具节点",
      "alias_name": "用户自定义名称"
    },
    "inputs": [
      {
        "name": "变量名",
        "schema": {
          "type": "string|integer|number|array|object|boolean",
          "value": {
            "type": "ref|literal",
            "content": {/* ref时: {"node_id": "xxx", "name": "xxx"} */} // literal时: "值"
          }
        }
      }
    ],
    "outputs": [
      {
        "name": "变量名",
        "schema": {"type": "string|..."},
        "required": true|false
      }
    ],
    "node_param": {
      // 各节点类型的特有参数，详见 references/nodes.md
    }
  }
}
```

### 边（Edge）
```json
{
  "source_node_id": "源节点ID",
  "target_node_id": "目标节点ID",
  "source_handle": "分支节点专用，格式 intent_chain|branch_one_of::uuid"
}
```

branch/if-else 节点的边**必须**携带 `source_handle`，值为 intent_chains[i].id 或 cases[i].id。

### 引用规范（inputs 中的 value）
- **ref 引用**：引用其他节点的输出值
  ```json
  { "type": "ref", "content": { "node_id": "llm::xxx", "name": "text" } }
  ```
- **literal 透传**：直接传值
  ```json
  { "type": "literal", "content": "hello" }
  ```

### node_param 入参模板引用规范（{{变量名}}）
在 `node_param` 的字符串字段（如 `template`、`system_prompt`、`code`、`body`、`output_message` 等）中，如需引用当前节点 `inputs` 中已声明（通过 `ref` 引用上游）的变量值，**必须使用 `{{变量名}}` 模板语法**。

```json
{
  "inputs": [
    { "name": "usr_input", "schema": { "type": "string", "value": { "type": "ref", "content": { "node_id": "start::xxx", "name": "usr_input" } } } }
  ],
  "node_param": {
    "template": "用户的问题是：{{usr_input}}，请回答。"
  }
}
```

> 引擎运行时会自动将 `{{变量名}}` 替换为运行时实际值。变量名必须与 `inputs` 中声明的 `name` **完全一致**（区分大小写），否则将无法正确填充。

## 节点类型速查

> 详细参数配置请查阅 `references/nodes.md`，协议示例请查阅 `references/protocol.md`

| 节点类型 | node_type | 用途 | 关键参数 |
|---------|-----------|------|---------|
| **start** | 基础节点 | 工作流入口，无 inputs | `enable_file_upload` |
| **end** | 基础节点 | 工作流出口 | `output_mode`(0/1), `template` |
| **llm** | 基础节点 | 大模型调用 | `model`(未指定时默认`Qwen3.6-35B-A3B`), `template`, `api_base`(未指定时默认`https://aaas.lcfuturecenter.com/prod-api/aibase/model`), `temperature`, `max_tokens`, `enable_chat_history`, `enable_vision`, `enable_react`, `system_prompt`, `output_type` |
| **message** | 基础节点 | 消息渲染 | `output_message`, `stream` |
| **code** | 工具节点 | 代码执行 | `code`, `language` |
| **http** | — | HTTP请求 | `method`, `headers`, `body`, `timeout`, `retry_times` |
| **sse** | — | SSE流消费 | `method`, `timeout`, `parse_json` |
| **knowledge-retrieval** | — | 知识库检索 | `strategy`, `top_k`, `min_score`, `knowledge_list` |
| **decision** | 基础节点 | 意图识别/分类 | `model`, `prompt_prefix`, `intent_chains[]` |
| **if-else** | 分支节点 | 条件分支 | `cases[]`（level, logical_operator, conditions） |
| **image-understand** | — | 图片理解 | `model`, `template`, `output_type` |
| **document-extractor** | — | 文档解析 | `max_chars`, `include_metadata` |
| **document-converter** | — | 文档格式转换 | `source_format`, `target_format` |
| **parameter-extractor** | — | 参数提取 | `model`, `tool_name`, `use_function_call` |
| **variable-aggregator** | — | 变量聚合 | `output_message` |
| **variable-update** | — | 变量更新 | `updates[]` |
| **json-serialize** | — | JSON序列化 | `indent`, `ensure_ascii` |
| **json-deserialize** | — | JSON反序列化 | 无参数 |
| **loop** | — | 循环节点（体内不可含 continue/terminate） | `loop_type`, `loop_count`, `blocks` |
| **loop-pro** | — | 循环Pro（体内可含 continue/terminate） | `loop_type`, `blocks`, `max_iterations`（1–50） |
| **loop-continue** | — | 继续循环（仅 loop-pro 体内使用） | 无 outputs；inputs 作为下一轮输入 |
| **loop-terminate** | — | 终止循环（仅 loop-pro 体内使用） | 无 outputs；inputs 作为提前退出结果 |
| **database-connection** | — | 数据库连接 | `dialect`, `pool_size` |
| **database-query** | — | 数据库查询 | `sql`, `timeout`, `max_rows` |
| **database-insert** | — | 数据新增 | `sql`, `timeout` |
| **database-update** | — | 数据更新 | `sql`, `timeout` |
| **database-delete** | — | 数据删除 | `sql`, `timeout` |
| **input** | — | 人工介入 | 暂停等待用户输入 |
| **form-input** | — | 表单输入 | `instruction_text` |
| **workflow** | — | 子工作流 | `flow_id`, `app_id` |

## 典型工作流模式

### 1. 简单线性流：start → llm → end
最基本的"输入→处理→输出"模式。

### 2. 并行处理流：start → llm1,llm2 → end
开始节点后并行调用多个大模型，结果汇聚到结束节点。

### 3. 分支决策流：start → decision/if-else → llm → end
根据条件分流到不同处理路径。

### 4. 复合业务流：start → code/message → llm → if-else → llm1,llm2 → end
组合多种节点类型实现复杂业务逻辑。

### 5. 知识库增强流：start → knowledge-retrieval → llm → end
先检索知识库再交给大模型生成答案。

## 注意事项

1. **每个 workflow 必须且只能有一个 start 和一个 end 节点**
2. start 节点无 inputs，outputs 为入口变量；**主画布 start/end 的 schema.description 不能为空**
3. end 节点无 outputs，inputs 接收所有要输出的结果
4. **循环体内**的 loop-开始/loop-结束**不需要** description
5. 节点 ID 必须全局唯一，格式 `type::32位hex`
6. decision/if-else 的分支边必须带 `source_handle`，值为 `intent_chain|{id}` 或 `branch_one_of|{id}`
7. inputs 中 value.type 为 `ref` 时需注明引用的 `node_id` 和 `name`
8. 敏感信息（api_key、password等）用占位符表示
9. 并行处理时，多个节点可以连接到同一个下游节点
10. **禁止有向环** — 主图与循环体内均不可出现循环依赖
11. `loop-continue`/`loop-terminate` **仅**可在 `loop-pro` 的 `blocks` 内使用
12. 循环体内**禁止**放入：主画布级 start/end、workflow、database-*、document-*、input/form-input
13. **数据库节点**：`engine_handle` **必须** ref 引用 `database-connection` 的输出，禁止字面量伪造
14. **子工作流**：`workflow` 节点的 inputs/outputs 由子流 start/end 定义，**不可擅自改名**
15. `loop-pro` 的 `max_iterations` 取值范围 **1–50**（默认 10）
16. 注册/修改后的自动运行验证，**从首次注册/修改起完整循环最多执行 8 次**，第 8 次仍不符合预期则停止并通知用户人工介入，同时输出失败原因分析与后续计划
17. **临时文件/验证脚本铁律：再三思考 → 不需要则不创建 → 确需创建则放入 `workflow_result/{当前日期}_tmp/` → 严禁放入任何其他目录。** 违反视为执行流程严重错误，必须立即纠正后重试。

## 参考资料
- **协议结构详细说明**：`references/protocol.md`
- **全节点参数配置**：`references/nodes.md`
- **UUID生成脚本**：`scripts/generate_uuid.py`
- **工作流平台注册 CLI**：`scripts/workflow_cli.py`（create / update / delete / get / list / run / verify）
