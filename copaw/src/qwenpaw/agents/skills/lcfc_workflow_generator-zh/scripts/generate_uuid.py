#!/usr/bin/env python3
"""
LCFC Workflow UUID Generator
生成符合 RFC 4122 标准的 UUID (v4)，用于工作流节点ID。
输出格式：节点类型::uuid
用法：
    python generate_uuid.py <node_type> [count]
示例：
    python generate_uuid.py llm
    python generate_uuid.py start 3
"""
import uuid
import sys

def generate_node_ids(node_type: str, count: int = 1) -> list[str]:
    """生成指定数量的节点ID"""
    return [f"{node_type}::{uuid.uuid4().hex}" for _ in range(count)]

def generate_uuid() -> str:
    """生成纯 UUID hex 字符串"""
    return uuid.uuid4().hex

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python generate_uuid.py <node_type> [count]")
        print("       python generate_uuid.py --uuid")
        sys.exit(1)
    
    if sys.argv[1] == "--uuid":
        print(generate_uuid())
    else:
        node_type = sys.argv[1]
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        ids = generate_node_ids(node_type, count)
        for nid in ids:
            print(nid)
