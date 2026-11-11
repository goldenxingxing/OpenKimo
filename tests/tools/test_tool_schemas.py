from __future__ import annotations

# ruff: noqa

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from inline_snapshot import snapshot

from kimo_cli.soul.agent import Runtime
from kimo_cli.tools.agent import Agent as AgentTool
from kimo_cli.tools.background import TaskList, TaskOutput, TaskStop
from kimo_cli.tools.dmail import SendDMail
from kimo_cli.tools.file.glob import Glob
from kimo_cli.tools.file.grep_local import Grep
from kimo_cli.tools.file.read import ReadFile
from kimo_cli.tools.file.read_media import ReadMediaFile
from kimo_cli.tools.file.replace import StrReplaceFile
from kimo_cli.tools.file.write import WriteFile
from kimo_cli.tools.memory import Memory
from kimo_cli.tools.shell import Shell
from kimo_cli.tools.think import Think
from kimo_cli.tools.todo import SetTodoList
from kimo_cli.tools.web.fetch import FetchURL
from kimo_cli.tools.wiki import Wiki, WikiToolContext


def test_agent_params_schema(agent_tool: AgentTool):
    """Test the schema of Agent tool parameters."""
    assert agent_tool.base.parameters == snapshot(
        {
            "properties": {
                "description": {
                    "description": "A short (3-5 word) description of the task",
                    "type": "string",
                },
                "prompt": {
                    "description": "The task for the agent to perform",
                    "type": "string",
                },
                "subagent_type": {
                    "default": "coder",
                    "description": "The built-in agent type to use. Defaults to `coder`.",
                    "type": "string",
                },
                "model": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Optional model override. Selection priority is: this parameter, then the built-in type default model, then the parent agent's current model.",
                },
                "resume": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Optional agent ID to resume instead of creating a new instance.",
                },
                "run_in_background": {
                    "default": False,
                    "description": "Whether to run the agent in the background. Prefer false unless the task can continue independently and there is a clear benefit to returning control before the result is needed.",
                    "type": "boolean",
                },
                "timeout": {
                    "anyOf": [
                        {"maximum": 3600, "minimum": 30, "type": "integer"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Timeout in seconds for the agent task. Foreground: no default timeout (runs until completion), max 3600s (1hr). Background: default from config (15min), max 3600s (1hr). The agent is stopped if it exceeds this limit.",
                },
            },
            "required": ["description", "prompt"],
            "type": "object",
        }
    )


def test_send_dmail_params_schema(send_dmail_tool: SendDMail):
    """Test the schema of SendDMail tool parameters."""
    assert send_dmail_tool.base.parameters == snapshot(
        {
            "properties": {
                "message": {"description": "The message to send.", "type": "string"},
                "checkpoint_id": {
                    "description": "The checkpoint to send the message back to.",
                    "minimum": 0,
                    "type": "integer",
                },
            },
            "required": ["message", "checkpoint_id"],
            "type": "object",
        }
    )


def test_think_params_schema(think_tool: Think):
    """Test the schema of Think tool parameters."""
    assert think_tool.base.parameters == snapshot(
        {
            "properties": {
                "thought": {
                    "description": "A thought to think about.",
                    "type": "string",
                }
            },
            "required": ["thought"],
            "type": "object",
        }
    )


