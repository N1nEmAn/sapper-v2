# OpenCode engine (Fase 2)

`SAPPER_ENGINE=opencode` selects the independent `OpenCodeEngine`. Claude
remains the default engine. This document describes the OpenCode adapter only.

## Invocation

```
opencode run --format json [--model M] [--agent A] [--session S] \
             [--archived] [--standalone] <prompt>
```

The prompt is a **positional argument** (the CLI documents `message...`), not
stdin. No `--auto` and no `--dangerously-skip-permissions` are ever emitted.
`--skip-permissions` is ignored with a warning; `permission.asked` without a
matching `permission.replied` fails fast.

## Configuration

### Engine selection

`SAPPER_ENGINE` selects the engine: `claude` (default) or `opencode`
(alternative). The variables below are OpenCode-specific.

### OpenCode variables

| Variable | Meaning |
|---|---|
| `SAPPER_OPENCODE_BIN` | command prefix, default `opencode` |
| `SAPPER_OPENCODE_MODEL` | model string `provider/model#variant`; no automatic mapping from `SAPPER_MODEL` |
| `SAPPER_OPENCODE_AGENT` | optional `--agent` |
| `SAPPER_OPENCODE_SESSION` | optional `--session`; `--continue` is never used |
| `SAPPER_OPENCODE_ARCHIVED` | truthy → `--archived` |
| `SAPPER_OPENCODE_STANDALONE` | truthy → `--standalone` |

Each engine owns its model configuration: `sonnet`, `glm-5.3-flash[1m]`, etc.
are **not** translated.

## Event normalisation

`--format json` emits NDJSON events, not a final-answer envelope. The adapter:

- captures the main `sessionID` and ignores events from child sessions;
- collects text from `type:"text"` events using `part.text`;
- takes final text from `session.text.ended` (`data.text`), falling back to collected text events and concatenated `session.text.delta`;
- joins assistant messages with `\n`;
- ignores reasoning, tool and idle events;
- fails on `type:"error"`, `session.execution.failed`, `session.step.failed`,
  `session.execution.interrupted`, pending `permission.asked`, non-zero exit,
  malformed JSON, non-object events and timeouts;
- returns `{"result": <text>, "sessionID": <id>}` so existing Sapper handlers
  and `extract_json_object()` keep working.

`append_system` is prepended as delimited text because `opencode run` has no
system-prompt flag; this is not a true system-level prompt.

## Testing

All tests use synthetic NDJSON fixtures and an injected `Popen`/clock. No
provider, target host or network is contacted.
