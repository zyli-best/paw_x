---
name: agent_create_delete
description: 通过 QwenPaw CLI 创建和删除智能体（agent），并自动为新 agent 生成「灵魂三件套」AGENTS.md（工作流程）、SOUL.md（性格灵魂）、PROFILE.md（身份名片）。
---

通过 CLI 管理 agent，创建后自动注入人格文件。

## 前置条件

- `devcopaw` 命令在 PATH 中可用（`devcopaw --help` 验证）
- **创建 agent** → 不需要 `devcopaw app` 在运行
- **删除 agent** → 需要 `devcopaw app` 运行，但是不需要检查`devcopaw app`是否运行的，`devcopaw app`一定处于运行状态，不要怀疑

---

## 1. 创建 Agent

```bash
devcopaw agent create --name "名称" --agent-id 唯一ID
```

### 参数

| 参数 | 必填 | 说明 |
|------|:----:|------|
| `--name` | ✅ | 显示名称 |
| `--agent-id` | ✅ | 唯一标识符 |
| `--description` | ✅ | 描述（协作时其他 agent 读取此字段判断何时调用） |
| `--workspace-dir` | ❌ | **不填**，系统根据 `--agent-id` 自动生成路径 `WORKING_DIR/workspaces/<agent-id>` |
| `--language` | ✅ | 语言，如 `zh` |
| `--template` | ✅ | 模板：`default` / `local` / `qa` |
| `--skill` | ✅ | 初始技能，可重复使用多次。**只需传入 skill name**，本技能会自动拼接为 `WORKING_DIR/skills/<skill-name>/` 的绝对路径，如 `--skill skill-creator` |
| `--provider-id` | ✅ | 模型提供商 ID；用户未明确指出时，**必须先读取 default agent 的 `agent.json`** 获取后传入 |
| `--model-id` | ✅ | 模型 ID；用户未明确指出时，**必须先读取 default agent 的 `agent.json`** 获取后传入 |

### ⚡ 五大规则

> **① 工作区自动创建** — 无需手动建目录。`devcopaw agent create` 会自动在 `WORKING_DIR/workspaces/<agent-id>` 生成工作区。`--workspace-dir` 不需要传。

> **② 模型继承（必做前置步骤）** — `--provider-id` 和 `--model-id` 是必填参数。用户未明确指出时，**必须在执行 CLI create 之前**先读取 default agent 的 `agent.json`，提取 `provider`（即 provider-id）和 `active_model`（即 model-id）作为值传入。不可省略这两个参数。

> **③ 技能必装策略：**
> - 🟢 **`skill-creator`** — 必装。所有 agent 都带上 `--skill skill-creator`
> - 🟡 **`agent_create_delete`** — 复杂任务 agent 加装（涉及多步骤/多工具/需要管理子 agent）
> - 其余技能按任务定
>
> 📍 **路径自动拼接：** `--skill` 只需传 **skill name**（如 `skill-creator`），执行 CLI 时会自动拼接为 `WORKING_DIR/skills/<skill-name>/` 的绝对路径，无需手动写完整路径。

> **④ 技能同步** — `devcopaw agent create` 执行后，**必须**检查新建 agent 工作目录下的 skills 是否完整，缺失的从 default agent 拷贝，并同步生成 `skill.json`（详见第 **5 节**）。

> **⑤ CLI 报错处理（重要！）：** 执行 `devcopaw agent create` 时，若出现类似 `Skill 'xxx' not found in skill pool` 的错误，**说明该 skill 不在新 agent 的初始 skill pool 中，但在 default agent 中通常存在**。这不是真正的"技能不存在"，而是 skill pool 分配机制导致。**不要因此中断流程**，继续执行后续步骤。第 **5 节**（技能同步）会从 default agent 将缺失技能拷贝到新 agent。若 default agent 中也不存在该 skill，则在同步时按缺失处理（告知用户并跳过）。

### --template 选型

| 维度 | `default` 🏠 | `local` 🖥️ | `qa` 📖 |
|------|:---:|:---:|:---:|
| 适用场景 | 通用全能 | 本地小模型协作 | QwenPaw 官方问答 |
| 工具范围 | 🔓 全部开启 | 🛠️ 7 个（文件 + agent 协作） | 🛠️ 5 个（文件 + shell） |
| 自带技能 | `skill-creator` | `skill-creator` + `make_plan` | `skill-creator` + `guidance` + `QA_source_index` |
| 推荐模型 | 任意 | 本地小模型（如 7B） | 任意 |

