# AGENT.md — Development Guide Template

> Place this file at the managed project root (or point `guide_file` in config.yaml at another file).
> The Agent reads it before every task — these are the top rules for working in this repo.

## Project

<!-- Optional: fill in to save the Agent time and tokens; it can also explore the repo itself. -->

- Stack:
- Run:
- Test:

## Code Style

- Follow the repo's existing style; never introduce a new formatting style.
- Add a dependency only when clearly necessary.
- Comments/docstrings stay brief and explain "why", not "what".

## Workflow

1. **Read before write**: study the relevant modules first; confirm you understand the current implementation.
2. **Small steps**: do only what the task asks; no drive-by refactoring of unrelated code.
3. **Always verify**:
   - Bug fix: reproduce with a failing test first, then make it pass.
   - New feature: add basic tests. If automated verification is impossible, state the manual steps in notes.
4. **Commits**: follow Conventional Commits (feat: / fix: / docs: / refactor: / test: / chore:); the body states motivation and scope of impact.

## Never Do

- Do not modify or delete anything under the tasks directory (`tasks/`).
- Do not touch CI/CD configs, dependency lock files, secrets or env files; return blocked if the task needs them.
- No major dependency upgrades or framework swaps beyond the task's scope.
- Never hardcode secrets, passwords or internal addresses in code.

## Return blocked When

- The task description is ambiguous and the intent cannot be inferred from the code.
- A human decision is required (product behavior, UI trade-offs, database schema changes).
- Verification fails after two attempts.
- External systems are needed (database credentials, third-party consoles) but not configured in the project.