def test_set_todo_list_params_schema(set_todo_list_tool: SetTodoList):
    """Test the schema of SetTodoList tool parameters."""
    assert set_todo_list_tool.base.parameters == snapshot(
        {
            "properties": {
                "todos": {
                    "anyOf": [
                        {
                            "items": {
                                "properties": {
                                    "title": {
                                        "description": "The title of the todo",
                                        "minLength": 1,
                                        "type": "string",
                                    },
                                    "status": {
                                        "description": "The status of the todo",
                                        "enum": ["pending", "in_progress", "done"],
                                        "type": "string",
                                    },
                                },
                                "required": ["title", "status"],
                                "type": "object",
                            },
                            "type": "array",
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "The updated todo list. If not provided, returns the current todo list without making changes.",
                }
            },
            "type": "object",
        }
    )


def test_shell_params_schema(shell_tool: Shell):
    """Test the schema of Shell tool parameters."""
    assert shell_tool.base.parameters == snapshot(
        {
            "properties": {
                "command": {
                    "description": "The command to execute.",
                    "type": "string",
                },
                "timeout": {
                    "default": 60,
                    "description": "The timeout in seconds for the command to execute. If the command takes longer than this, it will be killed. Foreground calls are rejected above 300s — for anything longer use run_in_background=true (up to 86400s). The default (60s) kills slow builds and tests unexpectedly often; set an explicit timeout that fits the command, or run it in the background.",
                    "maximum": 86400,
                    "minimum": 1,
                    "type": "integer",
                },
                "run_in_background": {
                    "default": False,
                    "description": "Whether to run the command as a background task.",
                    "type": "boolean",
                },
                "description": {
                    "default": "",
                    "description": "A short description for the background task. Required when run_in_background=true.",
                    "type": "string",
                },
            },
            "required": ["command"],
            "type": "object",
        }
    )


def test_task_output_params_schema(task_output_tool: TaskOutput):
    assert task_output_tool.base.parameters == snapshot(
        {
            "properties": {
                "task_id": {
                    "description": "The background task ID to inspect.",
                    "type": "string",
                },
                "block": {
                    "default": False,
                    "description": "Whether to wait for the task to finish before returning.",
                    "type": "boolean",
                },
                "timeout": {
                    "default": 30,
                    "description": "Maximum number of seconds to wait when block=true.",
                    "maximum": 3600,
                    "minimum": 0,
                    "type": "integer",
                },
            },
            "required": ["task_id"],
            "type": "object",
        }
    )


def test_task_list_params_schema(task_list_tool: TaskList):
    assert task_list_tool.base.parameters == snapshot(
        {
            "properties": {
                "active_only": {
                    "default": True,
                    "description": "Whether to list only non-terminal background tasks.",
                    "type": "boolean",
                },
                "limit": {
                    "default": 20,
                    "description": "Maximum number of tasks to return.",
                    "maximum": 100,
                    "minimum": 1,
                    "type": "integer",
                },
            },
            "type": "object",
        }
    )


def test_task_stop_params_schema(task_stop_tool: TaskStop):
    assert task_stop_tool.base.parameters == snapshot(
        {
            "properties": {
                "task_id": {
                    "description": "The background task ID to stop.",
                    "type": "string",
                },
                "reason": {
                    "default": "Stopped by TaskStop",
                    "description": "Short reason recorded when the task is stopped.",
                    "type": "string",
                },
            },
            "required": ["task_id"],
            "type": "object",
        }
    )


def test_read_file_params_schema(read_file_tool: ReadFile):
    """Test the schema of ReadFile tool parameters."""
    assert read_file_tool.base.parameters == snapshot(
        {
            "properties": {
                "path": {
                    "description": "The path to the file to read. Absolute paths are required when reading files outside the working directory.",
                    "type": "string",
                },
                "line_offset": {
                    "default": 1,
                    "description": "The line number to start reading from. By default read from the beginning of the file. Set this when the file is too large to read at once. Negative values read from the end of the file (e.g. -100 reads the last 100 lines). The absolute value of negative offset cannot exceed 1000.",
                    "type": "integer",
                },
                "n_lines": {
                    "default": 1000,
                    "description": "The number of lines to read. By default read up to 1000 lines, which is the max allowed value. Set this value when the file is too large to read at once.",
                    "minimum": 1,
                    "type": "integer",
                },
            },
            "required": ["path"],
            "type": "object",
        }
    )


def test_read_media_file_params_schema(read_media_file_tool: ReadMediaFile):
    """Test the schema of ReadMediaFile tool parameters."""
    assert read_media_file_tool.base.parameters == snapshot(
        {
            "properties": {
                "path": {
                    "description": "The path to the file to read. Absolute paths are required when reading files outside the working directory.",
                    "type": "string",
                }
            },
            "required": ["path"],
            "type": "object",
        }
    )


def test_glob_params_schema(glob_tool: Glob):
    """Test the schema of Glob tool parameters."""
    assert glob_tool.base.parameters == snapshot(
        {
            "properties": {
                "pattern": {
                    "description": "Glob pattern to match files/directories.",
                    "type": "string",
                },
                "directory": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Absolute path to the directory to search in (defaults to working directory).",
                },
                "include_dirs": {
                    "default": True,
                    "description": "Whether to include directories in results.",
                    "type": "boolean",
                },
            },
            "required": ["pattern"],
            "type": "object",
        }
    )


def test_grep_params_schema(grep_tool: Grep):
    """Test the schema of Grep tool parameters."""
    assert grep_tool.base.parameters == snapshot(
        {
            "properties": {
                "pattern": {
                    "description": "The regular expression pattern to search for in file contents",
                    "type": "string",
                },
                "path": {
                    "default": ".",
                    "description": "File or directory to search in. Defaults to current working directory. If specified, it must be an absolute path.",
                    "type": "string",
                },
                "glob": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Glob pattern to filter files (e.g. `*.js`, `*.{ts,tsx}`). No filter by default.",
                },
                "output_mode": {
                    "default": "files_with_matches",
                    "description": "`content`: Show matching lines (supports `-B`, `-A`, `-C`, `-n`, `head_limit`); `files_with_matches`: Show file paths (supports `head_limit`); `count_matches`: Show total number of matches. Defaults to `files_with_matches`.",
                    "type": "string",
                },
                "-B": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "description": "Number of lines to show before each match (the `-B` option). Requires `output_mode` to be `content`.",
                },
                "-A": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "description": "Number of lines to show after each match (the `-A` option). Requires `output_mode` to be `content`.",
                },
                "-C": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                    "description": "Number of lines to show before and after each match (the `-C` option). Requires `output_mode` to be `content`.",
                },
                "-n": {
                    "default": True,
                    "description": "Show line numbers in output (the `-n` option). Requires `output_mode` to be `content`. Defaults to true.",
                    "type": "boolean",
                },
                "-i": {
                    "default": False,
                    "description": "Case insensitive search (the `-i` option).",
                    "type": "boolean",
                },
                "type": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "File type to search. Examples: py, rust, js, ts, go, java, etc. More efficient than `glob` for standard file types.",
                },
                "head_limit": {
                    "anyOf": [{"minimum": 0, "type": "integer"}, {"type": "null"}],
                    "default": 250,
                    "description": "Limit output to first N lines/entries, equivalent to `| head -N`. Works across all output modes: content (limits output lines), files_with_matches (limits file paths), count_matches (limits count entries). Defaults to 250. Pass 0 for unlimited (use sparingly — large result sets waste context).",
                },
                "offset": {
                    "default": 0,
                    "description": "Skip first N lines/entries before applying head_limit, equivalent to `| tail -n +N | head -N`. Works across all output modes. Defaults to 0.",
                    "minimum": 0,
                    "type": "integer",
                },
                "multiline": {
                    "default": False,
                    "description": "Enable multiline mode where `.` matches newlines and patterns can span lines (the `-U` and `--multiline-dotall` options). By default, multiline mode is disabled.",
                    "type": "boolean",
                },
                "include_ignored": {
                    "default": False,
                    "description": "Include files that are ignored by `.gitignore`, `.ignore`, and other ignore rules. Useful for searching gitignored artifacts such as build outputs (e.g. `dist/`, `build/`) or `node_modules`. Sensitive files (like `.env`) remain filtered by the sensitive-file protection layer. Defaults to false.",
                    "type": "boolean",
                },
            },
            "required": ["pattern"],
            "type": "object",
        }
    )


