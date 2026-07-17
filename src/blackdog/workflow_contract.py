from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkflowCommand:
    """One visible CLI command node owned by the Blackdog product layer."""

    name: str
    children: tuple["WorkflowCommand", ...] = ()

    def signature(self) -> tuple[str, tuple[object, ...]]:
        return self.name, tuple(child.signature() for child in self.children)

    def leaf_invocations(self, *parents: str) -> tuple[str, ...]:
        path = (*parents, self.name)
        if not self.children:
            return (" ".join(("blackdog", *path)),)
        return tuple(
            invocation
            for child in self.children
            for invocation in child.leaf_invocations(*path)
        )


@dataclass(frozen=True, slots=True)
class CommandInventorySection:
    label: str
    roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentWorkflow:
    begin_command: str
    land_command: str
    preflight_command: str


PROMPT_INPUT_DISPOSAL_GUIDANCE = (
    "Delete `request_file` and `execution_prompt_file` only when the structured `task begin` "
    "result contains both a nonempty `execution_prompt_replay_artifact_path` and a nonempty "
    "`user_prompt_replay_artifact_path`; otherwise preserve both temporary inputs."
)
NEXT_ACTION_AUTHORITY_GUIDANCE = (
    "For every structured result from `task begin`, `task show`, `task recover`, `task cancel`, "
    "`task reopen`, `task land`, `task reconcile-landing`, `task close`, and `task cleanup`, "
    "treat its `next_action` as the sole authority regardless of `operation_status`: execute its "
    "exact `argv` when `kind=command`; choose only a complete action from `choices` or "
    "`alternatives`; stop when `kind=blocked` or `kind=complete`; never infer an action from "
    "display text, reason or error prose, summaries, or compatibility recommendations."
)
TARGET_BRANCH_GUIDANCE = (
    "Treat the `target_branch` selected and recorded by Blackdog for the task as authoritative "
    "when landing and verifying the result; never assume it is `main` and never switch it manually."
)


@dataclass(frozen=True, slots=True)
class PromptInputContract:
    role: str
    inline_flag: str
    file_flag: str
    compatibility_inline_flag: str
    compatibility_file_flag: str
    canonical_inline_source: str
    compatibility_status: str = "supported_alias"


REQUEST_INPUT = PromptInputContract(
    role="request",
    inline_flag="--request",
    file_flag="--request-file",
    compatibility_inline_flag="--prompt",
    compatibility_file_flag="--prompt-file",
    canonical_inline_source="inline:--prompt",
)
EXECUTION_PROMPT_INPUT = PromptInputContract(
    role="execution",
    inline_flag="--execution-prompt",
    file_flag="--execution-prompt-file",
    compatibility_inline_flag="--prompt",
    compatibility_file_flag="--prompt-file",
    canonical_inline_source="inline:--prompt",
)
REQUEST_LINEAGE_INPUT = PromptInputContract(
    role="request_lineage",
    inline_flag="--request",
    file_flag="--request-file",
    compatibility_inline_flag="--user-prompt",
    compatibility_file_flag="--user-prompt-file",
    canonical_inline_source="inline:--user-prompt",
)
PROMPT_INPUT_CONTRACTS = (REQUEST_INPUT, EXECUTION_PROMPT_INPUT, REQUEST_LINEAGE_INPUT)


SHIPPED_VISIBLE_COMMAND_TREE = (
    WorkflowCommand("init"),
    WorkflowCommand("summary"),
    WorkflowCommand("snapshot"),
    WorkflowCommand("stats"),
    WorkflowCommand(
        "local-repo",
        (
            WorkflowCommand("add"),
            WorkflowCommand("list"),
            WorkflowCommand("remove"),
        ),
    ),
    WorkflowCommand(
        "prompt",
        (
            WorkflowCommand("preview"),
            WorkflowCommand("tune"),
        ),
    ),
    WorkflowCommand(
        "attempts",
        (
            WorkflowCommand("summary"),
            WorkflowCommand("table"),
        ),
    ),
    WorkflowCommand(
        "codex",
        (
            WorkflowCommand("coverage"),
            WorkflowCommand("history"),
            WorkflowCommand("hook", (WorkflowCommand("stamp"),)),
        ),
    ),
    WorkflowCommand(
        "repo",
        (
            WorkflowCommand("install"),
            WorkflowCommand("bind"),
            WorkflowCommand("table"),
            WorkflowCommand("archive"),
            WorkflowCommand("unarchive"),
            WorkflowCommand("unbind"),
            WorkflowCommand("analyze"),
            WorkflowCommand("scaffold"),
            WorkflowCommand("update"),
            WorkflowCommand("refresh"),
        ),
    ),
    WorkflowCommand(
        "task",
        (
            WorkflowCommand("begin"),
            WorkflowCommand("show"),
            WorkflowCommand("recover"),
            WorkflowCommand("cancel"),
            WorkflowCommand("reopen"),
            WorkflowCommand("land"),
            WorkflowCommand("reconcile-landing"),
            WorkflowCommand("close"),
            WorkflowCommand("cleanup"),
        ),
    ),
    WorkflowCommand(
        "worktree",
        (
            WorkflowCommand("preflight"),
            WorkflowCommand("table"),
            WorkflowCommand("preview"),
            WorkflowCommand("start"),
            WorkflowCommand("show"),
            WorkflowCommand("land"),
            WorkflowCommand("close"),
            WorkflowCommand("cleanup"),
        ),
    ),
)

