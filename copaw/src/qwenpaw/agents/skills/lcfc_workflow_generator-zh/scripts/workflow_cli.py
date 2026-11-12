#!/usr/bin/env python3
"""
LCFC Workflow CLI — 工作流平台命令行工具

根据联宝（LCFC）Workflow Engine 接口文档封装，
支持创建、修改、删除、查询、分页查询、运行、登录、验证 8 个操作。

运行接口支持自动签名登录（对应 Java RemoteRequestUtils），
也可通过 login 子命令独立获取 token。

用法:
    python workflow_cli.py create --name xxx --uid xxx [--data file.json] [options]
    python workflow_cli.py update <uuid> --name new_name [options]
    python workflow_cli.py delete <uuid> [options]
    python workflow_cli.py get <uuid> [options]
    python workflow_cli.py list [--name xxx] [options]
    python workflow_cli.py login [--auth-base-url xxx] [options]
    python workflow_cli.py run (--flow-id xxx | --workflow-uuid xxx) [--param key=val]... [options]
    python workflow_cli.py verify (--flow-id xxx | --workflow-uuid xxx) --expected "关键词1,关键词2" [--param key=val]... [options]

注意: — workflow-uuid 用于未发布的工作流（刚注册的版本默认未发布）；
      — flow-id   用于已发布的工作流。

环境变量:
    WORKFLOW_BASE_URL         管理接口地址（默认 https://aaas.lcfuturecenter.com/prod-api/agent/workflow）
    WORKFLOW_RUN_URL          运行接口地址（默认 https://aaas.lcfuturecenter.com/prod-api/agent/workflow/run）
    WORKFLOW_AUTH_BASE_URL    认证服务地址（默认 https://aaas.lcfuturecenter.com）
    WORKFLOW_CLIENT_ID        客户端ID（默认 872f1e1bbf5181cf43918edd7ad56550）
    WORKFLOW_SIGN_SECRET_KEY  签名密钥（默认 visitclientkey）
"""
import argparse
import hashlib
import json
import os
import random
import re
import string
import sys
import time
from typing import Any

import requests


# ======================================================================
# 签名登录（对应 Java RemoteRequestUtils.getRemoteToken）
# ======================================================================

