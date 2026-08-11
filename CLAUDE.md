# debridlair

@AGENTS.md

This file exists so Claude Code finds the guidance by its own convention. The
content lives in `AGENTS.md`, which every other agent reads, so the two can
never drift apart. Add nothing here that is not Claude-specific.

Claude-specific notes:

- `.claude/settings.json` pins the model to Sonnet, so a session started in
  this directory opens on it. The file is tracked, which is why `.gitignore`
  names it beside the skills. Anything personal belongs in
  `.claude/settings.local.json`, which stays out of git.
