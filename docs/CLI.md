# CLI Reference

## `blackdog`

### Project setup

- `blackdog init --project-root PATH --project-name NAME`
- `blackdog validate`
- `blackdog render`

### Backlog management

- `blackdog add --title ... --bucket ... --why ... --evidence ... --safe-first-slice ...`
- `blackdog summary`
- `blackdog next`
- `blackdog claim --agent NAME`
- `blackdog release --id TASK --agent NAME`
- `blackdog complete --id TASK --agent NAME`
- `blackdog decide --id TASK --agent NAME --decision approved|denied|deferred|done`
- `blackdog comment --actor NAME --id TASK --body ...`
- `blackdog events`

### Structured results

- `blackdog result record --id TASK --actor NAME --status success|blocked|partial ...`

### Inbox

- `blackdog inbox send --sender NAME --recipient NAME --body ...`
- `blackdog inbox list`
- `blackdog inbox resolve --message-id ID --actor NAME`

## `blackdog-skill`

- `blackdog-skill new backlog --project-root PATH`

This command ensures the project has a Blackdog profile/artifact set and then generates a project-local skill under `.codex/skills/blackdog-backlog/`.