> 创建时仍须额外加上 `--skill skill-creator`（必装）。复杂任务加 `--skill agent_create_delete`。

### 示例

**① 最小创建（用户未指定模型，从 default 继承）：**
```bash
# 读取 default agent 的 agent.json，提取 provider 和 active_model
# （假设读到 provider=deepseek, active_model=deepseek-v4-flash）

# 执行创建，所有参数必填，包括 provider-id 和 model-id
# 工作区由系统自动创建，无需手动建目录
devcopaw agent create \
  --name "代码助手" --agent-id code_helper \
  --description "通用代码编写与调试助手" --language zh \
  --template default \
  --provider-id deepseek --model-id deepseek-v4-flash \
  --skill skill-creator
del /f /q "WORKING_DIR/workspaces/code_helper/BOOTSTRAP.md"
# 后续还需同步技能与 skill.json（参照第 5 节）
```

**② 完整创建（复杂任务 + 指定模板 + 多技能，用户已明确指定模型）：**
```bash
# 工作区由系统自动创建，无需手动建目录
devcopaw agent create \
  --name "项目总管" --agent-id project_manager \
  --description "管理多个子 agent 和技能" --language zh \
  --template default \
  --provider-id deepseek --model-id deepseek-v4-flash \
  --skill skill-creator \
  --skill agent_create_delete \
  --skill multi_agent_collaboration
del /f /q "WORKING_DIR/workspaces/project_manager/BOOTSTRAP.md"
# 后续还需同步技能与 skill.json（参照第 5 节）
```

---

## 2. 删除 Agent

```bash
devcopaw agent delete <agent_id> --base-url http://127.0.0.1:<端口>
```

> **默认行为（务必遵守）：** 用户要求删除 agent 时，**不要询问**是否一并删除工作区目录。CLI **默认会删除**该 agent 的本地工作区（等同旧版的 `--remove-workspace`），并**默认跳过**交互确认。只有用户**明确说**要保留磁盘上的工作区文件时，才追加 `--keep-workspace`；只有用户**明确要**先看警告再确认时，才追加 `--interactive`。

### 参数

| 参数 | 必填 | 说明 |
|------|:----:|------|
| `agent_id` | ✅ | 要删除的 agent ID（位置参数） |
| `--remove-workspace` / `--keep-workspace` | ❌ | **默认删除**工作区目录；仅当用户明确要求保留本地文件时加 `--keep-workspace` |
| `--yes` / `--interactive` | ❌ | **默认** `--yes`（不提示）；仅当用户明确要求交互确认时加 `--interactive` |
| `--base-url` | ✅ | **必填**，API 地址，端口因人而异需动态获取 |

> ⚠️ `--base-url` 是必填参数。每个人启动 devcopaw app 的端口不同，不能硬编码。

### 2.1 获取端口号 ⚡

**方式一：根据进程名查找端口（Windows / 推荐）**

思路与 Linux 一致：先按名字找进程（类似 `ps -ef | grep qwenpaw`），再按 PID 找监听端口（类似 `netstat -tlnp` 里对应该 PID 的一行，或 `ss -lntp | grep <pid>`）。

**分步（好理解，对应 ps + netstat）**

1. 查进程名得到 PID（进程名固定为 `qwenpaw` 时）：

```powershell
Get-Process -Name qwenpaw -ErrorAction SilentlyContinue | Select-Object Id, ProcessName
```

在 **cmd** 里可写成（接近 `ps -ef | grep qwenpaw`）：

```bat
tasklist | findstr /i qwenpaw
```

记下该行中的 **PID**（`tasklist` 表头里为「PID」列，一般在映像名右侧那一列数字）。

2. 按 PID 查监听端口（接近 `netstat -anlop | grep <PID>`；Windows 用 `-ano`）：

```bat
netstat -ano | findstr LISTENING | findstr "<PID>"
```

把 `<PID>` 换成上一步的进程号；同一行里最后一列是 PID，本地地址里的 `:端口` 即为监听端口。

**一键脚本（PowerShell，等价于上面两步串起来）**

在 **PowerShell** 窗口中整块粘贴执行（不必再包一层 `powershell -Command`）：

```powershell
$p = Get-Process -Name qwenpaw -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $p) { Write-Host 'QwenPaw process not found'; exit 1 }
$ports = Get-NetTCPConnection -OwningProcess $p.Id -State Listen -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty LocalPort -Unique
if ($ports) { Write-Host "[FOUND] QwenPaw app port: $($ports -join ', ')" } else { Write-Host 'No LISTENING port found for this PID' }
```

