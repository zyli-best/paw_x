# platform-kb-search.exe 协议

运行时只需要 `.NET Framework 4.5`，不需要 Python、Node 或额外 DLL。配置文件 `platform-kb-search.config.json` 必须与 EXE 位于同一目录；调用方无需关注或手动传入该文件，`platform-kb-search.exe` 会在运行时自动读取。

## 命令行输入

业务入参只有两个：

```json
{
  "query": "用户的问题",
  "kbInfoUuids": "knowledge-base-id-1, knowledge-base-id-2"
}
```

执行 exe 时映射为：

```powershell
.\platform-kb-search.exe --query "用户的问题" --kbInfoUuids "knowledge-base-id-1, knowledge-base-id-2"
```

- `--query`：必填，字符串，去除首尾空白后不能为空，最长 20000 字符。
- `--kbInfoUuids`：必填，知识库 ID 字符串，多个 ID 用逗号分隔。空值会被过滤、重复 ID 会去重，一次最多 20 个。

程序会自动从同目录配置文件读取服务地址，并把规范化后的参数转发给上游知识库服务；调用时只需要提供命令行入参。

## 配置

`platform-kb-search.config.json`：

```json
{
  "endpoint": "http://example/mcp-server",
  "timeoutSeconds": 30,
  "maxResponseBytes": 2097152
}
```

- `endpoint`：必填，HTTP/HTTPS 地址。
- `timeoutSeconds`：可选，默认 30，范围 1 到 300。
- `maxResponseBytes`：可选，默认 2MB，范围 1KB 到 32MB。

## stdout 输出

成功：

```json
{
  "ok": true,
  "code": "200",
  "message": "操作成功",
  "results": []
}
```

如果上游返回的是非标准 JSON 文本：

```json
{
  "ok": true,
  "code": "200",
  "message": "操作成功",
  "content": "原始文本",
  "results": []
}
```

失败：

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_INPUT",
    "message": "query 不能为空"
  }
}
```

日志只写入 stderr。

## 构建

在 Windows PowerShell 中运行：

```powershell
.\build.ps1
```

构建脚本会编译 `PlatformKbSearch.cs`，执行 `platform-kb-search.exe --self-test`，并打包 skill。

只需要编译和自检时可跳过打包：

```powershell
.\build.ps1 -SkipPackage
```