def sign_login(
    auth_base_url: str | None = None,
    client_id: str | None = None,
    sign_secret_key: str | None = None,
) -> tuple[str, str]:
    """签名登录，返回 (token: "Bearer xxx", client_id)

    对应 Java：
      - md5("nonce=" + nonce + "&timestamp=" + timestamp + "&key=" + secretKey)
      - POST {baseUrl}/prod-api/auth/login
      - 解析 data.access_token
    """
    auth_base_url = (auth_base_url or os.environ.get(
        "WORKFLOW_AUTH_BASE_URL",
        "https://aaas.lcfuturecenter.com",
    )).rstrip("/")

    client_id = client_id or os.environ.get(
        "WORKFLOW_CLIENT_ID",
        "872f1e1bbf5181cf43918edd7ad56550",
    )

    secret_key = sign_secret_key or os.environ.get(
        "WORKFLOW_SIGN_SECRET_KEY",
        "visitclientkey",
    )

    # 生成签名参数
    timestamp = str(int(time.time() * 1000))
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=32))
    sign_str = f"nonce={nonce}&timestamp={timestamp}&key={secret_key}"
    sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()

    # 登录请求
    body = {
        "timestamp": timestamp,
        "nonce": nonce,
        "sign": sign,
        "grantType": "sign",
        "clientId": client_id,
    }

    resp = requests.post(
        f"{auth_base_url}/prod-api/auth/login",
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    code = data.get("code")
    if code not in (0, 200):
        raise RuntimeError(f"签名登录失败: {data.get('message', '未知错误')}")

    access_token = data.get("data", {}).get("access_token")
    if not access_token:
        raise RuntimeError("签名登录返回中无 access_token")

    return f"Bearer {access_token}", client_id


def _extract_auth_base(run_url: str) -> str:
    """从运行接口URL中提取认证基础地址"""
    m = re.match(r"(https?://[^/]+)", run_url)
    return m.group(1) if m else "https://aaas.lcfuturecenter.com"


# ======================================================================
# WorkflowClient
# ======================================================================

class WorkflowClient:
    """工作流 API 客户端（管理接口）"""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = 30,
        # 鉴权参数（全可选，不传 token 则自动签名登录）
        token: str | None = None,
        client_id: str | None = None,
        sign_secret_key: str | None = None,
        auth_base_url: str | None = None,
    ):
        self.base_url = (base_url or os.environ.get(
            "WORKFLOW_BASE_URL",
            "https://aaas.lcfuturecenter.com/prod-api/agent/workflow",
        )).rstrip("/")
        self.timeout = timeout
        self._token = token
        self._client_id = client_id or os.environ.get(
            "WORKFLOW_CLIENT_ID", "872f1e1bbf5181cf43918edd7ad56550"
        )
        self._sign_secret_key = sign_secret_key
        self._auth_base_url = auth_base_url

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def _ensure_token(self) -> str:
        """确保有有效 token，没有则自动签名登录"""
        if self._token:
            return self._token
        token_val, self._client_id = sign_login(
            auth_base_url=self._auth_base_url,
            client_id=self._client_id,
            sign_secret_key=self._sign_secret_key,
        )
        self._token = token_val
        return self._token

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        """返回带 Authorization + clientId 的请求头"""
        token = self._ensure_token()
        # 确保 _client_id 已填充（_ensure_token 内会赋值）
        return {"Authorization": token, "ClientId": self._client_id or ""}

    def _raise_if_error(self, resp: requests.Response) -> dict:
        """统一检查响应 code，非 0 直接抛异常"""
        data = resp.json()
        if data.get("code") != 0:
            msg = data.get("message", "未知错误")
            raise RuntimeError(f"[{resp.status_code}] {msg}")
        return data

    # ------------------------------------------------------------------
    # 接口封装
    # ------------------------------------------------------------------

    def create_flow(
        self,
        name: str,
        uid: str,
        data: dict | None = None,
        description: str | None = None,
        icon: str | None = None,
        flow_mode: int | None = None,
        create_by: str | None = None,
        create_dep: str | None = None,
    ) -> dict:
        """创建工作流，返回 { workflow_uuid, flow_id, sid }"""
        body: dict[str, Any] = {"name": name, "uid": uid}
        if data is not None:
            body["data"] = json.dumps(data, ensure_ascii=False)
        if description is not None:
            body["description"] = description
        if icon is not None:
            body["icon"] = icon
        if flow_mode is not None:
            body["flow_mode"] = flow_mode
        if create_by is not None:
            body["create_by"] = create_by
        if create_dep is not None:
            body["create_dep"] = create_dep

        resp = requests.post(self.base_url, json=body, headers=self._headers(), timeout=self.timeout)
        result = self._raise_if_error(resp)
        return result.get("data", {})

    def update_flow(self, workflow_uuid: int, **kwargs: Any) -> None:
        """修改工作流，kwargs 支持 name / uid / data / description / icon /
        flow_mode / publish_flag / proxy_enable / update_by 等"""
        resp = requests.put(
            f"{self.base_url}/{workflow_uuid}",
            json=kwargs,
            headers=self._headers(),
            timeout=self.timeout,
        )
        self._raise_if_error(resp)

    def delete_flow(self, workflow_uuid: int) -> None:
        """逻辑删除工作流"""
        resp = requests.delete(f"{self.base_url}/{workflow_uuid}", headers=self._headers(), timeout=self.timeout)
        self._raise_if_error(resp)

    def get_flow(self, workflow_uuid: int) -> dict:
        """查询工作流详情"""
        resp = requests.get(f"{self.base_url}/{workflow_uuid}", headers=self._headers(), timeout=self.timeout)
        result = self._raise_if_error(resp)
        return result.get("data", {})

    def list_flows(
        self,
        page: int = 1,
        page_size: int = 10,
        name: str | None = None,
        flow_mode: int | None = None,
        uid: str | None = None,
        publish_flag: int | None = None,
        owner_id: int | None = None,
    ) -> dict:
        """分页查询，返回 { records, total, page, pageSize }"""
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if name is not None:
            params["name"] = name
        if flow_mode is not None:
            params["flowMode"] = flow_mode
        if uid is not None:
            params["uid"] = uid
        if publish_flag is not None:
            params["publishFlag"] = publish_flag
        if owner_id is not None:
            params["ownerId"] = owner_id

        resp = requests.get(self._url("/flows/page"), params=params, headers=self._headers(), timeout=self.timeout)
        result = self._raise_if_error(resp)
        return result.get("data", {})

    # ------------------------------------------------------------------
    # 运行接口（SSE 流式）
    # ------------------------------------------------------------------

    @staticmethod
    def run_flow(
        flow_id: str | None = None,
        parameters: dict | None = None,
        *,
        workflow_uuid: str | None = None,
        run_url: str | None = None,
        # 鉴权参数（全可选，不传 token 则自动签名登录）
        token: str | None = None,
        client_id: str | None = None,
        sign_secret_key: str | None = None,
        auth_base_url: str | None = None,
        timeout: int = 600,
    ) -> str:
        """运行工作流（SSE 流式），返回完整输出文本

        参数:
            flow_id: 已发布工作流的 flow_id
            workflow_uuid: 未发布工作流的 workflow_uuid（优先于 flow_id）
            parameters: start 节点入参 dict
            run_url: 运行接口地址
            token: Bearer token，不传则自动签名登录
            client_id: 客户端 ID
            sign_secret_key: 签名密钥
            auth_base_url: 认证服务地址
            timeout: 请求超时秒数

        返回:
            拼接后的完整回复文本

        注意:
            - 未发布的工作流（刚注册）→ 传 workflow_uuid
            - 已发布的工作流          → 传 flow_id
        """
        run_url = run_url or os.environ.get(
            "WORKFLOW_RUN_URL",
            "https://aaas.lcfuturecenter.com/prod-api/agent/workflow/run",
        )

        # 自动签名登录（若无 token）
        if not token:
            abu = auth_base_url or _extract_auth_base(run_url)
            token, client_id = sign_login(abu, client_id, sign_secret_key)

        # 发请求（流式）
        headers = {
            "Authorization": token,
            "ClientId": client_id,
            "Accept": "text/event-stream",
        }
        # 未发布的工作流传 workflow_uuid，已发布的传 flow_id
        if workflow_uuid:
            body: dict[str, Any] = {"workflow_uuid": workflow_uuid}
        else:
            body: dict[str, Any] = {"flow_id": flow_id}
        if parameters:
            body["parameters"] = parameters

        resp = requests.post(
            run_url,
            json=body,
            headers=headers,
            stream=True,
            timeout=timeout,
        )
        resp.raise_for_status()

        # 判断响应类型：JSON 错误 vs SSE 流
        content_type = (resp.headers.get("content-type") or "").lower()

        # ── 非 SSE 响应：JSON 错误处理 ──
        if "application/json" in content_type:
            err_body = resp.json()
            err_code = err_body.get("code")
            err_msg = err_body.get("message") or err_body.get("msg", "未知错误")
            if err_code not in (None, 0, 200):
                raise RuntimeError(f"引擎运行失败: {err_msg}")
            return ""

        # ── SSE 流式解析 ──
        collected_end_inputs: list[Any] = []

        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data: "):
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                # 检查 SSE 事件内是否包含错误（如 token 无效）
                event_code = event.get("code")
                if event_code not in (None, 0, 200):
                    err_msg = event.get("msg") or event.get("message", "未知错误")
                    raise RuntimeError(f"运行失败: {err_msg}")

                # 仅输出 end 节点完成时的 inputs
                steps = event.get("steps")
                step_list: list[dict[str, Any]] = []
                if isinstance(steps, dict):
                    step_list = [steps]
                elif isinstance(steps, list):
                    step_list = [s for s in steps if isinstance(s, dict)]

                for step in step_list:
                    node_id = step.get("node_id", "")
                    action = step.get("action", "")
                    if action != "end" or not str(node_id).startswith("end"):
                        continue
                    inputs = step.get("inputs")
                    collected_end_inputs.append(inputs)

        if collected_end_inputs:
            return json.dumps(collected_end_inputs, ensure_ascii=False)
        return ""


