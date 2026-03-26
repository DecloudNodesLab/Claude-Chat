import os
import json
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import anthropic

from app.tools import (
    read_file_tool,
    write_file_tool,
    list_files_tool,
    run_command,
    delete_path_tool,
    TOOL_DEFINITIONS,
    execute_tool,
)

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MAX_OUTPUT_TOKENS = 4096
SYSTEM_PROMPT = """You are a helpful AI assistant with access to a Linux workspace.
You can read/write files, list directories, and run shell commands in /workspace.
Always use the available tools when the user asks you to work with files or run commands.
When running commands, explain what you are doing.
All file operations are restricted to /workspace directory.
"""


async def handle_chat_message(
    messages: List[Dict],
    workspace_dir: Path,
    shell_manager,
    chat_id: str,
) -> Tuple[str, List[Dict]]:
    """
    Send messages to Claude, handle tool calls in a loop, return final reply and tool_uses log.
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set")

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # Convert stored messages to Anthropic format
    api_messages = _convert_messages(messages)

    tool_uses_log = []
    max_iterations = 10

    for iteration in range(max_iterations):
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=api_messages,
        )

        # Collect text and tool use blocks
        text_content = ""
        tool_use_blocks = []

        for block in response.content:
            if block.type == "text":
                text_content += block.text
            elif block.type == "tool_use":
                tool_use_blocks.append(block)

        # If no tool calls or stop_reason is end_turn, we're done
        if response.stop_reason == "end_turn" or not tool_use_blocks:
            return text_content, tool_uses_log

        # Add assistant message with all content blocks to api_messages
        api_messages.append({
            "role": "assistant",
            "content": response.content,
        })

        # Process tool calls
        tool_results = []
        for tool_block in tool_use_blocks:
            tool_name = tool_block.name
            tool_input = tool_block.input
            tool_id = tool_block.id

            try:
                result = await execute_tool(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    workspace_dir=workspace_dir,
                    shell_manager=shell_manager,
                )
                tool_uses_log.append({
                    "tool": tool_name,
                    "input": tool_input,
                    "result": result,
                    "error": None,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": str(result),
                })
            except Exception as e:
                error_msg = str(e)
                tool_uses_log.append({
                    "tool": tool_name,
                    "input": tool_input,
                    "result": None,
                    "error": error_msg,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Error: {error_msg}",
                    "is_error": True,
                })

        # Add tool results to conversation
        api_messages.append({
            "role": "user",
            "content": tool_results,
        })

    # If we hit max iterations, return what we have
    return "I've completed the requested operations. Please let me know if you need anything else.", tool_uses_log


def _convert_messages(messages: List[Dict]) -> List[Dict]:
    """Convert stored simple messages to Anthropic API format."""
    result = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            result.append({"role": role, "content": content})
    return result
