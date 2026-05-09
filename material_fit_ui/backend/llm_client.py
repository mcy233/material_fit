"""OpenAI-compatible LLM client for shader semantic preanalysis."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .case_loader import PROJECT_ROOT


@dataclass(frozen=True)
class LlmRuntimeConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0

    def public_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "api_key_configured": bool(self.api_key),
        }


class LlmConfigError(RuntimeError):
    pass


def load_llm_runtime_config(project_root: Path | None = None) -> LlmRuntimeConfig:
    """Load OpenAI-compatible settings from environment plus repo-root .env."""

    root = project_root or PROJECT_ROOT / "tools"
    values = _read_env_file(root / ".env")

    def pick(*names: str, default: str = "") -> str:
        for name in names:
            value = os.environ.get(name) or values.get(name)
            if value:
                return value.strip()
        return default

    api_key = pick("OPENAI_API_KEY", "OPENAI_KEY", "LLM_API_KEY")
    if not api_key:
        raise LlmConfigError("missing OPENAI_API_KEY in environment or .env")
    base_url = pick(
        "OPENAI_BASE_URL",
        "OPENAI_API_URL",
        "OPENAI_API_BASE",
        "OPENAI_API_BASE_URL",
        "LLM_BASE_URL",
        "LLM_URL",
        default="https://api.openai.com/v1",
    )
    model = pick("OPENAI_MODEL", "LLM_MODEL", default="gpt-4o-mini")
    timeout = _float(pick("OPENAI_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"), 120.0)
    return LlmRuntimeConfig(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        timeout_seconds=timeout,
    )


def run_shader_semantics_llm(
    context: dict[str, Any],
    *,
    runtime: LlmRuntimeConfig | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Call the configured model and parse the JSON object it returns."""

    runtime = runtime or load_llm_runtime_config()
    body = {
        "model": runtime.model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a shader semantic analyzer for Unity-to-Laya material fitting. "
                    "Return only one JSON object matching the requested schema. "
                    "Do not suggest file writes. Treat Unity values as visual evidence, not direct Laya targets."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False),
            },
        ],
    }
    if max_tokens is not None and max_tokens > 0:
        body["max_tokens"] = max_tokens
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{runtime.base_url}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {runtime.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=runtime.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        detail = _redact_secret(detail, runtime.api_key)
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc.reason}") from exc

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        parsed = _parse_json_content(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("LLM response did not contain a valid JSON object") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM response JSON must be an object")
    return parsed


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _float(value: str, default: float) -> float:
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _redact_secret(text: str, secret: str) -> str:
    if not text or not secret:
        return text
    redacted = text.replace(secret, "[REDACTED_API_KEY]")
    if len(secret) > 12:
        redacted = redacted.replace(secret[:8], "[REDACTED_API_KEY_PREFIX]")
    return redacted


def _parse_json_content(content: str) -> Any:
    text = str(content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        candidate = _extract_first_json_object(text)
        if candidate is None:
            raise
        return json.loads(candidate)


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
