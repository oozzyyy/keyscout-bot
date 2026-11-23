import json
import os
import re

import anthropic

ANTHROPIC_URL = "https://api.anthropic.com"
DEEPSEEK_URL = "https://api.deepseek.com/anthropic"
NAMES = {"claude": "Claude", "deepseek": "DeepSeek"}


class LLMError(RuntimeError):
    pass


def provider() -> str | None:
    has = {"claude": bool(os.getenv("ANTHROPIC_API_KEY")), "deepseek": bool(os.getenv("DEEPSEEK_API_KEY"))}
    want = os.getenv("LLM_PROVIDER", "").strip().lower()
    if has.get(want):
        return want
    return next((p for p in ("claude", "deepseek") if has[p]), None)


def enabled() -> bool:
    return provider() is not None


def label() -> str:
    return NAMES.get(provider() or "", "ИИ")


def model(task: str) -> str:
    if provider() == "deepseek":
        return os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    return os.getenv("CLAUDE_MODEL" if task == "expand" else "DIGEST_MODEL", "claude-haiku-5-5")


_clients: dict[str, anthropic.AsyncAnthropic] = {}


def client() -> anthropic.AsyncAnthropic:
    p = provider()
    if p not in _clients:
        key, url = ("DEEPSEEK_API_KEY", DEEPSEEK_URL) if p == "deepseek" else ("ANTHROPIC_API_KEY", ANTHROPIC_URL)
        _clients[p] = anthropic.AsyncAnthropic(api_key=os.environ[key], base_url=url)
    return _clients[p]


async def ask_json(task: str, system: str, user: str, schema: dict, max_tokens: int, effort: str) -> dict:
    request = {"model": model(task), "max_tokens": max_tokens, "messages": [{"role": "user", "content": user}]}
    if provider() == "deepseek":
        request["system"] = (system + "\n\nОтвет — только один JSON-объект по этой JSON-схеме, без пояснений "
                             "и без обёртки ```:\n" + json.dumps(schema, ensure_ascii=False))
        request["thinking"] = {"type": "disabled"}
    else:
        request["system"] = system
        request["output_config"] = {"effort": effort, "format": {"type": "json_schema", "schema": schema}}
    resp = await client().messages.create(**request)
    if resp.stop_reason not in ("end_turn", "stop_sequence"):
        raise LLMError(f"{label()}: ответ оборвался ({resp.stop_reason})")
    return parse_json("".join(b.text for b in resp.content if b.type == "text"))


def parse_json(text: str) -> dict:
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    for candidate in (t, t[t.find("{"): t.rfind("}") + 1] if "{" in t else ""):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise LLMError(f"{label()}: в ответе нет JSON-объекта")
