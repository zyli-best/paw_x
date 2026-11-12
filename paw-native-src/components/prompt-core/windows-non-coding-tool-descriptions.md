# Windows 非 Coding 工具描述

适用范围为 Windows 上明确进入非 Coding 的请求，且 Shell 已启用 `QWENPAW_FORCE_DIRECT_SUBPROCESS_SYNC`。以下给出可直接使用的 `function.description` 和参数 `description`。代码块及参数表中的文案才发给模型。运行依据、示例和验收表供维护者使用。

主要替换 `execute_shell_command`、`read_file`、`write_file`、`edit_file` 和 `append_file`。检查内置工具的实际说明后，补齐两处同样包含其他场景文案的工具。`desktop_screenshot` 需要替换正文及参数，`spawn_subagent` 重点精简 `fork` 的说明并修正正文里对 Git 隔离的承诺。

## 1. 运行前提与注入位置

1. **在运行时确认场景。** 用 `sys.platform == "win32"`、请求明确的 `is_coding_mode is False` 及现有 `force_direct_subprocess_sync_enabled()` 选择本版。启动参数或环境变量由服务端设置，模型不负责启用。模式信息缺失，或 Coding 开关已开启但未选项目目录时，沿用原版。由构建器在创建工具集前确认并传递选择结果，切换文案只作用于本轮实例。[请求模式](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/runtime/builder.py:274) · [开关解析](D:/project/fw_agent/code/paw/copaw/paw-native-src/components/prompt-core/src/qwenpaw/config/shell_policy.py:10)
2. **同时替换正文与适用的参数描述。** AgentScope 从函数文档分别生成工具正文和参数说明。当前两个包装器都调用 `shell_tool_description()`，它在旧正文后追加规则，`cwd` 的跨模式说明也保留在参数里。可复用这两个入口，让它们调用同一个请求级文案选择器。保留工具名称、参数结构、类型、必填项、默认值、执行函数和权限判定。参数说明在本轮实例的 schema 副本上替换。[现有选择器](D:/project/fw_agent/code/paw/copaw/paw-native-src/components/prompt-core/src/qwenpaw/config/shell_policy.py:96) · [权限包装器](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/governance/tool_adapter.py:157) · [普通包装器](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/runtime/tool_guard.py:56)
3. **本轮目录和公共规则只说明一次。** 环境提示给出实际 Shell、默认目录及授权挂载。相对文件路径和省略的 Shell `cwd` 都用请求默认目录。有可写外部挂载时用第一个，无挂载时用会话临时目录，无会话时回退 Agent 工作区。只读／可执行挂载不作默认输出根。跨工具的发现、Skill 加载、依赖和审批保留在系统提示。工具描述写清用途、填参和副作用。[默认目录](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/config/context.py:180) · [环境提示](D:/project/fw_agent/code/paw/copaw/paw-native-src/components/prompt-core/src/qwenpaw/app/chats/utils.py:457)
4. **专用工具提示以本轮可用工具为准。** 工具集会经过配置、模式和请求过滤。短文案可直接说「可用的专用工具」，需要列名字时用最终可见集合。位置未知的文件按系统规则依次查可写挂载、只读挂载和会话临时目录，每次给明确的搜索根。`read_file` 和 `edit_file` 接收已知路径，不跨根找文件。普通新输出用相对路径，例如 `reports/a.txt`，不拼接工作区名、挂载目录名或会话临时根。已确认的目标被拒绝时按原因处理，不私自换目标。[工具过滤](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/runtime/builder.py:400) · [文件查找与输出规则](D:/project/fw_agent/code/paw/copaw/paw-native-src/components/prompt-core/src/qwenpaw/app/chats/utils.py:247)

## 2. `execute_shell_command`

### 工具正文

```text
在本机通过新子进程执行一个需要 Shell 的独立操作。文件读写、查找等优先用本轮可用的专用工具。每次调用只提交一条单用途命令，禁止串接、管道、重定向和多行组合。允许运行单用途脚本，脚本和解释器命令都不能用来打包多个独立操作。用 cwd 指定工作目录，上一调用改变的目录或环境变量不会传给下一调用。

有依赖的命令等前一步实际结果。写入、影响不明或依赖结果的命令在每条模型回复中只提交一个，并等待结果。参数已知且独立的必要只读查询可在同一条模型回复中分别提交，由运行时决定并发。收到拒绝、非零退出、超时或依赖提示时，按实际原因处理，保留已确认的目标。进程返回不代表后台任务已经完成。
```