COMMAND_INVENTORY_SECTIONS = (
    CommandInventorySection(
        "project initialization, status, and fleet reporting",
        ("init", "summary", "snapshot", "stats"),
    ),
    CommandInventorySection("local registry", ("local-repo",)),
    CommandInventorySection("prompt composition", ("prompt",)),
    CommandInventorySection("attempt evidence", ("attempts",)),
    CommandInventorySection("Codex evidence and hooks", ("codex",)),
    CommandInventorySection("repo lifecycle", ("repo",)),
    CommandInventorySection("task execution and repair", ("task",)),
    CommandInventorySection("explicit low-level diagnosis and repair", ("worktree",)),
)

AGENT_WORKFLOW = AgentWorkflow(
    begin_command=(
        "./.VE/bin/blackdog task begin --project-root . --actor codex "
        "--execution-prompt-file \"$execution_prompt_file\" --prompt-mode skill "
        "--request-file \"$request_file\" --json"
    ),
    land_command=(
        './.VE/bin/blackdog task land --project-root . --summary "$completion_summary" '
        '"${validation_args[@]}" --json'
    ),
    preflight_command="./.VE/bin/blackdog worktree preflight --project-root .",
)


def visible_command_signature() -> tuple[tuple[str, tuple[object, ...]], ...]:
    return tuple(command.signature() for command in SHIPPED_VISIBLE_COMMAND_TREE)


def command_invocations(*roots: str) -> tuple[str, ...]:
    selected = set(roots)
    unknown = selected.difference(command.name for command in SHIPPED_VISIBLE_COMMAND_TREE)
    if unknown:
        raise ValueError(f"unknown visible command roots: {', '.join(sorted(unknown))}")
    return tuple(
        invocation
        for command in SHIPPED_VISIBLE_COMMAND_TREE
        if not selected or command.name in selected
        for invocation in command.leaf_invocations()
    )


SHIPPED_VISIBLE_COMMAND_INVOCATIONS = command_invocations()
REPO_OPERATOR_COMMANDS = command_invocations(
    "init",
    "summary",
    "snapshot",
    "stats",
    "local-repo",
    "prompt",
    "attempts",
    "codex",
    "repo",
)
TASK_AND_REPAIR_COMMANDS = command_invocations("task", "worktree")


def render_command_inventory_markdown() -> str:
    command_by_name = {command.name: command for command in SHIPPED_VISIBLE_COMMAND_TREE}
    lines: list[str] = []
    for section in COMMAND_INVENTORY_SECTIONS:
        invocations = tuple(
            invocation
            for root in section.roots
            for invocation in command_by_name[root].leaf_invocations()
        )
        rendered = ", ".join(f"`{invocation}`" for invocation in invocations)
        lines.append(f"- {section.label}: {rendered}")
    return "\n".join(lines)


__all__ = [
    "AGENT_WORKFLOW",
    "COMMAND_INVENTORY_SECTIONS",
    "EXECUTION_PROMPT_INPUT",
    "NEXT_ACTION_AUTHORITY_GUIDANCE",
    "PROMPT_INPUT_CONTRACTS",
    "PROMPT_INPUT_DISPOSAL_GUIDANCE",
    "REPO_OPERATOR_COMMANDS",
    "REQUEST_INPUT",
    "REQUEST_LINEAGE_INPUT",
    "SHIPPED_VISIBLE_COMMAND_INVOCATIONS",
    "SHIPPED_VISIBLE_COMMAND_TREE",
    "TASK_AND_REPAIR_COMMANDS",
    "TARGET_BRANCH_GUIDANCE",
    "AgentWorkflow",
    "CommandInventorySection",
    "PromptInputContract",
    "WorkflowCommand",
    "command_invocations",
    "render_command_inventory_markdown",
    "visible_command_signature",
]