def test_write_file_params_schema(write_file_tool: WriteFile):
    """Test the schema of WriteFile tool parameters."""
    assert write_file_tool.base.parameters == snapshot(
        {
            "properties": {
                "path": {
                    "description": "The path to the file to write. Absolute paths are required when writing files outside the working directory.",
                    "type": "string",
                },
                "content": {
                    "description": "The content to write to the file",
                    "type": "string",
                },
                "mode": {
                    "default": "overwrite",
                    "description": "The mode to use to write to the file. Two modes are supported: `overwrite` for overwriting the whole file and `append` for appending to the end of an existing file.",
                    "enum": ["overwrite", "append"],
                    "type": "string",
                },
            },
            "required": ["path", "content"],
            "type": "object",
        }
    )


def test_str_replace_file_params_schema(str_replace_file_tool: StrReplaceFile):
    """Test the schema of StrReplaceFile tool parameters."""
    assert str_replace_file_tool.base.parameters == snapshot(
        {
            "properties": {
                "path": {
                    "description": "The path to the file to edit. Absolute paths are required when editing files outside the working directory.",
                    "type": "string",
                },
                "edit": {
                    "anyOf": [
                        {
                            "properties": {
                                "old": {
                                    "description": "The old string to replace. Can be multi-line. Must not be empty.",
                                    "minLength": 1,
                                    "type": "string",
                                },
                                "new": {
                                    "description": "The new string to replace with. Can be multi-line.",
                                    "type": "string",
                                },
                                "replace_all": {
                                    "default": False,
                                    "description": "Whether to replace all occurrences.",
                                    "type": "boolean",
                                },
                            },
                            "required": ["old", "new"],
                            "type": "object",
                        },
                        {
                            "items": {
                                "properties": {
                                    "old": {
                                        "description": "The old string to replace. Can be multi-line. Must not be empty.",
                                        "minLength": 1,
                                        "type": "string",
                                    },
                                    "new": {
                                        "description": "The new string to replace with. Can be multi-line.",
                                        "type": "string",
                                    },
                                    "replace_all": {
                                        "default": False,
                                        "description": "Whether to replace all occurrences.",
                                        "type": "boolean",
                                    },
                                },
                                "required": ["old", "new"],
                                "type": "object",
                            },
                            "type": "array",
                        },
                    ],
                    "description": "The edit(s) to apply to the file. You can provide a single edit or a list of edits here.",
                },
            },
            "required": ["path", "edit"],
            "type": "object",
        }
    )


