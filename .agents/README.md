# AI configuration

Provider-neutral AI configuration lives here, so this repo works the same
whichever agent you point at it.

## Layout

- `skills/` — repo-owned workflows, one directory each, `SKILL.md` inside.

## Where the guidance lives

- `AGENTS.md` at the root is the tool-neutral source of truth. Every agent
  reads it, either directly or through a one-line adapter file.
- `CLAUDE.md` is that adapter for Claude Code: it imports `AGENTS.md` and adds
  nothing but Claude-specific notes.
- `PREFS.md` is the user's own taste, is gitignored, and overrides `AGENTS.md`
  on conflict. `PREFS.example.md` is its tracked template.

## The mirror rule

`.agents/skills/` and `.claude/skills/` hold **byte-identical** copies of the
same skills. Claude Code only loads `.claude/skills/`, and an agent that is
not Claude Code has no reason to look there, so both have to exist.

Edit either one, then sync the other and check it:

```bash
rsync -a --delete .claude/skills/ .agents/skills/
python3 scripts/selftest.py          # fails loudly when the two drift apart
```

The check is part of the self-test rather than a note in a document, because
a mirror maintained by good intentions stops being a mirror.

## Rules

- Shared config owns intent; adapters own syntax.
- Never commit credentials. The Deezer `arl` is a password: it lives in
  `state/session.json`, mode 600, git-ignored, and belongs in no other file.
- A skill that only makes sense for one provider belongs in that provider's
  directory alone, and the mirror check should not be extended to cover it.