| 参数 | 现有类型及默认值 | 参数描述 |
| --- | --- | --- |
| `command` | `string`，必填 | 按本轮实际 Shell 的语法填写一条命令。使用已提供的运行时路径。 |
| `timeout` | `number`，默认 `60.0` | 子进程等待时限，单位为秒。省略或传 `60` 时，优先使用 Agent 配置的时限。 |
| `cwd` | 路径字符串或 `null`，默认 `null` | 执行目录。省略时用本轮默认目录。指定其他已获授权的目录时，传绝对路径。 |
| `sandbox_config` | 可选，默认 `null` | 由运行时管理。此执行路径无需传入。 |

实际 Shell 为 PowerShell 时，在 `command` 描述末尾加一句「可用前置 `&` 调用一个可执行文件，例如 `& 'C:/Program Files/Tool/tool.exe' --version`」。`&` 位于两条命令之间时仍属于串接。其他 Shell 不发送这一句。

**调用示例**

- 查看两个独立的仓库状态，可在同一条模型回复里分别提交 `git status` 和 `git branch -a`，各自指定相同的已授权绝对 `cwd`。组合成 `git status && git branch -a` 会触发原子性拦截。
- 安装后查版本，先等安装成功，再发版本查询。已有脚本直接按当前 Shell 语法运行。需要新建单用途脚本时，先用 `write_file` 写入，确认写入结果后再执行。
- 要把命令输出保存为文本，先取得完整、未截断且符合目标格式的文本，再用 `write_file`。二进制结果不能经过文本输出中转。

**运行依据**

- 标准权限包装器以 `allow_sandbox=False` 评估强制直执行 Shell 调用并沿用策略结果。需要宿主执行授权的回退走现有审批，`sandbox_config` 在调用时移除。Shell 接着检查危险命令、可识别的只读挂载写目标和原子性，再执行专用工具路由检查及语义检查。两类路由检查在此场景默认 `block`，仍可由服务端配置。原子性检查独立执行。[权限评估](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/governance/tool_adapter.py:400) · [直执行包装](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/governance/tool_adapter.py:543) · [Shell 检查顺序](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/shell.py:4161)
- `subprocess.Popen` 启动配置的 Shell，未配置且无 `SHELL` 时走 `cmd`。PowerShell、`cmd` 及其他已配置 Shell 使用各自的启动参数。显式相对 `cwd` 会相对服务进程目录解析，所以短版建议显式 `cwd` 一律传已获授权的绝对路径。[子进程执行](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/shell.py:2484) · [相对 cwd 回归用例](D:/project/fw_agent/code/paw/copaw/copaw/tests/test_governance_direct_shell.py:171)
- 托管 Python、Node.js 或 Git 只有在当前请求确实准备了对应运行时时才参与命令校验和环境注入。按环境提示使用本轮路径。收到运行时冲突或依赖提示时，按提示修正后再提交。结果文本可报告非零退出码，即使工具状态为 `SUCCESS`，也要看结果文本。[托管运行时分支](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/shell.py:4291) · [退出码格式](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/shell.py:2260)

## 3. `read_file`

### 工具正文

```text
读取已知路径的现有文本文件。相对路径从本轮默认目录解析，其他授权目录中的文件传绝对路径。找不到时先核对路径，位置未知时使用可用的文件查找工具。按 UTF-8 读取并去掉开头的 BOM，无效字节可能被忽略。行范围从 1 开始，含起止行，返回正文不加行号。输出受读取和返回上限约束。结果提示续读时，按给出的 start_line 继续。
```

| 参数 | 现有类型及默认值 | 参数描述 |
| --- | --- | --- |
| `file_path` | `string`，必填 | 已有文本文件路径。相对路径基于本轮默认目录，其他授权目录使用绝对路径。 |
| `start_line` | 整数、整数字符串或 `null`，默认 `null` | 起始行，包含该行，省略从第 1 行开始。 |
| `end_line` | 整数、整数字符串或 `null`，默认 `null` | 结束行，包含该行，省略时读到可读取内容末尾。 |

**示例** `{"file_path":"E:/资料/报告.txt","start_line":10,"end_line":20}` 读取已授权文件的第 10 至 20 行。起始行超出已读取内容或有效范围倒置会报错。只读挂载可作为授权读取来源，工具不会创建文件或遍历其他根。[实现](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/file_io.py:233) · [读取及编码处理](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/utils.py:238)

## 4. `write_file`

### 工具正文

```text
创建文本文件或整文件覆盖。已有文件的内容会被替换，局部修改用 edit_file，保留原内容并追加用 append_file。路径检查通过后会创建缺少的父目录。普通新文件传相对路径。有可写外部挂载时，write_file 的目标即使已存在也须位于首个可写挂载。经授权显式写入当前 Agent 工作区的 skills/ 子目录或已有的根 skill.json 时，可使用绝对路径例外。其他权限及只读限制仍适用。
```

