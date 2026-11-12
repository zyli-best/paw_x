#!/usr/bin/env python3
"""
workflow_cli.py run_flow 功能测试

测试场景：
1. 非 SSE JSON 错误响应（如：引擎返回"工作流不存在"）
2. SSE 事件内嵌错误（如：token 无效、认证失败）
3. 正常 SSE 流完整解析
4. HTTP 层面错误（4xx/5xx）

用法:
    python scripts/test_workflow_run.py
"""
import json
import unittest
from unittest.mock import patch, MagicMock
from io import StringIO
import sys
import os

# 将脚本所在目录加入 path，确保能 import workflow_cli
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__)),
)

import workflow_cli as wc


class TestRunFlow(unittest.TestCase):
    """WorkflowClient.run_flow 各种响应场景测试"""

    def _mock_resp(
        self,
        status=200,
        content_type="text/event-stream",
        body=b"",
        iter_lines=None,
    ):
        """构造模拟的 requests.Response"""
        m = MagicMock()
        m.status_code = status
        m.headers = {"content-type": content_type}
        m.ok = status < 400

        if iter_lines is not None:
            # SSE 场景：模拟 iter_lines
            m.iter_lines.return_value = iter_lines
            m.json.side_effect = RuntimeError("not json")
        else:
            # JSON 场景
            m.json.return_value = body if isinstance(body, dict) else json.loads(body)
            m.iter_lines.return_value = [json.dumps(body)] if body else []

        return m

    # ── 场景 1：非 SSE JSON 错误 ──

    def test_json_error_workflow_not_found(self):
        """JSON 错误：工作流不存在（code=600003）"""
        resp = self._mock_resp(
            content_type="application/json;charset=UTF-8",
            body={"code": 600003, "message": "引擎运行失败：: 工作流不存在 flow_id=xxx"},
        )
        with patch("requests.post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                wc.WorkflowClient.run_flow(
                    flow_id="xxx",
                    token="Bearer test",
                    client_id="test",
                )
        self.assertIn("工作流不存在", str(ctx.exception))

    def test_json_ok_no_error(self):
        """JSON 成功响应（code=200），无 SSE 内容 → 正常返回空字符串"""
        resp = self._mock_resp(
            content_type="application/json;charset=UTF-8",
            body={"code": 200, "message": "操作成功", "data": {}},
        )
        with patch("requests.post", return_value=resp):
            result = wc.WorkflowClient.run_flow(
                flow_id="xxx",
                token="Bearer test",
                client_id="test",
            )
        self.assertEqual(result, "")

    # ── 场景 2：SSE 事件内含错误 ──

    def test_sse_error_token_invalid(self):
        """SSE 错误：token 无效（code=401）"""
        sse_lines = [
            'data: {"code": 401, "msg": "token 无效：xxx"}',
        ]
        resp = self._mock_resp(iter_lines=sse_lines)
        with patch("requests.post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                wc.WorkflowClient.run_flow(
                    flow_id="xxx",
                    token="Bearer test",
                    client_id="test",
                )
        self.assertIn("token 无效", str(ctx.exception))

    # ── 场景 3：正常 SSE 流 ──

    def test_sse_normal_flow(self):
        """正常 SSE 流：收集 end 节点 inputs 并返回 JSON"""
        end_inputs = {"output": "你好！我是AI助手。"}
        sse_lines = [
            'data: {"steps":{"action":"end","node_id":"end_1","inputs":'
            + json.dumps(end_inputs, ensure_ascii=False)
            + "}}",
            "data: [DONE]",
        ]
        resp = self._mock_resp(iter_lines=sse_lines)
        with patch("requests.post", return_value=resp):
            result = wc.WorkflowClient.run_flow(
                flow_id="xxx",
                token="Bearer test",
                client_id="test",
            )
        self.assertEqual(result, json.dumps([end_inputs], ensure_ascii=False))

    def test_sse_empty_flow(self):
        """正常 SSE 流：无 end 节点输出"""
        sse_lines = [
            "data: [DONE]",
        ]
        resp = self._mock_resp(iter_lines=sse_lines)
        with patch("requests.post", return_value=resp):
            result = wc.WorkflowClient.run_flow(
                flow_id="xxx",
                token="Bearer test",
                client_id="test",
            )
        self.assertEqual(result, "")

    # ── 场景 4：HTTP 错误 ──

    def test_http_401(self):
        """HTTP 层面 401 未授权"""
        resp = self._mock_resp(
            status=401,
            content_type="application/json",
            body={"code": 401, "message": "未授权"},
        )
        with patch("requests.post", return_value=resp):
            with self.assertRaises(Exception):
                wc.WorkflowClient.run_flow(
                    flow_id="xxx",
                    token="Bearer bad",
                    client_id="test",
                )

    # ── 场景 5：运行过程不输出中间信息 ──

    def test_no_stderr_during_run(self):
        """运行过程不向 stderr 输出鉴权或进度信息"""
        sse_lines = [
            'data: {"steps":{"action":"end","node_id":"end_1","inputs":{"text":"hi"}}}',
            "data: [DONE]",
        ]
        resp = self._mock_resp(iter_lines=sse_lines)

        with patch("requests.post", return_value=resp):
            stderr = StringIO()
            stdout = StringIO()
            old_stderr = sys.stderr
            old_stdout = sys.stdout
            sys.stderr = stderr
            sys.stdout = stdout
            try:
                wc.WorkflowClient.run_flow(
                    flow_id="xxx",
                    token="Bearer test",
                    client_id="test",
                )
            finally:
                sys.stderr = old_stderr
                sys.stdout = old_stdout
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