def test_fetch_url_params_schema(fetch_url_tool: FetchURL):
    """Test the schema of FetchURL tool parameters."""
    assert fetch_url_tool.base.parameters == snapshot(
        {
            "properties": {
                "url": {
                    "description": "The URL to fetch content from.",
                    "type": "string",
                }
            },
            "required": ["url"],
            "type": "object",
        }
    )


def test_memory_params_schema(runtime: Runtime):
    """Test the schema of Memory tool parameters."""
    memory_tool = Memory(runtime)
    assert memory_tool.base.parameters == snapshot(
        {
            "properties": {
                "operation": {
                    "description": "The memory operation to perform.",
                    "discriminator": {
                        "mapping": {
                            "add": "#/$defs/AddOp",
                            "affirm": "#/$defs/AffirmOp",
                            "consolidate": "#/$defs/ConsolidateOp",
                            "delete": "#/$defs/DeleteOp",
                            "dismiss": "#/$defs/DismissOp",
                            "get": "#/$defs/GetOp",
                            "list": "#/$defs/ListOp",
                            "promote": "#/$defs/PromoteOp",
                            "restore": "#/$defs/RestoreOp",
                            "retire": "#/$defs/RetireOp",
                            "search": "#/$defs/SearchOp",
                            "update": "#/$defs/UpdateOp",
                        },
                        "propertyName": "op",
                    },
                    "oneOf": [
                        {
                            "properties": {
                                "op": {"const": "add", "type": "string"},
                                "kind": {
                                    "description": "The category of memory being recorded.",
                                    "enum": [
                                        "user",
                                        "feedback",
                                        "project",
                                        "reference",
                                    ],
                                    "type": "string",
                                },
                                "scope": {
                                    "description": "`session` keeps the entry in the current conversation only. `persistent` writes to the user's cross-session memory.",
                                    "enum": ["session", "persistent"],
                                    "type": "string",
                                },
                                "content": {
                                    "description": "The memory body. Be concise but specific.",
                                    "minLength": 1,
                                    "type": "string",
                                },
                                "key": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "default": None,
                                    "description": "Short semantic handle, `namespace/slug` — e.g. `acls/repo-path`. Lets a later session recognise this entry from a one-line index and fetch it by name. Give one for `project` and `reference` entries, which are read back on demand; group related entries under the same namespace. Optional, but an entry without one can only be addressed by an opaque id.",
                                },
                            },
                            "required": ["op", "kind", "scope", "content"],
                            "type": "object",
                        },
                        {
                            "properties": {
                                "op": {"const": "get", "type": "string"},
                                "handle": {
                                    "description": "The `key` or id shown in parentheses in the memory index. Returns the entry in full.",
                                    "type": "string",
                                },
                            },
                            "required": ["op", "handle"],
                            "type": "object",
                        },
                        {
                            "properties": {
                                "op": {"const": "search", "type": "string"},
                                "query": {
                                    "description": "Free text to look for across stored memory. Send the user's question as asked: on LoCoMo it recalls the right entry 63.0% of the time at eight results, against 28.4% for the single most distinctive word in it. The scan weights by inverse document frequency, so common words already cost the ranking nothing. Returns handles and snippets; read a hit in full with `get`.",
                                    "minLength": 1,
                                    "type": "string",
                                },
                            },
                            "required": ["op", "query"],
                            "type": "object",
                        },
                        {
                            "properties": {
                                "op": {"const": "promote", "type": "string"},
                                "id": {
                                    "description": "Id of the suggested memory to keep, from the suggestions list.",
                                    "type": "string",
                                },
                            },
                            "required": ["op", "id"],
                            "type": "object",
                        },
                        {
                            "properties": {
                                "op": {"const": "dismiss", "type": "string"},
                                "id": {
                                    "description": "Id of the suggested memory to drop, from the suggestions list.",
                                    "type": "string",
                                },
                            },
                            "required": ["op", "id"],
                            "type": "object",
                        },
                        {
                            "properties": {
                                "op": {"const": "list", "type": "string"},
                                "scope": {
                                    "default": "all",
                                    "description": "Which scope(s) to list.",
                                    "enum": ["session", "persistent", "all"],
                                    "type": "string",
                                },
                            },
                            "required": ["op"],
                            "type": "object",
                        },
                        {
                            "properties": {
                                "op": {"const": "update", "type": "string"},
                                "id": {
                                    "description": "Handle of the entry to update — the `key` or id shown in parentheses in the memory index, or a full id. Prefer updating an entry in place over adding one that says it supersedes an older record.",
                                    "type": "string",
                                },
                                "content": {
                                    "description": "The new body for the entry.",
                                    "minLength": 1,
                                    "type": "string",
                                },
                            },
                            "required": ["op", "id", "content"],
                            "type": "object",
                        },
                        {
                            "description": """\
Stop injecting an entry without losing it.

Deleting is for something that was wrong. This is for something that was
right and no longer applies — a convention that changed, a project that
ended. The record stays and stays searchable; it just stops arriving in
every conversation, which is the only cost a stale behavioural entry
actually imposes.\
""",
                            "properties": {
                                "op": {"const": "retire", "type": "string"},
                                "handle": {"type": "string"},
                            },
                            "required": ["op", "handle"],
                            "type": "object",
                        },
                        {
                            "description": "Put a retired entry back into force.",
                            "properties": {
                                "op": {"const": "restore", "type": "string"},
                                "handle": {"type": "string"},
                            },
                            "required": ["op", "handle"],
                            "type": "object",
                        },
                        {
                            "description": """\
Record that an entry was raised with the user and still holds.

The answer to a supersession suggestion that is not "retire it", and more
often the right one: a later instruction usually adds to an earlier one
rather than replacing it, and what it leaves out is still required.

Without somewhere to put that answer the same pair is suggested again next
session and the one after, which teaches the user to stop reading the
suggestion. Affirming settles the question as it stands; an entry written
later reopens it.\
""",
                            "properties": {
                                "op": {"const": "affirm", "type": "string"},
                                "handle": {"type": "string"},
                            },
                            "required": ["op", "handle"],
                            "type": "object",
                        },
                        {
                            "description": """\
Replace several persistent entries with one that keeps what all of them said.

The right answer more often than `retire` is, and the one that used to have
no operation: a later instruction usually *adds* to an earlier one rather
than replacing it, and what it leaves out is still required. Retiring the
older entry then drops those requirements with nothing to show it happened.

Read every entry named in `replacing` in full first. If the merged text does
not carry something one of them said, that requirement is being deleted.\
""",
                            "properties": {
                                "op": {"const": "consolidate", "type": "string"},
                                "content": {
                                    "description": "The merged entry. Must carry every requirement from every entry being replaced — check each one against the originals.",
                                    "minLength": 1,
                                    "type": "string",
                                },
                                "replacing": {
                                    "description": "Handles or ids of the entries this replaces. They are retired, not deleted.",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                    "type": "array",
                                },
                                "key": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "default": None,
                                    "description": "Handle for the merged entry.",
                                },
                            },
                            "required": ["op", "content", "replacing"],
                            "type": "object",
                        },
                        {
                            "properties": {
                                "op": {"const": "delete", "type": "string"},
                                "id": {
                                    "description": "Handle of the entry to delete — the `key` or id shown in parentheses in the memory index, or a full id.",
                                    "type": "string",
                                },
                            },
                            "required": ["op", "id"],
                            "type": "object",
                        },
                    ],
                }
            },
            "required": ["operation"],
            "type": "object",
        }
    )


