# Optional bounded context recall

`skills/embedded-harness/context_recall.py` supplies bounded source-linked
historical context when an adopting host calls it. It uses the source reader
for a disposable catalog and exact source access; it does not call a model,
browse, change tool input, grant authority or write durable memory in a hook.

## Configuration

Stage `codex-local-source-recall` with
`integrations/codex-local/build-deployment-bundle.py`. Adapt
`context_recall.local.example.json` to an authorized private memory collection,
explicit session roots and a private derived-index path. Keep the resulting
`context_recall.local.json`, catalogs and host database outside Git. The
optional `state_db` is a host-owned discovery aid, not a semantic truth store.
Its absence does not authorize scanning unrelated directories.

```powershell
python -X utf8 -B skills/embedded-harness/context_recall.py --config <PRIVATE_CONFIG> index
python -X utf8 -B skills/embedded-harness/context_recall.py --config <PRIVATE_CONFIG> search --query "specific question"
python -X utf8 -B skills/embedded-harness/context_recall.py --config <PRIVATE_CONFIG> read --id <RETURNED_ID>
```

Exact reads validate the complete selected source hash. Appending a transcript
does not invalidate an earlier record. Long reads provide an explicit next
offset. Original records remain canonical; summaries only locate them.

## Host and evidence boundaries

The hook command can handle `UserPromptSubmit` and `SessionStart` when the host
actually exposes them, including its compact/resume source fields. Register
callbacks only in the adopting host, set a finite timeout and verify returned
additional context with fresh host observations before reporting activation.
This repository contains no maintainer hook file or runtime invocation receipt.

Recall projects retained public messages, excluding tool output, startup
instructions, private reasoning and opaque checkpoint state. Retained windows
after compaction help suppress duplicate historical context; recall does not
decode or replace the host's opaque state. Recognizable ERR/CE/IE/INC material
is exposed as diagnostic navigation rather than an automatic failure recipe.
On-demand reads can still retrieve it for authorized diagnosis.

Output defaults to three candidate cards and 2,200 characters. History reads
are bounded to the current/selected transcript and at most two explicitly
linked histories, with a 32 MiB tail limit per large history. Paths are scoped
after resolving junctions and Windows extended-path spellings. Missing matches
do not prove that a fact was never discussed; the model judges relevance,
currentness and conflicts. Lexical discovery is not universal semantic recall.

Repository regressions cover visible-message projection, source hashes,
changed-source recall, duplicate suppression, compaction retained windows,
incident filtering and scoped path handling. They do not establish activation
in every host, independent semantic quality, complete evidence coverage or
lower end-to-end model cost.
