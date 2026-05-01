## What

<!-- One sentence — what changes when this PR merges? -->

## Why

<!-- Link the issue (`Closes #N`) or describe the motivation. -->

## How

<!-- High-level approach. Anything subtle a reviewer should notice. -->

## Risk & blast radius

- [ ] No chassis-mutating code changed
- [ ] Chassis-mutating code changed; **backup-first envelope is honoured**
- [ ] Touches the audit log path (#21)
- [ ] Touches authentication / credentials (#22)
- [ ] Adds a new external dependency (justification below)

## Test plan

- [ ] `pytest tests/` passes locally
- [ ] `ruff check src/ tests/` clean
- [ ] `mypy src/hmm_client` clean (or no new errors)
- [ ] Manual exercise on (describe target / hardware / env)

## Checklist

- [ ] Conventional commit message (`feat:`, `fix:`, `chore:`, etc.)
- [ ] Updated `docs/roadmap.md` if scope/phase changes
- [ ] Updated `SECURITY.md` if attack surface changes
- [ ] No credentials / pcaps / customer data committed