若可执行文件名不是 `qwenpaw.exe`（例如用 `python` 拉起、任务管理器里显示为 `python`），则不能用进程名过滤，需按命令行匹配（类似在 `ps -ef` 全文里 grep）：

```powershell
$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'qwenpaw' } | Select-Object -First 1
if ($null -eq $p) { Write-Host 'QwenPaw process not found'; exit 1 }
$ports = Get-NetTCPConnection -OwningProcess $p.ProcessId -State Listen -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty LocalPort -Unique
if ($ports) { Write-Host "[FOUND] QwenPaw app port: $($ports -join ', ')" } else { Write-Host 'No LISTENING port found for this PID' }
```

**方式二：动态扫描端口范围 8080~8100（兜底）**

Windows（cmd）：
```bash
for /L %p in (8080,1,8100) do @(curl -s http://127.0.0.1:%p/docs >nul 2>nul && curl -s http://127.0.0.1:%p/docs 2>nul | findstr "QwenPaw" >nul && echo [FOUND] QwenPaw app port: %p)
```

Linux / macOS：
```bash
for port in $(seq 8080 8100); do
  if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$port/docs 2>/dev/null | grep -q "200"; then
    if curl -s http://127.0.0.1:$port/docs 2>/dev/null | grep -q "QwenPaw\|FastAPI"; then
      echo "[FOUND] QwenPaw app port: $port"
    fi
  fi
done
```

> 💡 优先用方式一（进程名查找），找不到时兜底扫描。

### 2.2 带端口执行删除

获取到端口后，始终带上 `--base-url`：

```bash
devcopaw agent list --base-url http://127.0.0.1:<端口>           # 先确认 ID
devcopaw agent delete <agent_id> --base-url http://127.0.0.1:<端口>  # 删除
```

### 示例

```bash
# 假设已查到端口为 8088（见 2.1）
devcopaw agent delete chinese_teacher --base-url http://127.0.0.1:8088
```

（可选）用户明确要求**保留**工作区目录时：

```bash
devcopaw agent delete chinese_teacher --keep-workspace --base-url http://127.0.0.1:8088
```

### 注意

- `default` agent 不能被删除
- 删除前先用 `devcopaw agent list` 确认 ID

---

## 3. 查看所有 Agent

```bash
devcopaw agent list
```

---

## 4. 为新 Agent 注入人格（AGENTS.md + SOUL.md + PROFILE.md）

创建 agent 后，**必须**立即为其生成三份人格文件，写入工作目录。

### 文件

| 文件 | 作用 | 必填 |
|------|------|:----:|
| **AGENTS.md** | 🧠 操作手册 — 核心职责 + skill-creator 调用逻辑 | ✅ |
| **SOUL.md** | 💖 性格灵魂 — 风格、价值观、沟通方式 | ❌（参考 default 定制） |
| **PROFILE.md** | 🪪 身份名片 — 身份 + 服务对象信息 | ❌（参考 default 定制） |

### 信息收集顺序

```
① AGENTS.md → 用户已提供则直接用，否则提问获取
② SOUL.md   → 用户已提供则直接用，否则读 default 的 SOUL.md 参考定制
③ PROFILE.md → 用户已提供则直接用，否则读 default 的 PROFILE.md 参考定制
```

> 💡 `--provider-id` 和 `--model-id` 已在第 **1 节**（CLI 创建前）从 default 的 `agent.json` 获取并传入，此处不再重复读取。

> 💡 **省流模式：** 用户说"简单弄一下"或"默认就好" → SOUL.md / PROFILE.md 直接参考 default 生成简化版。

### 文件路径

```
# default agent 参考文件（WORKING_DIR 即 default agent 工作目录）
WORKING_DIR/AGENTS.md
WORKING_DIR/SOUL.md
WORKING_DIR/PROFILE.md
WORKING_DIR/agent.json                      ← 提取 active_model
WORKING_DIR/skills/<skill-name>/SKILL.md    ← 查阅已有技能（--skill 从这里加载）
```

### 模板

**AGENTS.md：**
```markdown
---
summary: "<名称> 的操作手册"
---

## 🎯 核心任务

<核心职责>

## 📋 工作流程

<操作步骤>

## 🧩 构建 Skill

**何时创建：** 完成复杂任务、调用 2+ 个工具、修复棘手错误、发现可复用流程、用户要求时。

**创建步骤：** 查阅 `WORKING_DIR/skills/skill-creator/SKILL.md` → 保存到 `skills/` → 编写 SKILL.md → 注册到 `skill.json` → 验证。

> 先创建 Skill，再重复劳动。

## ⚙️ 安全规则

- 绝不泄露私密信息
- 拿不准的先问用户
```

