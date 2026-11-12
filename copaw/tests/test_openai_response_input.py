"""Responses wire compatibility checks; all SDK traffic stays in mocks."""

import copy
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import openai
import pytest
from agentscope.message import (
    Base64Source, DataBlock, Msg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock,
    URLSource,
)

from qwenpaw.providers.openai_provider import OpenAIProvider
from qwenpaw.providers.openai_response_provider import _normalize_response_input


def finish(coroutine):
    """Drive mock-only awaits without opening an event loop's socket pair."""
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("Mock-only coroutine unexpectedly waited for I/O")


@pytest.fixture
def model():
    provider = OpenAIProvider(
        id="project-test", name="Test", is_custom=True,
        api_key="test-secret-key", base_url="https://gateway.invalid/v1",
        custom_headers={"X-Project": "project-test"},
        extra_models=[{
            "id": "gpt-5.6-sol", "name": "gpt-5.6-sol", "max_tokens": 4096,
        }],
    )
    return provider.get_chat_model_instance("gpt-5.6-sol")


@pytest.fixture
def sdk(monkeypatch):
    response = SimpleNamespace(
        id="resp_test", usage=None,
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(
            type="output_text", text="done",
        )])],
    )
    create = AsyncMock(return_value=response)
    factory = Mock(return_value=SimpleNamespace(
        responses=SimpleNamespace(create=create),
    ))
    monkeypatch.setattr(openai, "AsyncClient", factory)
    return SimpleNamespace(create=create, factory=factory)


@pytest.mark.parametrize("role", ["system", "developer", "user", "assistant"])
@pytest.mark.parametrize("discriminator", [None, "", "message"])
def test_text_messages_are_explicit_without_changing_history(role, discriminator):
    part_type = "output_text" if role == "assistant" else "input_text"
    original = [{"type": discriminator, "role": role, "content": [
        {"type": part_type, "text": "first\n"},
        {"type": part_type, "text": "second"},
    ]}]
    before = copy.deepcopy(original)
    normalized = _normalize_response_input(original)
    assert normalized == [{
        "type": "message", "role": role, "content": "first\nsecond",
    }]
    assert original == before
    assert _normalize_response_input(normalized) == normalized


@pytest.mark.parametrize("image", [
    {"type": "input_image", "image_url": "https://media.invalid/image.png"},
    {"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8="},
    {"type": "input_image", "file_id": "file_image", "detail": "high"},
])
@pytest.mark.parametrize("file", [
    {"type": "input_file", "file_id": "file_pdf"},
    {"type": "input_file", "file_url": "https://media.invalid/report.pdf"},
    {"type": "input_file", "filename": "report.pdf", "file_data": "JVBERg=="},
])
def test_mixed_media_keeps_order_sources_and_explicit_detail(image, file):
    original = [{"role": "user", "content": [
        {"type": "input_text", "text": "Compare these"}, image, file,
    ]}]
    before = copy.deepcopy(original)
    result = _normalize_response_input(original)[0]
    expected_image = {"detail": "auto", **image}
    assert result == {"type": "message", "role": "user", "content": [
        before[0]["content"][0], expected_image, file,
    ]}
    assert original == before


def test_tools_reasoning_and_raw_output_keep_ids_metadata_and_order():
    items = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "content": [],
         "encrypted_content": "opaque-reasoning"},
        {"type": "function_call", "call_id": "call_a", "name": "read_file",
         "arguments": '{"path":"a.txt"}'},
        {"type": "function_call", "call_id": "call_b", "name": "read_file",
         "arguments": '{"path":"b.txt"}'},
        {"type": "function_call_output", "call_id": "call_b", "output": "B"},
        {"type": "function_call_output", "call_id": "call_a", "output": "A"},
        {"type": "message", "role": "assistant", "id": "msg_1",
         "status": "completed", "phase": "final_answer", "content": [
             {"type": "output_text", "text": "Read both", "annotations": []},
         ]},
    ]
    before = copy.deepcopy(items)
    assert _normalize_response_input(items) == before
    assert items == before


