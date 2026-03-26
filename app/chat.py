import os
from pathlib import Path
from typing import List, Dict, Tuple

import anthropic

from app.tools import TOOL_DEFINITIONS, execute_tool

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MAX_OUTPUT_TOKENS = 4096
BASE_SYSTEM_PROMPT = """You are a helpful AI assistant with access to a Linux workspace running as root inside a Docker container.
You can read/write files, list directories, and run shell commands in /workspace.
Always use the available tools when the user asks you to work with files or run commands.
When running commands, explain what you are doing.
All file operations are restricted to /workspace directory.
You are already root - never use sudo, it is not needed.
"""

import json as _json

def _load_settings(data_dir: Path) -> dict:
    path = data_dir / "settings.json"
    if path.exists():
        try:
            return _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


async def handle_chat_message(
    messages: List[Dict],
    workspace_dir: Path,
    shell_manager,
    chat_id: str,
    data_dir: Path = None,
) -> Tuple[str, List[Dict]]:
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set")

    # Load runtime settings (model + custom system prompt)
    settings = _load_settings(data_dir) if data_dir else {}
    model = settings.get("model") or CLAUDE_MODEL
    custom_prompt = settings.get("system_prompt", "").strip()
    system = BASE_SYSTEM_PROMPT
    if custom_prompt:
        system = BASE_SYSTEM_PROMPT + "\n\nUser preferences:\n" + custom_prompt

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    api_messages = _convert_messages(messages)
    tool_uses_log = []
    max_iterations = 10

    for _ in range(max_iterations):
        response = await client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            tools=TOOL_DEFINITIONS,
            messages=api_messages,
        )

        text_content = ""
        tool_use_blocks = []

        for block in response.content:
            if block.type == "text":
                text_content += block.text
            elif block.type == "tool_use":
                tool_use_blocks.append(block)

        if response.stop_reason == "end_turn" or not tool_use_blocks:
            return text_content, tool_uses_log

        api_messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_block in tool_use_blocks:
            try:
                result = await execute_tool(
                    tool_name=tool_block.name,
                    tool_input=tool_block.input,
                    workspace_dir=workspace_dir,
                    shell_manager=shell_manager,
                )
                tool_uses_log.append({"tool": tool_block.name, "input": tool_block.input, "result": result, "error": None})
                tool_results.append({"type": "tool_result", "tool_use_id": tool_block.id, "content": str(result)})
            except Exception as e:
                err = str(e)
                tool_uses_log.append({"tool": tool_block.name, "input": tool_block.input, "result": None, "error": err})
                tool_results.append({"type": "tool_result", "tool_use_id": tool_block.id, "content": f"Error: {err}", "is_error": True})

        api_messages.append({"role": "user", "content": tool_results})

    return "Operations completed.", tool_uses_log


def _convert_messages(messages: List[Dict]) -> List[Dict]:
    return [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
