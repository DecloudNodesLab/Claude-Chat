import os
import json
from pathlib import Path
from typing import Any, Dict

TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file in /workspace. Returns the file content as text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to /workspace or absolute within /workspace",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file in /workspace. Creates the file if it doesn't exist, overwrites if it does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to /workspace or absolute within /workspace",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_files",
        "description": "List files and directories in /workspace or a subdirectory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to list, relative to /workspace. Defaults to /workspace root.",
                    "default": "",
                }
            },
            "required": [],
        },
    },
    {
        "name": "run_command_tool",
        "description": "Run a shell command in /workspace. Returns stdout, stderr, and exit code. The command is also visible in the user's shell window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds (default 30, max 120)",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "delete_path",
        "description": "Delete a file or directory in /workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to delete, relative to /workspace or absolute within /workspace",
                }
            },
            "required": ["path"],
        },
    },
]


def _safe_path(workspace_dir: Path, user_path: str) -> Path:
    """Resolve and validate a path is within workspace_dir."""
    if not user_path or user_path.strip() in ("", "/"):
        return workspace_dir

    p = Path(user_path)
    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (workspace_dir / p).resolve()

    workspace_resolved = workspace_dir.resolve()
    if not str(resolved).startswith(str(workspace_resolved)):
        raise ValueError(f"Path '{user_path}' is outside the workspace directory")
    return resolved


def read_file_tool(workspace_dir: Path, path: str) -> str:
    target = _safe_path(workspace_dir, path)
    if not target.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not target.is_file():
        raise ValueError(f"Not a file: {path}")
    content = target.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = f"[Binary file, {len(content)} bytes]"
    # Limit output size
    if len(text) > 32000:
        text = text[:32000] + f"\n... [truncated, total {len(content)} bytes]"
    return text


def write_file_tool(workspace_dir: Path, path: str, content: str) -> str:
    target = _safe_path(workspace_dir, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Written {len(content)} characters to {target.relative_to(workspace_dir)}"


def list_files_tool(workspace_dir: Path, path: str = "") -> str:
    target = _safe_path(workspace_dir, path)
    if not target.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if not target.is_dir():
        raise ValueError(f"Not a directory: {path}")

    items = []
    try:
        entries = sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name))
        for entry in entries:
            rel = entry.relative_to(workspace_dir)
            if entry.is_dir():
                items.append(f"[DIR]  {rel}/")
            else:
                size = entry.stat().st_size
                items.append(f"[FILE] {rel} ({size} bytes)")
    except PermissionError as e:
        raise ValueError(f"Permission denied: {e}")

    if not items:
        return "Directory is empty"
    return "\n".join(items)


def delete_path_tool(workspace_dir: Path, path: str) -> str:
    import shutil
    target = _safe_path(workspace_dir, path)
    if not target.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    rel = str(target.relative_to(workspace_dir))
    if target.is_file():
        target.unlink()
        return f"Deleted file: {rel}"
    elif target.is_dir():
        shutil.rmtree(target)
        return f"Deleted directory: {rel}"
    return f"Deleted: {rel}"


async def execute_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    workspace_dir: Path,
    shell_manager,
) -> str:
    if tool_name == "read_file":
        return read_file_tool(workspace_dir, tool_input.get("path", ""))

    elif tool_name == "write_file":
        return write_file_tool(
            workspace_dir,
            tool_input.get("path", ""),
            tool_input.get("content", ""),
        )

    elif tool_name == "list_files":
        return list_files_tool(workspace_dir, tool_input.get("path", ""))

    elif tool_name == "run_command":
        command = tool_input.get("command", "")
        timeout = float(tool_input.get("timeout", 30))
        timeout = min(max(timeout, 1), 120)

        result = await shell_manager.run_command_in_session(
            command=command,
            session_id="default",
            timeout=timeout,
        )
        output_parts = []
        if result["stdout"]:
            output_parts.append(f"STDOUT:\n{result['stdout']}")
        if result["stderr"]:
            output_parts.append(f"STDERR:\n{result['stderr']}")
        output_parts.append(f"EXIT CODE: {result['returncode']}")
        return "\n".join(output_parts)

    elif tool_name == "delete_path":
        return delete_path_tool(workspace_dir, tool_input.get("path", ""))

    else:
        raise ValueError(f"Unknown tool: {tool_name}")