def test_cache_metadata_and_structured_tool_output_are_preserved():
    text = {"type": "input_text", "text": "cached instructions",
            "prompt_cache_breakpoint": {"mode": "explicit"}}
    tool_output = {"type": "function_call_output", "call_id": "call_image",
                   "output": [
                       {"type": "input_text", "text": "Screenshot"},
                       {"type": "input_image", "file_id": "file_image"},
                       {"type": "input_file", "file_id": "file_report"},
                   ]}
    before = copy.deepcopy(tool_output)
    result = _normalize_response_input([
        {"role": "system", "content": [text]}, tool_output,
    ])
    assert result[0]["content"] == [text]
    assert result[1]["call_id"] == "call_image"
    assert result[1]["output"] == [
        before["output"][0],
        {**before["output"][1], "detail": "auto"},
        before["output"][2],
    ]
    assert tool_output == before


@pytest.mark.parametrize("bad", [
    "private-prompt",
    {"type": "", "content": "private-prompt"},
    {"type": "not_a_type", "role": "user", "content": "private-prompt"},
    {"role": "tool", "content": "private-prompt"},
    {"role": [], "content": "private-prompt"},
    {"role": "user", "content": [{"type": {}, "text": "private-prompt"}]},
    {"role": "user", "content": [{"type": "input_text"}]},
    {"role": "user", "content": [{"type": "input_image"}]},
    {"role": "user", "content": [{"type": "input_file"}]},
    {"role": "assistant", "content": [
        {"type": "output_text", "text": "private-prompt", "annotations": []},
    ]},
    {"type": "message", "id": "msg_1", "status": "completed",
     "role": "assistant", "content": [
         {"type": "output_text", "text": "private-prompt"},
     ]},
    {"type": "function_call", "name": "read_file", "arguments": "{}"},
    {"type": "function_call_output", "output": "private-prompt"},
    {"type": "function_call_output", "call_id": "call_1", "output": [
        {"type": "input_image", "image_url": "https://media.invalid/x.png",
         "detail": "invalid"},
    ]},
    {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text"}]},
    {"type": "message", "role": "assistant", "id": "msg_1",
     "status": "completed", "content": [{"type": "unknown_block"}]},
])
def test_invalid_items_report_index_without_prompt_values(bad):
    with pytest.raises(ValueError, match=r"input\[1\]") as caught:
        _normalize_response_input([{"role": "user", "content": "ok"}, bad])
    assert "private-prompt" not in str(caught.value)


def test_normalization_happens_after_final_formatter_and_kwargs(model, sdk):
    final_items = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
        {"role": "user", "content": [
            {"type": "input_image", "image_url": "https://media.invalid/x.png"},
        ]},
    ]
    before = copy.deepcopy(final_items)
    formatter = SimpleNamespace(format=AsyncMock(return_value=final_items))
    model.formatter = formatter
    model.stream = False
    response = finish(model._call_api(
        "gpt-5.6-sol", [], reasoning_effort="high",
        modalities=["text"], audio={"voice": "test"},
    ))
    formatter.format.assert_awaited_once_with([])
    wire = sdk.create.call_args.kwargs
    assert [item["type"] for item in wire["input"]] == ["message"] * 3
    assert wire["input"][1]["content"] == "hi"
    assert wire["input"][2]["content"][0]["detail"] == "auto"
    assert wire["reasoning"] == {"effort": "high"}
    assert wire["max_output_tokens"] == 4096
    assert "audio" not in wire and "modalities" not in wire
    assert model.formatter is formatter and final_items == before
    assert response.content[0].text == "done"
    assert sdk.factory.call_args.kwargs["default_headers"] == {
        "X-Project": "project-test",
    }


@pytest.mark.parametrize("via_extra_body", [False, True])
def test_input_override_cannot_bypass_normalization(model, sdk, via_extra_body):
    model.stream = False
    payload = [{"role": "user", "content": "override"}]
    options = {"input": payload}
    if via_extra_body:
        options = {"extra_body": {**options, "keep": True}}
    before = copy.deepcopy(options)
    finish(model._call_api("gpt-5.6-sol", [], **options))
    wire = sdk.create.call_args.kwargs
    assert wire["input"] == [{
        "type": "message", "role": "user", "content": "override",
    }]
    assert "input" not in wire.get("extra_body", {})
    assert options == before