**SOUL.md：**
```markdown
---
summary: "<名称> 的灵魂"
---

## 🧭 核心价值观

<行为准则>

## 🎭 风格与个性

<说话风格、激励方式>

## 🚫 边界

- 私密的保持私密
- <其他禁忌>
```

**PROFILE.md：**
```markdown
---
summary: "<名称> 的身份"
---

## 🤖 Agent 身份

- **名字：** <名称>
- **定位：** <核心定位>
- **风格：** <性格风格>

## 👤 服务对象资料

- **称呼：** <称呼>
- **身份：** <描述>
```

### 写入步骤

1. 确认工作目录存在
2. 删除 `BOOTSTRAP.md`（自动生成，必须删）
3. 用 `write_file` 依次写入三个文件
4. **同步技能与 `skill.json`**（参照第 **5 节** 操作）
5. 通知用户完成

---

## 5. 同步技能与 skill.json

> `devcopaw agent create` 的 `--skill` 参数虽能将技能注册到新 agent，但**新 agent 的 `skills/` 目录中不一定有实际的技能文件**。本节确保技能文件实体和 `skill.json` 配置都完整到位。
>
> ⚠️ **即使上一步 CLI 报错 `Skill 'xxx' not found in skill pool`，本节流程仍必须完整执行。** CLI 报错只是因为该 skill 不在新 agent 的初始 skill pool 中，但并不影响它在 default agent 中存在。本节会从 default agent 将缺失技能拷贝过来，CLI 报错的那个技能也会被正常补齐。只有 default agent 中也找不到该 skill 时，才按原流程处理（告知用户并跳过）。

### 5.1 检查技能目录

```
新 agent 工作目录结构预期：
<workspace-dir>/
├── skills/               ← 必须有，存放技能文件
│   ├── skill-creator/    ← 每个注册的技能对应一个子目录
│   │   └── SKILL.md
│   └── ...
├── skill.json            ← 必须有，技能登记清单
├── AGENTS.md
├── SOUL.md
├── PROFILE.md
└── agent.json
```

**检查命令示例（Windows）：**
```bash
if not exist "<workspace-dir>\skills" echo MISSING
dir "<workspace-dir>\skills" /b
```

**检查命令示例（Linux/macOS）：**
```bash
ls -la "<workspace-dir>/skills/" 2>/dev/null || echo "MISSING"
```

### 5.2 缺失技能补齐策略

> 无论上一步 CLI 是否报错（`Skill 'xxx' not found in skill pool`），此处的补齐逻辑一致：**逐个 skill 检查 → 缺失则从 default agent 拷贝**。CLI 报错的 skill 在此也会被正常补齐。

对于 `--skill` 传入的每一个技能名：

1. 检查 `<workspace-dir>/skills/<skill-name>/` 目录是否存在
2. 缺失则从 default agent 拷贝：
   ```bash
   # Windows
   xcopy /E /I "WORKING_DIR\skills\<skill-name>" "<workspace-dir>\skills\<skill-name>"
   
   # Linux/macOS
   cp -r "WORKING_DIR/skills/<skill-name>" "<workspace-dir>/skills/<skill-name>"
   ```
3. 若 default agent 中也不存在该 skill，则记录并告知用户该 skill 不可用，跳过继续处理下一个

> 💡 若整个 `skills/` 目录都不存在，先创建目录再拷贝。

### 5.3 生成 skill.json

`skill.json` 是 QwenPaw 的技能登记清单，必须按以下流程生成：

**步骤：**

1. **读取 default 的 `skill.json`** → `read_file WORKING_DIR/skill.json`
2. **按 skill name 提取匹配条目** — 只保留 `--skill` 传入的那些技能对应的 entry
3. **调整 `version` 字段** — 将 `skill.json` 中的顶级 `version` 值更新为当前时间戳（秒级，如 `Math.floor(Date.now() / 1000)`）
4. **写入新 agent** → `write_file <workspace-dir>/skill.json`，保持 JSON 格式

