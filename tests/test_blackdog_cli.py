from __future__ import annotations

from contextlib import chdir, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import blackdog.wtam as wtam
from blackdog.contract import managed_skill_relative_path
from blackdog.observability import read_lifecycle_observability
from blackdog.prompt_artifacts import PROMPT_ARTIFACT_MAX_BYTES
from blackdog_core.backlog import finish_task, load_planning_state, start_task, upsert_workset
from blackdog_core.codex_sessions import codex_task_context_path
from blackdog_core.profile import load_profile
from blackdog_core.state import (
    StoreError,
    TaskClaimRecord,
    ValidationRecord,
    append_event,
    append_event_once,
    create_prompt_receipt,
    load_events,
    load_runtime_state,
    merge_workset_runtime,
    now_iso,
    save_runtime_state,
)
from blackdog_cli.main import main as blackdog_main
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class BlackdogCliTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("CLI Demo")
        subprocess.run(["git", "-C", str(self.root), "add", "blackdog.toml"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Add Blackdog profile"],
            check=True,
            capture_output=True,
            text=True,
        )

    def run_cli(self, *args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with chdir(cwd or Path.cwd()), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def put_workset(self, payload: dict[str, object]) -> tuple[int, str, str]:
        with patch.dict(os.environ, {"BLACKDOG_ENABLE_WORKSET_COMMANDS": "1"}, clear=False):
            return self.run_cli(
                "workset",
                "put",
                "--project-root",
                str(self.root),
                "--json",
                json.dumps(payload),
            )

    def install_repo_runtime(self) -> None:
        exit_code, _, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--source-root",
            str(REPO_ROOT),
        )
        self.assertEqual(exit_code, 0, stderr)
        profile = load_profile(self.root)
        skill_metadata_path = managed_skill_relative_path(profile).parent / "agents" / "openai.yaml"
        tracked_paths = [
            "blackdog.toml",
            "AGENTS.md",
            str(managed_skill_relative_path(profile)),
        ]
        if (self.root / skill_metadata_path).exists():
            tracked_paths.append(str(skill_metadata_path))
        subprocess.run(
            ["git", "-C", str(self.root), "add", *tracked_paths],
            check=True,
            capture_output=True,
            text=True,
        )
        if self.git_output("status", "--short"):
            subprocess.run(
                ["git", "-C", str(self.root), "commit", "-m", "Add Blackdog repo runtime"],
                check=True,
                capture_output=True,
                text=True,
            )

    def _runtime_transition_fault_fixture(
        self,
        *,
        workset_id: str,
        operation: str,
    ):
        profile = load_profile(self.root)
        task_id = "STATE-A"
        upsert_workset(
            profile,
            {
                "id": workset_id,
                "title": "Runtime transition fault fixture",
                "tasks": [
                    {
                        "id": task_id,
                        "title": "Transition state",
                        "intent": "exercise durable transition recovery",
                    }
                ],
            },
        )
        if operation == "reopen":
            setup = wtam.cancel_task(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor="setup-owner",
                summary="prepare canceled state",
            )
            self.assertEqual(setup.operation_status, "succeeded")
        return profile, task_id

    @staticmethod
    def _runtime_transition_fault_expectation(
        *,
        boundary: str,
        position: str,
    ) -> tuple[bool, bool, str, tuple[str, ...]]:
        if boundary == "request" and position == "before":
            return False, False, "none", ()
        if boundary == "request":
            return True, False, "preflight", ("task.runtime-transition.request",)
        if boundary == "decision" and position == "before":
            return True, False, "preflight", ("task.runtime-transition.request",)
        if boundary == "decision":
            return (
                True,
                False,
                "preflight",
                (
                    "task.runtime-transition.request",
                    "task.runtime-transition.decision",
                ),
            )
        if position == "before":
            return (
                True,
                False,
                "runtime_finalized",
                (
                    "task.runtime-transition.request",
                    "task.runtime-transition.decision",
                ),
            )
        return (
            True,
            True,
            "event_finalized",
            (
                "task.runtime-transition.request",
                "task.runtime-transition.decision",
                "owned",
            ),
        )

    def _stale_release_fault_fixture(
        self,
        *,
        workset_id: str,
        topology: str,
        null_attempt_id: bool = False,
    ):
        profile = load_profile(self.root)
        task_id = "STALE-A"
        tasks = [
            {
                "id": task_id,
                "title": "Release stale A",
                "intent": "exercise stale-claim transaction recovery",
            }
        ]
        if topology == "remaining":
            tasks.append(
                {
                    "id": "STALE-B",
                    "title": "Preserve B",
                    "intent": "preserve an unrelated task claim",
                }
            )
        upsert_workset(
            profile,
            {
                "id": workset_id,
                "title": "Stale release fault fixture",
                "tasks": tasks,
            },
        )
        attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="stale-owner",
            prompt_receipt=create_prompt_receipt(
                "Release stale claim transactionally.", source="unit-test"
            ),
        )
        runtime = load_runtime_state(profile.paths)
        runtime_workset = next(
            row for row in runtime.worksets if row.workset_id == workset_id
        )
        active_attempt = next(
            row for row in runtime_workset.attempts
            if row.attempt_id == attempt.attempt_id
        )
        target_claim = next(
            row for row in runtime_workset.task_claims
            if row.task_id == task_id
        )
        claims = [
            replace(
                target_claim,
                attempt_id=None if null_attempt_id else target_claim.attempt_id,
            )
        ]
        if topology == "remaining":
            claims.append(
                TaskClaimRecord(
                    task_id="STALE-B",
                    actor="stale-owner",
                    execution_model=target_claim.execution_model,
                    claimed_at=target_claim.claimed_at,
                    attempt_id=None,
                    note="preserve byte-for-byte",
                )
            )
        stale_attempt = replace(
            active_attempt,
            status="blocked",
            ended_at=now_iso(),
            summary="interrupted before stale claim release",
            elapsed_seconds=1,
        )
        stale_runtime = merge_workset_runtime(
            runtime,
            workset_id=workset_id,
            task_ids={str(task["id"]) for task in tasks},
            incoming_records=None,
            incoming_workset_claim=(
                None
                if topology == "no-workset"
                else runtime_workset.workset_claim
            ),
            incoming_task_claims=tuple(claims),
            incoming_attempts=(stale_attempt,),
        )
        save_runtime_state(profile.paths, stale_runtime)
        return profile, task_id, stale_runtime

    def _pending_stale_release_with_active_sibling(self, workset_id: str):
        profile = load_profile(self.root)
        upsert_workset(
            profile,
            {
                "id": workset_id,
                "title": "Pending stale release product gate",
                "tasks": [
                    {
                        "id": "STALE-A",
                        "title": "Stale owner",
                        "intent": "own the pending release",
                    },
                    {
                        "id": "ACTIVE-B",
                        "title": "Active sibling",
                        "intent": "exercise product preflight",
                    },
                ],
            },
        )
        stale_attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id="STALE-A",
            actor="owner",
            prompt_receipt=wtam.create_prompt_receipt(
                "Leave A stale.", source="unit-test"
            ),
        )
        runtime = load_runtime_state(profile.paths)
        save_runtime_state(
            profile.paths,
            merge_workset_runtime(
                runtime,
                workset_id=workset_id,
                task_ids={"STALE-A", "ACTIVE-B"},
                incoming_records=None,
                incoming_attempts=(
                    replace(
                        stale_attempt,
                        status="blocked",
                        ended_at=now_iso(),
                        summary="stale owner interrupted",
                    ),
                ),
            ),
        )
        active_attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id="ACTIVE-B",
            actor="owner",
            prompt_receipt=wtam.create_prompt_receipt(
                "Keep B active.", source="unit-test"
            ),
        )
        runtime_path = profile.paths.runtime_file.resolve()
        real_replace = os.replace
        injected = False

        def fail_runtime(source, destination):
            nonlocal injected
            if Path(destination).resolve() == runtime_path and not injected:
                injected = True
                raise OSError("leave stale release at its durable decision")
            return real_replace(source, destination)

        with patch("blackdog_core.state.os.replace", side_effect=fail_runtime):
            partial = wtam.recover_task(
                profile,
                workset_id=workset_id,
                task_id="STALE-A",
                release_stale_claim=True,
                status="failed",
                summary="repair A before another claim mutation",
            )
        self.assertEqual(partial.operation_status, "partial")
        self.assertEqual(partial.mutation_phase, "preflight")
        return profile, active_attempt, partial

    @staticmethod
    def _stale_release_fault_expectation(
        *,
        topology: str,
        boundary: str,
        position: str,
    ) -> tuple[bool, bool, str, tuple[str, ...]]:
        request = "task.stale-claim-release.request"
        decision = "task.stale-claim-release.decision"
        if boundary == "request" and position == "before":
            return False, False, "none", ()
        if boundary == "request":
            return True, False, "preflight", (request,)
        if boundary == "decision" and position == "before":
            return True, False, "preflight", (request,)
        if boundary == "decision":
            return True, False, "preflight", (request, decision)
        if boundary == "runtime" and position == "before":
            return True, False, "preflight", (request, decision)
        if boundary == "runtime":
            return True, False, "runtime_finalized", (request, decision)
        if boundary == "task" and position == "before":
            return True, False, "runtime_finalized", (request, decision)
        if boundary == "task" and topology == "last":
            return (
                True,
                False,
                "event_finalization_partial",
                (request, decision, "task.release"),
            )
        if boundary == "task":
            return (
                True,
                True,
                "event_finalized",
                (request, decision, "task.release"),
            )
        if position == "before":
            return (
                True,
                False,
                "event_finalization_partial",
                (request, decision, "task.release"),
            )
        return (
            True,
            True,
            "event_finalized",
            (request, decision, "task.release", "workset.release"),
        )

    def test_runtime_transition_store_faults_return_structured_direct_results(self) -> None:
        real_append_event_once = append_event_once
        for operation in ("cancel", "reopen"):
            for boundary in ("request", "decision", "owned"):
                for position in ("before", "after"):
                    case = f"direct-{operation}-{boundary}-{position}"
                    with self.subTest(case=case):
                        profile, task_id = self._runtime_transition_fault_fixture(
                            workset_id=case,
                            operation=operation,
                        )
                        actor = f"{case}-owner"
                        target_type = {
                            "request": "task.runtime-transition.request",
                            "decision": "task.runtime-transition.decision",
                            "owned": f"task.{operation}",
                        }[boundary]
                        injected = False

                        def fail_transition(*args, **kwargs):
                            nonlocal injected
                            if kwargs.get("event_type") != target_type or injected:
                                return real_append_event_once(*args, **kwargs)
                            injected = True
                            if position == "before":
                                raise StoreError(f"injected before {target_type}")
                            real_append_event_once(*args, **kwargs)
                            raise StoreError(f"injected after {target_type}")

                        call = wtam.cancel_task if operation == "cancel" else wtam.reopen_task
                        call_kwargs = {
                            "profile": profile,
                            "workset_id": case,
                            "task_id": task_id,
                            "actor": actor,
                            "summary": f"{operation} through injected storage fault",
                        }
                        if operation == "cancel":
                            call_kwargs.update(
                                failure_class="unknown",
                                recovery_action="inspect_transition",
                            )
                        with patch(
                            "blackdog_core.backlog.append_event_once",
                            side_effect=fail_transition,
                        ):
                            result = call(**call_kwargs)
                        expected_started, expected_completed, phase, prefix = (
                            self._runtime_transition_fault_expectation(
                                boundary=boundary,
                                position=position,
                            )
                        )
                        self.assertEqual(result.operation_status, "partial")
                        self.assertEqual(result.mutation_started, expected_started)
                        self.assertEqual(result.mutation_completed, expected_completed)
                        self.assertEqual(result.mutation_phase, phase)
                        self.assertEqual(
                            result.next_action.action_id,
                            f"retry_task_{operation}_finalization",
                        )
                        request_id = result["transition_request_event_id"]
                        decision_id = result["transition_decision_event_id"]
                        owned_id = result["transition_owned_event_id"]
                        self.assertIsNotNone(request_id)
                        self.assertIn(
                            f"--transition-request={request_id}",
                            result.next_action.argv,
                        )
                        decision_durable = (
                            "task.runtime-transition.decision" in prefix
                        )
                        self.assertEqual(
                            f"--transition-decision={decision_id}"
                            in result.next_action.argv,
                            decision_durable,
                        )
                        events = load_events(profile.paths.events_file)
                        durable_types = []
                        for event in events:
                            if event.get("event_id") == request_id:
                                durable_types.append(event["type"])
                            elif event.get("event_id") == decision_id:
                                durable_types.append(event["type"])
                            elif event.get("event_id") == owned_id:
                                durable_types.append("owned")
                        self.assertEqual(tuple(durable_types), prefix)

                        retry_kwargs = {
                            **call_kwargs,
                            "transition_request_event_id": request_id,
                            "transition_decision_event_id": (
                                decision_id if decision_durable else None
                            ),
                        }
                        partial_runtime = profile.paths.runtime_file.read_bytes()
                        partial_events = profile.paths.events_file.read_bytes()
                        repaired = call(**retry_kwargs)
                        self.assertEqual(repaired.operation_status, "succeeded")
                        if expected_completed:
                            self.assertEqual(
                                profile.paths.runtime_file.read_bytes(),
                                partial_runtime,
                            )
                            self.assertEqual(
                                profile.paths.events_file.read_bytes(),
                                partial_events,
                            )
                        runtime_before_third = profile.paths.runtime_file.read_bytes()
                        events_before_third = profile.paths.events_file.read_bytes()
                        third = call(**retry_kwargs)
                        self.assertEqual(third.operation_status, "succeeded")
                        self.assertFalse(third.mutation_started)
                        self.assertEqual(
                            profile.paths.runtime_file.read_bytes(),
                            runtime_before_third,
                        )
                        self.assertEqual(
                            profile.paths.events_file.read_bytes(),
                            events_before_third,
                        )

    def test_runtime_transition_store_faults_return_structured_cli_results(self) -> None:
        real_append_event_once = append_event_once
        for operation in ("cancel", "reopen"):
            for boundary in ("request", "decision", "owned"):
                for position in ("before", "after"):
                    case = f"cli-{operation}-{boundary}-{position}"
                    with self.subTest(case=case):
                        profile, task_id = self._runtime_transition_fault_fixture(
                            workset_id=case,
                            operation=operation,
                        )
                        actor = f"{case}-owner"
                        target_type = {
                            "request": "task.runtime-transition.request",
                            "decision": "task.runtime-transition.decision",
                            "owned": f"task.{operation}",
                        }[boundary]
                        injected = False

                        def fail_transition(*args, **kwargs):
                            nonlocal injected
                            if kwargs.get("event_type") != target_type or injected:
                                return real_append_event_once(*args, **kwargs)
                            injected = True
                            if position == "before":
                                raise StoreError(f"injected before {target_type}")
                            real_append_event_once(*args, **kwargs)
                            raise StoreError(f"injected after {target_type}")

                        base_argv = [
                            "task",
                            operation,
                            "--project-root",
                            str(self.root),
                            "--workset",
                            case,
                            "--task",
                            task_id,
                            "--actor",
                            actor,
                            "--summary",
                            f"{operation} through injected storage fault",
                        ]
                        if operation == "cancel":
                            base_argv.extend(
                                [
                                    "--failure-class",
                                    "unknown",
                                    "--recovery-action",
                                    "inspect_transition",
                                ]
                            )
                        base_argv.append("--json")
                        with patch(
                            "blackdog_core.backlog.append_event_once",
                            side_effect=fail_transition,
                        ):
                            exit_code, stdout, stderr = self.run_cli(*base_argv)
                        self.assertEqual(exit_code, 1, stderr)
                        payload = json.loads(stdout)["task_state"]
                        expected_started, expected_completed, phase, prefix = (
                            self._runtime_transition_fault_expectation(
                                boundary=boundary,
                                position=position,
                            )
                        )
                        self.assertEqual(payload["operation_status"], "partial")
                        self.assertEqual(payload["mutation_started"], expected_started)
                        self.assertEqual(payload["mutation_completed"], expected_completed)
                        self.assertEqual(payload["mutation_phase"], phase)
                        self.assertEqual(
                            payload["next_action"]["action_id"],
                            f"retry_task_{operation}_finalization",
                        )
                        request_id = payload["transition_request_event_id"]
                        decision_id = payload["transition_decision_event_id"]
                        owned_id = payload["transition_owned_event_id"]
                        action_argv = payload["next_action"]["argv"]
                        self.assertIn(
                            f"--transition-request={request_id}",
                            action_argv,
                        )
                        decision_durable = (
                            "task.runtime-transition.decision" in prefix
                        )
                        self.assertEqual(
                            f"--transition-decision={decision_id}" in action_argv,
                            decision_durable,
                        )
                        durable_types = []
                        for event in load_events(profile.paths.events_file):
                            if event.get("event_id") == request_id:
                                durable_types.append(event["type"])
                            elif event.get("event_id") == decision_id:
                                durable_types.append(event["type"])
                            elif event.get("event_id") == owned_id:
                                durable_types.append("owned")
                        self.assertEqual(tuple(durable_types), prefix)

                        retry_argv = list(base_argv[:-1])
                        retry_argv.extend(
                            ["--transition-request", request_id]
                        )
                        if decision_durable:
                            retry_argv.extend(
                                ["--transition-decision", decision_id]
                            )
                        retry_argv.append("--json")
                        partial_runtime = profile.paths.runtime_file.read_bytes()
                        partial_events = profile.paths.events_file.read_bytes()
                        exit_code, stdout, stderr = self.run_cli(*retry_argv)
                        self.assertEqual(exit_code, 0, stderr)
                        repaired = json.loads(stdout)["task_state"]
                        self.assertEqual(repaired["operation_status"], "succeeded")
                        if expected_completed:
                            self.assertEqual(
                                profile.paths.runtime_file.read_bytes(),
                                partial_runtime,
                            )
                            self.assertEqual(
                                profile.paths.events_file.read_bytes(),
                                partial_events,
                            )
                        runtime_before_third = profile.paths.runtime_file.read_bytes()
                        events_before_third = profile.paths.events_file.read_bytes()
                        exit_code, stdout, stderr = self.run_cli(*retry_argv)
                        self.assertEqual(exit_code, 0, stderr)
                        third = json.loads(stdout)["task_state"]
                        self.assertFalse(third["mutation_started"])
                        self.assertEqual(
                            profile.paths.runtime_file.read_bytes(),
                            runtime_before_third,
                        )
                        self.assertEqual(
                            profile.paths.events_file.read_bytes(),
                            events_before_third,
                        )

    def _assert_stale_release_fault_case(
        self,
        *,
        surface: str,
        topology: str,
        boundary: str,
        position: str,
    ) -> None:
        case = f"stale-{surface}-{topology}-{boundary}-{position}"
        profile, task_id, stale_runtime = self._stale_release_fault_fixture(
            workset_id=case,
            topology=topology,
            null_attempt_id=topology == "no-workset",
        )
        real_append_event_once = append_event_once
        real_replace = os.replace
        injected = False
        target_event_type = {
            "request": "task.stale-claim-release.request",
            "decision": "task.stale-claim-release.decision",
            "task": "task.release",
            "workset": "workset.release",
        }.get(boundary)
        runtime_path = profile.paths.runtime_file.resolve()

        def fail_event(*args, **kwargs):
            nonlocal injected
            if kwargs.get("event_type") != target_event_type or injected:
                return real_append_event_once(*args, **kwargs)
            injected = True
            if position == "before":
                raise StoreError(f"injected before {target_event_type}")
            real_append_event_once(*args, **kwargs)
            raise StoreError(f"injected after {target_event_type}")

        def fail_runtime_replace(source, destination):
            nonlocal injected
            if Path(destination).resolve() != runtime_path or injected:
                return real_replace(source, destination)
            injected = True
            if position == "before":
                raise OSError("injected before stale runtime replacement")
            real_replace(source, destination)
            raise OSError("injected after stale runtime replacement")

        direct_kwargs = {
            "profile": profile,
            "workset_id": case,
            "task_id": task_id,
            "release_stale_claim": True,
            "status": "abandoned",
            "summary": "release stale claim through injected fault",
            "note": "fault matrix",
        }
        cli_argv = [
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--workset",
            case,
            "--task",
            task_id,
            "--release-stale-claim",
            "--status",
            "abandoned",
            "--summary",
            "release stale claim through injected fault",
            "--note",
            "fault matrix",
            "--json",
        ]

        def invoke_initial():
            if surface == "direct":
                result = wtam.recover_task(**direct_kwargs)
                return result, result.to_dict(), None
            exit_code, stdout, stderr = self.run_cli(*cli_argv)
            return None, json.loads(stdout)["recovery"], (exit_code, stderr)

        if boundary == "runtime":
            with patch(
                "blackdog_core.state.os.replace",
                side_effect=fail_runtime_replace,
            ):
                direct_result, payload, cli_status = invoke_initial()
        else:
            with patch(
                "blackdog_core.backlog.append_event_once",
                side_effect=fail_event,
            ):
                direct_result, payload, cli_status = invoke_initial()
        if cli_status is not None:
            self.assertEqual(cli_status[0], 1, cli_status[1])
        expected_started, expected_completed, phase, prefix = (
            self._stale_release_fault_expectation(
                topology=topology,
                boundary=boundary,
                position=position,
            )
        )
        self.assertEqual(payload["operation_status"], "partial")
        self.assertEqual(payload["mutation_started"], expected_started)
        self.assertEqual(payload["mutation_completed"], expected_completed)
        self.assertEqual(payload["mutation_phase"], phase)
        runtime_finalized = phase in {
            "runtime_finalized",
            "event_finalization_partial",
            "event_finalized",
        }
        self.assertEqual(payload["released_stale_claim"], runtime_finalized)
        self.assertEqual(
            payload["stale_claim_release_runtime_finalized"],
            runtime_finalized,
        )
        self.assertEqual(
            payload["stale_claim_release_event_finalized"],
            phase == "event_finalized",
        )
        self.assertEqual(
            payload["stale_claim_release_finalization_pending"],
            phase != "event_finalized",
        )
        self.assertEqual(
            payload["next_action"]["action_id"],
            "retry_stale_claim_release_finalization",
        )
        request_id = payload["stale_claim_release_request_event_id"]
        decision_id = payload["stale_claim_release_decision_event_id"]
        task_event_id = payload["stale_claim_release_task_event_id"]
        workset_event_id = payload["stale_claim_release_workset_event_id"]
        self.assertIsNotNone(request_id)
        action_argv = payload["next_action"]["argv"]
        self.assertIn(
            f"--stale-claim-release-request={request_id}",
            action_argv,
        )
        decision_durable = "task.stale-claim-release.decision" in prefix
        self.assertEqual(
            f"--stale-claim-release-decision={decision_id}" in action_argv,
            decision_durable,
        )
        durable_types = []
        for event in load_events(profile.paths.events_file):
            if event.get("event_id") == request_id:
                durable_types.append(event["type"])
            elif event.get("event_id") == decision_id:
                durable_types.append(event["type"])
            elif event.get("event_id") == task_event_id:
                durable_types.append(event["type"])
            elif event.get("event_id") == workset_event_id:
                durable_types.append(event["type"])
        self.assertEqual(tuple(durable_types), prefix)

        if request_id in {event.get("event_id") for event in load_events(profile.paths.events_file)} and not expected_completed:
            for observed in (
                wtam.recover_task(
                    profile,
                    workset_id=case,
                    task_id=task_id,
                ),
                wtam.show_task(
                    profile,
                    workset_id=case,
                    task_id=task_id,
                ),
            ):
                self.assertFalse(observed.mutation_started)
                self.assertEqual(
                    observed.next_action.action_id,
                    "retry_stale_claim_release_finalization",
                )
                self.assertIn(
                    f"--stale-claim-release-request={request_id}",
                    observed.next_action.argv,
                )

        retry_direct = {
            **direct_kwargs,
            "stale_claim_release_request_event_id": request_id,
            "stale_claim_release_decision_event_id": (
                decision_id if decision_durable else None
            ),
        }
        retry_cli = list(cli_argv[:-1])
        retry_cli.extend(["--stale-claim-release-request", request_id])
        if decision_durable:
            retry_cli.extend(["--stale-claim-release-decision", decision_id])
        retry_cli.append("--json")
        partial_runtime = profile.paths.runtime_file.read_bytes()
        partial_events = profile.paths.events_file.read_bytes()
        if surface == "direct":
            repaired_result = wtam.recover_task(**retry_direct)
            repaired = repaired_result.to_dict()
        else:
            exit_code, stdout, stderr = self.run_cli(*retry_cli)
            self.assertEqual(exit_code, 0, stderr)
            repaired = json.loads(stdout)["recovery"]
        self.assertEqual(repaired["operation_status"], "succeeded")
        if expected_completed:
            self.assertEqual(profile.paths.runtime_file.read_bytes(), partial_runtime)
            self.assertEqual(profile.paths.events_file.read_bytes(), partial_events)

        current = next(
            row for row in load_runtime_state(profile.paths).worksets
            if row.workset_id == case
        )
        prior = next(
            row for row in stale_runtime.worksets if row.workset_id == case
        )
        self.assertEqual(current.attempts, prior.attempts)
        self.assertEqual(
            [claim.task_id for claim in current.task_claims],
            ["STALE-B"] if topology == "remaining" else [],
        )
        self.assertEqual(
            current.workset_claim,
            prior.workset_claim if topology == "remaining" else None,
        )
        target_state = next(
            state for state in current.task_states if state.task_id == task_id
        )
        self.assertEqual(target_state.status, "canceled")
        runtime_before_third = profile.paths.runtime_file.read_bytes()
        events_before_third = profile.paths.events_file.read_bytes()
        if surface == "direct":
            third = wtam.recover_task(**retry_direct).to_dict()
        else:
            exit_code, stdout, stderr = self.run_cli(*retry_cli)
            self.assertEqual(exit_code, 0, stderr)
            third = json.loads(stdout)["recovery"]
        self.assertFalse(third["mutation_started"])
        self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before_third)
        self.assertEqual(profile.paths.events_file.read_bytes(), events_before_third)

    def test_stale_release_fault_matrix_direct(self) -> None:
        for topology in ("last", "remaining", "no-workset"):
            boundaries = (
                ("request", "decision", "runtime", "task", "workset")
                if topology == "last"
                else ("request", "decision", "runtime", "task")
            )
            for boundary in boundaries:
                for position in ("before", "after"):
                    with self.subTest(
                        topology=topology,
                        boundary=boundary,
                        position=position,
                    ):
                        self._assert_stale_release_fault_case(
                            surface="direct",
                            topology=topology,
                            boundary=boundary,
                            position=position,
                        )

    def test_stale_release_fault_matrix_cli(self) -> None:
        for topology in ("last", "remaining", "no-workset"):
            boundaries = (
                ("request", "decision", "runtime", "task", "workset")
                if topology == "last"
                else ("request", "decision", "runtime", "task")
            )
            for boundary in boundaries:
                for position in ("before", "after"):
                    with self.subTest(
                        topology=topology,
                        boundary=boundary,
                        position=position,
                    ):
                        self._assert_stale_release_fault_case(
                            surface="cli",
                            topology=topology,
                            boundary=boundary,
                            position=position,
                        )

    def test_pending_stale_release_blocks_claim_mutating_product_effects(self) -> None:
        workset_id = "stale-product-preflight"
        profile, active_attempt, partial = (
            self._pending_stale_release_with_active_sibling(workset_id)
        )
        request_guard = (
            f"--stale-claim-release-request="
            f"{partial['stale_claim_release_request_event_id']}"
        )
        runtime_before = profile.paths.runtime_file.read_bytes()
        events_before = profile.paths.events_file.read_bytes()
        refs_before = self.git_output("show-ref", "--heads")

        for observed in (
            wtam.show_task(
                profile,
                workset_id=workset_id,
                task_id="ACTIVE-B",
            ),
            wtam.recover_task(
                profile,
                workset_id=workset_id,
                task_id="ACTIVE-B",
            ),
            wtam.cancel_task(
                profile,
                workset_id=workset_id,
                task_id="STALE-A",
                actor="owner",
                summary="must repair exact stale release first",
            ),
            wtam.reopen_task(
                profile,
                workset_id=workset_id,
                task_id="STALE-A",
                actor="owner",
                summary="must repair exact stale release first",
            ),
        ):
            self.assertIn(observed.operation_status, {"observed", "blocked"})
            self.assertEqual(
                observed.next_action.action_id,
                "retry_stale_claim_release_finalization",
            )
            self.assertIn("--task=STALE-A", observed.next_action.argv)
            self.assertIn(request_guard, observed.next_action.argv)
        self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(profile.paths.events_file.read_bytes(), events_before)

        calls = (
            (
                "begin",
                "start_task_worktree",
                lambda: wtam.begin_task_worktree(
                    profile,
                    actor="owner",
                    prompt="Do not start B before repairing A.",
                    workset_id=workset_id,
                    task_id="ACTIVE-B",
                ),
            ),
            (
                "land",
                "land_task_worktree",
                lambda: wtam.land_task(
                    profile,
                    workset_id=workset_id,
                    task_id="ACTIVE-B",
                    actor="owner",
                    summary="Do not mutate Git before repairing A.",
                ),
            ),
            (
                "close",
                "close_task_worktree",
                lambda: wtam.close_task(
                    profile,
                    workset_id=workset_id,
                    task_id="ACTIVE-B",
                    actor="owner",
                    status="failed",
                    summary="Do not close B before repairing A.",
                ),
            ),
        )
        for operation, mutation_name, invoke in calls:
            with self.subTest(operation=operation):
                with patch.object(wtam, mutation_name) as mutation:
                    result = invoke()
                mutation.assert_not_called()
                self.assertEqual(result.operation_status, "blocked")
                self.assertFalse(result.mutation_started)
                self.assertEqual(
                    result.next_action.action_id,
                    "retry_stale_claim_release_finalization",
                )
                self.assertIn("--task=STALE-A", result.next_action.argv)
                self.assertIn(request_guard, result.next_action.argv)
                self.assertEqual(
                    result["stale_claim_release_owner_task_id"],
                    "STALE-A",
                )
                self.assertEqual(
                    profile.paths.runtime_file.read_bytes(), runtime_before
                )
                self.assertEqual(
                    profile.paths.events_file.read_bytes(), events_before
                )
                self.assertEqual(self.git_output("show-ref", "--heads"), refs_before)
        runtime = load_runtime_state(profile.paths)
        self.assertEqual(
            next(
                row for row in runtime.worksets
                if row.workset_id == workset_id
                for row in row.attempts
                if row.attempt_id == active_attempt.attempt_id
            ).status,
            "in_progress",
        )

    def test_stale_release_retry_argv_replays_exactly_and_hidden_guards_stay_hidden(self) -> None:
        workset_id = "stale-parser-replay"
        profile, task_id, _runtime = self._stale_release_fault_fixture(
            workset_id=workset_id,
            topology="last",
        )
        real_append = append_event_once
        injected = False

        def fail_task_event(*args, **kwargs):
            nonlocal injected
            if kwargs.get("event_type") == "task.release" and not injected:
                injected = True
                raise StoreError("leave parser replay pending")
            return real_append(*args, **kwargs)

        with patch(
            "blackdog_core.backlog.append_event_once",
            side_effect=fail_task_event,
        ):
            partial = wtam.recover_task(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                release_stale_claim=True,
                status="abandoned",
                summary="repair through exact parser replay",
            )
        self.assertEqual(partial.operation_status, "partial")
        self.assertEqual(partial.mutation_phase, "runtime_finalized")
        action_argv = list(partial.next_action.argv)
        self.assertEqual(
            partial.next_action.action_id,
            "retry_stale_claim_release_finalization",
        )
        self.assertTrue(action_argv[0].endswith("blackdog"))
        self.assertTrue(
            any(value.startswith("--stale-claim-release-request=") for value in action_argv)
        )
        self.assertTrue(
            any(value.startswith("--stale-claim-release-decision=") for value in action_argv)
        )

        exit_code, stdout, stderr = self.run_cli(*action_argv[1:], "--json")
        self.assertEqual(exit_code, 0, stderr)
        repaired = json.loads(stdout)["recovery"]
        self.assertEqual(repaired["operation_status"], "succeeded")
        self.assertTrue(repaired["released_stale_claim"])
        self.assertFalse(repaired["stale_claim_release_finalization_pending"])
        repaired_runtime = profile.paths.runtime_file.read_bytes()
        repaired_events = profile.paths.events_file.read_bytes()
        exit_code, stdout, stderr = self.run_cli(*action_argv[1:], "--json")
        self.assertEqual(exit_code, 0, stderr)
        idempotent = json.loads(stdout)["recovery"]
        self.assertFalse(idempotent["mutation_started"])
        self.assertEqual(profile.paths.runtime_file.read_bytes(), repaired_runtime)
        self.assertEqual(profile.paths.events_file.read_bytes(), repaired_events)

        reopened = wtam.reopen_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="stale-owner",
            summary="later progress supersedes the completed release",
        )
        self.assertEqual(reopened.operation_status, "succeeded")
        progressed_runtime = profile.paths.runtime_file.read_bytes()
        progressed_events = profile.paths.events_file.read_bytes()
        request_count = sum(
            event.get("type") == "task.stale-claim-release.request"
            and event.get("payload", {}).get("workset_id") == workset_id
            for event in load_events(profile.paths.events_file)
        )
        exit_code, stdout, stderr = self.run_cli(*action_argv[1:], "--json")
        self.assertEqual(exit_code, 1, stderr)
        superseded = json.loads(stdout)["recovery"]
        self.assertEqual(superseded["operation_status"], "blocked")
        self.assertEqual(
            superseded["next_action"]["action_id"],
            "inspect_stale_claim_release_conflict",
        )
        self.assertEqual(superseded["next_action"]["argv"], [])
        self.assertEqual(profile.paths.runtime_file.read_bytes(), progressed_runtime)
        self.assertEqual(profile.paths.events_file.read_bytes(), progressed_events)
        self.assertEqual(
            sum(
                event.get("type") == "task.stale-claim-release.request"
                and event.get("payload", {}).get("workset_id") == workset_id
                for event in load_events(profile.paths.events_file)
            ),
            request_count,
        )

        help_stdout = io.StringIO()
        help_stderr = io.StringIO()
        with redirect_stdout(help_stdout), redirect_stderr(help_stderr):
            with self.assertRaises(SystemExit) as raised:
                blackdog_main(["task", "recover", "--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = help_stdout.getvalue()
        self.assertNotIn("--stale-claim-release-request", help_text)
        self.assertNotIn("--stale-claim-release-decision", help_text)

    def test_stale_release_guard_and_semantic_conflicts_are_commandless_and_write_nothing(self) -> None:
        cases = ("foreign-request", "foreign-decision", "different-semantics")
        for suffix in cases:
            with self.subTest(case=suffix):
                workset_id = f"stale-guard-{suffix}"
                profile, task_id, _runtime = self._stale_release_fault_fixture(
                    workset_id=workset_id,
                    topology="last",
                )
                runtime_path = profile.paths.runtime_file.resolve()
                real_replace = os.replace
                injected = False

                def fail_runtime(source, destination):
                    nonlocal injected
                    if Path(destination).resolve() == runtime_path and not injected:
                        injected = True
                        raise OSError("leave guarded release at decision")
                    return real_replace(source, destination)

                with patch(
                    "blackdog_core.state.os.replace",
                    side_effect=fail_runtime,
                ):
                    partial = wtam.recover_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        release_stale_claim=True,
                        status="failed",
                        summary="canonical guarded semantics",
                    )
                request_id = partial["stale_claim_release_request_event_id"]
                decision_id = partial["stale_claim_release_decision_event_id"]
                call_kwargs = {
                    "profile": profile,
                    "workset_id": workset_id,
                    "task_id": task_id,
                    "release_stale_claim": True,
                    "status": "failed",
                    "summary": "canonical guarded semantics",
                    "stale_claim_release_request_event_id": request_id,
                    "stale_claim_release_decision_event_id": decision_id,
                }
                if suffix == "foreign-request":
                    call_kwargs["stale_claim_release_request_event_id"] = "0" * 64
                    call_kwargs["stale_claim_release_decision_event_id"] = None
                elif suffix == "foreign-decision":
                    call_kwargs["stale_claim_release_decision_event_id"] = "1" * 64
                else:
                    call_kwargs["summary"] = "different durable semantics"
                runtime_before = profile.paths.runtime_file.read_bytes()
                events_before = profile.paths.events_file.read_bytes()
                blocked = wtam.recover_task(**call_kwargs)
                self.assertEqual(blocked.operation_status, "blocked")
                self.assertFalse(blocked.mutation_started)
                self.assertEqual(
                    blocked.next_action.action_id,
                    "inspect_stale_claim_release_conflict",
                )
                self.assertEqual(blocked.next_action.argv, ())
                self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
                self.assertEqual(profile.paths.events_file.read_bytes(), events_before)

    def test_workset_put_is_disabled_without_explicit_opt_in(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            json.dumps({"id": "accidental", "title": "Accidental", "tasks": []}),
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("direct workset authoring is disabled by default", stderr)
        self.assertIn("BLACKDOG_ENABLE_WORKSET_COMMANDS=1", stderr)

    def test_default_help_hides_planned_workset_commands(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                blackdog_main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("task", help_text)
        self.assertIn("summary", help_text)
        self.assertNotIn("workset", help_text)
        self.assertNotIn("next", help_text)

    def test_task_begin_help_hides_generated_adoption_guards(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                blackdog_main(["task", "begin", "--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertNotIn("--expected-actor", help_text)
        self.assertNotIn("--expected-execution-prompt-hash", help_text)
        self.assertNotIn("--expected-execution-prompt-mode", help_text)
        self.assertNotIn("--expected-request-prompt-hash", help_text)
        self.assertNotIn("--expected-request-prompt-mode", help_text)
        self.assertNotIn("--adopt-aborted-landing-source", help_text)
        self.assertNotIn("--expected-predecessor-attempt", help_text)
        self.assertNotIn("--expected-landing-transaction", help_text)
        self.assertNotIn("--expected-source-commit", help_text)
        self.assertNotIn("--expected-source-tree", help_text)
        self.assertNotIn("--expected-branch", help_text)
        self.assertNotIn("--expected-path", help_text)
        self.assertNotIn("--expected-target-branch", help_text)
        self.assertNotIn("--expected-target-commit", help_text)

    def test_task_begin_partial_existing_target_points_to_normal_new_task_path(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Implement a new task.",
            "--workset",
            "invented-workset",
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("For new work, omit both flags", stderr)
        self.assertIn("provide both", stderr)

    def test_worktree_start_unknown_workset_points_to_task_begin(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "invented-workset",
            "--task",
            "TASK-1",
            "--actor",
            "codex",
            "--prompt",
            "Implement a new task.",
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Unknown workset 'invented-workset'", stderr)
        self.assertIn("use `blackdog task begin` without --workset/--task", stderr)

    def test_workset_put_summary_next_and_snapshot_form_one_vertical_slice(self) -> None:
        payload = {
            "id": "vertical-slice",
            "title": "Vertical slice",
            "scope": {"kind": "repo", "paths": ["src", "docs"]},
            "visibility": {"kind": "workset"},
            "policies": {"validation": ["make test"]},
            "workspace": {"identity": "vertical-slice-workspace"},
            "branch_intent": {"target_branch": "main", "integration_branch": "main"},
            "tasks": [
                {
                    "id": "VS-1",
                    "title": "Create planning data",
                    "intent": "write a workset payload through the CLI",
                },
                {
                    "id": "VS-2",
                    "title": "Read status",
                    "intent": "surface a machine-readable snapshot",
                    "depends_on": ["VS-1"],
                },
            ],
            "task_states": [{"task_id": "VS-1", "status": "done"}],
        }

        exit_code, stdout, stderr = self.put_workset(payload)
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["workset"]["id"], "vertical-slice")

        exit_code, stdout, stderr = self.run_cli("summary", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("Ready tasks:", stdout)
        self.assertIn("vertical-slice/VS-2 Read status", stdout)

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "vertical-slice",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        scoped_summary = json.loads(stdout)
        self.assertEqual(scoped_summary["workset_scope"], "vertical-slice")
        self.assertEqual(scoped_summary["counts"]["worksets"], 1)
        self.assertNotIn("worksets", scoped_summary)
        self.assertEqual(scoped_summary["ready_tasks"][0]["task_ref"], "vertical-slice/VS-2")

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "vertical-slice",
            "--include-legacy-worksets",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["worksets"][0]["id"], "vertical-slice")

        exit_code, stdout, stderr = self.run_cli(
            "next",
            "--project-root",
            str(self.root),
            "--workset",
            "vertical-slice",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        next_payload = json.loads(stdout)
        self.assertEqual(next_payload["workset_id"], "vertical-slice")
        self.assertEqual(next_payload["selection_mode"], "start")
        self.assertEqual(next_payload["selected_task"]["task_id"], "VS-2")
        self.assertEqual(next_payload["ready_tasks"][0]["task_ref"], "vertical-slice/VS-2")

        exit_code, stdout, stderr = self.run_cli(
            "snapshot",
            "--project-root",
            str(self.root),
            "--workset",
            "vertical-slice",
        )
        self.assertEqual(exit_code, 0, stderr)
        snapshot = json.loads(stdout)
        self.assertNotIn("worksets", snapshot["runtime_model"])
        self.assertEqual(len(snapshot["runtime_model"]["tasks"]), 2)
        self.assertEqual(snapshot["runtime_model"]["counts"]["ready"], 1)
        self.assertEqual(snapshot["runtime_model"]["tasks"][1]["task_ref"], "vertical-slice/VS-2")
        self.assertEqual(snapshot["runtime_model"]["counts"]["attempts"], 0)

    def test_workset_put_rejects_non_object_payload(self) -> None:
        with patch.dict(os.environ, {"BLACKDOG_ENABLE_WORKSET_COMMANDS": "1"}, clear=False):
            exit_code, stdout, stderr = self.run_cli(
                "workset",
                "put",
                "--project-root",
                str(self.root),
                "--json",
                '["not-an-object"]',
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("JSON object payload", stderr)

    def test_task_cancel_and_reopen_control_normal_visibility(self) -> None:
        payload = {
            "id": "manual-cancel",
            "title": "Manual cancel",
            "tasks": [{"id": "CAN-1", "title": "Cancel this", "intent": "hide stale work"}],
        }
        exit_code, _, stderr = self.put_workset(payload)
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cancel",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--task",
            "CAN-1",
            "--actor",
            "cancel-agent",
            "--summary",
            "stale",
            "--failure-class",
            "superseded",
            "--recovery-action",
            "leave_canceled",
            "--operator-issue",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_state = json.loads(stdout)["task_state"]
        self.assertEqual(task_state["status"], "canceled")
        self.assertEqual(task_state["failure_class"], "superseded")
        self.assertEqual(task_state["recovery_action"], "leave_canceled")
        self.assertTrue(task_state["operator_issue"])

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["counts"]["tasks"], 0)

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--include-canceled",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["counts"]["canceled"], 1)
        self.assertEqual(json.loads(stdout)["tasks"][0]["failure_class"], "superseded")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "reopen",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--task",
            "CAN-1",
            "--actor",
            "reopen-agent",
            "--summary",
            "needed",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["task_state"]["status"], "planned")

        exit_code, stdout, stderr = self.run_cli(
            "next",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        next_payload = json.loads(stdout)
        self.assertEqual(next_payload["selection_mode"], "start")
        self.assertEqual(next_payload["selected_task"]["task_id"], "CAN-1")

    def test_worktree_preview_shows_the_start_plan_and_contract_inputs(self) -> None:
        profile = load_profile(self.root)
        skill_path = (self.root / managed_skill_relative_path(profile)).resolve()
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text("repo skill\n", encoding="utf-8")
        agents_path = self.root / "AGENTS.md"
        agents_path.write_text("repo contract\n", encoding="utf-8")

        payload = {
            "id": "preview-mode",
            "title": "Preview mode",
            "scope": {"kind": "repo", "paths": ["src", "docs"]},
            "workspace": {"identity": "preview-workspace"},
            "branch_intent": {"target_branch": "main", "integration_branch": "feature/preview"},
            "tasks": [
                {
                    "id": "PV-1",
                    "title": "Preview the WTAM plan",
                    "intent": "surface the prompt receipt and contract inputs",
                    "paths": ["src/blackdog/wtam.py"],
                    "docs": ["docs/CLI.md"],
                    "checks": ["make test"],
                }
            ],
        }
        exit_code, stdout, stderr = self.put_workset(payload)
        self.assertEqual(exit_code, 0, stderr)
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "preview",
            "--project-root",
            str(self.root),
            "--workset",
            "preview-mode",
            "--task",
            "PV-1",
            "--actor",
            "codex",
            "--prompt",
            "Show me the exact WTAM start plan.",
            "--show-prompt",
            "--expand-contract",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preview = json.loads(stdout)["worktree_preview"]
        self.assertTrue(preview["start_ready"])
        self.assertEqual(preview["execution_model"], "direct_wtam")
        self.assertEqual(preview["workspace_identity"], "preview-workspace")
        self.assertEqual(preview["prompt_text"], "Show me the exact WTAM start plan.")
        self.assertEqual(preview["prompt_source"], "inline:--prompt")
        self.assertEqual(preview["task_paths"], ["src/blackdog/wtam.py"])
        self.assertEqual(preview["task_docs"], ["docs/CLI.md"])
        self.assertEqual(preview["task_checks"], ["make test"])
        self.assertEqual(preview["handlers"]["runtime_mode"], "launcher-shim")
        self.assertEqual(preview["handlers"]["source_mode"], "local-override")
        self.assertTrue(any(action["action"] == "ensure-worktree-venv" for action in preview["handlers"]["actions"]))
        self.assertTrue(any(item["path"] == str(skill_path.resolve()) for item in preview["contract_documents"]))
        self.assertTrue(any(item["path"] == str(agents_path.resolve()) for item in preview["contract_documents"]))
        self.assertTrue(any(item["text"] == "repo skill\n" for item in preview["contract_documents"]))

    def test_worktree_preflight_ignores_configured_generated_primary_paths(self) -> None:
        self.install_repo_runtime()
        profile_path = self.root / "blackdog.toml"
        profile_text = profile_path.read_text(encoding="utf-8")
        profile_text = profile_text.replace('root_path = ".VE"', 'root_path = "generated-env"', 1)
        profile_text = profile_text.replace('worktree_path = ".VE"', 'worktree_path = "generated-env"', 1)
        profile_text = profile_text.replace('launcher_path = ".VE/bin/blackdog"', 'launcher_path = "generated-env/bin/blackdog"', 1)
        profile_path.write_text(profile_text, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "blackdog.toml"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Use visible generated handler path"],
            check=True,
            capture_output=True,
            text=True,
        )

        generated_path = self.root / "generated-env" / "generated.txt"
        generated_path.parent.mkdir(parents=True, exist_ok=True)
        generated_path.write_text("generated\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "preflight",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preflight_payload = json.loads(stdout)
        self.assertTrue(preflight_payload["dirty"])
        self.assertFalse(preflight_payload["primary_dirty"])
        self.assertFalse(preflight_payload["implementation_dirty"])
        self.assertEqual(preflight_payload["primary_dirty_paths"], [])

        (self.root / "real-dirty.txt").write_text("dirty\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "preflight",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preflight_payload = json.loads(stdout)
        self.assertTrue(preflight_payload["primary_dirty"])
        self.assertEqual(preflight_payload["primary_dirty_paths"], ["real-dirty.txt"])

    def test_worktree_start_land_and_cleanup_drive_the_kept_change_flow(self) -> None:
        payload = {
            "id": "direct-mode",
            "title": "Direct mode",
            "workspace": {"identity": "direct-mode-workspace"},
            "branch_intent": {"target_branch": "main", "integration_branch": "feature/direct-mode"},
            "tasks": [{"id": "DM-1", "title": "Record stats", "intent": "exercise direct-agent mode"}],
        }
        exit_code, stdout, stderr = self.put_workset(payload)
        self.assertEqual(exit_code, 0, stderr)
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "preflight",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preflight_payload = json.loads(stdout)
        self.assertTrue(preflight_payload["current_is_primary"])
        self.assertEqual(preflight_payload["workspace_role"], "primary")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "direct-mode",
            "--task",
            "DM-1",
            "--actor",
            "codex",
            "--prompt",
            "Implement the direct slice and record repo execution lineage.",
            "--model",
            "gpt-5.4",
            "--reasoning-effort",
            "high",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        attempt_id = start_payload["attempt_id"]
        prompt_hash = hashlib.sha256(
            "Implement the direct slice and record repo execution lineage.".encode("utf-8")
        ).hexdigest()
        worktree_path = Path(start_payload["worktree_path"])
        self.assertTrue(worktree_path.exists())
        self.assertEqual(start_payload["runtime_mode"], "launcher-shim")
        self.assertEqual(start_payload["source_mode"], "local-override")
        self.assertEqual(start_payload["script_policy"], "root-bin-fallback")
        self.assertEqual(start_payload["primary_worktree"], str(self.root.resolve()))
        self.assertTrue(start_payload["branch"].startswith("agent/"))
        self.assertEqual(start_payload["base_commit"], self.git_output("rev-parse", "HEAD"))
        workspace_cli = worktree_path / ".VE" / "bin" / "blackdog"
        self.assertTrue(workspace_cli.is_file())
        completed = subprocess.run(
            [str(workspace_cli), "summary", "--project-root", str(self.root)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Project: CLI Demo", completed.stdout)

        exit_code, stdout, stderr = self.run_cli(
            "snapshot",
            "--project-root",
            str(self.root),
            "--include-legacy-worksets",
        )
        self.assertEqual(exit_code, 0, stderr)
        snapshot = json.loads(stdout)
        self.assertEqual(snapshot["runtime_model"]["counts"]["claimed_worksets"], 1)
        self.assertEqual(snapshot["runtime_model"]["counts"]["claimed_tasks"], 1)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["execution_model"], "direct_wtam")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["claim"]["actor"], "codex")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["claim"]["execution_model"], "direct_wtam")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["task_claims"][0]["task_id"], "DM-1")

        note_path = worktree_path / "notes.txt"
        note_path.write_text("WTAM kept change\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "direct-mode",
            "--task",
            "DM-1",
            "--actor",
            "codex",
            "--summary",
            "finished direct mode",
            "--validation",
            "unit=passed",
            "--residual",
            "none",
            "--followup",
            "publish",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "success")
        self.assertEqual(land_payload["attempt_id"], attempt_id)
        self.assertEqual(land_payload["branch"], start_payload["branch"])
        self.assertIn("notes.txt", land_payload["changed_paths"])
        self.assertNotEqual(land_payload["commit"], land_payload["landed_commit"])
        self.assertTrue(land_payload["deleted_branch"])
        self.assertEqual(land_payload["cleaned_worktree"], str(worktree_path))
        self.assertFalse(worktree_path.exists())
        landed_message = self.git_output("show", "-s", "--format=%B", land_payload["landed_commit"])
        self.assertIn("blackdog(direct-mode/DM-1): Record stats", landed_message)
        self.assertIn("Blackdog-Workset: direct-mode", landed_message)
        self.assertIn("Blackdog-Task: DM-1", landed_message)
        self.assertIn("Blackdog-Status: success", landed_message)
        self.assertIn("Blackdog-Execution-Model: direct_wtam", landed_message)
        self.assertIn("Blackdog-Model: gpt-5.4", landed_message)
        self.assertIn("Blackdog-Reasoning-Effort: high", landed_message)
        self.assertIn(f"Blackdog-Prompt-Hash: {prompt_hash}", landed_message)
        self.assertIn("Blackdog-Prompt-Source: inline:--prompt", landed_message)
        self.assertIn("Blackdog-Prompt-Mode: raw", landed_message)
        self.assertIn("Blackdog-Changed-Path: notes.txt", landed_message)
        self.assertNotIn("Blackdog-User-Prompt-Hash:", landed_message)

        exit_code, stdout, stderr = self.run_cli("summary", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("Attempts: 1 | Active attempts: 0", stdout)
        self.assertIn("Recent attempts:", stdout)
        self.assertIn("status=success", stdout)
        self.assertIn("branch=", stdout)
        self.assertIn("prompt=", stdout)

        exit_code, stdout, stderr = self.run_cli(
            "snapshot",
            "--project-root",
            str(self.root),
            "--include-legacy-worksets",
        )
        self.assertEqual(exit_code, 0, stderr)
        snapshot = json.loads(stdout)
        self.assertEqual(snapshot["runtime_model"]["counts"]["attempts"], 1)
        self.assertEqual(snapshot["runtime_model"]["counts"]["claimed_worksets"], 0)
        self.assertEqual(snapshot["runtime_model"]["counts"]["claimed_tasks"], 0)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["attempt_id"], attempt_id)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["prompt_receipt"]["prompt_hash"], prompt_hash)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["user_prompt_receipt"]["prompt_hash"], prompt_hash)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["prompt_receipt"]["mode"], "raw")
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["execution_model"], "direct_wtam")
        self.assertIsNone(snapshot["runtime_model"]["worksets"][0]["claim"])
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["task_claims"], [])
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["attempts"][0]["worktree_role"], "task")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["attempts"][0]["landed_commit"], land_payload["landed_commit"])
        self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"), "WTAM kept change\n")

    def test_linked_worktree_preflight_and_task_begin_target_the_current_branch(self) -> None:
        self.install_repo_runtime()
        task_worktree_path: Path | None = None
        task_branch: str | None = None
        expected_worktrees_dir = (self.root.parent / f".worktrees-{self.root.name}").resolve()
        with tempfile.TemporaryDirectory() as linked_base:
            linked_worktree = Path(linked_base) / "wt-feature"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "worktree",
                    "add",
                    "-b",
                    "feature/stable",
                    str(linked_worktree),
                    "main",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                for project_root in (linked_worktree, self.root):
                    with self.subTest(project_root=project_root):
                        exit_code, stdout, stderr = self.run_cli(
                            "worktree",
                            "preflight",
                            "--project-root",
                            str(project_root),
                            "--json",
                            cwd=linked_worktree,
                        )
                        self.assertEqual(exit_code, 0, stderr)
                        preflight_payload = json.loads(stdout)
                        self.assertFalse(preflight_payload["current_is_primary"])
                        self.assertEqual(preflight_payload["workspace_role"], "linked")
                        self.assertEqual(preflight_payload["current_branch"], "feature/stable")
                        self.assertEqual(preflight_payload["primary_branch"], "main")
                        self.assertEqual(preflight_payload["target_branch"], "feature/stable")
                        self.assertEqual(Path(preflight_payload["worktrees_dir"]), expected_worktrees_dir)

                subprocess.run(
                    [sys.executable, "-m", "venv", str(linked_worktree / ".VE")],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                exit_code, stdout, stderr = self.run_cli(
                    "task",
                    "begin",
                    "--project-root",
                    str(self.root),
                    "--actor",
                    "codex",
                    "--prompt",
                    "Exercise linked-branch task targeting.",
                    "--json",
                    cwd=linked_worktree,
                )
                self.assertEqual(exit_code, 0, stderr)
                task_payload = json.loads(stdout)["task"]
                self.assertEqual(task_payload["worktree"]["target_branch"], "feature/stable")
                self.assertEqual(task_payload["worktree"]["current_worktree"], str(linked_worktree.resolve()))
                task_branch = task_payload["worktree"]["branch"]
                task_worktree_path = Path(task_payload["worktree"]["worktree_path"])
                self.assertEqual(task_worktree_path.parent, expected_worktrees_dir)
            finally:
                if task_worktree_path is not None:
                    subprocess.run(
                        ["git", "-C", str(self.root), "worktree", "remove", "--force", str(task_worktree_path)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                subprocess.run(
                    ["git", "-C", str(self.root), "worktree", "remove", "--force", str(linked_worktree)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if task_branch:
                    subprocess.run(
                        ["git", "-C", str(self.root), "branch", "-D", task_branch],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                subprocess.run(
                    ["git", "-C", str(self.root), "branch", "-D", "feature/stable"],
                    check=False,
                    capture_output=True,
                    text=True,
                )

    def test_task_begin_creates_a_single_task_envelope_and_lands_from_the_task_worktree(self) -> None:
        self.install_repo_runtime()
        codex_home = self.root / ".git" / "codex-home"
        (codex_home / "sessions" / "2026" / "05" / "04").mkdir(parents=True)
        session_path = codex_home / "sessions" / "2026" / "05" / "04" / "rollout-2026-05-04T12-00-00-thread-task-begin.jsonl"
        session_path.write_text("", encoding="utf-8")
        (codex_home / "config.toml").write_text(
            'model = "gpt-5.5"\nmodel_reasoning_effort = "xhigh"\n',
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"CODEX_HOME": str(codex_home), "CODEX_THREAD_ID": "thread-task-begin"},
            clear=False,
        ):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "begin",
                "--project-root",
                str(self.root),
                "--actor",
                "codex",
                "--prompt",
                "Implement the same-thread task flow and capture the lineage.",
                "--json",
            )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        self.assertTrue(task_payload["created_workset"])
        self.assertEqual(task_payload["task_id"], "TASK-1")
        self.assertEqual(task_payload["prompt_mode"], "raw")
        self.assertEqual(task_payload["user_prompt_hash"], task_payload["execution_prompt_hash"])
        self.assertTrue(workset_id.startswith("task-"))
        self.assertTrue(worktree_path.exists())
        self.assertEqual(task_payload["worktree"]["setup_receipt"]["status"], "ok")
        self.assertEqual(task_payload["worktree"]["setup_receipt"]["task_class"], "implementation")
        self.assertNotIn("skill_provenance", task_payload)
        self.assertNotIn("skill_provenance", task_payload["worktree"]["setup_receipt"])

        exit_code, stdout, stderr = self.run_cli("snapshot", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        started_attempt = json.loads(stdout)["runtime_model"]["recent_attempts"][0]
        self.assertEqual(started_attempt["model"], "gpt-5.5")
        self.assertEqual(started_attempt["reasoning_effort"], "xhigh")
        self.assertEqual(started_attempt["codex_session"]["thread_id"], "thread-task-begin")
        self.assertEqual(
            started_attempt["codex_session"]["session_path"],
            "sessions/2026/05/04/rollout-2026-05-04T12-00-00-thread-task-begin.jsonl",
        )
        self.assertIsNone(started_attempt["prompt_receipt"]["text"])
        attempt_row = json.loads(stdout)["runtime_model"]["attempts"][0]
        self.assertEqual(attempt_row["setup_status"], "ok")
        self.assertEqual(attempt_row["task_class"], "implementation")
        self.assertEqual(attempt_row["setup_blockers_count"], 0)
        self.assertEqual(attempt_row["setup_receipt"]["status"], "ok")
        self.assertIsNone(attempt_row["skill_path"])
        self.assertIsNone(attempt_row["skill_hash"])
        self.assertIsNone(attempt_row["skill_source"])

        (worktree_path / "task-begin.txt").write_text("task begin\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["task_show"]
        self.assertTrue(show_payload["active_attempt"])
        self.assertEqual(show_payload["workset_id"], workset_id)
        self.assertEqual(show_payload["task_id"], "TASK-1")
        self.assertIn("task-begin.txt", show_payload["changed_paths"])
        self.assertEqual(show_payload["user_prompt_hash"], task_payload["user_prompt_hash"])
        self.assertEqual(show_payload["execution_prompt_hash"], task_payload["execution_prompt_hash"])
        self.assertNotIn("skill_provenance", show_payload)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "finished the same-thread task flow",
            "--validation",
            "unit=passed",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "success")
        self.assertEqual(land_payload["task_id"], "TASK-1")
        self.assertIn("task-begin.txt", land_payload["changed_paths"])
        self.assertFalse(worktree_path.exists())
        landed_message = self.git_output("show", "-s", "--format=%B", land_payload["landed_commit"])
        self.assertIn(f"blackdog({workset_id}/TASK-1)", landed_message)
        self.assertIn("Blackdog-Changed-Path: task-begin.txt", landed_message)
        self.assertIn("Blackdog-Validation: unit=passed", landed_message)
        self.assertIn("Blackdog-Model: gpt-5.5", landed_message)
        self.assertIn("Blackdog-Reasoning-Effort: xhigh", landed_message)
        self.assertIn("Blackdog-Codex-Thread: thread-task-begin", landed_message)
        self.assertNotIn("Blackdog-User-Prompt-Hash:", landed_message)

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary_payload = json.loads(stdout)
        self.assertEqual(summary_payload["counts"]["active_attempts"], 0)
        self.assertEqual(summary_payload["counts"]["claimed_tasks"], 0)
        self.assertEqual((self.root / "task-begin.txt").read_text(encoding="utf-8"), "task begin\n")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            "TASK-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        completed_payload = json.loads(stdout)["task_show"]
        self.assertFalse(completed_payload["active_attempt"])
        self.assertEqual(completed_payload["latest_attempt_status"], "success")
        self.assertEqual(completed_payload["task_runtime_status"], "done")
        self.assertEqual(completed_payload["recovery_state"], "idle")
        self.assertFalse(completed_payload["branch_exists"])
        self.assertTrue(completed_payload["target_branch_exists"])
        self.assertIsNone(completed_payload["branch_ahead_error"])
        self.assertIsNone(completed_payload["failure_class"])
        self.assertIsNone(completed_payload["recovery_action"])
        self.assertFalse(completed_payload["operator_issue"])
        self.assertEqual(completed_payload["recommended_actions"], [])
        self.assertEqual(completed_payload["recommended_commands"], [])

    def test_task_land_rejects_wrong_actor_before_mutating_git(self) -> None:
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "owner",
            "--prompt",
            "Exercise task landing actor ownership.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        branch = task_payload["worktree"]["branch"]
        primary_head = self.git_output("rev-parse", "HEAD")
        branch_head = self.git_output("rev-parse", branch)
        (worktree_path / "actor-owned.txt").write_text("owned\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--actor",
            "intruder",
            "--summary",
            "must not land",
            "--json",
            cwd=worktree_path,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("is owned by owner, not intruder", stderr)
        self.assertEqual(self.git_output("rev-parse", "HEAD"), primary_head)
        self.assertEqual(self.git_output("rev-parse", branch), branch_head)
        self.assertTrue(worktree_path.exists())
        self.assertIn("actor-owned.txt", self.git_output("-C", str(worktree_path), "status", "--short"))
        (worktree_path / "actor-owned.txt").unlink()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--actor",
            "owner",
            "--status",
            "abandoned",
            "--summary",
            "ownership preflight test cleanup",
            "--cleanup",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertFalse(worktree_path.exists())

    def test_task_reconcile_landing_is_dry_run_first_and_allows_proven_actor_finalize_mismatch(self) -> None:
        workset_id = "reconcile-landing"
        task_id = "REC-1"
        exit_code, _, stderr = self.put_workset(
            {
                "id": workset_id,
                "title": "Reconcile landing",
                "branch_intent": {"target_branch": "main", "integration_branch": "main"},
                "tasks": [{"id": task_id, "title": "Repair ledger", "intent": "reconcile a landed commit"}],
            }
        )
        self.assertEqual(exit_code, 0, stderr)
        profile = load_profile(self.root)
        attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="AGENT",
            target_branch="main",
            prompt_receipt=create_prompt_receipt("Repair the historical ledger.", source="unit-test"),
        )
        (self.root / "reconciled.txt").write_text("landed\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "reconciled.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        commit_message = (
            f"blackdog({workset_id}/{task_id}): Repair ledger\n\n"
            "The Git landing completed before runtime finalization failed.\n\n"
            f"Blackdog-Workset: {workset_id}\n"
            f"Blackdog-Task: {task_id}\n"
            f"Blackdog-Attempt: {attempt.attempt_id}\n"
            "Blackdog-Actor: CODEX\n"
            "Blackdog-Status: success\n"
            "Blackdog-Target-Branch: main\n"
            "Blackdog-Changed-Path: reconciled.txt\n"
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "--quiet", "-F", "-"],
            input=commit_message,
            check=True,
            capture_output=True,
            text=True,
        )
        landed_commit = self.git_output("rev-parse", "HEAD")
        finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            actor="AGENT",
            status="failed",
            summary=(
                "Git landing completed, but runtime finalization rejected the deliberately mismatched actor "
                "after mutation."
            ),
            validations=(ValidationRecord(name="unit", status="passed"),),
            failure_class="unknown",
            recovery_action="inspect",
        )
        # Exercise a real absent source-object lookup. Reconciliation can still
        # rely on canonical landed-commit trailers/diff when historical source
        # commit proof has been pruned, but must not confuse an operational Git
        # error with this explicit missing result.
        runtime_payload = json.loads(profile.paths.runtime_file.read_text(encoding="utf-8"))
        runtime_payload["worksets"][0]["attempts"][0]["commit"] = "f" * 40
        profile.paths.runtime_file.write_text(
            json.dumps(runtime_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        runtime_before = profile.paths.runtime_file.read_bytes()
        events_before = profile.paths.events_file.read_bytes()
        head_before = self.git_output("rev-parse", "HEAD")
        arguments = (
            "task",
            "reconcile-landing",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--attempt",
            attempt.attempt_id,
            "--landed-commit",
            landed_commit,
            "--actor",
            "ledger-auditor",
            "--json",
        )

        real_run_git_no_check = wtam._run_git_no_check

        def fail_source_commit_inspection(repo_root: Path, *args: str):
            if args == (
                "rev-parse",
                "--verify",
                "--quiet",
                f"{'f' * 40}^{{commit}}",
            ):
                return subprocess.CompletedProcess(
                    ["git", *args],
                    128,
                    stdout="",
                    stderr="simulated reconciliation object database failure",
                )
            return real_run_git_no_check(repo_root, *args)

        with patch("blackdog.wtam._run_git_no_check", side_effect=fail_source_commit_inspection):
            exit_code, stdout, stderr = self.run_cli(*arguments)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("source_commit", stderr)
        self.assertIn("return_code", stderr)
        self.assertIn("128", stderr)
        self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(profile.paths.events_file.read_bytes(), events_before)
        self.assertEqual(self.git_output("rev-parse", "HEAD"), head_before)

        exit_code, stdout, stderr = self.run_cli(*arguments)

        self.assertEqual(exit_code, 0, stderr)
        dry_run = json.loads(stdout)["landing_reconciliation"]
        self.assertFalse(dry_run["apply"])
        self.assertEqual(dry_run["status"], "ready")
        self.assertEqual(dry_run["commit_actor"], "CODEX")
        self.assertEqual(dry_run["operator_actor"], "ledger-auditor")
        self.assertFalse(dry_run["proof"]["actor_matches_attempt"])
        self.assertEqual(dry_run["proof"]["actor_mismatch_evidence"]["event_type"], "task.finish")
        self.assertEqual(dry_run["proof"]["changed_paths"], ["reconciled.txt"])
        self.assertIsNone(dry_run["proof"]["source_commit"])
        self.assertIsNone(dry_run["proof"]["source_patch_equivalent"])
        self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(profile.paths.events_file.read_bytes(), events_before)
        self.assertEqual(self.git_output("rev-parse", "HEAD"), head_before)

        exit_code, stdout, stderr = self.run_cli(*arguments[:-1], "--reason", "repair post-Git finalization", "--apply", "--json")

        self.assertEqual(exit_code, 0, stderr)
        applied = json.loads(stdout)["landing_reconciliation"]
        self.assertEqual(applied["status"], "success")
        self.assertTrue(applied["runtime_changed"])
        self.assertTrue(applied["event_appended"])
        current = load_runtime_state(profile.paths)
        corrected = current.worksets[0].attempts[0]
        self.assertEqual(corrected.status, "success")
        self.assertEqual(corrected.actor, "AGENT")
        self.assertEqual(corrected.landed_commit, landed_commit)
        self.assertEqual(corrected.changed_paths, ("reconciled.txt",))
        self.assertEqual(current.worksets[0].task_states[0].status, "done")
        reconciliation_events = [
            event for event in load_events(profile.paths.events_file) if event["type"] == "task.landing.reconciled"
        ]
        self.assertEqual(len(reconciliation_events), 1)
        self.assertEqual(reconciliation_events[0]["actor"], "ledger-auditor")
        self.assertEqual(reconciliation_events[0]["payload"]["previous_status"], "failed")

        remaining_events = [
            event
            for event in load_events(profile.paths.events_file)
            if event["type"] != "task.landing.reconciled"
        ]
        profile.paths.events_file.write_text(
            "".join(f"{json.dumps(event, sort_keys=True)}\n" for event in remaining_events),
            encoding="utf-8",
        )

        exit_code, stdout, stderr = self.run_cli(*arguments[:-1], "--apply", "--json")
        self.assertEqual(exit_code, 0, stderr)
        retried = json.loads(stdout)["landing_reconciliation"]
        self.assertFalse(retried["runtime_changed"])
        self.assertTrue(retried["event_appended"])
        self.assertTrue(retried["event_repaired"])
        self.assertTrue(retried["mutation_started"])
        self.assertTrue(retried["mutation_completed"])
        self.assertEqual(retried["mutation_phase"], "event_finalized")
        self.assertEqual(
            len([event for event in load_events(profile.paths.events_file) if event["type"] == "task.landing.reconciled"]),
            1,
        )

        exit_code, stdout, stderr = self.run_cli(*arguments[:-1], "--apply", "--json")
        self.assertEqual(exit_code, 0, stderr)
        idempotent = json.loads(stdout)["landing_reconciliation"]
        self.assertFalse(idempotent["runtime_changed"])
        self.assertFalse(idempotent["event_appended"])
        self.assertFalse(idempotent["mutation_started"])
        self.assertFalse(idempotent["mutation_completed"])
        self.assertEqual(idempotent["mutation_phase"], "none")

    def test_task_reconcile_landing_rejects_actor_mismatch_without_terminal_evidence(self) -> None:
        workset_id = "reject-reconcile"
        task_id = "REC-1"
        exit_code, _, stderr = self.put_workset(
            {
                "id": workset_id,
                "title": "Reject reconcile",
                "branch_intent": {"target_branch": "main", "integration_branch": "main"},
                "tasks": [{"id": task_id, "title": "Reject mismatch", "intent": "reject weak proof"}],
            }
        )
        self.assertEqual(exit_code, 0, stderr)
        profile = load_profile(self.root)
        attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="AGENT",
            target_branch="main",
            prompt_receipt=create_prompt_receipt("Reject weak proof.", source="unit-test"),
        )
        (self.root / "reject.txt").write_text("reject\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "reject.txt"], check=True, capture_output=True, text=True)
        message = (
            f"blackdog({workset_id}/{task_id}): Reject mismatch\n\nproof\n\n"
            f"Blackdog-Workset: {workset_id}\nBlackdog-Task: {task_id}\n"
            f"Blackdog-Attempt: {attempt.attempt_id}\nBlackdog-Actor: CODEX\n"
            "Blackdog-Status: success\nBlackdog-Target-Branch: main\n"
            "Blackdog-Changed-Path: reject.txt\n"
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "--quiet", "-F", "-"],
            input=message,
            check=True,
            capture_output=True,
            text=True,
        )
        landed_commit = self.git_output("rev-parse", "HEAD")
        finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            actor="AGENT",
            status="failed",
            summary="Actor mismatch did not occur; failure before Git landing and runtime finalization.",
        )
        append_event(
            profile.paths.events_file,
            event_type="task.finish",
            actor="INTRUDER",
            payload={
                "workset_id": workset_id,
                "task_id": task_id,
                "attempt_id": attempt.attempt_id,
                "status": "failed",
                "summary": (
                    "Git landing completed, but runtime finalization rejected the deliberately mismatched actor "
                    "after mutation."
                ),
            },
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "reconcile-landing",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--attempt",
            attempt.attempt_id,
            "--landed-commit",
            landed_commit,
            "--actor",
            "auditor",
            "--json",
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("does not prove a post-Git actor-ownership finalization failure", stderr)
        self.assertEqual(load_runtime_state(profile.paths).worksets[0].attempts[0].status, "failed")

    def test_task_reconcile_landing_rejects_merge_when_source_commit_is_absent(self) -> None:
        workset_id = "reject-merge-reconcile"
        task_id = "REC-1"
        exit_code, _, stderr = self.put_workset(
            {
                "id": workset_id,
                "title": "Reject merge reconcile",
                "branch_intent": {"target_branch": "main", "integration_branch": "main"},
                "tasks": [{"id": task_id, "title": "Reject merge", "intent": "require a canonical commit"}],
            }
        )
        self.assertEqual(exit_code, 0, stderr)
        profile = load_profile(self.root)
        attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="AGENT",
            target_branch="main",
            prompt_receipt=create_prompt_receipt("Reject a merge landing.", source="unit-test"),
        )
        subprocess.run(
            ["git", "-C", str(self.root), "checkout", "-q", "-b", "merge-source"],
            check=True,
            capture_output=True,
            text=True,
        )
        (self.root / "merge.txt").write_text("merge\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "merge.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-q", "-m", "Prepare merge source"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "checkout", "-q", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
        message = (
            f"blackdog({workset_id}/{task_id}): Invalid merge landing\n\nproof\n\n"
            f"Blackdog-Workset: {workset_id}\nBlackdog-Task: {task_id}\n"
            f"Blackdog-Attempt: {attempt.attempt_id}\nBlackdog-Actor: AGENT\n"
            "Blackdog-Status: success\nBlackdog-Target-Branch: main\n"
            "Blackdog-Changed-Path: merge.txt\n"
        )
        subprocess.run(
            ["git", "-C", str(self.root), "merge", "--no-ff", "merge-source", "-q", "-m", message],
            check=True,
            capture_output=True,
            text=True,
        )
        landed_commit = self.git_output("rev-parse", "HEAD")
        finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            actor="AGENT",
            status="failed",
            summary="terminal runtime failure",
        )
        self.assertIsNone(load_runtime_state(profile.paths).worksets[0].attempts[0].commit)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "reconcile-landing",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--attempt",
            attempt.attempt_id,
            "--landed-commit",
            landed_commit,
            "--actor",
            "auditor",
            "--json",
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("must have exactly one parent for reconciliation", stderr)
        self.assertEqual(load_runtime_state(profile.paths).worksets[0].attempts[0].status, "failed")

    def test_task_begin_deployment_guard_blocks_before_auto_task_creation(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Deploy production now.",
            "--json",
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        task_payload = json.loads(stdout)["task"]
        self.assertEqual(task_payload["operation"], "task.begin")
        self.assertEqual(task_payload["actor"], "codex")
        self.assertEqual(task_payload["operation_status"], "blocked")
        self.assertIsNone(task_payload["task_status"])
        self.assertIsNone(task_payload["attempt_status"])
        self.assertFalse(task_payload["mutation_started"])
        self.assertFalse(task_payload["mutation_completed"])
        self.assertEqual(task_payload["mutation_phase"], "none")
        self.assertEqual(task_payload["failure_code"], "setup_guard")
        self.assertEqual(task_payload["next_action"]["action_id"], "deployment_route_required")
        self.assertEqual(task_payload["next_action"]["kind"], "blocked")
        self.assertEqual(task_payload["next_action"]["argv"], [])
        self.assertEqual(task_payload["next_action"]["required_inputs"], ["deployment_route"])
        self.assertIn("deployment tasks must name the CI/GitHub Actions route", task_payload["error"])
        profile = load_profile(self.root)
        self.assertEqual(load_planning_state(profile.paths).worksets, ())
        self.assertEqual(load_runtime_state(profile.paths).worksets, ())
        self.assertFalse((profile.paths.control_dir / "prompts").exists())
        report = read_lifecycle_observability(profile)
        self.assertEqual(report.surface_counts["task.begin"], 1)
        self.assertEqual(report.outcome_counts["blocked"], 1)
        self.assertEqual(report.label_counts["failure_class"]["setup_guard"], 1)

        exit_code, text_stdout, text_stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--prompt",
            "Deploy production now.",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(text_stderr, "")
        self.assertIn("operation status: blocked", text_stdout)
        self.assertIn("next action: deployment_route_required", text_stdout)
        self.assertIn("next action kind: blocked", text_stdout)
        self.assertNotIn("next command:", text_stdout)

    def test_task_begin_oversized_prompt_leaves_no_task_runtime_or_git_mutation(self) -> None:
        self.install_repo_runtime()
        profile = load_profile(self.root)

        def optional_bytes(path: Path) -> bytes | None:
            return path.read_bytes() if path.exists() else None

        before = {
            "planning": optional_bytes(profile.paths.planning_file),
            "runtime": optional_bytes(profile.paths.runtime_file),
            "events": optional_bytes(profile.paths.events_file),
            "worktrees": self.git_output("worktree", "list", "--porcelain"),
            "branches": self.git_output("branch", "--format=%(refname)"),
        }
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "x" * (PROMPT_ARTIFACT_MAX_BYTES + 1),
            "--json",
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("replay artifact limit", stderr)
        self.assertEqual(optional_bytes(profile.paths.planning_file), before["planning"])
        self.assertEqual(optional_bytes(profile.paths.runtime_file), before["runtime"])
        self.assertEqual(optional_bytes(profile.paths.events_file), before["events"])
        self.assertEqual(self.git_output("worktree", "list", "--porcelain"), before["worktrees"])
        self.assertEqual(self.git_output("branch", "--format=%(refname)"), before["branches"])
        self.assertFalse((profile.paths.control_dir / "prompts").exists())

    def test_task_begin_preflight_observation_failure_is_fail_open(self) -> None:
        profile = load_profile(self.root)
        with patch("blackdog.observability.observe_lifecycle", side_effect=OSError("telemetry unavailable")):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "begin",
                "--project-root",
                str(self.root),
                "--prompt",
                "Deploy production now.",
                "--json",
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        task_payload = json.loads(stdout)["task"]
        self.assertEqual(task_payload["operation_status"], "blocked")
        self.assertEqual(task_payload["failure_code"], "setup_guard")
        self.assertFalse(task_payload["mutation_started"])
        self.assertEqual(load_planning_state(profile.paths).worksets, ())
        self.assertEqual(load_runtime_state(profile.paths).worksets, ())

    def test_task_prompt_classification_requires_positive_specific_signals(self) -> None:
        implementation_prompts = (
            "Correct stale guidance and avoid any deploy targets.",
            "Deduplicate pairing stability report modules and refresh the managed skill.",
            "Remove duplicate Reportdog helpers without publishing anything.",
            "Update the release notes.",
        )
        for prompt in implementation_prompts:
            with self.subTest(prompt=prompt):
                receipt = wtam._task_start_guard_receipt(prompt)
                self.assertEqual(receipt["task_class"], "implementation")
                self.assertEqual(receipt["status"], "ok")
                self.assertEqual(receipt["blockers"], [])

        classified_prompts = (
            ("Deploy production now.", "deployment", "blocked"),
            ("Do not deploy manually; deploy through GitHub Actions workflow_dispatch.", "deployment", "ok"),
            ("Publish the analysis report to SharePoint.", "analysis_publish", "ok"),
            ("Refresh the production database from its source.", "deployment", "blocked"),
            ("Refresh the local dataset from its source.", "data_refresh", "ok"),
            ("Ingest the latest dataset.", "data_refresh", "ok"),
        )
        for prompt, expected_class, expected_status in classified_prompts:
            with self.subTest(prompt=prompt):
                receipt = wtam._task_start_guard_receipt(prompt)
                self.assertEqual(receipt["task_class"], expected_class)
                self.assertEqual(receipt["status"], expected_status)

    def test_task_begin_deployment_route_records_setup_receipt(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Deploy production through GitHub Actions workflow_dispatch.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        branch = task_payload["worktree"]["branch"]
        setup_receipt = task_payload["worktree"]["setup_receipt"]
        self.assertEqual(setup_receipt["status"], "ok")
        self.assertEqual(setup_receipt["task_class"], "deployment")
        self.assertEqual(setup_receipt["blockers"], [])
        self.assertTrue(any(row["name"] == "deployment_route" and row["status"] == "ok" for row in setup_receipt["probes"]))

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "closed deployment route receipt smoke test",
            "--cleanup",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertFalse(worktree_path.exists())
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.root), "branch", "--list", branch],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "",
        )

    def test_codex_coverage_and_history_cli_read_codex_sessions(self) -> None:
        codex_home = self.root / ".codex-home"
        session_path = codex_home / "sessions" / "2026" / "05" / "04" / "rollout-2026-05-04T12-00-00-thread-cli.jsonl"
        session_path.parent.mkdir(parents=True)
        session_path.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {
                        "timestamp": "2026-05-04T19:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "thread-cli", "timestamp": "2026-05-04T19:00:00Z", "cwd": str(self.root)},
                    },
                    {
                        "timestamp": "2026-05-04T19:00:01Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-cli", "started_at": 1777921201},
                    },
                    {
                        "timestamp": "2026-05-04T19:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": "turn-cli", "cwd": str(self.root), "model": "gpt-5.5", "effort": "xhigh"},
                    },
                    {
                        "timestamp": "2026-05-04T19:00:02Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "Implement a CLI-visible Codex history row."},
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
            exit_code, stdout, stderr = self.run_cli("codex", "coverage", "--project-root", str(self.root), "--json")
            self.assertEqual(exit_code, 0, stderr)
            coverage = json.loads(stdout)["codex_coverage"]
            self.assertEqual(coverage["counts"]["codex_user_turns"], 1)
            self.assertEqual(coverage["counts"]["implementation_like_unlinked_turns"], 1)

            exit_code, stdout, stderr = self.run_cli("codex", "history", "--project-root", str(self.root), "--jsonl")
            self.assertEqual(exit_code, 0, stderr)
            rows = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "codex_turn")
            self.assertNotIn("message_excerpt", rows[0])

            exit_code, stdout, stderr = self.run_cli("codex", "history", "--project-root", str(self.root), "--write")
            self.assertEqual(exit_code, 0, stderr)
            self.assertTrue((self.root / ".blackdog" / "history.jsonl").exists())

    def test_codex_link_targets_blackdog_owned_task_worktree_without_prompt_leakage(self) -> None:
        self.install_repo_runtime()
        user_prompt = "PRIVATE USER REQUEST: rotate the cobalt narwhal."
        execution_prompt = "PRIVATE EXECUTION DETAIL: use token delta-917."
        user_prompt_path = self.root / "USER_PROMPT.txt"
        execution_prompt_path = self.root / "EXECUTION_PROMPT.txt"
        user_prompt_path.write_text(user_prompt + "\n", encoding="utf-8")
        execution_prompt_path.write_text(execution_prompt + "\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--execution-prompt-file",
            str(execution_prompt_path),
            "--prompt-mode",
            "skill",
            "--request-file",
            str(user_prompt_path),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        task_id = task_payload["task_id"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])

        exit_code, stdout, stderr = self.run_cli(
            "codex",
            "link",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )

        self.assertEqual(exit_code, 0, stderr)
        link = json.loads(stdout)["codex_link"]
        self.assertEqual(link["schema_version"], 1)
        self.assertEqual(link["kind"], "codex_local_workspace_link")
        self.assertEqual(link["workspace_owner"], "blackdog")
        self.assertEqual(link["workspace_role"], "task")
        self.assertEqual(link["codex_workspace_kind"], "local")
        self.assertEqual(link["thread_continuity"], "new_thread")
        self.assertFalse(link["auto_submits"])
        self.assertEqual(link["workspace_path"], str(worktree_path.resolve()))
        self.assertEqual(link["workset_id"], workset_id)
        self.assertEqual(link["task_id"], task_id)
        self.assertEqual(link["attempt_id"], task_payload["worktree"]["attempt_id"])
        self.assertEqual(link["branch"], task_payload["worktree"]["branch"])
        self.assertEqual(link["target_branch"], task_payload["worktree"]["target_branch"])
        self.assertEqual(link["fallback_argv"], ["codex", "app", str(worktree_path.resolve())])
        self.assertFalse(link["fallback_prefills_prompt"])
        self.assertLessEqual(len(link["prompt"]), 1024)
        self.assertIn("follow its next_action exactly", link["prompt"])
        self.assertIn("Blackdog owns this worktree, branch, landing, and cleanup", link["prompt"])
        parsed = urlparse(link["url"])
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("codex", "threads", "/new"))
        self.assertEqual(
            parse_qs(parsed.query),
            {"path": [str(worktree_path.resolve())], "prompt": [link["prompt"]]},
        )
        self.assertNotIn(user_prompt, stdout)
        self.assertNotIn(execution_prompt, stdout)
        self.assertNotIn(task_payload["user_prompt_hash"], stdout)
        self.assertNotIn(task_payload["execution_prompt_hash"], stdout)
        self.assertNotIn(task_payload["user_prompt_replay_artifact_path"], stdout)
        self.assertNotIn(task_payload["execution_prompt_replay_artifact_path"], stdout)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "closed disposable Codex link integration test",
            "--cleanup",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertFalse(worktree_path.exists())

        exit_code, stdout, stderr = self.run_cli(
            "codex",
            "link",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--json",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("requires an active in-progress Blackdog task attempt", stderr)

    def test_codex_hook_stamp_cli_records_active_task_context(self) -> None:
        profile = load_profile(self.root)
        upsert_workset(
            profile,
            {
                "id": "hook-cli",
                "title": "Hook CLI",
                "tasks": [{"id": "TASK-1", "title": "Hook CLI task"}],
            },
        )
        attempt = start_task(
            profile,
            workset_id="hook-cli",
            task_id="TASK-1",
            actor="codex",
            prompt_receipt=create_prompt_receipt("Implement hook CLI stamping."),
            worktree_path=str(self.root),
            branch="main",
            target_branch="main",
        )
        event_payload = {
            "hook_event_name": "Stop",
            "session_id": "thread-cli-hook",
            "turn_id": "turn-cli-hook",
            "cwd": str(self.root),
            "prompt": "this text must not be persisted",
        }

        exit_code, stdout, stderr = self.run_cli(
            "codex",
            "hook",
            "stamp",
            "--project-root",
            str(self.root),
            "--event-json",
            json.dumps(event_payload),
            "--json",
            cwd=self.root,
        )

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["codex_hook_stamp"]
        self.assertTrue(payload["context_found"])
        self.assertEqual(payload["active_attempt"]["attempt_id"], attempt.attempt_id)
        self.assertEqual(payload["turn_classification"]["source"], "heuristic")
        rows = load_events(codex_task_context_path(profile))
        self.assertEqual(rows[0]["payload"]["hook"]["session_id"], "thread-cli-hook")
        self.assertEqual(rows[0]["payload"]["turn_classification"], payload["turn_classification"])
        self.assertNotIn("this text", json.dumps(rows[0]["payload"]))

        silent_payload = {**event_payload, "turn_id": "turn-cli-hook-silent"}
        exit_code, stdout, stderr = self.run_cli(
            "codex",
            "hook",
            "stamp",
            "--project-root",
            str(self.root),
            "--event-json",
            json.dumps(silent_payload),
            cwd=self.root,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        self.assertEqual(
            load_events(codex_task_context_path(profile))[-1]["payload"]["hook"]["turn_id"],
            "turn-cli-hook-silent",
        )

    def test_task_begin_can_tune_the_prompt_and_task_close_can_infer_the_current_attempt(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Make a tuned execution prompt for this slice.",
            "--prompt-mode",
            "tuned",
            "--show-prompt",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        self.assertEqual(task_payload["prompt_mode"], "tuned")
        self.assertNotEqual(task_payload["user_prompt_hash"], task_payload["execution_prompt_hash"])
        self.assertIn("You are working in the repo", task_payload["execution_prompt_text"])

        (worktree_path / "tuned.txt").write_text("tuned\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["task_show"]
        self.assertEqual(show_payload["user_prompt_hash"], task_payload["user_prompt_hash"])
        self.assertEqual(show_payload["user_prompt_mode"], "raw")
        self.assertEqual(show_payload["execution_prompt_hash"], task_payload["execution_prompt_hash"])
        self.assertEqual(show_payload["execution_prompt_mode"], "tuned")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "abandoned the tuned slice",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        close_payload = json.loads(stdout)["closure"]
        self.assertEqual(close_payload["status"], "abandoned")
        self.assertIn("tuned.txt", close_payload["changed_paths"])

        exit_code, stdout, stderr = self.run_cli(
            "next",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        next_payload = json.loads(stdout)
        self.assertEqual(next_payload["selection_mode"], "none")
        self.assertIsNone(next_payload["selected_task"])

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary_payload = json.loads(stdout)
        self.assertEqual(summary_payload["counts"]["tasks"], 0)
        self.assertNotIn("worksets", summary_payload)
        self.assertEqual(summary_payload["tasks"], [])

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--include-canceled",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary_payload = json.loads(stdout)
        self.assertEqual(summary_payload["counts"]["canceled"], 1)
        self.assertEqual(summary_payload["recent_attempts"][0]["user_prompt_hash"], task_payload["user_prompt_hash"])
        self.assertEqual(
            summary_payload["recent_attempts"][0]["execution_prompt_hash"],
            task_payload["execution_prompt_hash"],
        )
        self.assertIsNone(summary_payload["recent_attempts"][0]["prompt_hash"])

    def test_task_begin_accepts_skill_execution_prompt_and_user_prompt(self) -> None:
        self.install_repo_runtime()
        profile = load_profile(self.root)
        skill_relative_path = managed_skill_relative_path(profile)
        skill_path = self.root / skill_relative_path
        skill_bytes = skill_path.read_bytes()
        skill_text = skill_bytes.decode("utf-8")
        expected_skill_provenance = {
            "schema_version": 1,
            "path": skill_relative_path.as_posix(),
            "sha256": hashlib.sha256(skill_bytes).hexdigest(),
            "source": "repo_managed",
        }
        user_prompt_path = self.root / "USER_PROMPT.txt"
        execution_prompt_path = self.root / "EXECUTION_PROMPT.txt"
        user_prompt_path.write_text("Add a repo-local feature.\n", encoding="utf-8")
        execution_prompt_path.write_text("Implement the feature with the repo skill guardrails.\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt-file",
            str(execution_prompt_path),
            "--prompt-mode",
            "skill",
            "--user-prompt-file",
            str(user_prompt_path),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        self.assertEqual(task_payload["prompt_mode"], "skill")
        self.assertNotEqual(task_payload["user_prompt_hash"], task_payload["execution_prompt_hash"])
        for artifact_field in (
            "execution_prompt_replay_artifact_path",
            "user_prompt_replay_artifact_path",
        ):
            artifact_path = task_payload[artifact_field]
            self.assertIsInstance(artifact_path, str)
            self.assertTrue(artifact_path)
            self.assertTrue((profile.paths.control_dir / artifact_path).is_file())
        self.assertEqual(task_payload["skill_provenance"], expected_skill_provenance)
        self.assertEqual(
            task_payload["worktree"]["setup_receipt"]["skill_provenance"],
            expected_skill_provenance,
        )
        self.assertEqual(
            set(task_payload["worktree"]["setup_receipt"]["skill_provenance"]),
            {"schema_version", "path", "sha256", "source"},
        )
        self.assertNotIn(skill_text, stdout)
        for durable_path in (profile.paths.runtime_file, profile.paths.events_file):
            self.assertNotIn(skill_text, durable_path.read_text(encoding="utf-8"))

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["task_show"]
        self.assertEqual(show_payload["user_prompt_mode"], "raw")
        self.assertEqual(show_payload["execution_prompt_mode"], "skill")
        self.assertEqual(show_payload["user_prompt_hash"], task_payload["user_prompt_hash"])
        self.assertEqual(show_payload["execution_prompt_hash"], task_payload["execution_prompt_hash"])
        self.assertEqual(show_payload["skill_provenance"], expected_skill_provenance)
        self.assertEqual(show_payload["setup_receipt"]["skill_provenance"], expected_skill_provenance)
        self.assertNotIn(skill_text, stdout)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "closed the skill prompt smoke",
            "--cleanup",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertFalse(worktree_path.exists())

        exit_code, stdout, stderr = self.run_cli(
            "attempts",
            "table",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table_payload = json.loads(stdout)
        self.assertEqual(len(table_payload["rows"]), 1)
        attempt_row = table_payload["rows"][0]
        self.assertEqual(attempt_row["skill_path"], expected_skill_provenance["path"])
        self.assertEqual(attempt_row["skill_hash"], expected_skill_provenance["sha256"])
        self.assertEqual(attempt_row["skill_source"], expected_skill_provenance["source"])
        self.assertNotIn(skill_text, stdout)

        exit_code, stdout, stderr = self.run_cli(
            "snapshot",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
        )
        self.assertEqual(exit_code, 0, stderr)
        snapshot_payload = json.loads(stdout)
        self.assertEqual(
            snapshot_payload["runtime_model"]["recent_attempts"][0]["user_prompt_receipt"]["prompt_hash"],
            task_payload["user_prompt_hash"],
        )
        self.assertEqual(
            snapshot_payload["runtime_model"]["recent_attempts"][0]["prompt_receipt"]["prompt_hash"],
            task_payload["execution_prompt_hash"],
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--execution-prompt-file",
            str(execution_prompt_path),
            "--prompt-mode",
            "skill",
            "--request-file",
            str(user_prompt_path),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        canonical_payload = json.loads(stdout)["task"]
        for field in (
            "prompt_mode",
            "user_prompt_hash",
            "user_prompt_source",
            "execution_prompt_hash",
            "execution_prompt_source",
            "skill_provenance",
        ):
            self.assertEqual(canonical_payload[field], task_payload[field])
        self.assertEqual(
            canonical_payload["worktree"]["setup_receipt"]["skill_provenance"],
            task_payload["worktree"]["setup_receipt"]["skill_provenance"],
        )

        canonical_worktree = Path(canonical_payload["worktree"]["worktree_path"])
        exit_code, _, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "closed the canonical prompt alias smoke",
            "--cleanup",
            "--json",
            cwd=canonical_worktree,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertFalse(canonical_worktree.exists())

    def test_skill_mode_task_begin_requires_the_managed_skill_without_affecting_raw_mode(self) -> None:
        self.install_repo_runtime()
        profile = load_profile(self.root)
        skill_relative_path = managed_skill_relative_path(profile)
        skill_path = self.root / skill_relative_path
        skill_path.unlink()
        subprocess.run(
            ["git", "-C", str(self.root), "add", "-u", skill_relative_path.as_posix()],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Remove managed skill"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertFalse(skill_path.exists())

        def optional_bytes(path: Path) -> bytes | None:
            return path.read_bytes() if path.exists() else None

        state_before = {
            "planning": optional_bytes(profile.paths.planning_file),
            "runtime": optional_bytes(profile.paths.runtime_file),
            "events": optional_bytes(profile.paths.events_file),
            "worktrees": self.git_output("worktree", "list", "--porcelain"),
            "branches": self.git_output("branch", "--format=%(refname)"),
        }
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Exercise missing managed skill handling.",
            "--prompt-mode",
            "skill",
            "--json",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        task_payload = json.loads(stdout)["task"]
        self.assertEqual(task_payload["operation_status"], "blocked")
        self.assertEqual(task_payload["failure_code"], "managed_skill_missing")
        self.assertEqual(task_payload["next_action"]["action_id"], "managed_skill_required")
        self.assertEqual(task_payload["next_action"]["kind"], "blocked")
        self.assertEqual(task_payload["next_action"]["argv"], [])
        self.assertEqual(task_payload["next_action"]["required_inputs"], ["managed_skill"])
        self.assertIn("managed", task_payload["error"].lower())
        self.assertIn("skill", task_payload["error"].lower())
        self.assertIn(skill_relative_path.as_posix(), task_payload["error"])
        self.assertEqual(load_planning_state(profile.paths).worksets, ())
        self.assertEqual(load_runtime_state(profile.paths).worksets, ())
        self.assertEqual(optional_bytes(profile.paths.planning_file), state_before["planning"])
        self.assertEqual(optional_bytes(profile.paths.runtime_file), state_before["runtime"])
        self.assertEqual(optional_bytes(profile.paths.events_file), state_before["events"])
        self.assertEqual(self.git_output("worktree", "list", "--porcelain"), state_before["worktrees"])
        self.assertEqual(self.git_output("branch", "--format=%(refname)"), state_before["branches"])
        self.assertFalse((profile.paths.control_dir / "prompts").exists())
        observation_report = read_lifecycle_observability(profile)
        self.assertEqual(
            observation_report.label_counts["failure_class"]["managed_skill_missing"],
            1,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Exercise raw task begin without a managed skill.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        self.assertNotIn("skill_provenance", task_payload)
        self.assertNotIn("skill_provenance", task_payload["worktree"]["setup_receipt"])
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "closed the raw missing-skill compatibility smoke",
            "--cleanup",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertFalse(worktree_path.exists())

    def test_task_land_records_user_and_execution_prompt_lineage_when_prompt_was_tuned(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Make a tuned execution prompt and then land it.",
            "--prompt-mode",
            "tuned",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        self.assertNotEqual(task_payload["user_prompt_hash"], task_payload["execution_prompt_hash"])
        self.assertNotIn("skill_provenance", task_payload)
        self.assertNotIn("skill_provenance", task_payload["worktree"]["setup_receipt"])

        (worktree_path / "tuned-land.txt").write_text("tuned land\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "finished the tuned landing flow",
            "--validation",
            "tuned-flow=passed",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "success")
        landed_message = self.git_output("show", "-s", "--format=%B", land_payload["landed_commit"])
        self.assertIn(f"Blackdog-Prompt-Hash: {task_payload['execution_prompt_hash']}", landed_message)
        self.assertIn("Blackdog-Prompt-Source: inline:--prompt", landed_message)
        self.assertIn("Blackdog-Prompt-Mode: tuned", landed_message)
        self.assertIn(f"Blackdog-User-Prompt-Hash: {task_payload['user_prompt_hash']}", landed_message)
        self.assertIn("Blackdog-User-Prompt-Source: inline:--prompt", landed_message)
        self.assertIn("Blackdog-User-Prompt-Mode: raw", landed_message)
        self.assertIn("Blackdog-Changed-Path: tuned-land.txt", landed_message)

    def test_task_cleanup_removes_a_retained_task_workspace(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Keep the task workspace around, then clean it up through the task surface.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "cleanup.txt").write_text("cleanup\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "kept the workspace for explicit cleanup",
            "--validation",
            "cleanup-fixture=passed",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "success")
        self.assertTrue(worktree_path.exists())
        self.assertIsNone(land_payload["cleaned_worktree"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup_payload = json.loads(stdout)["cleanup"]
        self.assertEqual(cleanup_payload["worktree_path"], str(worktree_path))
        self.assertTrue(cleanup_payload["worktree_existed"])
        self.assertTrue(cleanup_payload["deleted_branch"])
        self.assertTrue(cleanup_payload["force_deleted_branch"])
        self.assertEqual(cleanup_payload["branch_cleanup_proof"], "patch_equivalent")
        self.assertIn("canonical landed commit", cleanup_payload["branch_cleanup_reason"])
        self.assertFalse(worktree_path.exists())

    def test_task_cleanup_removes_missing_retained_workspace_when_branch_is_proven(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Land and retain a workspace that later disappears.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "missing-cleanup.txt").write_text("cleanup\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "landed but retained before external cleanup",
            "--validation",
            "external-cleanup-fixture=passed",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 1)
        row = table["rows"][0]
        self.assertEqual(row["cleanup_status"], "cleanup_ready")
        self.assertEqual(row["cleanup_proof"], "patch_equivalent")
        self.assertIn("worktree already absent", row["cleanup_reason"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "cleanup",
            "--project-root",
            str(self.root),
            "--all",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup = json.loads(stdout)["cleanup"]
        self.assertEqual(len(cleanup["cleaned"]), 1)
        self.assertFalse(cleanup["cleaned"][0]["worktree_existed"])
        self.assertEqual(cleanup["cleaned"][0]["branch_cleanup_proof"], "patch_equivalent")
        self.assertEqual(cleanup["remaining"]["counts"]["rows"], 0)
        self.assertNotIn(branch, self.git_output("branch", "--format=%(refname:short)").splitlines())

    def test_task_cleanup_accepts_abandoned_branch_with_patches_already_on_target(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Create a task patch that is later landed outside the canonical Blackdog path.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        branch = task_payload["worktree"]["branch"]
        (worktree_path / "manual-equivalent.txt").write_text("landed manually\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worktree_path), "add", "manual-equivalent.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree_path), "commit", "-m", "Add manually landed cleanup fixture"],
            check=True,
            capture_output=True,
            text=True,
        )
        branch_tip = self.git_output("rev-parse", branch)
        subprocess.run(
            ["git", "-C", str(self.root), "cherry-pick", "--no-commit", branch_tip],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Land task patch with an alternate commit"],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "patch landed manually on the target branch",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["closure"]["status"], "abandoned")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        row = json.loads(stdout)["worktree_table"]["rows"][0]
        self.assertEqual(row["cleanup_status"], "cleanup_ready")
        self.assertEqual(row["cleanup_proof"], "patch_equivalent")
        self.assertIn("all terminal task-branch patches", row["cleanup_reason"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup = json.loads(stdout)["cleanup"]
        self.assertTrue(cleanup["deleted_branch"])
        self.assertTrue(cleanup["force_deleted_branch"])
        self.assertEqual(cleanup["branch_cleanup_proof"], "patch_equivalent")
        self.assertFalse(worktree_path.exists())

    def test_task_cleanup_refuses_active_attempt_even_when_worktree_is_clean(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Start active work that must not be cleaned directly.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        worktree_path = Path(json.loads(stdout)["task"]["worktree"]["worktree_path"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("active attempts must be landed or closed before cleanup", stderr)
        self.assertTrue(worktree_path.exists())

    def test_task_cleanup_refuses_unproven_branch_after_landing(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Keep the task workspace around, then add unlanded work after landing.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "landed.txt").write_text("landed\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "landed but retained the workspace",
            "--validation",
            "unproven-cleanup-fixture=passed",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        (worktree_path / "unlanded.txt").write_text("unlanded\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worktree_path), "add", "unlanded.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree_path), "commit", "-m", "Add unlanded follow-up work"],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("not proven landed", stderr)
        self.assertIn("branch tip changed", stderr)
        self.assertTrue(worktree_path.exists())
        worktree_head = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(self.git_output("rev-parse", "--verify", branch), worktree_head)

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", branch],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_worktree_table_reports_active_dirty_task_worktree(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Leave active work in the task worktree.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "unlanded.txt").write_text("unlanded\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 1)
        row = table["rows"][0]
        self.assertEqual(row["workset_id"], task_payload["workset_id"])
        self.assertEqual(row["task_id"], task_payload["task_id"])
        self.assertEqual(row["state"], "active_attempt")
        self.assertEqual(row["cleanup_status"], "blocked_dirty")
        self.assertEqual(row["worktree_dirty_count"], 1)
        self.assertEqual(row["changed_paths_count"], 1)
        self.assertEqual(row["worktree_path"], str(worktree_path))
        self.assertEqual(row["cleanup_reason"], "worktree has uncommitted changes")
        self.assertIn("blackdog worktree land", row["recommended_action"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("last_commit_message", stdout.splitlines()[0])
        self.assertIn("size_bytes", stdout.splitlines()[0])

    def test_worktree_table_reports_no_ahead_cleanup_proof(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Close a clean no-ahead task workspace.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        worktree_path = Path(json.loads(stdout)["task"]["worktree"]["worktree_path"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "blocked",
            "--summary",
            "closed without changes",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        row = json.loads(stdout)["worktree_table"]["rows"][0]
        self.assertEqual(row["cleanup_status"], "cleanup_ready")
        self.assertEqual(row["cleanup_proof"], "no_ahead")
        self.assertIn("no commits ahead", row["cleanup_reason"])

    def test_worktree_table_reports_contained_branch_cleanup_proof(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Close a branch already contained by main.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "contained.txt").write_text("contained\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree_path), "add", "contained.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(worktree_path), "commit", "-m", "Add contained work"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "merge", "--ff-only", branch],
            check=True,
            capture_output=True,
            text=True,
        )
        (self.root / "after-contained.txt").write_text("after\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "after-contained.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Advance after contained work"],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "blocked",
            "--summary",
            "closed after branch was already contained",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        row = json.loads(stdout)["worktree_table"]["rows"][0]
        self.assertEqual(row["cleanup_status"], "cleanup_ready")
        self.assertEqual(row["cleanup_proof"], "contained")
        self.assertIn("already merged", row["cleanup_reason"])

    def test_worktree_cleanup_all_removes_cleanup_ready_rows_until_table_is_empty(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Retain a landed task worktree for bulk cleanup.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "landed.txt").write_text("landed\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "landed but retained for table cleanup",
            "--validation",
            "table-cleanup-fixture=passed",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 1)
        self.assertEqual(table["counts"]["cleanup_ready"], 1)
        self.assertEqual(table["rows"][0]["cleanup_status"], "cleanup_ready")
        self.assertIn("blackdog task cleanup", table["rows"][0]["cleanup_command"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "cleanup",
            "--project-root",
            str(self.root),
            "--all",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup = json.loads(stdout)["cleanup"]
        self.assertEqual(len(cleanup["cleaned"]), 1)
        self.assertEqual(cleanup["remaining"]["counts"]["rows"], 0)
        self.assertFalse(worktree_path.exists())

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["worktree_table"]["counts"]["rows"], 0)

    def test_worktree_table_reuses_primary_worktree_lookup_for_multiple_rows(self) -> None:
        self.install_repo_runtime()

        for index in range(2):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "begin",
                "--project-root",
                str(self.root),
                "--actor",
                "codex",
                "--prompt",
                f"Retain landed task worktree {index}.",
                "--json",
            )
            self.assertEqual(exit_code, 0, stderr)
            task_payload = json.loads(stdout)["task"]
            worktree_path = Path(task_payload["worktree"]["worktree_path"])
            (worktree_path / f"landed-{index}.txt").write_text(f"landed {index}\n", encoding="utf-8")

            exit_code, _stdout, stderr = self.run_cli(
                "task",
                "land",
                "--project-root",
                str(self.root),
                "--summary",
                f"landed retained task {index}",
                "--validation",
                "table-lookup-fixture=passed",
                "--keep-worktree",
                "--json",
                cwd=worktree_path,
            )
            self.assertEqual(exit_code, 0, stderr)

        with patch("blackdog.wtam.find_primary_worktree", wraps=wtam.find_primary_worktree) as find_primary:
            exit_code, stdout, stderr = self.run_cli(
                "worktree",
                "table",
                "--project-root",
                str(self.root),
                "--json",
            )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 2)
        self.assertEqual(table["counts"]["cleanup_ready"], 2)
        self.assertEqual(find_primary.call_count, 1)

    def test_worktree_cleanup_all_handles_missing_landed_worktree(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Retain a landed task worktree, then lose the directory before cleanup.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        branch = task_payload["worktree"]["branch"]
        (worktree_path / "landed.txt").write_text("landed\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "landed but retained before external cleanup",
            "--validation",
            "missing-worktree-fixture=passed",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        subprocess.run(["git", "-C", str(self.root), "worktree", "remove", str(worktree_path)], check=True)
        self.assertFalse(worktree_path.exists())
        worktree_path.mkdir(parents=True)
        (worktree_path / "leftover.txt").write_text("not a git worktree\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 1)
        self.assertEqual(table["counts"]["cleanup_ready"], 1)
        self.assertEqual(table["rows"][0]["cleanup_status"], "cleanup_ready")
        self.assertIn("worktree already absent", table["rows"][0]["cleanup_reason"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "cleanup",
            "--project-root",
            str(self.root),
            "--all",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup = json.loads(stdout)["cleanup"]
        self.assertEqual(len(cleanup["cleaned"]), 1)
        self.assertEqual(cleanup["remaining"]["counts"]["rows"], 0)
        branch_list = subprocess.run(
            ["git", "-C", str(self.root), "branch", "--list", branch],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(branch_list, "")

    def test_task_recover_reports_dirty_same_thread_recovery_state(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Recover a dirty task worktree through the task surface.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        branch = task_payload["worktree"]["branch"]
        (worktree_path / "recover-task.txt").write_text("recover\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        recovery_payload = json.loads(stdout)["recovery"]
        self.assertEqual(recovery_payload["recovery_state"], "active_attempt")
        self.assertFalse(recovery_payload["stale_claim"])
        self.assertEqual(recovery_payload["task_runtime_status"], "in_progress")
        self.assertEqual(recovery_payload["task_claim"]["actor"], "codex")
        self.assertTrue(recovery_payload["worktree_dirty"])
        actions = "\n".join(recovery_payload["recommended_actions"])
        self.assertIn("blackdog task land", actions)
        self.assertIn("blackdog task close", actions)
        command_rows = recovery_payload["recommended_commands"]
        commands = [row["command"] for row in command_rows]
        self.assertIn('blackdog task land --summary "..."', commands)
        self.assertIn('blackdog task close --status blocked|failed|abandoned --summary "..."', commands)
        self.assertTrue(all(row["reason"] for row in command_rows))
        self.assertTrue(all(row["disposition"] for row in command_rows))

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", branch],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_task_show_reports_missing_target_branch_without_crashing(self) -> None:
        payload = {
            "id": "missing-target",
            "title": "Missing target",
            "tasks": [{"id": "MT-1", "title": "Inspect missing target", "intent": "recover stale target refs"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "missing-target",
            "--task",
            "MT-1",
            "--actor",
            "codex",
            "--prompt",
            "Start the missing target slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        attempt_id = start_payload["attempt_id"]
        branch = start_payload["branch"]
        worktree_path = Path(start_payload["worktree_path"])
        profile = load_profile(self.root)
        finished = finish_task(
            profile,
            workset_id="missing-target",
            task_id="MT-1",
            attempt_id=attempt_id,
            actor="codex",
            status="blocked",
            summary="blocked before stale target inspection",
        )
        runtime_state = load_runtime_state(profile.paths)
        runtime_workset = next(item for item in runtime_state.worksets if item.workset_id == "missing-target")
        runtime_task_state = next(item for item in runtime_workset.task_states if item.task_id == "MT-1")
        rewritten_runtime = merge_workset_runtime(
            runtime_state,
            workset_id="missing-target",
            task_ids={"MT-1"},
            incoming_records=(replace(runtime_task_state, failure_class=None, recovery_action=None),),
            incoming_attempts=(replace(finished, target_branch="v3", failure_class=None, recovery_action=None),),
        )
        save_runtime_state(profile.paths, rewritten_runtime)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            "missing-target",
            "--task",
            "MT-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["task_show"]
        self.assertEqual(show_payload["recovery_state"], "stale_reference")
        self.assertEqual(show_payload["target_branch"], "v3")
        self.assertTrue(show_payload["branch_exists"])
        self.assertFalse(show_payload["target_branch_exists"])
        self.assertEqual(show_payload["failure_class"], "stale_branch")
        self.assertEqual(show_payload["recovery_action"], "restore_ref_or_cancel_task")
        self.assertIn("target branch 'v3' is missing", show_payload["branch_ahead_error"])
        self.assertIn("restore target branch `v3`", "\n".join(show_payload["recommended_actions"]))

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", branch],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_task_recover_reports_missing_task_branch_without_crashing(self) -> None:
        payload = {
            "id": "missing-branch",
            "title": "Missing branch",
            "tasks": [{"id": "MB-1", "title": "Inspect missing branch", "intent": "recover stale branch refs"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "missing-branch",
            "--task",
            "MB-1",
            "--actor",
            "codex",
            "--prompt",
            "Start the missing branch slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        attempt_id = start_payload["attempt_id"]
        branch = start_payload["branch"]
        worktree_path = Path(start_payload["worktree_path"])
        profile = load_profile(self.root)
        finished = finish_task(
            profile,
            workset_id="missing-branch",
            task_id="MB-1",
            attempt_id=attempt_id,
            actor="codex",
            status="blocked",
            summary="blocked before stale branch inspection",
        )
        runtime_state = load_runtime_state(profile.paths)
        runtime_workset = next(item for item in runtime_state.worksets if item.workset_id == "missing-branch")
        runtime_task_state = next(item for item in runtime_workset.task_states if item.task_id == "MB-1")
        rewritten_runtime = merge_workset_runtime(
            runtime_state,
            workset_id="missing-branch",
            task_ids={"MB-1"},
            incoming_records=(replace(runtime_task_state, failure_class=None, recovery_action=None),),
            incoming_attempts=(replace(finished, failure_class=None, recovery_action=None),),
        )
        save_runtime_state(profile.paths, rewritten_runtime)
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", branch],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--workset",
            "missing-branch",
            "--task",
            "MB-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        recovery_payload = json.loads(stdout)["recovery"]
        self.assertEqual(recovery_payload["recovery_state"], "stale_reference")
        self.assertEqual(recovery_payload["branch"], branch)
        self.assertFalse(recovery_payload["branch_exists"])
        self.assertTrue(recovery_payload["target_branch_exists"])
        self.assertEqual(recovery_payload["failure_class"], "stale_branch")
        self.assertIn(f"task branch {branch!r} is missing", recovery_payload["branch_ahead_error"])
        self.assertIn("use `blackdog task cancel`", "\n".join(recovery_payload["recommended_actions"]))

    def test_task_recover_reports_missing_active_attempt_worktree(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Inspect a missing active attempt worktree.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        task_id = task_payload["task_id"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        recovery_payload = json.loads(stdout)["recovery"]
        self.assertTrue(recovery_payload["active_attempt"])
        self.assertFalse(recovery_payload["worktree_exists"])
        self.assertTrue(recovery_payload["branch_exists"])
        self.assertTrue(recovery_payload["target_branch_exists"])
        self.assertEqual(recovery_payload["failure_class"], "missing_worktree")
        self.assertEqual(recovery_payload["recovery_action"], "restore_or_cleanup_worktree")
        self.assertIn("restore the task workspace", "\n".join(recovery_payload["recommended_actions"]))

        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", branch],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_task_recover_can_release_a_stale_claim(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Recover a stale claim without editing snapshots by hand.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        task_id = task_payload["task_id"]
        attempt_id = task_payload["worktree"]["attempt_id"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])

        profile = load_profile(self.root)
        runtime_state = load_runtime_state(profile.paths)
        runtime_workset = next(item for item in runtime_state.worksets if item.workset_id == workset_id)
        active_attempt = next(item for item in runtime_workset.attempts if item.attempt_id == attempt_id)
        stale_attempt = replace(
            active_attempt,
            status="blocked",
            ended_at=now_iso(),
            summary="agent interrupted before releasing claims",
            elapsed_seconds=1,
        )
        stale_runtime_state = merge_workset_runtime(
            runtime_state,
            workset_id=workset_id,
            task_ids={task_id},
            incoming_records=None,
            incoming_attempts=(stale_attempt,),
        )
        save_runtime_state(profile.paths, stale_runtime_state)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        recovery_payload = json.loads(stdout)["recovery"]
        self.assertEqual(recovery_payload["recovery_state"], "stale_claim")
        self.assertTrue(recovery_payload["stale_claim"])
        self.assertFalse(recovery_payload["active_attempt"])
        self.assertEqual(recovery_payload["task_claim"]["attempt_id"], attempt_id)
        self.assertIn("release-stale-claim", "\n".join(recovery_payload["recommended_actions"]))

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--release-stale-claim",
            "--status",
            "abandoned",
            "--summary",
            "released the stale claim after interruption",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        released_payload = json.loads(stdout)["recovery"]
        self.assertTrue(released_payload["released_stale_claim"])
        self.assertFalse(released_payload["stale_claim"])
        self.assertIsNone(released_payload["task_claim"])
        self.assertIsNone(released_payload["workset_claim"])
        self.assertEqual(released_payload["task_runtime_status"], "canceled")
        self.assertEqual(released_payload["repaired_runtime_status"], "canceled")
        released_runtime = load_runtime_state(profile.paths)
        released_workset = next(
            item for item in released_runtime.worksets if item.workset_id == workset_id
        )
        released_task_state = next(
            item for item in released_workset.task_states if item.task_id == task_id
        )
        self.assertEqual(released_task_state.actor, "codex")

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary_payload = json.loads(stdout)
        self.assertEqual(summary_payload["counts"]["claimed_tasks"], 0)
        self.assertEqual(summary_payload["counts"]["claimed_worksets"], 0)
        self.assertEqual(summary_payload["counts"]["active_attempts"], 0)

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", branch],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_worktree_show_and_close_surface_active_attempt_recovery(self) -> None:
        payload = {
            "id": "recovery-mode",
            "title": "Recovery mode",
            "tasks": [{"id": "RC-1", "title": "Recover the slice", "intent": "inspect and close an active attempt"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "recovery-mode",
            "--task",
            "RC-1",
            "--actor",
            "codex",
            "--prompt",
            "Start the recovery slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        worktree_path = Path(start_payload["worktree_path"])
        (worktree_path / "recover.txt").write_text("recover\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            "recovery-mode",
            "--task",
            "RC-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["worktree_show"]
        self.assertTrue(show_payload["active_attempt"])
        self.assertTrue(show_payload["worktree_dirty"])
        self.assertIn("recover.txt", show_payload["changed_paths"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "close",
            "--project-root",
            str(self.root),
            "--workset",
            "recovery-mode",
            "--task",
            "RC-1",
            "--actor",
            "codex",
            "--status",
            "abandoned",
            "--summary",
            "abandoned the recovery slice",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        close_payload = json.loads(stdout)["closure"]
        self.assertEqual(close_payload["status"], "abandoned")
        self.assertIn("recover.txt", close_payload["changed_paths"])

        exit_code, stdout, stderr = self.run_cli(
            "next",
            "--project-root",
            str(self.root),
            "--workset",
            "recovery-mode",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        next_payload = json.loads(stdout)
        self.assertEqual(next_payload["selection_mode"], "none")
        self.assertIsNone(next_payload["selected_task"])

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", start_payload["branch"]],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_worktree_land_keeps_attempt_active_when_landing_is_blocked_and_can_retry(self) -> None:
        payload = {
            "id": "blocked-land",
            "title": "Blocked land",
            "tasks": [{"id": "BL-1", "title": "Block landing", "intent": "retry after landing cannot proceed"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "blocked-land",
            "--task",
            "BL-1",
            "--actor",
            "codex",
            "--prompt",
            "Attempt the blocked land slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        attempt_id = start_payload["attempt_id"]
        worktree_path = Path(start_payload["worktree_path"])
        (worktree_path / "blocked.txt").write_text("blocked\n", encoding="utf-8")
        (self.root / "primary-dirty.txt").write_text("dirty\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "blocked-land",
            "--task",
            "BL-1",
            "--actor",
            "codex",
            "--summary",
            "attempted the blocked land slice",
            "--json",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "blocked")
        self.assertEqual(land_payload["attempt_id"], attempt_id)
        self.assertTrue(land_payload["attempt_active"])
        self.assertEqual(land_payload["land_failure_disposition"], "retryable")
        self.assertIn("dirty primary worktree", land_payload["error"])
        self.assertEqual(land_payload["failure_class"], "dirty_primary")
        self.assertEqual(land_payload["recovery_action"], "clean_primary_worktree")
        self.assertTrue(land_payload["operator_issue"])

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "blocked-land",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["counts"]["active_attempts"], 1)
        self.assertEqual(summary["counts"]["claimed_tasks"], 1)
        self.assertEqual(summary["recent_attempts"][0]["status"], "in_progress")

        (self.root / "primary-dirty.txt").unlink()

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "blocked-land",
            "--task",
            "BL-1",
            "--actor",
            "codex",
            "--summary",
            "retried the blocked land slice",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        retry_payload = json.loads(stdout)["landing"]
        self.assertEqual(retry_payload["status"], "success")
        self.assertEqual(retry_payload["attempt_id"], attempt_id)
        self.assertIn("blocked.txt", retry_payload["changed_paths"])
        self.assertFalse(worktree_path.exists())
        self.assertEqual((self.root / "blocked.txt").read_text(encoding="utf-8"), "blocked\n")

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "blocked-land",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["counts"]["active_attempts"], 0)
        self.assertEqual(summary["counts"]["claimed_tasks"], 0)
        self.assertEqual(summary["recent_attempts"][0]["status"], "success")

    def _canonical_landing_blocked_after_intent(
        self,
    ) -> tuple[dict[str, object], Path, object]:
        self.put_workset(
            {
                "id": "durable-abort",
                "title": "Durable abort",
                "tasks": [
                    {
                        "id": "DA-1",
                        "title": "Exercise durable abort",
                        "intent": "prove close and landing recovery",
                    }
                ],
            }
        )
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "durable-abort",
            "--task",
            "DA-1",
            "--actor",
            "codex",
            "--prompt",
            "Exercise the durable landing abort transaction.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        worktree_path = Path(start_payload["worktree_path"])
        (worktree_path / "durable-abort.txt").write_text(
            "durable abort\n",
            encoding="utf-8",
        )
        blocker = wtam.StaleTaskBranchError(
            branch=start_payload["branch"],
            target_branch="main",
            branch_worktree=worktree_path,
        )
        with patch.object(wtam, "_update_landing_target", side_effect=blocker):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "land",
                "--project-root",
                str(self.root),
                "--workset",
                "durable-abort",
                "--task",
                "DA-1",
                "--actor",
                "codex",
                "--summary",
                "land the durable abort change",
                "--validation",
                "durable-abort-fixture=passed",
                "--json",
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        blocked = json.loads(stdout)["landing"]
        self.assertEqual(blocked["operation_status"], "partial")
        transaction = wtam.load_landing_transaction(
            load_profile(self.root),
            workset_id="durable-abort",
            task_id="DA-1",
            attempt_id=start_payload["attempt_id"],
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(
            transaction.phases,
            ("intent_recorded", "source_prepared", "canonical_commit_created"),
        )
        return start_payload, worktree_path, transaction

    def _close_durable_abort(
        self,
        *,
        summary: str = "block the stale landing",
        status: str = "blocked",
    ) -> tuple[int, dict[str, object], str]:
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--workset",
            "durable-abort",
            "--task",
            "DA-1",
            "--actor",
            "codex",
            "--status",
            status,
            "--summary",
            summary,
            "--validation",
            "abort-proof=passed",
            "--residual",
            "retained source requires a successor attempt",
            "--followup",
            "rebase the retained source",
            "--cleanup",
            "--json",
        )
        return exit_code, json.loads(stdout)["closure"] if stdout else {}, stderr

    def test_landing_abort_binds_close_request_and_exact_retry_is_a_noop(self) -> None:
        start_payload, worktree_path, _transaction = self._canonical_landing_blocked_after_intent()
        exit_code, closure, stderr = self._close_durable_abort()
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(closure["operation_status"], "succeeded")
        self.assertTrue(closure["mutation_started"])
        self.assertTrue(closure["mutation_completed"])
        self.assertEqual(closure["mutation_phase"], "landing_abort_complete")
        self.assertTrue(closure["abort_complete"])
        self.assertTrue(worktree_path.exists())
        self.assertFalse(closure["cleanup_performed"])
        self.assertIn("source retained", closure["cleanup_reason"])

        profile = load_profile(self.root)
        transaction = wtam.load_landing_transaction(
            profile,
            workset_id="durable-abort",
            task_id="DA-1",
            attempt_id=start_payload["attempt_id"],
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction.outcome, "abort_complete")
        self.assertEqual(
            transaction.abort_data["close_request"]["summary"],
            "block the stale landing",
        )
        before_events = profile.paths.events_file.read_bytes()
        before_runtime = profile.paths.runtime_file.read_bytes()

        exit_code, retried, stderr = self._close_durable_abort()
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(retried["operation_status"], "succeeded")
        self.assertFalse(retried["mutation_started"])
        self.assertFalse(retried["mutation_completed"])
        self.assertEqual(profile.paths.events_file.read_bytes(), before_events)
        self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)

        exit_code, _conflict, stderr = self._close_durable_abort(
            summary="changed closure evidence"
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("conflicts with the durable close request", stderr)
        self.assertEqual(profile.paths.events_file.read_bytes(), before_events)
        self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)

    def test_landing_abort_supersedes_before_finalization_and_lands_same_candidate(self) -> None:
        start_payload, worktree_path, _transaction = self._canonical_landing_blocked_after_intent()
        original_target_state = wtam._landing_abort_target_state
        calls = 0

        def stop_after_abort_cleanup(*, intent, landed_commit):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("stop after abort cleanup")
            return original_target_state(intent=intent, landed_commit=landed_commit)

        with patch.object(
            wtam,
            "_landing_abort_target_state",
            side_effect=stop_after_abort_cleanup,
        ):
            exit_code, closure, stderr = self._close_durable_abort()
        self.assertEqual(exit_code, 1, stderr)
        self.assertEqual(closure["operation_status"], "blocked")
        self.assertTrue(closure["mutation_started"])
        transaction = wtam.load_landing_transaction(
            load_profile(self.root),
            workset_id="durable-abort",
            task_id="DA-1",
            attempt_id=start_payload["attempt_id"],
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None and transaction.abort_data is not None
        candidate = transaction.abort_data["landed_commit"]
        self.assertTrue(transaction.abort_cleanup_complete)
        self.assertFalse(transaction.abort_runtime_finalized)
        self.assertFalse(Path(transaction.intent.temporary_worktree_path).exists())
        subprocess.run(
            ["git", "-C", str(self.root), "merge", "--ff-only", candidate],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, closure, stderr = self._close_durable_abort()
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(closure["status"], "success")
        self.assertTrue(closure["close_superseded_by_landing"])
        self.assertEqual(closure["landed_commit"], candidate)
        self.assertFalse(worktree_path.exists())
        transaction = wtam.load_landing_transaction(
            load_profile(self.root),
            workset_id="durable-abort",
            task_id="DA-1",
            attempt_id=start_payload["attempt_id"],
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction.abort_superseded)
        self.assertTrue(transaction.complete)

    def test_landing_abort_runtime_fault_retries_public_close_then_reconciles_late_target(self) -> None:
        start_payload, worktree_path, _transaction = self._canonical_landing_blocked_after_intent()
        with patch(
            "blackdog_core.backlog._append_decision_owned_events",
            side_effect=OSError("stop after abort runtime save"),
        ):
            exit_code, closure, stderr = self._close_durable_abort()
        self.assertEqual(exit_code, 1, stderr)
        self.assertEqual(closure["operation_status"], "blocked")
        self.assertTrue(closure["mutation_started"])
        self.assertFalse(closure["mutation_completed"])
        runtime = load_runtime_state(load_profile(self.root).paths)
        attempt = next(
            attempt
            for workset in runtime.worksets
            if workset.workset_id == "durable-abort"
            for attempt in workset.attempts
            if attempt.attempt_id == start_payload["attempt_id"]
        )
        self.assertEqual(attempt.status, "blocked")

        transaction = wtam.load_landing_transaction(
            load_profile(self.root),
            workset_id="durable-abort",
            task_id="DA-1",
            attempt_id=start_payload["attempt_id"],
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None and transaction.abort_data is not None
        candidate = transaction.abort_data["landed_commit"]
        subprocess.run(
            ["git", "-C", str(self.root), "merge", "--ff-only", candidate],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, closure, stderr = self._close_durable_abort()
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(closure["mutation_phase"], "landing_abort_complete")
        self.assertFalse(closure.get("close_superseded_by_landing", False))
        self.assertTrue(worktree_path.exists())

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            "durable-abort",
            "--task",
            "DA-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        shown = json.loads(stdout)["task_show"]
        self.assertEqual(
            shown["next_action"]["action_id"],
            "verify_late_landing_reconciliation",
        )
        reconcile_argv = shown["next_action"]["argv"]
        self.assertIn(f"--landed-commit={candidate}", reconcile_argv)

        exit_code, stdout, stderr = self.run_cli(*reconcile_argv[1:], "--json")
        self.assertEqual(exit_code, 0, stderr)
        dry_run = json.loads(stdout)["landing_reconciliation"]
        self.assertEqual(dry_run["operation_status"], "observed")
        apply_argv = dry_run["next_action"]["argv"]
        exit_code, stdout, stderr = self.run_cli(*apply_argv[1:], "--json")
        self.assertEqual(exit_code, 0, stderr)
        applied = json.loads(stdout)["landing_reconciliation"]
        self.assertEqual(applied["operation_status"], "succeeded")
        self.assertTrue(applied["native_abort_reconciled"])
        self.assertEqual(applied["landed_commit"], candidate)
        self.assertIsNone(applied["native_cleanup_error"], applied)
        self.assertTrue(applied["native_cleanup"]["worktree_removed"], applied)
        self.assertFalse(worktree_path.exists())
        runtime = load_runtime_state(load_profile(self.root).paths)
        attempt = next(
            attempt
            for workset in runtime.worksets
            if workset.workset_id == "durable-abort"
            for attempt in workset.attempts
            if attempt.attempt_id == start_payload["attempt_id"]
        )
        self.assertEqual(attempt.status, "success")
        self.assertEqual(attempt.landed_commit, candidate)
        land_events = [
            event
            for event in load_events(load_profile(self.root).paths.events_file)
            if event.get("type") == "worktree.land"
            and event.get("payload", {}).get("attempt_id") == start_payload["attempt_id"]
        ]
        self.assertEqual(len(land_events), 1)

    def test_nonterminal_abort_guards_other_task_mutators_until_close_repairs(self) -> None:
        start_payload, _worktree_path, _transaction = self._canonical_landing_blocked_after_intent()
        with patch(
            "blackdog_core.backlog._append_decision_owned_events",
            side_effect=OSError("stop after abort runtime save"),
        ):
            exit_code, closure, stderr = self._close_durable_abort()
        self.assertEqual(exit_code, 1, stderr)
        self.assertTrue(closure["mutation_started"])
        profile = load_profile(self.root)
        transaction = wtam.load_landing_transaction(
            profile,
            workset_id="durable-abort",
            task_id="DA-1",
            attempt_id=start_payload["attempt_id"],
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction.outcome, "abort_in_progress")
        before_events = profile.paths.events_file.read_bytes()
        before_runtime = profile.paths.runtime_file.read_bytes()
        expected_action = closure["next_action"]
        self.assertEqual(expected_action["action_id"], "resume_landing_abort")

        guarded_commands = (
            (
                "task",
                "cancel",
                "--project-root",
                str(self.root),
                "--workset",
                "durable-abort",
                "--task",
                "DA-1",
                "--actor",
                "codex",
                "--summary",
                "must not cancel partial abort",
                "--json",
            ),
            (
                "task",
                "reopen",
                "--project-root",
                str(self.root),
                "--workset",
                "durable-abort",
                "--task",
                "DA-1",
                "--actor",
                "codex",
                "--summary",
                "must not reopen partial abort",
                "--json",
            ),
            (
                "task",
                "begin",
                "--project-root",
                str(self.root),
                "--workset",
                "durable-abort",
                "--task",
                "DA-1",
                "--actor",
                "codex",
                "--prompt",
                "Exercise the durable landing abort transaction.",
                "--json",
            ),
        )
        for argv in guarded_commands:
            with self.subTest(command=argv[1]):
                exit_code, stdout, stderr = self.run_cli(*argv)
                self.assertEqual(exit_code, 1)
                self.assertEqual(stderr, "")
                result_key = "task" if argv[1] == "begin" else "task_state"
                guarded = json.loads(stdout)[result_key]
                self.assertEqual(guarded["operation_status"], "blocked")
                self.assertFalse(guarded["mutation_started"])
                self.assertFalse(guarded["mutation_completed"])
                self.assertEqual(guarded["mutation_phase"], "none")
                self.assertEqual(guarded["next_action"], expected_action)
                self.assertEqual(profile.paths.events_file.read_bytes(), before_events)
                self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)

                exit_code, stdout, stderr = self.run_cli(*argv[:-1])
                self.assertEqual(exit_code, 1)
                self.assertEqual(stderr, "")
                self.assertIn("operation status: blocked", stdout)
                self.assertIn("mutation: started=no completed=no phase=none", stdout)
                self.assertIn("next action: resume_landing_abort", stdout)
                self.assertIn(
                    f"next command: {expected_action['command']}",
                    stdout,
                )
                self.assertEqual(profile.paths.events_file.read_bytes(), before_events)
                self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--workset",
            "durable-abort",
            "--task",
            "DA-1",
            "--json",
        )
        self.assertEqual(exit_code, 1, stderr)
        cleanup = json.loads(stdout)["cleanup"]
        self.assertEqual(cleanup["operation_status"], "blocked")
        self.assertTrue(cleanup["cleanup_refused"])
        self.assertEqual(profile.paths.events_file.read_bytes(), before_events)
        self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)

        candidate = transaction.abort_data["landed_commit"]
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "reconcile-landing",
            "--project-root",
            str(self.root),
            "--workset",
            "durable-abort",
            "--task",
            "DA-1",
            "--attempt",
            start_payload["attempt_id"],
            "--landed-commit",
            candidate,
            "--actor",
            "codex",
            "--apply",
            "--json",
        )
        self.assertEqual(exit_code, 1, stderr)
        reconciliation = json.loads(stdout)["landing_reconciliation"]
        self.assertEqual(reconciliation["operation_status"], "blocked")
        self.assertEqual(profile.paths.events_file.read_bytes(), before_events)
        self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)

        exit_code, closure, stderr = self._close_durable_abort()
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(closure["mutation_phase"], "landing_abort_complete")

    def test_abandoned_abort_can_reconcile_late_exact_target_containment(self) -> None:
        start_payload, worktree_path, _transaction = self._canonical_landing_blocked_after_intent()
        exit_code, closure, stderr = self._close_durable_abort(
            status="abandoned",
            summary="abandon the stale landing",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(closure["status"], "abandoned")
        transaction = wtam.load_landing_transaction(
            load_profile(self.root),
            workset_id="durable-abort",
            task_id="DA-1",
            attempt_id=start_payload["attempt_id"],
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None and transaction.abort_data is not None
        candidate = transaction.abort_data["landed_commit"]
        subprocess.run(
            ["git", "-C", str(self.root), "merge", "--ff-only", candidate],
            check=True,
            capture_output=True,
            text=True,
        )
        base_argv = (
            "task",
            "reconcile-landing",
            "--project-root",
            str(self.root),
            "--workset",
            "durable-abort",
            "--task",
            "DA-1",
            "--attempt",
            start_payload["attempt_id"],
            "--landed-commit",
            candidate,
            "--actor",
            "codex",
            "--reason",
            "late exact containment after abandoned abort",
        )
        exit_code, stdout, stderr = self.run_cli(*base_argv, "--json")
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(
            json.loads(stdout)["landing_reconciliation"]["previous_status"],
            "abandoned",
        )
        exit_code, stdout, stderr = self.run_cli(*base_argv, "--apply", "--json")
        self.assertEqual(exit_code, 0, stderr)
        applied = json.loads(stdout)["landing_reconciliation"]
        self.assertEqual(applied["status"], "success")
        self.assertEqual(applied["previous_status"], "abandoned")
        self.assertFalse(worktree_path.exists())

    def test_abandoned_abort_rejects_mismatched_candidate_without_mutation(self) -> None:
        start_payload, worktree_path, _transaction = self._canonical_landing_blocked_after_intent()
        exit_code, closure, stderr = self._close_durable_abort(
            status="abandoned",
            summary="abandon before mismatched reconciliation",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(closure["status"], "abandoned")
        profile = load_profile(self.root)
        transaction = wtam.load_landing_transaction(
            profile,
            workset_id="durable-abort",
            task_id="DA-1",
            attempt_id=start_payload["attempt_id"],
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None and transaction.abort_data is not None
        wrong_candidate = transaction.intent.target_base_commit
        self.assertNotEqual(wrong_candidate, transaction.abort_data["landed_commit"])
        runtime_before = profile.paths.runtime_file.read_bytes()
        events_before = profile.paths.events_file.read_bytes()
        worktrees_before = self.git_output("worktree", "list", "--porcelain")
        refs_before = self.git_output("branch", "--format=%(refname) %(objectname)")
        base_args = (
            "task",
            "reconcile-landing",
            "--project-root",
            str(self.root),
            "--workset",
            "durable-abort",
            "--task",
            "DA-1",
            "--attempt",
            start_payload["attempt_id"],
            "--landed-commit",
            wrong_candidate,
            "--actor",
            "codex",
        )

        for apply_args in ((), ("--apply",)):
            with self.subTest(apply=bool(apply_args)):
                exit_code, stdout, stderr = self.run_cli(
                    *base_args,
                    *apply_args,
                    "--json",
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("exact recorded canonical candidate", stderr)
                self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
                self.assertEqual(profile.paths.events_file.read_bytes(), events_before)
                self.assertEqual(
                    self.git_output("worktree", "list", "--porcelain"),
                    worktrees_before,
                )
                self.assertEqual(
                    self.git_output("branch", "--format=%(refname) %(objectname)"),
                    refs_before,
                )
                self.assertTrue(worktree_path.exists())

    def test_abandoned_without_native_abort_is_zero_mutation_reconciliation_refusal(self) -> None:
        workset_id = "abandoned-no-native-abort"
        task_id = "ANA-1"
        self.put_workset(
            {
                "id": workset_id,
                "title": "Reject arbitrary abandoned reconciliation",
                "branch_intent": {
                    "target_branch": "main",
                    "integration_branch": "main",
                },
                "tasks": [
                    {
                        "id": task_id,
                        "title": "Require native abort proof",
                        "intent": "reject arbitrary abandoned history",
                    }
                ],
            }
        )
        profile = load_profile(self.root)
        attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner",
            target_branch="main",
            prompt_receipt=create_prompt_receipt(
                "Require exact native abort proof.",
                source="unit-test",
            ),
        )
        (self.root / "arbitrary-abandoned.txt").write_text("landed\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "arbitrary-abandoned.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        commit_message = (
            f"blackdog({workset_id}/{task_id}): Candidate\n\n"
            "This candidate must not override arbitrary abandoned history.\n\n"
            f"Blackdog-Workset: {workset_id}\n"
            f"Blackdog-Task: {task_id}\n"
            f"Blackdog-Attempt: {attempt.attempt_id}\n"
            "Blackdog-Actor: owner\n"
            "Blackdog-Status: success\n"
            "Blackdog-Target-Branch: main\n"
            "Blackdog-Changed-Path: arbitrary-abandoned.txt\n"
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "--quiet", "-F", "-"],
            input=commit_message,
            check=True,
            capture_output=True,
            text=True,
        )
        candidate = self.git_output("rev-parse", "HEAD")
        finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            actor="owner",
            status="abandoned",
            summary="Abandoned without a native landing transaction.",
        )
        runtime_before = profile.paths.runtime_file.read_bytes()
        events_before = profile.paths.events_file.read_bytes()
        head_before = self.git_output("rev-parse", "HEAD")
        base_args = (
            "task",
            "reconcile-landing",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--attempt",
            attempt.attempt_id,
            "--landed-commit",
            candidate,
            "--actor",
            "auditor",
        )

        for apply_args in ((), ("--apply",)):
            with self.subTest(apply=bool(apply_args)):
                exit_code, stdout, stderr = self.run_cli(
                    *base_args,
                    *apply_args,
                    "--json",
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("exact terminal native abort-complete transaction", stderr)
                self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
                self.assertEqual(profile.paths.events_file.read_bytes(), events_before)
                self.assertEqual(self.git_output("rev-parse", "HEAD"), head_before)

    def test_worktree_land_classifies_stale_branch_blocker(self) -> None:
        payload = {
            "id": "stale-land",
            "title": "Stale land",
            "tasks": [{"id": "SL-1", "title": "Block stale branch", "intent": "detect stale task branch"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "stale-land",
            "--task",
            "SL-1",
            "--actor",
            "codex",
            "--prompt",
            "Attempt the stale branch land slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        worktree_path = Path(start_payload["worktree_path"])
        (worktree_path / "stale.txt").write_text("stale\n", encoding="utf-8")
        (self.root / "main-advanced.txt").write_text("advanced\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "main-advanced.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Advance main"],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "stale-land",
            "--task",
            "SL-1",
            "--actor",
            "codex",
            "--summary",
            "attempted stale branch land",
            "--json",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "blocked")
        self.assertTrue(land_payload["attempt_active"])
        self.assertEqual(land_payload["land_failure_disposition"], "retryable")
        self.assertEqual(land_payload["failure_class"], "stale_branch")
        self.assertEqual(land_payload["recovery_action"], "rebase_task_branch")
        self.assertIn(f"git -C {worktree_path} rebase main", land_payload["error"])
        self.assertIn(f"git -C {worktree_path} rebase main", land_payload["recommended_actions"][0])

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", start_payload["branch"]],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_worktree_land_closes_terminal_no_change_failure_without_extra_close_call(self) -> None:
        payload = {
            "id": "terminal-land",
            "title": "Terminal land",
            "tasks": [{"id": "TL-1", "title": "Close no-op land", "intent": "close terminal land failures"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "terminal-land",
            "--task",
            "TL-1",
            "--actor",
            "codex",
            "--prompt",
            "Attempt to land a no-op slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        attempt_id = start_payload["attempt_id"]
        worktree_path = Path(start_payload["worktree_path"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "terminal-land",
            "--task",
            "TL-1",
            "--actor",
            "codex",
            "--summary",
            "attempted a no-op land",
            "--json",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "blocked")
        self.assertEqual(land_payload["attempt_id"], attempt_id)
        self.assertFalse(land_payload["attempt_active"])
        self.assertEqual(land_payload["land_failure_disposition"], "closed")
        self.assertIn("has no changes relative to", land_payload["error"])
        self.assertTrue(land_payload["cleanup_performed"])
        self.assertFalse(worktree_path.exists())

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "terminal-land",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["counts"]["active_attempts"], 0)
        self.assertEqual(summary["counts"]["claimed_tasks"], 0)
        self.assertEqual(summary["recent_attempts"][0]["status"], "blocked")
        self.assertEqual(summary["recent_attempts"][0]["failure_class"], "no_changes")

    def test_attempts_summary_and_table_report_completed_history(self) -> None:
        profile = load_profile(self.root)
        upsert_workset(
            profile,
            {
                "id": "attempt-audit",
                "title": "Attempt audit",
                "workspace": {"identity": "attempt-audit-workspace"},
                "branch_intent": {"target_branch": "main", "integration_branch": "main"},
                "tasks": [
                    {"id": "AT-1", "title": "Land a change", "intent": "record a landed attempt"},
                    {"id": "AT-2", "title": "Block a change", "intent": "record a blocked attempt"},
                ],
            },
        )
        landed_attempt = start_task(
            profile,
            workset_id="attempt-audit",
            task_id="AT-1",
            actor="codex",
            workspace_mode="git-worktree",
            worktree_role="linked",
            worktree_path="/tmp/attempt-audit-1",
            branch="feature/attempt-audit-1",
            start_commit="abc123",
            prompt_receipt=create_prompt_receipt("Land the audit slice.", source="unit-test", mode="tuned"),
            user_prompt_receipt=create_prompt_receipt("Land the audit slice.", source="user-test", mode="raw"),
        )
        finish_task(
            profile,
            workset_id="attempt-audit",
            task_id="AT-1",
            attempt_id=landed_attempt.attempt_id,
            actor="codex",
            status="success",
            summary="landed the slice",
            changed_paths=("src/blackdog_cli/main.py",),
            validations=(ValidationRecord(name="unit", status="passed"),),
            landed_commit="def456",
            elapsed_seconds=11,
        )
        blocked_attempt = start_task(
            profile,
            workset_id="attempt-audit",
            task_id="AT-2",
            actor="codex",
            workspace_mode="git-worktree",
            worktree_role="linked",
            worktree_path="/tmp/attempt-audit-2",
            branch="feature/attempt-audit-2",
            start_commit="abc124",
            prompt_receipt=create_prompt_receipt("Block the audit slice.", source="unit-test", mode="tuned"),
            user_prompt_receipt=create_prompt_receipt("Block the audit slice.", source="user-test", mode="raw"),
        )
        finish_task(
            profile,
            workset_id="attempt-audit",
            task_id="AT-2",
            attempt_id=blocked_attempt.attempt_id,
            actor="codex",
            status="blocked",
            summary="waiting on review",
            validations=(ValidationRecord(name="unit", status="failed"),),
            elapsed_seconds=7,
        )

        exit_code, stdout, stderr = self.run_cli(
            "attempts",
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "attempt-audit",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["counts"]["completed_attempts"], 2)
        self.assertEqual(summary["counts"]["landed"], 1)
        self.assertEqual(summary["counts"]["not_landed"], 1)
        self.assertEqual(summary["counts"]["validation_passed"], 1)
        self.assertEqual(summary["counts"]["validation_failed"], 1)
        self.assertEqual(summary["workset_scope"], "attempt-audit")
        self.assertEqual(summary["tasks"][0]["task_ref"], "attempt-audit/AT-1")
        self.assertNotIn("worksets", summary)
        self.assertIsNone(summary["recent_completed_attempts"][0]["prompt_source"])
        self.assertIsNone(summary["recent_completed_attempts"][0]["prompt_hash"])
        self.assertEqual(summary["recent_completed_attempts"][0]["user_prompt_source"], "user-test")
        self.assertEqual(summary["recent_completed_attempts"][0]["execution_prompt_source"], "unit-test")
        self.assertEqual(
            summary["recent_completed_attempts"][0]["user_prompt_hash"],
            summary["recent_completed_attempts"][0]["execution_prompt_hash"],
        )

        exit_code, stdout, stderr = self.run_cli(
            "attempts",
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "attempt-audit",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("user_prompt=user-test:", stdout)
        self.assertIn("execution_prompt=unit-test:", stdout)
        self.assertNotIn(" prompt=unit-test:", stdout)

        exit_code, stdout, stderr = self.run_cli(
            "attempts",
            "table",
            "--project-root",
            str(self.root),
            "--workset",
            "attempt-audit",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)
        self.assertEqual(table["columns"][0], "task_ref")
        self.assertNotIn("workset_id", table["columns"])
        self.assertIn("model", table["columns"])
        self.assertIn("reasoning_effort", table["columns"])
        self.assertIn("prompt_source", table["columns"])
        self.assertIn("user_prompt_source", table["columns"])
        self.assertIn("execution_prompt_hash", table["columns"])
        self.assertIn("skill_path", table["columns"])
        self.assertIn("skill_hash", table["columns"])
        self.assertIn("skill_source", table["columns"])
        self.assertIn("commit", table["columns"])
        self.assertIn("failure_class", table["columns"])
        self.assertIn("summary", table["columns"])
        self.assertEqual(len(table["rows"]), 2)
        self.assertEqual(table["workset_scope"], "attempt-audit")
        self.assertTrue(table["rows"][0]["task_ref"].startswith("attempt-audit/"))
        self.assertIsNone(table["rows"][0]["prompt_source"])
        self.assertIsNone(table["rows"][0]["prompt_hash"])
        self.assertEqual(table["rows"][0]["user_prompt_source"], "user-test")
        self.assertEqual(table["rows"][0]["user_prompt_hash"], table["rows"][0]["execution_prompt_hash"])
        self.assertIsNone(table["rows"][0]["skill_path"])
        self.assertIsNone(table["rows"][0]["skill_hash"])
        self.assertIsNone(table["rows"][0]["skill_source"])
        self.assertIn(table["rows"][0]["validation_summary"], {"passed=1 failed=0 skipped=0", "passed=0 failed=1 skipped=0"})
        self.assertEqual(
            {row["landed_commit"] for row in table["rows"]},
            {"def456", None},
        )

        exit_code, stdout, stderr = self.run_cli(
            "attempts",
            "table",
            "--project-root",
            str(self.root),
            "--workset",
            "attempt-audit",
            "--include-legacy-worksets",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        legacy_table = json.loads(stdout)
        self.assertEqual(legacy_table["columns"][0], "workset_id")
        self.assertEqual(legacy_table["rows"][0]["workset_id"], "attempt-audit")

    def test_worktree_land_rejects_invalid_validation_status(self) -> None:
        payload = {
            "id": "invalid-validation",
            "title": "Invalid validation",
            "tasks": [{"id": "IV-1", "title": "Reject invalid validation", "intent": "guard the CLI"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "invalid-validation",
            "--task",
            "IV-1",
            "--actor",
            "codex",
            "--prompt",
            "Attempt the invalid validation task.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        worktree_payload = json.loads(stdout)["worktree"]
        worktree_path = Path(worktree_payload["worktree_path"])
        (worktree_path / "invalid.txt").write_text("invalid\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "invalid-validation",
            "--task",
            "IV-1",
            "--actor",
            "codex",
            "--summary",
            "attempt the invalid validation closure",
            "--validation",
            "unit=unknown",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("validation status must be one of", stderr)
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", worktree_payload["branch"]],
            check=True,
            capture_output=True,
            text=True,
        )