def test_wiki_params_schema():
    """Test the schema of Wiki tool parameters."""
    runtime = SimpleNamespace(
        workspace_id=None,
        wiki_tool_context=WikiToolContext(
            provenance_session_id=uuid4(),
            conversation_hashes=frozenset(),
            allowed_workspace_ids=frozenset(),
            candidate_high_value=True,
            stable=True,
            user_confirmed=True,
            reliable_source=False,
        ),
    )
    wiki_tool = Wiki(cast("Runtime", runtime))
    assert wiki_tool.base.parameters == snapshot(
        {
            "description": """\
One controlled Wiki operation.

``source`` intentionally has no raw-path or URL variant: ingest is limited to
current-turn inline content or a portable, registry-resolved workspace file.\
""",
            "properties": {
                "operation": {
                    "enum": ["search", "read", "remember", "ingest", "lint", "discard"],
                    "type": "string",
                },
                "checkpoint_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "The runtime-issued checkpoint this call resolves. Required for remember, ingest, and discard. It must be copied from a checkpoint block; it cannot be invented.",
                },
                "discard_reason": {
                    "anyOf": [
                        {
                            "enum": [
                                "low_value",
                                "unstable",
                                "ungrounded",
                                "duplicate",
                                "not_reusable",
                            ],
                            "type": "string",
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Why the checkpoint is not worth persisting. Required for discard.",
                },
                "query": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Search query for the Knowledge Base.",
                },
                "page": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": "Logical Wiki page path in `category/slug.md` form: category is one of entities|concepts|comparisons|sources|queries|lint, and the slug matches `[\\w][\\w-]*` (Unicode word characters — uppercase and CJK are allowed). A two-part path without the `.md` suffix is accepted and completed. For lint, a declared category alone is also accepted.",
                },
                "candidate": {
                    "anyOf": [
                        {
                            "additionalProperties": False,
                            "description": "A proposed, still-uncommitted Knowledge Base update.",
                            "properties": {
                                "summary": {
                                    "maxLength": 500,
                                    "minLength": 1,
                                    "type": "string",
                                },
                                "pages": {
                                    "items": {
                                        "additionalProperties": False,
                                        "description": "A revision-checked replacement for one logical Wiki content page.",
                                        "properties": {
                                            "page": {
                                                "additionalProperties": False,
                                                "description": "A parsed content page, excluding special generated Wiki files.",
                                                "properties": {
                                                    "logical_path": {
                                                        "description": "Logical page path in `category/slug.md` form: category is one of entities|concepts|comparisons|sources|queries|lint, and the slug matches `[\\w][\\w-]*` (Unicode word characters — uppercase and CJK are allowed). A two-part path without the `.md` suffix is accepted and completed.",
                                                        "type": "string",
                                                    },
                                                    "title": {
                                                        "maxLength": 500,
                                                        "minLength": 1,
                                                        "type": "string",
                                                    },
                                                    "created": {
                                                        "format": "date-time",
                                                        "type": "string",
                                                    },
                                                    "updated": {
                                                        "format": "date-time",
                                                        "type": "string",
                                                    },
                                                    "tags": {
                                                        "items": {"type": "string"},
                                                        "type": "array",
                                                    },
                                                    "sources": {
                                                        "description": "Leave this out when resolving a checkpoint: the runtime attaches the provenance it actually observed. A source you write yourself has to match those bytes exactly, which is not something you can reconstruct.",
                                                        "items": {
                                                            "additionalProperties": False,
                                                            "description": "Portable provenance for information recorded in a Wiki page.",
                                                            "properties": {
                                                                "kind": {
                                                                    "enum": [
                                                                        "workspace-file",
                                                                        "conversation",
                                                                        "web",
                                                                    ],
                                                                    "type": "string",
                                                                },
                                                                "workspace_id": {
                                                                    "anyOf": [
                                                                        {
                                                                            "format": "uuid",
                                                                            "type": "string",
                                                                        },
                                                                        {"type": "null"},
                                                                    ],
                                                                    "default": None,
                                                                },
                                                                "path": {
                                                                    "anyOf": [
                                                                        {"type": "string"},
                                                                        {"type": "null"},
                                                                    ],
                                                                    "default": None,
                                                                },
                                                                "session_id": {
                                                                    "anyOf": [
                                                                        {
                                                                            "format": "uuid",
                                                                            "type": "string",
                                                                        },
                                                                        {"type": "null"},
                                                                    ],
                                                                    "default": None,
                                                                },
                                                                "url": {
                                                                    "anyOf": [
                                                                        {
                                                                            "format": "uri",
                                                                            "maxLength": 2083,
                                                                            "minLength": 1,
                                                                            "type": "string",
                                                                        },
                                                                        {"type": "null"},
                                                                    ],
                                                                    "default": None,
                                                                },
                                                                "content_hash": {
                                                                    "pattern": "^sha256:[0-9a-f]{64}$",
                                                                    "type": "string",
                                                                },
                                                            },
                                                            "required": [
                                                                "kind",
                                                                "content_hash",
                                                            ],
                                                            "type": "object",
                                                        },
                                                        "type": "array",
                                                    },
                                                    "revision": {
                                                        "exclusiveMinimum": 0,
                                                        "type": "integer",
                                                    },
                                                    "body": {
                                                        "minLength": 1,
                                                        "type": "string",
                                                    },
                                                },
                                                "required": [
                                                    "logical_path",
                                                    "title",
                                                    "created",
                                                    "updated",
                                                    "revision",
                                                    "body",
                                                ],
                                                "type": "object",
                                            },
                                            "expected_revision": {
                                                "anyOf": [
                                                    {
                                                        "exclusiveMinimum": 0,
                                                        "type": "integer",
                                                    },
                                                    {"type": "null"},
                                                ],
                                                "default": None,
                                                "description": "Leave out to create a new page. To replace a page that already exists, set this to its current `revision` (as Wiki read or search report it); the write is refused if the page has changed since.",
                                            },
                                        },
                                        "required": ["page"],
                                        "type": "object",
                                    },
                                    "minItems": 1,
                                    "type": "array",
                                },
                                "sources": {
                                    "description": "Leave this out when resolving a checkpoint: the runtime attaches the provenance it actually observed. A source you write yourself has to match those bytes exactly, which is not something you can reconstruct.",
                                    "items": {
                                        "additionalProperties": False,
                                        "description": "Portable provenance for information recorded in a Wiki page.",
                                        "properties": {
                                            "kind": {
                                                "enum": [
                                                    "workspace-file",
                                                    "conversation",
                                                    "web",
                                                ],
                                                "type": "string",
                                            },
                                            "workspace_id": {
                                                "anyOf": [
                                                    {
                                                        "format": "uuid",
                                                        "type": "string",
                                                    },
                                                    {"type": "null"},
                                                ],
                                                "default": None,
                                            },
                                            "path": {
                                                "anyOf": [
                                                    {"type": "string"},
                                                    {"type": "null"},
                                                ],
                                                "default": None,
                                            },
                                            "session_id": {
                                                "anyOf": [
                                                    {
                                                        "format": "uuid",
                                                        "type": "string",
                                                    },
                                                    {"type": "null"},
                                                ],
                                                "default": None,
                                            },
                                            "url": {
                                                "anyOf": [
                                                    {
                                                        "format": "uri",
                                                        "maxLength": 2083,
                                                        "minLength": 1,
                                                        "type": "string",
                                                    },
                                                    {"type": "null"},
                                                ],
                                                "default": None,
                                            },
                                            "content_hash": {
                                                "pattern": "^sha256:[0-9a-f]{64}$",
                                                "type": "string",
                                            },
                                        },
                                        "required": ["kind", "content_hash"],
                                        "type": "object",
                                    },
                                    "type": "array",
                                },
                                "value": {
                                    "enum": ["high", "medium", "low"],
                                    "type": "string",
                                },
                            },
                            "required": ["summary", "pages", "value"],
                            "type": "object",
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Structured, sourced high-value change proposal for remember or ingest.",
                },
                "source": {
                    "anyOf": [
                        {
                            "additionalProperties": False,
                            "description": "Content supplied in the active session and eligible for controlled ingest.",
                            "properties": {
                                "kind": {
                                    "enum": ["inline", "workspace-file"],
                                    "type": "string",
                                },
                                "content": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "default": None,
                                },
                                "workspace_id": {
                                    "anyOf": [
                                        {"format": "uuid", "type": "string"},
                                        {"type": "null"},
                                    ],
                                    "default": None,
                                },
                                "relative_path": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "default": None,
                                },
                            },
                            "required": ["kind"],
                            "type": "object",
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Current-turn inline content or a registered workspace-relative file only.",
                },
                "limit": {
                    "default": 5,
                    "description": "Maximum number of search results.",
                    "maximum": 20,
                    "minimum": 1,
                    "type": "integer",
                },
            },
            "required": ["operation"],
            "type": "object",
        }
    )