# ======================================================================
# CLI
# ======================================================================

def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        help="服务端地址（默认 https://aaas.lcfuturecenter.com/v2，环境变量 WORKFLOW_BASE_URL）",
    )
    parser.add_argument(
        "--token",
        help="Bearer token（不传则自动签名登录）",
    )
    parser.add_argument(
        "--client-id",
        help="客户端 ID（自动登录使用，环境变量 WORKFLOW_CLIENT_ID）",
    )
    parser.add_argument(
        "--sign-secret-key",
        help="签名密钥（自动登录使用，环境变量 WORKFLOW_SIGN_SECRET_KEY），默认 visitclientkey",
    )
    parser.add_argument(
        "--auth-base-url",
        help="认证服务地址（自动登录使用，默认 https://aaas.lcfuturecenter.com，环境变量 WORKFLOW_AUTH_BASE_URL）",
    )


def _add_auth_args(sub: argparse.ArgumentParser) -> None:
    """运行/验证相关的鉴权参数"""
    sub.add_argument(
        "--token",
        help="Bearer token（不传则自动签名登录）",
    )
    sub.add_argument(
        "--client-id",
        help="客户端 ID（自动登录/请求头使用，环境变量 WORKFLOW_CLIENT_ID）",
    )
    sub.add_argument(
        "--sign-secret-key",
        help="签名密钥（自动登录使用，环境变量 WORKFLOW_SIGN_SECRET_KEY），默认 visitclientkey",
    )
    sub.add_argument(
        "--auth-base-url",
        help="认证服务地址（自动登录使用，默认从 --run-url 提取 base，环境变量 WORKFLOW_AUTH_BASE_URL）",
    )
    sub.add_argument(
        "--raw",
        action="store_true",
        help="仅输出 token 本身（不带格式），便于脚本调用",
    )
    sub.add_argument(
        "--run-url",
        help="运行接口地址（环境变量 WORKFLOW_RUN_URL）",
    )



