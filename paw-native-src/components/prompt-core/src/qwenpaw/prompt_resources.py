"""Built-in workspace templates embedded when prompt-core is compiled."""

COMPILED = True
_TEMPLATES: dict[str, bytes] = {}
_BUNDLED: dict[str, dict[str, bytes]] = {}
_LANGUAGES: frozenset[str] = frozenset()


def agent_languages() -> frozenset[str]:
    return _LANGUAGES


def templates(language: str, group: str = "") -> dict[str, bytes]:
    prefix = (group + "/" if group else "") + language + "/"
    return {
        name[len(prefix):]: content for name, content in _TEMPLATES.items()
        if name.startswith(prefix) and "/" not in name[len(prefix):]
    }


def bundled_agent_prompts(name: str) -> dict[str, bytes]:
    return dict(_BUNDLED.get(name, {}))