def test_real_formatter_keeps_tool_image_and_reasoning_correspondence(model, sdk):
    model.stream = False
    image_url = "https://media.invalid/screenshot.png"
    image = DataBlock(source=URLSource(url=image_url, media_type="image/png"))
    messages = [
        Msg(name="user", role="user", content=[TextBlock(text="Inspect"), image]),
        Msg(name="assistant", role="assistant", content=[
            ThinkingBlock(thinking="inspect image", reasoning_item_id="rs_1"),
            ToolCallBlock(id="call_1", name="screenshot", input="{}"),
        ]),
        Msg(name="tool", role="assistant", content=[ToolResultBlock(
            id="call_1", name="screenshot", output=[TextBlock(text="Captured"), image],
        )]),
    ]
    before = [message.model_dump() for message in messages]
    finish(model._call_api("gpt-5.6-sol", messages))
    items = sdk.create.call_args.kwargs["input"]
    assert [item["type"] for item in items] == [
        "message", "reasoning", "function_call", "function_call_output", "message",
    ]
    assert items[1]["id"] == "rs_1"
    assert items[2]["call_id"] == items[3]["call_id"] == "call_1"
    assert items[2]["name"] == "screenshot" and items[2]["arguments"] == "{}"
    assert "Captured" in items[3]["output"]
    for item in (items[0], items[4]):
        assert item["role"] == "user"
        images = [part for part in item["content"] if part["type"] == "input_image"]
        assert images == [{
            "type": "input_image", "image_url": image_url, "detail": "auto",
        }]
    assert [message.model_dump() for message in messages] == before


def test_actual_openai_sdk_stream_uses_mock_transport(model, monkeypatch):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        assert request.url.path == "/v1/responses"
        assert request.headers["X-Project"] == "project-test"
        events = [
            {"type": "response.output_text.delta", "delta": "Hello"},
            {"type": "response.output_item.added", "item": {
                "id": "fc_1", "type": "function_call", "call_id": "call_1",
                "name": "read_file", "arguments": "",
            }},
            {"type": "response.function_call_arguments.delta",
             "item_id": "fc_1", "delta": '{"path":"a.txt"}'},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=body + "data: [DONE]\n\n",
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    model.client_kwargs["http_client"] = client
    client_factory = openai.AsyncClient

    def make_client(**kwargs):
        sdk_client = client_factory(**kwargs)
        # Avoid SDK background OS detection; transport and SSE parsing are real,
        # but this test intentionally has no event loop or background I/O.
        sdk_client._platform = "Windows"
        return sdk_client

    monkeypatch.setattr(openai, "AsyncClient", make_client)

    async def collect():
        stream = await model._call_api("gpt-5.6-sol", [
            Msg(name="user", role="user", content=[TextBlock(text="hello")]),
        ])
        return [chunk async for chunk in stream]

    try:
        chunks = finish(collect())
    finally:
        finish(client.aclose())
    assert len(requests) == 1 and requests[0]["stream"] is True
    assert requests[0]["input"] == [{
        "type": "message", "role": "user", "content": "hello",
    }]
    assert chunks[0].content[0].text == "Hello"
    call = chunks[1].content[0]
    assert call.id == "call_1" and call.name == "read_file"
    assert call.input == '{"path":"a.txt"}'


def test_existing_media_cap_still_produces_valid_text(model, sdk):
    model.stream = False
    model.formatter.max_bytes = 1
    source = Base64Source(data="aGVsbG8=", media_type="image/png")
    message = Msg(name="user", role="user", content=[DataBlock(source=source)])
    before = message.model_dump()
    finish(model._call_api("gpt-5.6-sol", [message]))
    item = sdk.create.call_args.kwargs["input"][0]
    assert item["type"] == "message" and isinstance(item["content"], str)
    assert "omitted" in item["content"] and "inline limit" in item["content"]
    assert message.model_dump() == before


def test_status_error_and_diagnostics_do_not_log_request_content(model, sdk, caplog):
    response = httpx.Response(
        400, headers={"x-request-id": "req_test"},
        request=httpx.Request("POST", "https://gateway.invalid/v1/responses"),
    )
    sdk.create.side_effect = openai.BadRequestError(
        "private-prompt", response=response, body={"message": "private-prompt"},
    )
    caplog.set_level(logging.DEBUG, logger="qwenpaw.providers.openai_response_provider")
    with pytest.raises(openai.BadRequestError):
        finish(model._call_api("gpt-5.6-sol", [
            Msg(name="user", role="user", content=[TextBlock(text="private-prompt")]),
        ]))
    assert "message_types_added=1" in caplog.text
    assert "status=400 request_id=req_test" in caplog.text
    assert "private-prompt" not in caplog.text
    assert "test-secret-key" not in caplog.text