| 参数 | 现有类型 | 参数描述 |
| --- | --- | --- |
| `file_path` | `string`，必填 | 目标文件路径。普通新文件传相对于本轮默认目录的路径。覆盖前核对实际目标。 |
| `content` | `string`，必填 | 要写入的完整文本。`.csv`、`.tsv`、`.tab`、`.txt`、`.log` 使用带 BOM 的 UTF-8，其他扩展名使用普通 UTF-8。 |

**示例** 默认目录为 `D:/输出` 时，`{"file_path":"reports/result.txt","content":"完成\n"}` 写到 `D:/输出/reports/result.txt`，必要时创建 `reports`。有两个可写挂载时，传第二个挂载里的绝对路径依然会触发 `write_file` 的首挂载规则。现有目标如果只需局部修改，按授权和实际内容调用 `edit_file`，不要改到第一个挂载中的同名文件。[首挂载和 Skill 例外](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/file_io.py:160) · [写入实现](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/file_io.py:403)

## 5. `edit_file`

### 工具正文

```text
在现有 UTF-8 文本文件里替换精确匹配的原文。先确认目标与原文，所有匹配都会被替换，空格、缩进和换行必须一致。找不到原文时返回错误，文件保持原样。工具读取并重写整个文件，只用于能够完整读取和解码的文本。相对路径基于本轮默认目录，其他授权目录中的文件传绝对路径。只读目标不可修改。
```

| 参数 | 现有类型 | 参数描述 |
| --- | --- | --- |
| `file_path` | `string`，必填 | 已有的可写文本文件路径。 |
| `old_text` | `string`，必填 | 从文件内容取得的非空原文，按字面匹配。若只想修改一处，带上足够上下文使匹配唯一。 |
| `new_text` | `string`，必填 | 用于替换的文本。空字符串删除每处匹配。写回时 `.csv`、`.tsv`、`.tab`、`.txt`、`.log` 使用带 BOM 的 UTF-8，其他扩展名使用普通 UTF-8。 |

**示例** 文件中有两处 `timeout=30` 时，直接用它作为 `old_text` 会改两处。只改 `[download]` 那一处时，把实际文件里的区块名与换行一并放进 `old_text`。现有文件可在授权的其他可写挂载中使用绝对路径，`edit_file` 没有 `write_file` 的首挂载限制。写回编码按扩展名选择，同 `write_file`。底层读取有上限且会忽略无效 UTF-8 字节，超大文件或非 UTF-8 文件用此工具重写可能丢内容。参数说明要求传非空原文，保留当前 schema，不假定后端有空串校验。[编辑实现](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/file_io.py:480) · [已有路径解析](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/existing_path_guard.py:240)

## 6. `append_file`

### 工具正文

```text
将文本追加到文件末尾，保留原内容。文件不存在时会创建，父目录必须已存在。需要换行时把换行字符放进 content。普通新文件传相对路径，其他授权目录中的已有可写文件传绝对路径。遵守只读限制。追加到已有非空文件时不会在中间插入 BOM。
```

| 参数 | 现有类型 | 参数描述 |
| --- | --- | --- |
| `file_path` | `string`，必填 | 目标文件路径。相对路径从本轮默认目录解析。父目录必须已存在。 |
| `content` | `string`，必填 | 要追加的文本，按需包含换行。`.csv`、`.tsv`、`.tab`、`.txt`、`.log` 使用带 BOM 的 UTF-8 编码方式，其他扩展名使用普通 UTF-8。 |

**示例** `{"file_path":"logs/run.log","content":"\n已完成\n"}` 在 `logs` 已存在时追加文本，文件不存在则创建。`logs` 不存在时，先用可用的目录工具创建并等待结果。`append_file` 走通用输出路径校验，未附加 `write_file` 的首挂载限制。若明确要求用绝对路径创建，仍须通过后端允许的输出根和写权限校验。[追加实现](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/file_io.py:583) · [通用输出校验](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/output_path_guard.py:340)

## 7. `desktop_screenshot`

### 工具正文

```text
将所有显示器的桌面合成一张 PNG。省略 path 时在本轮默认目录自动命名，提供路径时使用其 .png 后缀或追加 .png。缺少的父目录会创建。有可写外部挂载时，输出须位于首个可写挂载。成功时返回图片及实际绝对路径。
```

