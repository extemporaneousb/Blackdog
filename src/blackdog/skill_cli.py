from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import ConfigError, load_profile
from .scaffold import ScaffoldError, bootstrap_project, refresh_project_skill


def cmd_new_backlog(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    profile, skill_file = bootstrap_project(
        root,
        project_name=args.project_name or root.name,
        force=args.force,
    )
    print(
        json.dumps(
            {
                "project_root": str(root),
                "profile": str(profile.paths.profile_file),
                "skill_file": str(skill_file),
            },
            indent=2,
        )
    )
    return 0


def cmd_refresh_backlog(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    profile = load_profile(root)
    skill_file = refresh_project_skill(profile)
    print(
        json.dumps(
            {
                "project_root": str(root),
                "profile": str(profile.paths.profile_file),
                "skill_file": str(skill_file),
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Blackdog skill scaffold generator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_new = subparsers.add_parser("new", help="Create a new project-local Blackdog skill scaffold")
    new_subparsers = p_new.add_subparsers(dest="new_command", required=True)

    p_new_backlog = new_subparsers.add_parser("backlog", help="Compatibility wrapper around `blackdog bootstrap`")
    p_new_backlog.add_argument("--project-root", default=".")
    p_new_backlog.add_argument("--project-name", default=None)
    p_new_backlog.add_argument("--force", action="store_true")
    p_new_backlog.set_defaults(func=cmd_new_backlog)

    p_refresh = subparsers.add_parser("refresh", help="Refresh an existing project-local Blackdog skill scaffold")
    refresh_subparsers = p_refresh.add_subparsers(dest="refresh_command", required=True)

    p_refresh_backlog = refresh_subparsers.add_parser("backlog", help="Regenerate the current project-local Blackdog backlog skill")
    p_refresh_backlog.add_argument("--project-root", default=".")
    p_refresh_backlog.set_defaults(func=cmd_refresh_backlog)

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
