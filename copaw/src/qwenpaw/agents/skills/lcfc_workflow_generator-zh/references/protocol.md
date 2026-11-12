# LCFC Workflow Engine 协议文档

> **版本**: v1.0.0 | **适用范围**: 联宝（LCFC）定制工作流协议构建
>
> 本文档定义了 LCFC Workflow Engine 的工作流协议结构，包括顶层协议、节点、边的完整规范，以及多种已测试通过的示例。

---

## 目录

- [一、背景](#一背景)
- [二、设计方案](#二设计方案)
  - [3.1 协议概览](#31-协议概览)
  - [3.2 节点](#32-节点)
    - [节点整体结构](#节点整体结构)
    - [节点元数据结构](#节点元数据结构)
    - [节点入参结构](#节点入参结构)
    - [节点出参结构](#节点出参结构)
    - [节点配置参数](#节点配置参数)
  - [3.3 边](#33-边)
- [三、完整示例](#三完整示例)
  - [示例1：大模型节点连接（线性流）](#示例-1大模型节点连接-startllmend-线性流)
  - [示例2：分支并行决策（并行流）](#示例-2分支并行决策-startmessagellmend-并行流)
  - [示例3：大模型和分支连接（分支流）](#示例-3大模型和分支连接-startif-elsellmend-分支流)
  - [示例4：消息和分支节点连接（分支流）](#示例-4消息和分支节点连接-startif-elsemessageend-分支流)
  - [示例5：消息和大模型节点连接（并行流）](#示例-5消息和大模型节点连接-startmessagellmend-并行流)
  - [示例6：代码解释器（工具流）](#示例-6代码解释器-startcodeend-工具流)
  - [示例7：多决策大模型分支（多分支并行流）](#示例-7多决策大模型分支-startdecision4llmend-多分支并行流)
  - [示例8：代码消息分支大模型（复合流）](#示例-8代码消息分支大模型-startcodellmif-elsellm1llm2end-复合流)

---

## 一、背景

> 工作流引擎需要标准的协议来定义节点和边的关系。本文档定义了 LCFC Workflow Engine 协议规范。

## 二、设计方案

### 3.1 协议概览

工作流协议顶层结构如下：

| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| id | string | 是 | flow id |
| name | string | 是 | 协议名称 |
| description | string | 是 | 描述 |
| version | string | 是 | 版本 默认 v1.0.0 |
| data | object | 是 | 协议数据 |
| |— nodes | array | 是 | 节点信息，参见：[节点](#32-节点) |
| |— edges | array | 是 | 边信息，参见：[边](#33-边) |

**顶层 JSON 结构示例：**

```json
{
  "id": "唯一ID（数字字符串）",
  "name": "工作流名称",
  "description": "工作流描述",
  "version": "v1.0.0",
  "data": {
    "nodes": [...],
    "edges": [...]
  }
}
```

---

### 3.2 节点

#### 节点整体结构

| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| id | string | 是 | 节点id，拼接方式: 节点名称`::`uuid，举例: llm::5f9ccf09-58c7-422c-833b-8797e8db6388 |
| data | object | 是 | 节点信息 |
| |— node_meta | object | 是 | 节点元数据，参见：[节点元数据结构](#节点元数据结构) |
| |— inputs | array[object] | 是 | 节点输入，参见：[节点入参结构](#节点入参结构) |
| |— outputs | array[object] | 是 | 节点输出，参见：[节点出参结构](#节点出参结构) |
| |— node_param | object | 是 | 节点参数配置，参见：[节点配置参数](#节点配置参数) |

**节点 ID 格式：** `节点类型::uuid32位hex`（如 `llm::5f9ccf09-58c7-422c-833b-8797e8db6388`）

**节点类型分类：**

| node_meta.node_type | 适用场景 | 示例节点 |
|:---|:---|:---|
| 基础节点 | 大多数通用节点 | start, end, llm, message, decision |
| 分支节点 | 条件判断/路由 | if-else |
| 工具节点 | 外部工具调用 | code（代码解释器） |

---

#### 节点元数据结构

| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| node_type | string | 否 | 节点类型 |
| alias_name | string | 是 | 节点别名，用户自定义节点名字 |

> node_type 为可选字段，但建议始终填写以利于识别节点分类。

---

#### 节点入参结构

| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| name | string | 是 | 参数变量名 |
| schema | object | 是 | 参数规范 |
| |— type | string | 是 | 变量值的类型，枚举：string, integer, number, array, object, boolean |
| |— value | object | 是 | 变量值规范 |
| |—— type | string | 是 | 变量值类型，枚举：`ref`（引用其他节点输出值）、`literal`（透传值） |
| |—— content | string/object | 是 | type=`ref`时: `{"node_id": "llm::xxxxxxx", "name": "xxxxx"}`；type=`literal`时: `content = "1"` |

---

#### 节点出参结构

| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| name | string | 是 | 参数变量名 |
| schema | object | 是 | 参数出参规范，参见：[JSON Schema 协议](https://json-schmea.apifox.cn) |
| required | boolean | 否 | 是否必须 |

---

#### 节点配置参数

各节点的 `node_param` 不同，详情请查阅同级目录下的 [nodes.md](./nodes.md) 文件。

| 节点类型 | 核心参数（参考） |
|:---|:---|
| start | `enable_file_upload`, `file_output_name` |
| end | `output_mode`(0/1), `template` |
| llm | `model`, `template`, `api_base`, `temperature`, `max_tokens` 等 |
| message | `output_message`, `stream` |
| code | `code`, `language` |
| if-else | `cases[]`（含 level, logical_operator, conditions） |
| decision | `model`, `prompt_prefix`, `intent_chains[]` |
| 其他 | 详见 nodes.md |

---

### 3.3 边

| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| source_node_id | string | 是 | 源节点ID |
| target_node_id | string | 是 | 目标节点ID |
| source_handle | string | 否 | 用于分支节点（决策节点和if-else节点），用于标识其不同分支。示例：`intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a641` |

**边连接规则：**

| 场景 | source_handle | 说明 |
|:---|:---|:---|
| 普通节点连接 | 无 | 直接 source→target |
| decision 分支 | `intent_chain|{intent_chains[].id}` | 必须携带 |
| if-else 分支 | `intent_chain|{cases[].id}` | 必须携带 |
| 并行连接 | 无 | 多 source 连同一 target |

---

## 三、完整示例

> 以下示例均经过 **LCFC Workflow Engine 测试验证通过**，可作为构建工作流协议的标准参考。

### 示例一览

| 编号 | 名称 | 核心模式 | 拓扑 | 涉及节点 |
|:---:|:---|:---|:---|:---|
| 1 | 大模型节点连接 | 线性流 | start→llm→end | start, llm, end |
| 2 | 分支并行决策 | 并行流 | start→message+llm(并行)→end | start, message, llm, end |
| 3 | 大模型和分支 | 分支流 | start→if-else→llm/end | start, if-else, llm, end |
| 4 | 消息和分支 | 分支流 | start→if-else→message/end | start, if-else, message, end |
| 5 | 消息和大模型 | 并行流 | start→message+llm(并行)→end | start, message, llm, end |
| 6 | 代码解释器 | 工具流 | start→code→end | start, code, end |
| 7 | 多决策大模型分支 | 多分支并行流 | start→decision→4×llm→end | start, decision, llm×4, end |
| 8 | 代码消息分支大模型 | 复合流 | start→code→llm→if-else→llm1,llm2→end | start, code, llm, if-else, llm×2, end |

---


## 一、背景
## 二、设计方案
### 3.1 协议概览
| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| id | string | 是 | flow id |
| name | string  | 是 | 协议名称 |
| description | string | 是 | 描述 |
| version | string | 是 | 版本 默认 v1.0.0 |
| data | object | 是 | 协议数据 |
| |— nodes | array | 是 | 节点信息<br/>> 参见：[节点](#节点)<br/> |
| |— edges | array | 是 | 边信息<br/>> 参见：[边](#边) <br/> |


### 3.2 节点
#### 3.2.1 节点结构设计
##### 3.2.1.1 节点整体结构
| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| id | string | 是 | 节点id<br/>拼接方式: 节点名称`::`uuid<br/>> 举例: llm::5f9ccf09-58c7-422c-833b-8797e8db6388<br/> |
| data | object | 是 | 节点信息 |
| |— node_meta | object | 是 | 节点元数据<br/>> 参见：[节点元数据结构](#节点元数据结构)<br/> |
| |— inputs | array[object] | 是 | 节点输入<br/>> 参见：[节点入参结构](#节点入参结构)<br/> |
| |— outputs | array[object] | 是 | 节点输出<br/>> 参见：[节点出参结构](#节点出参结构)<br/> |
| |— node_param | object | 是 | 节点参数配置<br/>> 参见：[节点参数配置](#节点参数配置)<br/> |


##### 3.2.1.2 节点元数据结构
| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| node_type | string | 否 | 节点类型 |
| alias_name | string | 是 | 节点别名，用户自定义节点名字 |


##### 3.2.1.3 节点入参结构
| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| name | string | 是 | 参数变量名 |
| schema | object | 是 | 参数规范 |
| |— type | string | 是 | 变量值的类型<br/>枚举：<br/>+ string<br/>+ integer<br/>+ number<br/>+ array<br/>+ object<br/>+ boolean |
| |— value | object | 是 | 变量值规范 |
| |—— type | string | 是 | 变量值类型<br/>枚举：<br/>+ `ref`: 引用，当前只引用其他节点输出值<br/>+ `literal`: 透传值 |
| |—— content | string/object | 是 | + type=`ref`<br/>```json {   "node_id": "llm::xxxxxxx",   "name": "xxxxx" } ```  + type=`literal`<br/>     `content = "1"` |


##### 3.2.1.4 节点出参结构
| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| name | string | 是 | 参数变量名 |
| schema | object | 是 | 参数出参规范<br/>> 参见：[json schema协议](https://json-schmea.apifox.cn)<br/> |
| required | boolean | 否 | 是否必须 |


##### 3.2.1.5 节点配置参数
+ 细节见节点设计

### 3.3 边
| 参数 | 类型 | 是否必须 | 备注 |
| --- | --- | --- | --- |
| source_node_id | string | 是 | 源节点ID |
| target_node_id | string | 是 | 目标节点ID |
| source_handle | string | 否 | 用于分支节点，比如`决策节点`和`ifelse`节点，用于标识其不同分支<br/>示例：`intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a641` |




## 三、示例

### **大模型节点连接示例**
```json
{
  "id": "12345677888655",
  "name": "示例",
  "description": "节点示例",
  "version": "v1.0.0",
  "data": {
    "edges": [
      {
        "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "target_node_id": "llm::db379de3-dc88-6bf6-a68f-103a1375a68f"
      },
      {
        "source_node_id": "llm::db379de3-dc88-6bf6-a68f-103a1375a68f",
        "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
      }
    ],
    "nodes": [
      {
        "id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "data": {
          "node_meta": {
            "node_type": "基础节点",
            "alias_name": "开始节点"
          },
          "inputs": [],
          "outputs": [
            {
              "name": "usr_input",
              "schema": {
                "type": "string"
              },
              "required": true,
            },
            {
              "name": "usr_info",
              "schema": {
                "type": "object",
                "properties": {
                  "name": {
                    "type": "string"
                  },
                  "age": {
                    "type": "integer"
                  }
                }
              },
              "required": false
            }
          ],
          "node_param": {}
        }
      },
      {
        "id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "data": {
          "node_meta": {
            "node_type": "基础节点",
            "alias_name": "结束节点"
          },
          "inputs": [
            {
              "name": "output0",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "llm::db379de3-dc88-6bf6-a68f-103a1375a68f",
                    "name": "text"
                  }
                }
              }
            },
            {
              "name": "output1",
              "schema": {
                "type": "integer",
                "value": {
                  "type": "literal",
                  "content": 1
                }
              }
            }
          ],
          "outputs": [],
          "node_param": {
          }
        }
      },
      {
        "id": "llm::db379de3-dc88-6bf6-a68f-103a1375a68f",
        "data": {
          "node_meta": {
            "node_type": "基础节点",
            "alias_name": "大模型节点"
          },
          "inputs": [
            {
              "name": "input",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                    "name": "usr_input"
                  }
                }
              }
            }
          ],
          "outputs": [
            {
              "name": "text",
              "schema": {
                "type": "string"
              },
              "required": true
            }
          ],
          "node_param": {
            "model": "gpt-4o-mini",
            "url": "https://openkey.cloud/v1",
            "domain": "",
            "temperature": 0.5,
            "app_id": "4eea9****",
            "api_key": "sk-************************************",
            "api_secret": "",
            "max_tokens": 1024,
            "uid": "103a1375a64f",
            "template": "你是一个人工客服，下面是用户问题\n{input_identifier} \n 需要处理的用户地址 {url}",
            "enable_chat_history": false
          }
        }
      }
    ]
  }
}
```


### **决策和分支节点连接示例(测试已通过)**
![画板](https://cdn.nlark.com/yuque/0/2025/jpeg/50962202/1736214234059-a5d41a9a-2c8e-453b-af81-4d12e0cdef7e.jpeg)

```json
{
  "id": "333333333333333333333",
  "name": "分支并行决策示例",
  "description": "分支并行决策示例",
  "version": "v1.0.0",
  "data": {
    "edges": [
        {
            "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
            "target_node_id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f"
        },
        {
            "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
            "target_node_id": "llm::db379de3-dc37-46f6-a47f-103a1375a60h"
        },
        {
            "source_node_id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f",
            "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a60f"
        },
        {
            "source_node_id": "llm::db379de3-dc37-46f6-a47f-103a1375a60h",
            "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a61f"
        },
        {
            "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a60f",
            "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a61f"
        }
    ],
    "nodes": [
        {
            "id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
            "data": {
                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "开始节点"
                },
                "inputs": [
                ],
                "outputs": [
                    {
                        "name": "usr_input",
                        "schema": {
                            "type": "string"
                        },
                        "required": True
                    }
                ],
                "node_param": {}
            }
        },
        {
            "id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f",
            "data": {
                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "消息节点"
                },
                "inputs": [
                    {
                        "name": "input",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    }
                ],
                "outputs": [
                    {
                        "name": "text",
                        "schema": {
                            "type": "string",
                            # "description": "xxxxxxx"
                        },
                        "required": True
                    }
                ],
                "node_param": {
                    # "stream": True,
                    "message": "111{{input}}"
                }
            }
        },
        {
            "id": "llm::db379de3-dc37-46f6-a47f-103a1375a60h",
            "data": {


                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "大模型节点"
                },
                "inputs": [
                    {
                        "name": "input",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    }
                ],
                "outputs": [
                    {
                        "name": "text",
                        "schema": {
                            "type": "string"
                        },
                        "required": True
                    }
                ],
                "node_param": {
                    "model": "gpt-4o-mini",
                    "api_base": "https://openkey.cloud/v1",
                    "api_key": "YOUR_API_KEY",
                    "temperature": 0.1,
                    "app_id": "1234567",
                    "api_secret": "xxxxxxxx",
                    "max_tokens": 1024,
                    "uid": "12",
                    "template": "{{input}}",
                    "enable_chat_history": False
                }
            }
        },
        {
            "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a60f",
            "data": {
                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "大模型节点"
                },
                "inputs": [
                    {
                        "name": "input",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    }
                ],
                "outputs": [
                    {
                        "name": "text",
                        "schema": {
                            "type": "string"
                        },
                        "required": True
                    }
                ],
                "node_param": {
                    "model": "gpt-4o-mini",
                    "api_base": "https://openkey.cloud/v1",
                    "api_key": "YOUR_API_KEY",
                    "temperature": 0.1,
                    "app_id": "1234567",
                    "api_secret": "xxxxxxxx",
                    "max_tokens": 1024,
                    "uid": "12",
                    "template": "{{input}}",
                    "enable_chat_history": False
                }
            }
        },
        {
            "id": "end::db379de3-dc37-4bf6-a47f-103a1375a61f",
            "data": {
                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "结束节点"
                },
                "inputs": [
                    {
                        "name": "output0",
                        "schema": {
                            "type": "integer",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a60f",
                                    "name": "text"
                                }
                            }
                        }
                    },
                    {
                        "name": "output1",
                        "schema": {
                            "type": "integer",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "llm::db379de3-dc37-46f6-a47f-103a1375a60h",
                                    "name": "text"
                                }
                            }
                        }
                    }
                ],
                "outputs": [],
                "node_param": {
                    "template": "{{output0}}-{{output1}}",
                    "output_mode": 0,
                    "output_message": "{{output0}}-{{output1}}"
                }
            }
        }
    ]
}
```

### 大模型和分支连接示例（测试已通过）
![画板](https://cdn.nlark.com/yuque/0/2025/jpeg/50962202/1736234825613-2f9ad4a9-58f9-42da-abbb-9bba2f898eac.jpeg)

```json
{
  "id": "333333333333333333333",
  "name": "分支并行LLM示例",
  "description": "分支并行LLM示例",
  "version": "v1.0.0",
  "data": {
    "edges": [
      {
        "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "target_node_id": "if-else::db379de3-dc88-6bf6-a68f-103a1375a68f"
      },
      {
        "source_node_id": "if-else::db379de3-dc37-4bf6-a47f-103a1375a68f",
        "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a641",
        "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a64f"
      },
      {
        "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
      },
      {
        "source_node_id": "if-else::db379de3-dc37-4bf6-a47f-103a1375a6f",
        "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a641"，
        "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
      }
    ],
    "nodes": [
      {
        "id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "data": {
          "node_meta": {
            "node_type": "基础节点",
            "alias_name": "开始节点"
          },
          "inputs": [],
          "outputs": [
            {
              "name": "usr_input",
              "schema": {
                "type": "string"
              },
              "required": true,
            },
            {
              "name": "usr_info",
              "schema": {
                "type": "object",
                "properties": {
                  "name": {
                    "type": "string"
                  },
                  "age": {
                    "type": "integer"
                  }
                }
              },
              "required": false
            }
          ],
          "node_param": {}
        }
      },
      {
        "id": "if-else::831c1984-9de2-4b55-9e0d-d23a87282539",
        "data": {
          "node_meta": {
            "node_type": "分支节点",
            "alias_name": "if-else节点"
          },
          "inputs": [
            {
              "name": "uuid",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "start::6fca1c6b-d6b7-4cd3-9b3c-be735f3756ef",
                    "name": "input"
                  }
                }
              }
            },
            {
              "name": "uuid",
              "schema": {
                "type": "string",
                "value": {
                  "type": "literal",
                  "content": "hello"
                }
              }
            },
            {
              "name": "uuid",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "start::6fca1c6b-d6b7-4cd3-9b3c-be735f3756ef",
                    "name": "input"
                  }
                }
              }
            },
            {
              "name": "uuid",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "start::6fca1c6b-d6b7-4cd3-9b3c-be735f3756ef",
                    "name": "input"
                  }
                }
              }
            }
          ],
          "outputs": [],
          "node_param": {
            "cases": [
              {
                "id": "branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a641",
                "level": 1,
                "logical_operator": "and",
                "conditions": [
                  {
                    "left_var_index": "uuid",
                    "right_var_index": "uuid",
                    "compare_operator": "start_with"
                  },
                  {
                    "left_var_index": "uuid",
                    "right_var_index": "uuid",
                    "compare_operator": "end_with"
                  }
                ]
              },
              {
                "id": "branch_one_of::db379de3-dc37-4bf6-a47f-375a64f103a1",
                "level": 999,
                "logical_operator": "and",
                "conditions": []
              }
            ]
          }
        }
      },
      {
        "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "data": {
          "node_meta": {
            "node_type": "基础节点",
            "alias_name": "大模型节点"
          },
          "inputs": [
            {
              "name": "input_identifier",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "llm::71887049-29c7-4495-95a0-7b0c837f203a",
                    "name": "llm_result"
                  }
                }
              }
            },
            {
              "name": "url",
              "schema": {
                "type": "string",
                "value": {
                  "type": "literal",
                  "content": "https://www.lcfc.com.cn"
                }
              }
            }
          ],
          "outputs": [
            {
              "name": "output_identifier",
              "schema": {
                "type": "string"
              }
            },
            {
              "name": "key0",
              "schema": {
                "type": "string"
              }
            },
            {
              "name": "key1",
              "schema": {
                "type": "array",
                "items": {
                  "type": "string"
                }
              }
            }
          ],
          "node_param": {
            "model": "gpt-4o-mini",
            "api_base": "https://openkey.cloud/v1",
            "temperature": 0.5,
            "app_id": "4eea9****",
            "api_key": "sk-************************************",
            "api_secret": "",
            "max_tokens": 1024,
            "uid": "103a1375a64f",
            "template": "你是一个人工客服，下面是用户问题\n{input_identifier} \n 需要处理的用户地址 {url}",
            "enable_chat_history": False
          }
        }
      },
      {
        "id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "data": {
          "node_meta": {
            "node_type": "基础节点",
            "alias_name": "结束节点"
          },
          "inputs": [
            {
              "name": "output0",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "llm": "llm::71887049-29c7-4495-95a0-7b0c837f203a",
                    "name": "text"
                  }
                }
              }
            },
            {
              "name": "output1",
              "schema": {
                "type": "integer",
                "value": {
                  "type": "literal",
                  "content": 1
                }
              }
            }
          ],
          "outputs": [],
          "node_param": {
          }
        }
      }
    ]
  }
}
```

### 消息和分支节点连接实例（测试已通过）
![画板](https://cdn.nlark.com/yuque/0/2025/jpeg/50962202/1736126723797-16e7088f-af70-4e44-a785-07e00459caac.jpeg)



```json
{
  "id": "4444444444444444444",
  "name": "消息和分支示例",
  "description": "消息和分支节点示例",
  "version": "v1.0.0",
  "data": {
    "edges": [
      {
        "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
        "target_node_id": "if-else::db379de3-dc88-6bf6-a68f-103a1375a68f"
      },
      {
        "source_node_id": "if-else::db379de3-dc88-6bf6-a68f-103a1375a68f",
        "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a641",
        "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a61f"
      },
      {
        "source_node_id": "if-else::db379de3-dc88-6bf6-a68f-103a1375a68f",
        "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-375a64f103a1",
        "target_node_id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f"
      },
      {
        "source_node_id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a61f"
      }
    ],
    "nodes": [
      {
        "id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
        "data": {
          "node_meta": {
            "node_type": "基础节点",
            "alias_name": "开始节点"
          },
          "inputs": [
          ],
          "outputs": [
            {
              "name": "usr_input",
              "schema": {
                "type": "string"
              },
              "required": True
            }
          ],
          "node_param": {}
        }
      },
      {
        "id": "if-else::db379de3-dc88-6bf6-a68f-103a1375a68f",
        "data": {
          "node_meta": {
            "node_type": "分支节点",
            "alias_name": "if-else节点"
          },
          "inputs": [
            {
              "name": "uuid1",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                    "name": "usr_input"
                  }
                }
              }
            },
            {
              "name": "uuid2",
              "schema": {
                "type": "string",
                "value": {
                  "type": "literal",
                  "content": "hello"
                }
              }
            },
            {
              "name": "uuid3",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                    "name": "usr_input"
                  }
                }
              }
            },
            {
              "name": "uuid4",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                    "name": "usr_input"
                  }
                }
              }
            }
          ],
          "outputs": [
            {
              "name": "uuid1",
              "schema": {
                "type": "string"
              },
              "required": True
            },
            {
              "name": "uuid2",
              "schema": {
                "type": "string"
              },
              "required": True
            },
            {
              "name": "uuid3",
              "schema": {
                "type": "string"
              },
              "required": True
            },
            {
              "name": "uuid4",
              "schema": {
                "type": "string"
              },
              "required": True
            }
          ],
          "node_param": {
            "cases": [
              {
                "id": "branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a641",
                "level": 1,
                "logical_operator": "and",
                "conditions": [
                  {
                    "left_var_index": "uuid1",
                    "right_var_index": "uuid2",
                    "compare_operator": "start_with"
                  },
                  {
                    "left_var_index": "uuid3",
                    "right_var_index": "uuid4",
                    "compare_operator": "end_with"
                  }
                ]
              },
              {
                "id": "branch_one_of::db379de3-dc37-4bf6-a47f-375a64f103a1",
                "level": 999,
                "logical_operator": "and",
                "conditions": []
              }
            ]
          },
          "logging": {
            "enabled": True,
            "level": "DEBUG",
            "message": "Processing if-else node with inputs: uuid1={uuid1}, uuid2={uuid2}, uuid3={uuid3}, uuid4={uuid4}"
          }
        }
      },
      {
        "id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f",
        "data": {
          "node_meta": {
            "node_type": "基础节点",
            "alias_name": "消息节点"
          },
          "inputs": [
            {
              "name": "input",
              "schema": {
                "type": "string",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                    "name": "usr_input"
                  }
                }
              }
            }
          ],
          "outputs": [
            {
              "name": "text",
              "schema": {
                "type": "string",
                # "description": "xxxxxxx"
              },
              "required": True
            }
          ],
          "node_param": {
            # "stream": True,
            "message": "111{{input}}"
          }
        }
      },
      {
        "id": "end::db379de3-dc37-4bf6-a47f-103a1375a61f",
        "data": {
          "node_meta": {
            "node_type": "基础节点",
            "alias_name": "结束节点"
          },
          "inputs": [
            {
              "name": "output0",
              "schema": {
                "type": "integer",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f",
                    "name": "text"
                  }
                }
              }
            },
            {
              "name": "output1",
              "schema": {
                "type": "integer",
                "value": {
                  "type": "ref",
                  "content": {
                    "node_id": "if-else::db379de3-dc88-6bf6-a68f-103a1375a68f",
                    "name": "uuid1.uuid2.uuid3.uuid4"
                  }
                }
              }
            }
          ],
          "outputs": [],
          "node_param": {
            "template": "{{output0}}-{{output1}}",
            "output_mode": 0,
            "output_message": "{{output0}}-{{output1}}"
          }
        }
      }
    ]
  }
```

### 消息和大模型节点连接示例（测试已通过）


![画板](https://cdn.nlark.com/yuque/0/2025/jpeg/50962202/1735899523786-410adc2e-492a-466f-8c20-750863461c31.jpeg)





```json
  "id": "55555555555555555555",
  "name": "消息和大模型示例",
  "description": "消息和大模型节点示例",
  "version": "v1.0.0",
  "data": {
    "edges": [
          {
              "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
              "target_node_id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f"
          },
          {
              "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
              "target_node_id": "llm::db379de3-dc37-46f6-a47f-103a1375a60h"
          },
          {
              "source_node_id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f",
              "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a60f"
          },
          {
              "source_node_id": "llm::db379de3-dc37-46f6-a47f-103a1375a60h",
              "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a61f"
          },
          {
              "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a60f",
              "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a61f"
          }
      ],
      "nodes": [
          {
              "id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
              "data": {
                  "node_meta": {
                      "node_type": "基础节点",
                      "alias_name": "开始节点"
                  },
                  "inputs": [
                  ],
                  "outputs": [
                      {
                          "name": "usr_input",
                          "schema": {
                              "type": "string"
                          },
                          "required": True
                      }
                  ],
                  "node_param": {}
              }
          },
          {
              "id": "message::db379de3-dc37-4bf6-a47f-103a1375a64f",
              "data": {
                  "node_meta": {
                      "node_type": "基础节点",
                      "alias_name": "消息节点"
                  },
                  "inputs": [
                      {
                          "name": "input",
                          "schema": {
                              "type": "string",
                              "value": {
                                  "type": "ref",
                                  "content": {
                                      "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                                      "name": "usr_input"
                                  }
                              }
                          }
                      }
                  ],
                  "outputs": [
                      {
                          "name": "text",
                          "schema": {
                              "type": "string",
                              # "description": "xxxxxxx"
                          },
                          "required": True
                      }
                  ],
                  "node_param": {
                      # "stream": True,
                      "message": "111{{input}}"
                  }
              }
          },
          {
              "id": "llm::db379de3-dc37-46f6-a47f-103a1375a60h",
              "data": {
  
  
                  "node_meta": {
                      "node_type": "基础节点",
                      "alias_name": "大模型节点"
                  },
                  "inputs": [
                      {
                          "name": "input",
                          "schema": {
                              "type": "string",
                              "value": {
                                  "type": "ref",
                                  "content": {
                                      "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                                      "name": "usr_input"
                                  }
                              }
                          }
                      }
                  ],
                  "outputs": [
                      {
                          "name": "text",
                          "schema": {
                              "type": "string"
                          },
                          "required": True
                      }
                  ],
                  "node_param": {
                      "model": "gpt-4o-mini",
                      "api_base": "https://openkey.cloud/v1",
                      "api_key": "YOUR_API_KEY",
                      "temperature": 0.1,
                      "app_id": "1234567",
                      "api_secret": "xxxxxxxx",
                      "max_tokens": 1024,
                      "uid": "12",
                      "template": "{{input}}",
                      "enable_chat_history": False
                  }
              }
          },
          {
              "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a60f",
              "data": {
                  "node_meta": {
                      "node_type": "基础节点",
                      "alias_name": "大模型节点"
                  },
                  "inputs": [
                      {
                          "name": "input",
                          "schema": {
                              "type": "string",
                              "value": {
                                  "type": "ref",
                                  "content": {
                                      "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a60f",
                                      "name": "usr_input"
                                  }
                              }
                          }
                      }
                  ],
                  "outputs": [
                      {
                          "name": "text",
                          "schema": {
                              "type": "string"
                          },
                          "required": True
                      }
                  ],
                  "node_param": {
                      "model": "gpt-4o-mini",
                      "api_base": "https://openkey.cloud/v1",
                      "api_key": "YOUR_API_KEY",
                      "temperature": 0.1,
                      "app_id": "1234567",
                      "api_secret": "xxxxxxxx",
                      "max_tokens": 1024,
                      "uid": "12",
                      "template": "{{input}}",
                      "enable_chat_history": False
                  }
              }
          },
          {
              "id": "end::db379de3-dc37-4bf6-a47f-103a1375a61f",
              "data": {
                  "node_meta": {
                      "node_type": "基础节点",
                      "alias_name": "结束节点"
                  },
                  "inputs": [
                      {
                          "name": "output0",
                          "schema": {
                              "type": "integer",
                              "value": {
                                  "type": "ref",
                                  "content": {
                                      "node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a60f",
                                      "name": "text"
                                  }
                              }
                          }
                      },
                      {
                          "name": "output1",
                          "schema": {
                              "type": "integer",
                              "value": {
                                  "type": "ref",
                                  "content": {
                                      "node_id": "llm::db379de3-dc37-46f6-a47f-103a1375a60h",
                                      "name": "text"
                                  }
                              }
                          }
                      }
                  ],
                  "outputs": [],
                  "node_param": {
                      "template": "{{output0}}-{{output1}}",
                      "output_mode": 0,
                      "output_message": "{{output0}}-{{output1}}"
                  }
              }
          }
      ]
  }
}

```

### 代码解释器示例（测试通过）
![画板](https://cdn.nlark.com/yuque/0/2025/jpeg/51281731/1736155673864-f7643234-69f3-4187-9769-07f2d8d8cd9a.jpeg)

```json
{  
  "id": "66666666666666666",
  "name": "代码解释器示例",
  "description": "代码解释器节点示例",
  "version": "v1.0.0",
  "edges": [
    {
      "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
      "target_node_id": "code::831c1984-9de2-4b55-9e0d-d23a87282539"
    },
    {
      "source_node_id": "code::831c1984-9de2-4b55-9e0d-d23a87282539",
      "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
    }
  ],
  "nodes": [
    {
      "id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
      "data": {
        "node_meta": {
          "node_type": "基础节点",
          "alias_name": "开始节点"
        },
        "inputs": [],
        "outputs": [
          {
            "name": "usr_input",
            "schema": {
              "type": "string"
            }
          }
        ],
        "node_param": {}
      }
    },
    {
      "id": "code::831c1984-9de2-4b55-9e0d-d23a87282539",
      "data": {
        "node_meta": {
          "node_type": "工具节点",
          "alias_name": "代码解释器"
        },
        "inputs": [
          {
            "name": "input_identifier",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                  "name": "usr_input"
                }
              }
            }
          }
        ],
        "outputs": [
          {
            "name": "code_return",
            "schema": {
              "type": "string"
            }
          }
        ],
        "node_param": {
          "code": "def main(arg1):\n  return {'code_return': arg1}\n",
          "app_id": "d23a87xxx",
          "uid": "9e0d87xxx",
          "args": {
            "arg1": "hello"
          }
        }
      }
    },
    {
      "id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f",
      "data": {
        "node_meta": {
          "node_type": "基础节点",
          "alias_name": "结束节点"
        },
        "inputs": [
          {
            "name": "output0",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                  "name": "usr_input"
                }
              }
            }
          },
          {
            "name": "output1",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "code::831c1984-9de2-4b55-9e0d-d23a87282539",
                  "name": "code_return"
                }
              }
            }
          }
        ],
        "outputs": [],
        "node_param": {
          "template": "{{output0}} 是一个 {{output1}}",
          "output_mode": 1,
          "output_message": "{{output0}} 是一个 {{output1}}"
        }
      }
    }
  ]
}
```

### 多决策大模型分支示例
![画板](https://cdn.nlark.com/yuque/0/2025/jpeg/51281731/1736236620936-88646415-a142-41aa-b3ed-fcd083f5c771.jpeg)

```json
{
  "id": "77777777777777777777",
  "name": "多决策大模型分支示例",
  "description": "多决策大模型分支示例",
  "version": "v1.0.0",
  "edges": [
    {
      "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
      "target_node_id": "decision::d4fdcad6-2640-45e6-8a63-e5b93f405c1e"
    },
    {
      "source_node_id": "decision::d4fdcad6-2640-45e6-8a63-e5b93f405c1e",
      "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a641",
      "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a641"
    },
    {
      "source_node_id": "decision::d4fdcad6-2640-45e6-8a63-e5b93f405c1e",
      "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a642",
      "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a642"
    },
    {
      "source_node_id": "decision::d4fdcad6-2640-45e6-8a63-e5b93f405c1e",
      "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a643",
      "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a643"
    },
    {
      "source_node_id": "decision::d4fdcad6-2640-45e6-8a63-e5b93f405c1e",
      "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a644",
      "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a644"
    },
    {
      "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a641",
      "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
    },
    {
      "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a642",
      "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
    },
    {
      "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a643",
      "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
    },
    {
      "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a644",
      "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
    }
  ],
  "nodes": [
    {
      "id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
      "data": {
        "node_meta": {
          "node_type": "基础节点",
          "alias_name": "开始节点"
        },
        "inputs": [],
        "outputs": [
          {
            "name": "usr_input",
            "schema": {
              "type": "string"
            }
          }
        ],
        "node_param": {}
      }
    },
    {
      "id": "decision::d4fdcad6-2640-45e6-8a63-e5b93f405c1e",
      "data": {
        "node_meta": {
          "node_type": "基础节点",
          "alias_name": "决策节点"
        },
        "inputs": [
          {
            "name": "input_identifier",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                  "name": "usr_input"
                }
              }
            }
          }
        ],
        "outputs": [
          {
            "name": "class_name",
            "schema": {
              "type": "string"
            }
          },
          {
            "name": "output",
            "schema": {
              "type": "string"
            }
          }
        ],
        "node_param": {
          "model": "",
          "temperature": 0.5,
          "app_id": "",
          "api_base": "",
          "api_key": "",
          "api_secret": "",
          "max_tokens": 1024,
          "uid": "",
          "prompt_prefix": "你是一个问题识别专家，你擅长识别各种问题类型：知识问答、内容生成、游戏创建、画图、其他",
          "enable_chat_history": True,
          "intent_chains": [
            {
              "id": "branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a641",
              "name": "知识问答",
              "description": "用户输入一个需要回答的问题",
              "intent_type": 2
            },
            {
              "id": "branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a642",
              "name": "内容生成",
              "description": "用户输入一个需要进行：扩写、改写、缩写、丰富、完善、扩展等内容加工的指令",
              "intent_type": 2
            },
            {
              "id": "branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a643",
              "name": "游戏创建",
              "description": "当识别用户的意图为“主题构思与立意”，则为其生成主题构思与立意",
              "intent_type": 2
            },
            {
              "id": "branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a644",
              "name": "defalut",
              "description": "defalut",
              "intent_type": 1
            }
          ]
        }
      }
    },
    {
      "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a641",
      "data": {
        "node_meta": {
          "node_type": "基础节点",
          "alias_name": "大模型节点"
        },
        "inputs": [
          {
            "name": "input",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                  "name": "usr_input"
                }
              }
            }
          },
          {
            "name": "url",
            "schema": {
              "type": "string",
              "value": {
                "type": "literal",
                "content": "https://www.lcfc.com.cn"
              }
            }
          }
        ],
        "outputs": [
          {
            "name": "output",
            "schema": {
              "type": "string"
            }
          }
        ],
        "node_param": {
          "model": "gpt-4o-mini",
          "api_base": "https://openkey.cloud/v1",
          "temperature": 0.5,
          "app_id": "4eea9****",
          "api_key": "sk-************************************",
          "api_secret": "",
          "max_tokens": 1024,
          "uid": "103a1375a64f",
          "template": "你是一个知识问答类问题的优化专家，你需要考虑如下5个优化地方，并针对用户提出的问题{{input}}进行改写\n1. 明确问题：转写保障问题具体、明确。避免使用模糊或开放式的问题。\n2. 关键词突出：在prompt中突出关键词，帮助模型理解问题的核心。\n3. 提供上下文：如果问题涉及特定领域或背景知识，补充相关上下文。\n4. 使用正确的术语：使用专业术语。\n5. 限制问题范围：尽量使问题范围狭窄，给出针对性的回答。\n\n###输出示例：\n例子1：请详细解释黑洞的形成过程及其对周围天体的影响。\n例子2：请列举我国“十四五”规划期间的主要科技成就。\n例子3：请比较太阳能和风能作为可再生能源的优缺点。\n例子4：请说明光合作用的基本原理及其在生态系统中的作用。\n例子5：请定义5G网络并简要介绍其主要应用场景。",
          "enable_chat_history": False
        }
      }
    },
    {
      "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a642",
      "data": {
        "node_meta": {
          "node_type": "基础节点",
          "alias_name": "大模型节点"
        },
        "inputs": [
          {
            "name": "input",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                  "name": "usr_input"
                }
              }
            }
          },
          {
            "name": "url",
            "schema": {
              "type": "string",
              "value": {
                "type": "literal",
                "content": "https://www.lcfc.com.cn"
              }
            }
          }
        ],
        "outputs": [
          {
            "name": "output",
            "schema": {
              "type": "string"
            }
          }
        ],
        "node_param": {
          "model": "gpt-4o-mini",
          "api_base": "https://openkey.cloud/v1",
          "temperature": 0.5,
          "app_id": "4eea9****",
          "api_key": "sk-************************************",
          "api_secret": "",
          "max_tokens": 1024,
          "uid": "103a1375a64f",
          "template": "你是一个prompt提示词专家，你需要根据用户的提问进行改写，你需要按照以下方式进行改写和扩充。\n1.明确目标：清楚地说明你想要生成的内容类型和目的。\n2.提供详细信息：背景信息、主题、风格、格式要求等。\n3.设定具体要求：指定字数、段落结构、要点、关键词等。\n4.示例指导：提供一些示例或参考内容，帮助模型理解你的期望。\n5.使用指令性语言：告诉模型要做什么，而不是怎么做。\n###正确示例\n示例1：请撰写一篇关于“可持续生活方式”的博客文章。文章应该包括三个主要部分：可持续生活的定义、实践可持续生活的五个建议、以及可持续生活对环境和社会的影响。文章风格应该是教育性和启发性的，字数在1000到1200字之间。请确保文章结构清晰，包含引人入胜的开头和总结性的结尾。\n示例2：生成一条关于即将到来的产品发布的Twitter帖子。帖子需要吸引科技爱好者的注意，包含产品名称、发布日期和一句吸引人的标语。使用#hashtags来提高可见度，并保持语气活泼和引人入胜。\n用户的输入是{{input}}",
          "enable_chat_history": False
        }
      }
    },
    {
      "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a643",
      "data": {
        "node_meta": {
          "node_type": "基础节点",
          "alias_name": "大模型节点"
        },
        "inputs": [
          {
            "name": "input",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                  "name": "usr_input"
                }
              }
            }
          },
          {
            "name": "url",
            "schema": {
              "type": "string",
              "value": {
                "type": "literal",
                "content": "https://www.lcfc.com.cn"
              }
            }
          }
        ],
        "outputs": [
          {
            "name": "output",
            "schema": {
              "type": "string"
            }
          }
        ],
        "node_param": {
          "model": "gpt-4o-mini",
          "api_base": "https://openkey.cloud/v1",
          "temperature": 0.5,
          "app_id": "4eea9****",
          "api_key": "sk-************************************",
          "api_secret": "",
          "max_tokens": 1024,
          "uid": "103a1375a64f",
          "template": "## 游戏介绍：\n在这个{{input}}的故事中，主角是……，他……\n## 游戏设定：\n- 游戏共n轮，每轮会有一个角色提问，提问包含……\n- 玩家需对每一轮问题做出回答，你需要判断加减分数。\n- 每个角色的初始分值为0分，每轮加减0~20分。\n- 分数调整后，继续给出下一轮的场景和剧情，进入下一轮问答，由下一个角色发起新提问，等待玩家回答。\n- 5轮后，根据得分决定玩家的最终结果\n- 在第2轮，可以根据场景和角色描述生成一个状态情况，体现角色状态。\n## 角色设定：\n- A：……\n- B：……\n## 角色回复要求：\n- 保持中文语境，每次输出控制在50字以内。\n- 使用幽默和有挑战性的语言。\n- 每次给玩意一个提问，等待玩家回答\n- 角色的回复可以给玩家一个结合场景的想象空间\n## 注意\n-不要代替玩家做出回答\n-用户回答后进行分数挑战并继续提问，不要中断\n-以 markdown 格式输出，在适当的地方里面加入 emoji 表情\n用户的输入是{{input}}",
          "enable_chat_history": False
        }
      }
    },
    {
      "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a644",
      "data": {
        "node_meta": {
          "node_type": "基础节点",
          "alias_name": "大模型节点"
        },
        "inputs": [
          {
            "name": "input",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                  "name": "usr_input"
                }
              }
            }
          }
        ],
        "outputs": [
          {
            "name": "output",
            "schema": {
              "type": "string"
            }
          }
        ],
        "node_param": {
          "model": "gpt-4o-mini",
          "api_base": "https://openkey.cloud/v1",
          "temperature": 0.5,
          "app_id": "4eea9****",
          "api_key": "sk-************************************",
          "api_secret": "",
          "max_tokens": 1024,
          "uid": "103a1375a64f",
          "template": "根据我给出的需求，创建一份提示词模版，你熟悉[CRISPE提示框架]，并擅长将常规的Prompt转化为符合[CRISPE提示框架]的优秀Prompt，并输出符合预期的回复。\n###你的工作流程是：\n1. 你会先分析我提出的需求。\n2. 结合需求，根据[CRISPE提示框架]的要求，确定最适合扮演的角色，拟定角色设定、目标任务、需求说明、风格设定等信息。\n3. 综合我的问题(Prompt)的原因、背景和上下文，构建一个符合[CRISPE提示框架]的优秀Prompt，你结合用户提问将回复中的需求说明描绘详尽。\n4. 按照回复格式生成回复，确保回复符合预期。\n回复内容限制信息：\n1. 分析用户输入后，在需求说明中至少分为5个点描述需求内容。\n###你的回复格式为：\n角色设定：[你是一个形容词的和用户输入相关的身份]\n目标任务：[根据用户的输入明确目标任务]\n需求说明：[根据用户的输入按照CRISPE提示框架进行需求补充详尽]\n风格设定：[描述助手风格]\n###示例：\n角色设定：Prompt提示词生成专家\n目标任务：根据我给出的需求，创建一份提示词模版\n需求说明：\n1. 用户希望获得一些提示和建议，以提升他们的口语能力和沟通技巧。\n2. 他们需要针对各种情景和对话主题的提示词，以帮助他们更自信地表达自己并与他人进行有效的交流。\n3. 拥有更多提高效率的角度...\n4. ...\n5. ...\n风格设定：积极、鼓励，注重实用性和可操作性。\n接下来我的输入是：{{input}}",
          "enable_chat_history": False
        }
      }
    },
    {
      "id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f",
      "data": {
        "node_meta": {
          "node_type": "基础节点",
          "alias_name": "结束节点"
        },
        "inputs": [
          {
            "name": "key1",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a641",
                  "name": "llm_result"
                }
              }
            }
          },
          {
            "name": "key2",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a642",
                  "name": "llm_result"
                }
              }
            }
          },
          {
            "name": "key3",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a643",
                  "name": "llm_result"
                }
              }
            }
          },
          {
            "name": "key4",
            "schema": {
              "type": "string",
              "value": {
                "type": "ref",
                "content": {
                  "node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a644",
                  "name": "llm_result"
                }
              }
            }
          }
        ],
        "outputs": [],
        "node_param": {
          "template": "{{key1}} \n{{key2}} \n{{key3}} \n{{key4}}",
          "output_mode": 1,
          "stream": False
        }
      }
    }
  ]
}
```

### 代码消息分支大模型示例(测试通过)
![画板](https://cdn.nlark.com/yuque/0/2025/jpeg/51281731/1736302056438-0b7132f1-e33c-452d-944f-e5e96d710291.jpeg)

```json
{
    "id": "8888888888888888888",
    "name": "代码消息分支大模型示例",
    "description": "代码消息分支大模型示例",
    "version": "v1.0.0",
    "edges": [
        {
            "source_node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
            "target_node_id": "code::831c1984-9de2-4b55-9e0d-d23a87282539"
        },
        {
            "source_node_id": "code::831c1984-9de2-4b55-9e0d-d23a87282539",
            "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a64f"
        },
        {
            "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a64f",
            "target_node_id": "if-else::831c1984-9de2-4b55-9e0d-d23a87282539"
        },
        {
            "source_node_id": "if-else::831c1984-9de2-4b55-9e0d-d23a87282539",
            "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a64f",
            "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a641"
        },
        {
            "source_node_id": "if-else::831c1984-9de2-4b55-9e0d-d23a87282539",
            "source_handle": "intent_chain|branch_one_of::db379de3-dc37-4bf6-a47f-375a64f103a1",
            "target_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a642"
        },
        {
            "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a641",
            "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
        },
        {
            "source_node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a642",
            "target_node_id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f"
        }
    ],
    "nodes": [
        {
            "id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
            "data": {
                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "开始节点"
                },
                "inputs": [],
                "outputs": [
                    {
                        "name": "usr_input",
                        "schema": {
                            "type": "string"
                        }
                    }
                ],
                "node_param": {}
            }
        },
        {
            "id": "code::831c1984-9de2-4b55-9e0d-d23a87282539",
            "data": {
                "node_meta": {
                    "node_type": "工具节点",
                    "alias_name": "代码解释器"
                },
                "inputs": [
                    {
                        "name": "input",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    }
                ],
                "outputs": [
                    {
                        "name": "current_time",
                        "schema": {
                            "type": "string"
                        }
                    }
                ],
                "node_param": {
                    "code": "from datetime import datetime\n\ndef main():\n    # 获取当前时间\n    now = datetime.now()\n    # 格式化为 yyyy-MM-dd 的形式\n    time = now.strftime('%Y-%m-%d')\n    system_time = {\n        \"current_time\": time\n    }\n    return system_time",
                    "app_id": "d23a87xxx",
                    "uid": "9e0d87xxx",
                    "args": {}
                }
            }
        },
        {
            "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a64f",
            "data": {
                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "大模型节点"
                },
                "inputs": [
                    {
                        "name": "input",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    },
                    {
                        "name": "current_time",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "code::831c1984-9de2-4b55-9e0d-d23a87282539",
                                    "name": "current_time"
                                }
                            }
                        }
                    }
                ],
                "outputs": [
                    {
                        "name": "end_time",
                        "schema": {
                            "type": "string"
                        },
                        "required": True
                    }
                ],
                "node_param": {
                    "model": "gpt-4o-mini",
                    "api_base": "https://openkey.cloud/v1",
                    "temperature": 0.5,
                    "app_id": "4eea9****",
                    "api_key": "YOUR_API_KEY",
                    "api_secret": "",
                    "max_tokens": 1024,
                    "uid": "103a1375a64f",
                    "template": "当前时间是{{current_time}},用户的问题是{{input}}。请基于以上信息输出资讯检索的结束时间，要求必须是yyyy-MM-dd结构，仅输出8位日期，不得有额外文本",
                    "enable_chat_history": False
                }
            }
        },
        {
            "id": "if-else::831c1984-9de2-4b55-9e0d-d23a87282539",
            "data": {
                "node_meta": {
                    "node_type": "分支节点",
                    "alias_name": "if-else节点"
                },
                "inputs": [
                    {
                        "name": "uuid1",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    },
                    {
                        "name": "uuid2",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "literal",
                                "content": "你好"
                            }
                        }
                    },
                    {
                        "name": "uuid3",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    },
                    {
                        "name": "uuid4",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    }
                ],
                "outputs": [
                    {
                        "name": "uuid1",
                        "schema": {
                            "type": "string"
                        },
                        "required": True
                    },
                    {
                        "name": "uuid2",
                        "schema": {
                            "type": "string"
                        },
                        "required": True
                    },
                    {
                        "name": "uuid3",
                        "schema": {
                            "type": "string"
                        },
                        "required": True
                    },
                    {
                        "name": "uuid4",
                        "schema": {
                            "type": "string"
                        },
                        "required": True
                    }
                ],
                "node_param": {
                    "cases": [
                        {
                            "id": "branch_one_of::db379de3-dc37-4bf6-a47f-103a1375a64f",
                            "level": 1,
                            "logical_operator": "and",
                            "conditions": [
                                {
                                    "left_var_index": "uuid1",
                                    "right_var_index": "uuid2",
                                    "compare_operator": "start_with"
                                },
                                {
                                    "left_var_index": "uuid3",
                                    "right_var_index": "uuid4",
                                    "compare_operator": "end_with"
                                }
                            ]
                        },
                        {
                            "id": "branch_one_of::db379de3-dc37-4bf6-a47f-375a64f103a1",
                            "level": 999,
                            "logical_operator": "and",
                            "conditions": []
                        }
                    ]
                }
            }
        },
        {
            "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a641",
            "data": {
                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "大模型节点"
                },
                "inputs": [
                    {
                        "name": "input",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    },
                    {
                        "name": "current_time",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "code::831c1984-9de2-4b55-9e0d-d23a87282539",
                                    "name": "current_time"
                                }
                            }
                        }
                    },
                    {
                        "name": "end_time",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a64f",
                                    "name": "end_time"
                                }
                            }
                        }
                    }
                ],
                "outputs": [
                    {
                        "name": "start_time",
                        "schema": {
                            "type": "string"
                        }
                    }
                ],
                "node_param": {
                    "model": "gpt-4o-mini",
                    "api_base": "https://openkey.cloud/v1",
                    "temperature": 0.5,
                    "app_id": "4eea9****",
                    "api_key": "YOUR_API_KEY",
                    "api_secret": "",
                    "max_tokens": 1024,
                    "uid": "103a1375a64f",
                    "template": "当前时间是{{current_time}},用户的问题是{{input}}，已知的结束时间是{{end_time}}。请基于以上信息输出资讯检索的开始时间，开始时间必须早于结束时间，至少早一天，日期结构要求必须是yyyy-MM-dd结构，仅输出8位日期，不得有额外文本",
                    "enable_chat_history": False
                }
            }
        },
        {
            "id": "llm::db379de3-dc37-4bf6-a47f-103a1375a642",
            "data": {
                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "大模型节点"
                },
                "inputs": [
                    {
                        "name": "input",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "start::db379de3-dc37-4bf6-a47f-103a1375a64f",
                                    "name": "usr_input"
                                }
                            }
                        }
                    }
                ],
                "outputs": [
                    {
                        "name": "llm_result",
                        "schema": {
                            "type": "string"
                        }
                    }
                ],
                "node_param": {
                    "model": "gpt-4o-mini",
                    "api_base": "https://openkey.cloud/v1",
                    "temperature": 0.5,
                    "app_id": "4eea9****",
                    "api_key": "YOUR_API_KEY",
                    "api_secret": "",
                    "max_tokens": 1024,
                    "uid": "103a1375a64f",
                    "template": "{{input}}",
                    "enable_chat_history": False
                }
            }
        },
        {
            "id": "end::db379de3-dc37-4bf6-a47f-103a1375a64f",
            "data": {
                "node_meta": {
                    "node_type": "基础节点",
                    "alias_name": "结束节点"
                },
                "inputs": [
                    {
                        "name": "llm_result1",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a641",
                                    "name": "start_time"
                                }
                            }
                        }
                    },
                    {
                        "name": "llm_result2",
                        "schema": {
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "node_id": "llm::db379de3-dc37-4bf6-a47f-103a1375a642",
                                    "name": "llm_result"
                                }
                            }
                        }
                    }
                ],
                "outputs": [],
                "node_param": {
                    "template": "{{llm_result1}}{{llm_result2}}",
                    "output_mode": 1,
                    "stream": False
                }
            }
        }
    ]
}
```

### 