def _interactive_select_flow(
    base_url: str | None = None,
    token: str | None = None,
    client_id: str | None = None,
    sign_secret_key: str | None = None,
    auth_base_url: str | None = None,
) -> str:
    """交互模式：查询所有工作流，列出让用户选择，返回选中的 flow_id。"""
    client = WorkflowClient(
        base_url=base_url,
        token=token,
        client_id=client_id,
        sign_secret_key=sign_secret_key,
        auth_base_url=auth_base_url,
    )
    result = client.list_flows(page=1, page_size=200)
    records = result.get("records") or []

    if not records:
        print("❌ 当前工作区下没有找到任何工作流", file=sys.stderr)
        sys.exit(1)

    print(f"\n📋 当前工作区共 {len(records)} 个工作流：\n", file=sys.stderr)
    for i, r in enumerate(records, 1):
        flow_name = r.get("name", "未命名")
        flow_desc = r.get("description") or "（无描述）"
        flow_id_val = r.get("flow_id", "")
        wf_uuid_val = r.get("workflow_uuid", r.get("id", ""))
        print(f"  {i}. {flow_name}", file=sys.stderr)
        print(f"     描述: {flow_desc}", file=sys.stderr)
        print(f"     flow_id: {flow_id_val}  |  workflow_uuid: {wf_uuid_val}", file=sys.stderr)
        print(file=sys.stderr)

    while True:
        try:
            raw = input(f"请选择工作流（输入 1-{len(records)}、flow_id 或 workflow_uuid）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            sys.exit(1)

        # 先尝试数字索引
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(records):
                return records[idx - 1]["flow_id"]
            print(f"❌ 请输入 1-{len(records)} 之间的数字", file=sys.stderr)
            continue

        # 尝试 flow_id 或 workflow_uuid 直接匹配
        for r in records:
            if r.get("flow_id") == raw or str(r.get("workflow_uuid", r.get("id", ""))) == raw:
                return r["flow_id"]
        print(f"❌ 未找到 flow_id 或 workflow_uuid 为「{raw}」的工作流，请重新选择", file=sys.stderr)


# ======================================================================
# verify 子命令 — 运行工作流并验证输出是否符合预期
# ======================================================================

def cmd_verify(args: argparse.Namespace) -> None:
    """运行工作流并验证输出是否包含所有预期关键词"""
    # 解析参数
    parameters: dict[str, Any] = {}
    if args.params_file:
        with open(args.params_file, "r", encoding="utf-8") as f:
            parameters.update(json.load(f))
    if args.params:
        for kv in args.params:
            key, _, val = kv.partition("=")
            parameters[key.strip()] = val.strip()

    try:
        actual_output = WorkflowClient.run_flow(
            flow_id=args.flow_id,
            workflow_uuid=args.workflow_uuid,
            parameters=parameters or None,
            run_url=args.run_url,
            token=args.token,
            client_id=args.client_id,
            sign_secret_key=args.sign_secret_key,
            auth_base_url=args.auth_base_url,
        )
    except Exception as e:
        result = {
            "success": False,
            "flow_id": args.flow_id,
            "workflow_uuid": args.workflow_uuid,
            "expected": args.expected,
            "actual_output": None,
            "error": str(e),
            "match_details": None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # 关键词匹配验证
    import re as _re

    if args.mode == "contain":
        # 子串包含模式
        success = args.expected in actual_output
        match_details = {
            "mode": "contain",
            "keywords_count": 1,
            "matched": success,
        }
    else:
        # keyword 模式（默认）：按分隔符拆分关键词
        keywords = [
            kw.strip()
            for kw in _re.split(r"[,;，；、\s]+", args.expected)
            if kw.strip()
        ]
        matched = [kw for kw in keywords if kw in actual_output]
        missing = [kw for kw in keywords if kw not in actual_output]
        success = len(missing) == 0
        match_details = {
            "mode": "keyword",
            "keywords_count": len(keywords),
            "matched_keywords": matched,
            "missing_keywords": missing,
        }

    result = {
        "success": success,
        "flow_id": args.flow_id,
        "workflow_uuid": args.workflow_uuid,
        "expected": args.expected,
        "actual_output": actual_output,
        "error": None,
        "match_details": match_details,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LCFC 工作流平台 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "环境变量:\n"
            "  WORKFLOW_BASE_URL         管理接口地址\n"
            "  WORKFLOW_RUN_URL          运行接口地址\n"
            "  WORKFLOW_AUTH_BASE_URL    认证服务地址\n"
            "  WORKFLOW_CLIENT_ID        客户端ID\n"
            "  WORKFLOW_SIGN_SECRET_KEY  签名密钥\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- create ----
    p_create = sub.add_parser("create", help="创建工作流")
    _add_common_args(p_create)
    p_create.add_argument("--name", required=True, help="工作流名称（全局唯一）")
    p_create.add_argument("--uid", required=True, help="创建人用户ID")
    p_create.add_argument("--data", help="DSL JSON 文件路径")
    p_create.add_argument("--description", help="工作流描述")
    p_create.add_argument("--icon", help="图标")
    p_create.add_argument("--flow-mode", type=int, help="模式：1 非对话 / 2 对话")
    p_create.add_argument("--create-by", help="创建人姓名")
    p_create.add_argument("--create-dep", help="创建部门")

    # ---- update ----
    p_update = sub.add_parser("update", help="修改工作流")
    _add_common_args(p_update)
    p_update.add_argument("workflow_uuid", type=int, help="工作流 UUID")
    p_update.add_argument("--name", help="新名称")
    p_update.add_argument("--uid", help="新创建人ID")
    p_update.add_argument("--data", help="新 DSL JSON 文件路径")
    p_update.add_argument("--description", help="新描述")
    p_update.add_argument("--flow-mode", type=int, help="模式")
    p_update.add_argument("--icon", help="新图标")
    p_update.add_argument("--publish-flag", type=int, help="发布状态")
    p_update.add_argument("--proxy-enable", type=int, help="是否启用代理")
    p_update.add_argument("--update-by", help="更新人姓名")

    # ---- delete ----
    p_delete = sub.add_parser("delete", help="删除工作流")
    _add_common_args(p_delete)
    p_delete.add_argument("workflow_uuid", type=int, help="工作流 UUID")

    # ---- get ----
    p_get = sub.add_parser("get", help="查询工作流详情")
    _add_common_args(p_get)
    p_get.add_argument("workflow_uuid", type=int, help="工作流 UUID")

    # ---- list ----
    p_list = sub.add_parser("list", help="分页查询工作流")
    _add_common_args(p_list)
    p_list.add_argument("--page", type=int, default=1, help="页码（默认 1）")
    p_list.add_argument("--page-size", type=int, default=10, help="每页条数（默认 10）")
    p_list.add_argument("--name", help="名称模糊查询")
    p_list.add_argument("--flow-mode", type=int, help="模式：1 非对话 / 2 对话")
    p_list.add_argument("--uid", help="创建人ID筛选")
    p_list.add_argument("--publish-flag", type=int, help="发布标志筛选")
    p_list.add_argument("--owner-id", type=int, help="用户ID筛选")

    # ---- login ----
    p_login = sub.add_parser("login", help="从平台获取 Bearer token（独立鉴权）")
    p_login.add_argument("--auth-base-url", help="认证服务地址（环境变量 WORKFLOW_AUTH_BASE_URL）")
    p_login.add_argument("--client-id", help="客户端 ID（环境变量 WORKFLOW_CLIENT_ID）")
    p_login.add_argument("--sign-secret-key", help="签名密钥（环境变量 WORKFLOW_SIGN_SECRET_KEY）")
    p_login.add_argument(
        "--raw", action="store_true",
        help="仅输出 token 本身（不带格式），便于脚本调用",
    )

    # ---- run ----
    p_run = sub.add_parser("run", help="运行工作流（自动签名登录）")
    _add_common_args(p_run)
    p_run.add_argument("--run-url", help="运行接口地址（环境变量 WORKFLOW_RUN_URL）")
    p_run.add_argument(
        "--raw", action="store_true",
        help="仅输出 token 本身（不带格式），便于脚本调用",
    )
    p_run.add_argument("--flow-id", help="已发布工作流的 flow_id（不传则交互选择）")
    p_run.add_argument(
        "--workflow-uuid", help="未发布工作流的 workflow_uuid（优先于 --flow-id）",
    )
    p_run.add_argument(
        "--param", action="append", dest="params",
        help="start 节点入参，格式 key=value，可多次传（如 --param usr_input=你好）",
    )
    p_run.add_argument(
        "--params-file", help="参数 JSON 文件路径（与 --param 合并，--param 优先级更高）",
    )
    # ---- verify ----
    p_verify = sub.add_parser("verify", help="运行工作流并验证输出是否符合预期（关键词匹配）")
    _add_common_args(p_verify)
    p_verify.add_argument("--run-url", help="运行接口地址（环境变量 WORKFLOW_RUN_URL）")
    p_verify.add_argument("--flow-id", help="已发布工作流的 flow_id")
    p_verify.add_argument(
        "--workflow-uuid", help="未发布工作流的 workflow_uuid（优先于 --flow-id）",
    )
    p_verify.add_argument(
        "--expected", required=True,
        help="预期输出关键词，逗号/分号/空格分隔（所有关键词均需出现在输出中才算通过）",
    )
    p_verify.add_argument(
        "--param", action="append", dest="params",
        help="start 节点入参，格式 key=value，可多次传",
    )
    p_verify.add_argument("--params-file", help="参数 JSON 文件路径")
    p_verify.add_argument(
        "--mode", choices=["keyword", "contain"], default="keyword",
        help="匹配模式：keyword（默认，所有关键词均出现）, contain（输出包含整个 expected 文本）",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    try:
        if args.command in ("create", "update", "delete", "get", "list"):
            client = WorkflowClient(
                base_url=args.base_url,
                token=args.token,
                client_id=args.client_id,
                sign_secret_key=args.sign_secret_key,
                auth_base_url=args.auth_base_url,
            )

        if args.command == "create":
            data = None
            if args.data:
                with open(args.data, "r", encoding="utf-8") as f:
                    data = json.load(f)
            result = client.create_flow(
                name=args.name,
                uid=args.uid,
                data=data,
                description=args.description,
                icon=args.icon,
                flow_mode=args.flow_mode,
                create_by=args.create_by,
                create_dep=args.create_dep,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "update":
            kwargs: dict[str, Any] = {}
            for key in ("name", "uid", "description", "icon", "publish_flag",
                        "proxy_enable", "update_by"):
                val = getattr(args, key, None)
                if val is not None:
                    kwargs[key] = val
            if args.flow_mode is not None:
                kwargs["flow_mode"] = args.flow_mode
            if args.data:
                with open(args.data, "r", encoding="utf-8") as f:
                    kwargs["data"] = json.load(f)
            client.update_flow(args.workflow_uuid, **kwargs)
            print(json.dumps({"success": True, "workflow_uuid": args.workflow_uuid}, ensure_ascii=False))

        elif args.command == "delete":
            client.delete_flow(args.workflow_uuid)
            print(json.dumps({"success": True, "workflow_uuid": args.workflow_uuid}, ensure_ascii=False))

        elif args.command == "get":
            result = client.get_flow(args.workflow_uuid)
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "list":
            result = client.list_flows(
                page=args.page,
                page_size=args.page_size,
                name=args.name,
                flow_mode=args.flow_mode,
                uid=args.uid,
                publish_flag=args.publish_flag,
                owner_id=args.owner_id,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "login":
            token_val, client_id_val = sign_login(
                auth_base_url=args.auth_base_url,
                client_id=args.client_id,
                sign_secret_key=args.sign_secret_key,
            )
            if args.raw:
                # 裸 token（去掉 "Bearer " 前缀）
                print(token_val.removeprefix("Bearer "))
            else:
                print(json.dumps({
                    "token": token_val,
                    "client_id": client_id_val,
                    "auth_base_url": args.auth_base_url or os.environ.get(
                        "WORKFLOW_AUTH_BASE_URL",
                        "https://aaas.lcfuturecenter.com",
                    ),
                }, ensure_ascii=False, indent=2))

        elif args.command == "run":
            # 确定运行标识：workflow_uuid（未发布）> flow_id（已发布+交互选择）
            workflow_uuid: str | None = args.workflow_uuid
            flow_id: str | None = args.flow_id
            if not workflow_uuid and not flow_id:
                flow_id = _interactive_select_flow(
                    base_url=args.base_url,
                    token=args.token,
                    client_id=args.client_id,
                    sign_secret_key=args.sign_secret_key,
                    auth_base_url=args.auth_base_url,
                )

            # 解析参数
            parameters: dict[str, Any] = {}
            if args.params_file:
                with open(args.params_file, "r", encoding="utf-8") as f:
                    parameters.update(json.load(f))
            if args.params:
                for kv in args.params:
                    key, _, val = kv.partition("=")
                    parameters[key.strip()] = val.strip()

            text = WorkflowClient.run_flow(
                flow_id=flow_id,
                workflow_uuid=workflow_uuid,
                parameters=parameters or None,
                run_url=args.run_url,
                token=args.token,
                client_id=args.client_id,
                sign_secret_key=args.sign_secret_key,
                auth_base_url=args.auth_base_url,
            )
            if text:
                print(text)

        elif args.command == "verify":
            cmd_verify(args)

    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
