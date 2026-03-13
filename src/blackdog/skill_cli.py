from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import ConfigError, load_profile
from .scaffold import ScaffoldError, generate_project_skill, scaffold_project


def cmd_new_backlog(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    if not (root / "blackdog.toml").exists():
        scaffold_project(
            root,
            project_name=args.project_name or root.name,
            force=args.force,
        )
    profile = load_profile(root)
    skill_file = generate_project_skill(profile, force=args.force)
    print(json.dumps({"project_root": str(root), "skill_file": str(skill_file)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Blackdog skill scaffold generator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_new = subparsers.add_parser("new", help="Create a new project-local Blackdog skill scaffold")
    new_subparsers = p_new.add_subparsers(dest="new_command", required=True)

    p_new_backlog = new_subparsers.add_parser("backlog", help="Initialize backlog files and generate a project-specific skill")
    p_new_backlog.add_argument("--project-root", default=".")
    p_new_backlog.add_argument("--project-name", default=None)
    p_new_backlog.add_argument("--force", action="store_true")
    p_new_backlog.set_defaults(func=cmd_new_backlog)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, ScaffoldError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
