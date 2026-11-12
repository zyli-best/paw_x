# -*- coding: utf-8 -*-
"""DevCo 打包入口：在 ``init --defaults --accept-security`` 且 PyInstaller 冻结态下预置 LLM。

由 ``fetch_qwenpaw`` 复制到 ``qwenpaw.cli``，并由 ``init_cmd`` 在读取全局 LLM 状态前调用。
环境变量与文档合并脚本一致：``OPENAI_BASE_URL``、``OPENAI_MODEL`` / ``LLM_MODEL``
（逗号分隔多个模型 id，依次写入 ``extra_models``，首个为默认激活）、
``OPENAI_API_KEY`` / ``LLM_API_KEY``；可选 ``DEVCO_BUNDLE_PROVIDER_ID``、
``DEVCO_BUNDLE_PROVIDER_NAME``。设置 ``DEVCO_INIT_SKIP_SEED=1`` 可跳过。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click

_DEFAULT_PROVIDER_ID = "devco-openai-compatible"
_QA_AGENT_ID = "QwenPaw_QA_Agent_0.2"


def _parse_seed_model_ids(raw: str) -> list[str]:
    """从 ``OPENAI_MODEL`` / ``LLM_MODEL`` 解析多个 id（逗号分隔），去重且保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        mid = part.strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def _extra_model_dict(model_id: str) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "supports_multimodal": None,
        "supports_image": None,
        "supports_video": None,
        "probe_source": None,
        "is_free": False,
        "generate_kwargs": {},
    }


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _sync_agent_active_model(
    *,
    agent_id: str,
    model_slot,
    load_agent_config,
    save_agent_config,
) -> bool:
    cfg = load_agent_config(agent_id)
    current = cfg.active_model
    if (
        current
        and current.provider_id == model_slot.provider_id
        and current.model == model_slot.model
    ):
        return False
    cfg.active_model = model_slot
    save_agent_config(agent_id, cfg)
    return True


def maybe_devco_bundle_seed_llm(
    *,
    use_defaults: bool,
    accept_security: bool,
    sync_agent_models: bool = False,
) -> None:
    """与 ``main.py`` 中 ``init_cmd.main(['--defaults', '--accept-security'])`` 配套：首次初始化时写入密钥目录并激活模型。"""
    if not sync_agent_models and not getattr(sys, "frozen", False):
        return
    if not (use_defaults and accept_security):
        return
    if os.environ.get("DEVCO_INIT_SKIP_SEED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return

    try:
        from qwenpaw.constant import SECRET_DIR
        from qwenpaw.config.config import ModelSlotConfig, load_agent_config, save_agent_config
        from qwenpaw.providers.provider_manager import ProviderManager
        from qwenpaw.security.secret_store import (
            PROVIDER_SECRET_FIELDS,
            encrypt_dict_fields,
        )
    except ImportError as exc:
        click.echo(f"[devco] init seed skipped (import): {exc}")
        return

    base_url = (
        os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    ).rstrip("/")
    raw_models = (
        os.environ.get("OPENAI_MODEL")
        or os.environ.get("LLM_MODEL")
        or "gpt-4o-mini"
    ).strip()
    model_ids = _parse_seed_model_ids(raw_models)
    model_id = model_ids[0] if model_ids else ""
    provider_id = (
        os.environ.get("DEVCO_BUNDLE_PROVIDER_ID") or _DEFAULT_PROVIDER_ID
    ).strip()
    if not provider_id or not model_ids:
        return

    active_path = SECRET_DIR / "providers" / "active_model.json"
    active_matches = False
    active_is_configured = False
    if active_path.is_file():
        try:
            data = json.loads(active_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                active_provider = str(data.get("provider_id") or "").strip()
                active_model = str(data.get("model") or "").strip()
                active_is_configured = bool(active_provider and active_model)
                active_matches = (
                    active_provider == provider_id
                    and active_model == model_id
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    if active_is_configured and not sync_agent_models:
        return

    provider_name = (
        os.environ.get("DEVCO_BUNDLE_PROVIDER_NAME") or "DevCo OpenAI Compatible"
    ).strip()
    api_key = (
        os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    ).strip()

    providers_root = SECRET_DIR / "providers"
    custom_dir = providers_root / "custom"
    builtin_dir = providers_root / "builtin"
    plugin_dir = providers_root / "plugin"
    for d in (SECRET_DIR, providers_root, builtin_dir, custom_dir, plugin_dir):
        d.mkdir(parents=True, exist_ok=True)
        _chmod(d, 0o700)

    provider_payload = {
        "id": provider_id,
        "name": provider_name,
        "base_url": base_url,
        "api_key": api_key,
        "chat_model": "OpenAIChatModel",
        "models": [],
        "extra_models": [_extra_model_dict(mid) for mid in model_ids],
        "api_key_prefix": "",
        "is_local": False,
        "freeze_url": False,
        "require_api_key": True,
        "is_custom": True,
        "support_model_discovery": False,
        "support_connection_check": False,
        "generate_kwargs": {},
        "meta": {},
    }
    provider_path = custom_dir / f"{provider_id}.json"
    to_write = encrypt_dict_fields(dict(provider_payload), PROVIDER_SECRET_FIELDS)
    provider_path.write_text(
        json.dumps(to_write, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _chmod(provider_path, 0o600)

    # 确保从磁盘重新加载刚写入的 custom 提供商
    ProviderManager._instance = None  # pylint: disable=protected-access
    manager = ProviderManager.get_instance()
    prov = manager.get_provider(provider_id)
    if prov is None or not prov.has_model(model_id):
        click.echo(
            "[devco] init seed: provider or model missing after write, "
            "skipping activation.",
        )
        return

    if not active_matches:
        try:
            asyncio.run(manager.activate_model(provider_id, model_id))
        except Exception as exc:
            click.echo(f"[devco] init seed: activate_model failed: {exc}")
            return

    try:
        model_slot = ModelSlotConfig(
            provider_id=provider_id,
            model=model_id,
        )
        default_only = os.environ.get(
            "QWENPAW_INIT_DEFAULT_ONLY",
            "",
        ).strip().lower() in {"1", "true", "yes", "on"}
        _sync_agent_active_model(
            agent_id="default",
            model_slot=model_slot,
            load_agent_config=load_agent_config,
            save_agent_config=save_agent_config,
        )
        if default_only:
            click.echo("[devco] Default-only init: skipped QA/bundled model sync")
            click.echo(
                f"[devco] Seeded LLM: {provider_name} ({provider_id}) / {model_id}",
            )
            return
        try:
            if _sync_agent_active_model(
                agent_id=_QA_AGENT_ID,
                model_slot=model_slot,
                load_agent_config=load_agent_config,
                save_agent_config=save_agent_config,
            ):
                click.echo("[devco] Synced QA agent active model")
        except Exception as qa_exc:
            click.echo(
                "[devco] init seed: QA agent model sync failed: "
                f"{qa_exc}",
            )
        try:
            from qwenpaw.app.bundled_agents import (
                sync_bundled_agents_active_model,
            )

            synced = sync_bundled_agents_active_model(model_slot)
            if synced:
                click.echo(
                    f"[devco] Synced bundled agent active models: {synced}",
                )
        except Exception as sync_exc:
            click.echo(
                "[devco] init seed: bundled agent model sync failed: "
                f"{sync_exc}",
            )
    except Exception as exc:
        click.echo(f"[devco] init seed: default agent.json update failed: {exc}")

    click.echo(
        f"[devco] Seeded LLM: {provider_name} ({provider_id}) / {model_id}",
    )