| 参数 | 现有类型及默认值 | 参数描述 |
| --- | --- | --- |
| `path` | `string`，默认 `""` | PNG 输出路径。普通输出传相对路径，留空则在本轮默认目录生成 `desktop_screenshot_<时间戳>.png`。 |
| `capture_window` | `boolean`，默认 `false` | 此场景请省略或传 `false`。传 `true` 也会截取完整桌面。 |

**示例** `{"path":"shots/desk.png"}` 保存为默认目录下的 `shots/desk.png`。传 `shots/desk.jpg` 会保存为 `shots/desk.jpg.png`。截屏由 `mss` 完成，依赖缺失时工具会报错。核对和交付以成功结果里的 `path` 为准。[截图路径与执行](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/desktop_screenshot.py:123) · [参数及返回](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/desktop_screenshot.py:229)

## 8. `spawn_subagent`

### 工具正文

```text
以当前 Agent 身份在当前工作区启动一次性子任务。前台等待结果，子任务不可续接。需要调用另一已配置 Agent 时使用 chat_with_agent。后台调用返回任务 ID，用 check_agent_task 跟踪并回收结果。仅在工作区协作规则允许时创建子 Agent。
```

| 参数 | 现有类型及默认值 | 参数描述 |
| --- | --- | --- |
| `task` | `string`，必填 | 需要独立完成的子任务，写清范围和交付要求。 |
| `fork` | `boolean`，默认 `false` | 默认启动空白会话。传 `true` 时复制父会话状态。工作区是 Git 仓库时，fork 接口还会尝试创建 worktree。 |
| `background` | `boolean`，默认 `false` | 默认等待结果。传 `true` 时提交后台任务，并用返回的任务 ID 查询结果。 |
| `timeout` | `integer`，默认 `600` | 前台等待时限，单位为秒。 |

**示例** `{"task":"读取已授权的说明文件并返回要点","background":true}` 提交后，拿到任务 ID 只代表已提交，继续用 `check_agent_task` 等结果。`fork` 接口可创建 worktree，调用方会传递 `fork_project_dir`。当前普通工具链未找到消费该字段的入口，所以描述只承诺尝试创建 worktree，避免承诺子任务写入一定隔离。[子任务实现](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/agent_management.py:853) · [fork 请求转发](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/agents/tools/agent_management.py:1077) · [fork 接口](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/app/routers/fork.py:276)

## 9. 共享提示和验收

`glob_search`、`grep_search` 负责位置和内容查找。`copy_file_or_dir`、`move_file_or_dir`、`create_directory` 已声明为 `non_coding_only`，其描述已有适用范围。保留这些工具的现有描述和路径规则。系统提示中的工具目录如需压缩，可去掉与最终工具 schema 重复的用途说明，保留本轮真实目录、挂载优先级和工具之间的依赖顺序。工具出错后的提示和独立的 Shell 语义守卫文案继续由原逻辑生成。[工具可见性](D:/project/fw_agent/code/paw/copaw/copaw/src/qwenpaw/app/workspace/local_workspace.py:75) · [工具目录渲染](D:/project/fw_agent/code/paw/copaw/paw-native-src/components/prompt-core/src/qwenpaw/app/chats/utils.py:165)

| 验收场景 | 预期 |
| --- | --- |
| Windows 非 Coding，强制 subprocess 开启 | 最终发给模型的工具正文和参数均命中短版。Shell 不再同时附上旧长描述。 |
| Coding、场景缺失或非目标执行路径 | 最终工具 schema 与现有版本一致。 |
| 有两个挂载、只有只读挂载、无挂载、无会话 | 默认根分别使用首个可写挂载、会话临时目录、会话临时目录或无会话时的工作区回退。读写操作仍由现有权限和具体工具规则判定。 |
| Shell 原子性、顺序和结果 | 同批独立只读查询各有一个调用。组合命令仍被拦截。PowerShell 前置 `&` 仅在适用时提示。依赖步骤等结果，非零退出按结果文本处理。 |
| 文件副作用 | 读不存在的文件失败。写入创建父目录且可整文件覆盖。编辑只替换精确原文的所有匹配。追加不创建父目录、不自动插换行。只读目标仍按原规则拒绝写入。 |
| 截屏与子任务 | 截屏路径以 PNG 实际落盘结果为准。后台子任务按返回的 ID 回收结果，描述不保证写入隔离。 |

比较最终请求的 `tools` JSON，分别核对 `function.description` 和 `function.parameters.properties.*.description`，再检查去掉文案字段后的 schema 是否一致。以相同模型、消息和实际工具集测量工具描述缩短量。系统提示、工具 schema 和对话历史分开统计。