**skill.json 结构参考（来自 default）：**
```json
{
  "schema_version": "workspace-skill-manifest.v1",
  "version": <当前秒级时间戳>,
  "skills": {
    "<skill-name>": {
      "enabled": true,
      "channels": ["all"],
      "source": "builtin",
      "metadata": {
        "name": "<skill-name>",
        "description": "...",
        "version_text": "...",
        "commit_text": "",
        "source": "builtin",
        "protected": false,
        "requirements": {
          "require_bins": [],
          "require_envs": []
        },
        "updated_at": "<ISO 时间戳>"
      },
      "requirements": {
        "require_bins": [],
        "require_envs": []
      },
      "updated_at": "<ISO 时间戳>",
      "config": {}
    }
  }
}
```

### 5.4 写入步骤（实操流程）

```bash
# ① 创建 skills 目录（如不存在）
mkdir -p "<workspace-dir>/skills"

# ② 遍历每个 --skill 传入的技能名，拷贝缺失的
#    e.g. skill-creator
#    if not exist "<workspace-dir>\skills\skill-creator" →
#    xcopy /E /I "WORKING_DIR\skills\skill-creator" "<workspace-dir>\skills\skill-creator"

# ③ 读 default 的 skill.json → 提取匹配条目 → 更新时间戳 → 写入新 agent
#    使用 read_file / write_file 工具操作
```

### 5.5 完整示例（含技能同步）

> ⚠️ 假设上一步 CLI 执行时遇到了 `Skill 'xxx' not found in skill pool` 错误，以下同步流程照常执行，CLI 报错的技能会在此被补齐。

接续第 4 节的例子——创建 `math_coach` agent 后：

```bash
# ① 检查 skills 目录
# 若 math_coach/workspaces/math_coach/skills/ 不存在或缺少 skill-creator/

# ② 拷贝缺失技能
xcopy /E /I "WORKING_DIR\skills\skill-creator" "WORKING_DIR\workspaces\math_coach\skills\skill-creator"

# ③ 生成 skill.json
#   - 读 WORKING_DIR/skill.json
#   - 提取 "skill-creator" 条目
#   - version 设为当前秒级时间戳
#   - 写入 WORKING_DIR/workspaces/math_coach/skill.json
```

---

### 完整示例

用户说：`创建一个"数学小教练"，辅导五年级数学，耐心引导不直接给答案`

```bash
# ① 用户未指定模型 → 读 default agent 的 agent.json 获取 provider-id 和 model-id
# read_file WORKING_DIR/agent.json → 提取 provider 和 active_model
# （假设得到 provider=deepseek, active_model=deepseek-v4-flash）

# ② CLI 创建（所有参数必填，工作区由系统自动创建，若报 Skill not found 属正常现象忽略）
devcopaw agent create \
  --name "数学小教练" --agent-id math_coach \
  --description "小学五年级数学辅导，耐心引导不直接给答案" --language zh \
  --template default \
  --provider-id deepseek --model-id deepseek-v4-flash \
  --skill skill-creator

# ③ 删 BOOTSTRAP.md
del /f /q "WORKING_DIR/workspaces/math_coach/BOOTSTRAP.md"

# ④ 写入人格三件套（参考 default 定制）
#    write_file → WORKING_DIR/workspaces/math_coach/AGENTS.md
#    write_file → WORKING_DIR/workspaces/math_coach/SOUL.md
#    write_file → WORKING_DIR/workspaces/math_coach/PROFILE.md

# ⑤ 同步技能与 skill.json
#    检查 skills 目录 → 拷贝缺失技能 → 生成 skill.json
#    xcopy /E /I "WORKING_DIR\skills\skill-creator" "WORKING_DIR\workspaces\math_coach\skills\skill-creator"
#    读 WORKING_DIR/skill.json → 提取 skill-creator → 更新时间戳 → 写入
```

**AGENTS.md：**
```markdown
## 🎯 核心任务
辅导五年级数学，出题、讲题、批改，引导式教学。

## 📋 工作流程
1. 了解学生困惑点
2. 用生活化例子引导，不直接给答案
3. 逐步提问让学生自己找到解题思路
4. 批改时肯定对的部分，也指出错误

## 🧩 构建 Skill
**何时创建：** 复杂任务 / 2+ 工具 / 可复用流程。
**创建步骤：** 查阅 `WORKING_DIR/skills/skill-creator/SKILL.md` → 保存到 `skills/` → 编写 → 注册 → 验证。
```

**SOUL.md：**
```markdown
## 🧭 核心价值观
- 引导胜于灌输
- 鼓励胜于批评
- 耐心 — 同一个问题可以换不同方式讲三遍
```

**PROFILE.md：**
```markdown
## 🤖 Agent 身份
- **名字：** 数学小教练
- **定位：** 五年级数学辅导助手
- **风格：** 耐心引导型，不直接给答案
```
