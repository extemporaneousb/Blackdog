from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from blackdog.store_migration import migrate_store
from blackdog_core.state import StoreError, JsonRuntimeStore, load_events
from blackdog_core.tasks import create_task
from blackdog_cli.main import main
from tests.core_audit_support import CoreAuditTestCase


class StoreMigrationTests(CoreAuditTestCase):
    def setUp(self):
        super().setUp()
        self.write_profile()
        self.profile = self.load_test_profile()
        self.control = self.profile.paths.control_dir
        self.control.mkdir(parents=True, exist_ok=True)
        self.runtime = {
            'schema_version': 3, 'store_version': 'blackdog.runtime/vnext3',
            'worksets': [{
                'id': 'group-1', 'workset_claim': None, 'task_claims': [],
                'task_states': [{'task_id': 'old-task', 'status': 'done', 'updated_at': '2026-01-02T00:00:00Z'}],
                'attempts': [{
                    'attempt_id': 'old-attempt', 'task_id': 'old-task', 'actor': 'test',
                    'status': 'success', 'started_at': '2026-01-01T00:00:00Z', 'ended_at': '2026-01-02T00:00:00Z',
                    'summary': 'Synthetic completed change', 'changed_paths': ['example.py'],
                    'codex_session': {'thread_id': 'synthetic-thread', 'capture': {'status': 'captured', 'method': 'exact_prompt_hash', 'missing_reason': None}},
                }],
            }],
        }
        self.planning = {'schema_version': 1, 'store_version': 'blackdog.planning/vnext1', 'worksets': [{'id': 'group-1', 'tasks': [{'id': 'old-task', 'title': 'Synthetic task'}]}]}
        self.write_legacy()
        (self.control / 'events.jsonl').write_text('{"historical": "unchanged"}\n')

    def write_legacy(self):
        (self.control / 'runtime.json').write_text(json.dumps(self.runtime))
        (self.control / 'planning.json').write_text(json.dumps(self.planning))

    def fingerprint(self):
        return {str(p.relative_to(self.control)): p.read_bytes() for p in self.control.rglob('*') if p.is_file()}

    def preview(self):
        return migrate_store(self.root)

    def apply(self, preview):
        return migrate_store(self.root, apply=True, expected_digest=preview['source_digest'])

    def test_preview_apply_replay_and_new_task_preserve_history(self):
        before = self.fingerprint()
        preview = self.preview()
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(preview['task_count'], 1)
        result = self.apply(preview)
        archive = Path(result['archive_path'])
        for name, data in before.items():
            self.assertEqual((archive / name).read_bytes(), data)
        state = JsonRuntimeStore().load(self.control / 'runtime.json')
        self.assertEqual(len(state.tasks), 1)
        self.assertEqual(state.tasks[0].attempts[0].changed_paths, ('example.py',))
        self.assertEqual(state.tasks[0].attempts[0].codex_session.capture_method, 'exact_prompt_hash')
        self.assertFalse((self.control / 'planning.json').exists())
        self.assertEqual(len(load_events(self.control / 'events.jsonl')), 1)
        migrated = self.fingerprint()
        self.assertEqual(self.apply(preview)['status'], 'current')
        self.assertEqual(self.fingerprint(), migrated)
        task = create_task(self.profile, title='Next task')
        self.assertNotEqual(task.task_id, state.tasks[0].task_id)
        self.assertEqual(len(JsonRuntimeStore().load(self.control / 'runtime.json').tasks), 2)

    def test_stale_digest_never_changes_source(self):
        preview = self.preview()
        self.planning['worksets'][0]['tasks'][0]['title'] = 'Changed title'
        self.write_legacy()
        before = self.fingerprint()
        with self.assertRaisesRegex(StoreError, 'expected-digest'):
            self.apply(preview)
        after = self.fingerprint()
        self.assertEqual({k:v for k,v in after.items() if not k.endswith('.lock')}, before)

    def test_claims_active_state_or_retained_workspace_block_without_writes(self):
        for field, value in [('workset_claim', {'actor': 'owner'}), ('task_claims', [{'actor': 'owner'}])]:
            with self.subTest(field=field):
                old = self.runtime['worksets'][0][field]
                self.runtime['worksets'][0][field] = value
                self.write_legacy()
                before = self.fingerprint()
                with self.assertRaises(StoreError): self.preview()
                self.assertEqual(before, self.fingerprint())
                self.runtime['worksets'][0][field] = old
        self.runtime['worksets'][0]['task_states'][0]['status'] = 'in_progress'
        self.write_legacy()
        with self.assertRaisesRegex(StoreError, 'terminal'): self.preview()
        self.runtime['worksets'][0]['task_states'][0]['status'] = 'done'
        self.runtime['worksets'][0]['attempts'][0]['worktree_path'] = str(self.root)
        self.write_legacy()
        with self.assertRaisesRegex(StoreError, 'retained'): self.preview()

    def test_missing_prompt_or_corrupt_hash_blocks(self):
        receipt = {'mode': 'skill', 'prompt_hash': hashlib.sha256(b'synthetic prompt').hexdigest(), 'recorded_at': '2026-01-01T00:00:00Z'}
        receipt['replay_artifact_path'] = f"prompts/sha256/{receipt['prompt_hash']}.txt"
        self.runtime['worksets'][0]['attempts'][0]['prompt_receipt'] = receipt
        self.write_legacy()
        with self.assertRaises(StoreError): self.preview()
        artifact = self.control / receipt['replay_artifact_path']
        artifact.parent.mkdir(parents=True)
        artifact.write_text('wrong content')
        artifact.chmod(0o600)
        with self.assertRaisesRegex(StoreError, 'hash'): self.preview()
        artifact.write_text('synthetic prompt')
        self.assertEqual(self.preview()['task_count'], 1)

    def test_interruption_after_each_publication_step_resumes(self):
        from blackdog_core.state import atomic_write_text
        for fail_name in ['events.jsonl', 'runtime.json']:
            with self.subTest(fail_name=fail_name):
                preview = self.preview()
                def fail(path, text):
                    atomic_write_text(path, text)
                    if path == self.control / fail_name:
                        raise OSError('simulated process failure')
                with patch('blackdog.store_migration.atomic_write_text', side_effect=fail):
                    with self.assertRaises(OSError): self.apply(preview)
                with self.assertRaisesRegex(StoreError, 'pending'):
                    JsonRuntimeStore().load(self.control / 'runtime.json')
                self.assertEqual(self.preview()['status'], 'pending')
                self.assertEqual(self.apply(preview)['status'], 'applied')
                self.write_legacy()
                (self.control / 'events.jsonl').write_text('{"historical": "unchanged"}\n')

    def test_corrupt_pending_target_fails_without_overwriting(self):
        from blackdog_core.state import atomic_write_text
        preview = self.preview()
        def fail(path, text):
            atomic_write_text(path, text)
            if path == self.control / 'migration-pending.json': raise OSError('simulated process failure')
        with patch('blackdog.store_migration.atomic_write_text', side_effect=fail):
            with self.assertRaises(OSError): self.apply(preview)
        (Path(preview['archive_path']) / 'target-events.jsonl').write_text('{}\n')
        before = self.fingerprint()
        with self.assertRaisesRegex(StoreError, 'digest'): self.apply(preview)
        self.assertEqual(before, self.fingerprint())

    def test_cli_emits_concrete_recovery_and_migration_actions(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['summary', '--project-root', str(self.root), '--json'])
        self.assertEqual(code, 1)
        action = json.loads(output.getvalue())['next_action']
        self.assertEqual(action['argv'][1:3], ['repo', 'migrate'])
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(action['argv'][1:])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['migration']['status'], 'preview')

    def test_repo_update_rejects_old_store_before_handler_mutation(self):
        from blackdog.repo_lifecycle import update_repo
        with patch('blackdog.repo_lifecycle.execute_repo_handlers') as handlers:
            with self.assertRaises(StoreError): update_repo(self.root)
            handlers.assert_not_called()

    def test_missing_planning_after_interruption_resumes(self):
        from blackdog.store_migration import _durable_unlink
        preview = self.preview()
        def fail(path):
            _durable_unlink(path)
            if path.name == 'planning.json': raise OSError('simulated failure')
        with patch('blackdog.store_migration._durable_unlink', side_effect=fail):
            with self.assertRaises(OSError): self.apply(preview)
        self.assertEqual(self.apply(preview)['status'], 'applied')

    def test_duplicate_task_or_unknown_schema_is_not_discarded(self):
        self.runtime['schema_version'] = 99
        self.write_legacy()
        with self.assertRaisesRegex(StoreError, 'schema 3'): self.preview()
        self.runtime['schema_version'] = 3
        self.planning['worksets'][0]['tasks'].append(dict(self.planning['worksets'][0]['tasks'][0]))
        self.write_legacy()
        with self.assertRaisesRegex(StoreError, 'Duplicate'): self.preview()

    def test_symlink_archive_is_rejected(self):
        preview = self.preview()
        external = self.root / 'external'
        external.mkdir()
        archive = Path(preview['archive_path'])
        archive.parent.mkdir()
        archive.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(StoreError, 'symbolic'): self.apply(preview)
        self.assertEqual(list(external.iterdir()), [])

    def test_linked_worktree_blocks(self):
        linked = self.root / 'linked'
        self.git_output('worktree', 'add', '-b', 'retained', str(linked))
        before = self.fingerprint()
        with self.assertRaisesRegex(StoreError, 'linked worktrees'): self.preview()
        self.assertEqual(before, self.fingerprint())
