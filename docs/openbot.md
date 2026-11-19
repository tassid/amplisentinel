# Using Sentinel from OpenBot

[OpenBot](https://open.bot) (CopilotKit, MIT) is a local, open source agent workspace.
Sentinel plugs into it as one MCP server per research project, and one Bot per project.

```
OpenBot (desktop)
 ├─ Bot "Sentinel · Cervical cancer" ── http://127.0.0.1:8701/mcp  (workspace cervical-cancer)
 ├─ Bot "Sentinel · Rice"            ── http://127.0.0.1:8702/mcp  (workspace rice)
 └─ Bot "Sentinel · Coral"           ── http://127.0.0.1:8703/mcp  (workspace coral)
```

## 1. One server per project

Each server is locked to its workspace when it starts. No tool takes a path, so the
cervical Bot cannot read the rice project, even if asked.

```bash
sentinel -w ~/pesquisa/cervical-cancer mcp --http --port 8701
sentinel -w ~/pesquisa/rice            mcp --http --port 8702
sentinel -w ~/pesquisa/coral           mcp --http --port 8703
```

The server listens on 127.0.0.1 only. Use `mcp` without `--http` for clients that start
servers over stdio.

Tools (all read only, all return the source file and its sha256):

| Tool | Returns |
|---|---|
| `workspace` | name, title, configured runs |
| `status` | state of every run, counts by state |
| `run_detail(run)` | state and full ledger history of one run |
| `run_metrics(run)` | recorded metrics and the researcher's rules file |
| `inventory(study?)` | accessions, ENA layout, instruments, reads, GB per study |
| `ledger_tail(n)` | last n ledger entries |
| `ledger_verify` | hash chain check |
| `errors(run?)` | error events, literally |

State changes (`move`) and decisions (`decide`) are not exposed. They stay in the CLI,
typed by the researcher.

## 2. Register the server in OpenBot

In OpenBot, MCP servers are configured at `/admin/plugins`, where their tools are also
granted to Bots. Add one entry per project with its URL, for example
`http://127.0.0.1:8701/mcp` for cervical cancer.

OpenBot treats tools from custom servers as writes, so it may ask for approval on each
call. The Sentinel tools are marked `readOnlyHint: true`; if OpenBot still asks, the
approval settings per Bot are at `/bots/:id/approvals`.

## 3. One Bot per project

Create the Bot at `/bots` and grant it only the tools of its own server.

**Name:** `Sentinel · Cervical cancer`

**Role description** (paste as is, changing the project name):

```
You are the Sentinel interface for ONE research project: cervical-cancer.
Report only data returned by the Sentinel tools: values, states, error messages,
file paths and hashes. Cite the source path for every number. Repeat error
messages literally. Compare values only against the rules returned in
`rules_in_use`; if there is no rule for a value, report the value alone.
Never suggest parameters. Never say a result is good, bad, expected or
suspicious. Never recommend a next step. Never write a decision or a
justification. For any other project, say it is outside this workspace.
```

The role description restates the rule; the guarantee comes from the tools, which
return data only.

## Notes

1. The OpenBot configuration steps above follow its README at the time of writing.
   Check `docs/configuration.md` in the OpenBot repository if a screen has moved.
2. To start every server at login on macOS, a `launchd` agent per project works; a
   template will be added later.
