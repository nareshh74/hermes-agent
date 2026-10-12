# Standalone Markdown KB recall plugin

`standalone-kb` is a standalone, opt-in Hermes general plugin. It adds bounded, read-only recall from an operator-approved Markdown vault through a separately refreshed QMD index. It does not replace Hermes memory, register a memory provider, patch core, or write to `MEMORY.md` or `USER.md`.

This directory is the complete plugin source: `plugin.yaml`, `__init__.py`, `kb_backend.py`, `kb_core.py`, `kb_multiscope.py`, `kb_process.py`, and `kb_scope.py`, plus this README. Keep those runtime files together. The manifest's plugin key is `naresh-kb`.

The plugin is not bundled and has no bundled autoload, migration, installer, model acquisition, or automatic setup. Copying it, enabling it, configuring it, refreshing its index, and starting a new session are separate operator actions.

## Data, privacy, and trust boundaries

Markdown is authoritative. The plugin opens notes read-only, validates canonical containment and exact collection names, rejects Windows trailing-dot/space aliases, ignores symlinks, reparse points, and hidden paths, and compares source bytes with the separately refreshed manifest. A quote is rendered from the same byte buffer that passed the hash check. An edited or deleted note is not recalled until refresh publishes a matching manifest.

The `pre_llm_call` hook returns an additive, bounded context dictionary once per admitted user turn. It never mutates callback history, system prompts, toolsets, previous recall, Hermes memory, or the vault. Evidence is serialized as untrusted JSON with `source`, `heading`, `scope`, `start_line`, `end_line`, and `quote`. This separates instructions from data but is not a security sandbox and does not prove that a hosted model will resist prompt injection.

Recalled excerpts are sent to the session's configured model provider. Hermes may persist the additive API content in session history, replay, and compression. QMD's local SQLite index contains note bodies. Local indexing therefore does not make hosted conversations private or trace-free.

In `layout: two_level`, audience admission runs before scope, corpus, index, or shared-settings access. CLI, TUI, and Desktop are admitted. WhatsApp and WhatsApp Cloud require the exact sender identifier's SHA-256 in `allowed_sender_hashes`; other messaging surfaces are denied. `bot_room` is denied. On the pinned host API, a bound RoomLink execution policy denies recall, while an ordinary owner API request is admitted; policy import/getter errors fail API recall closed without blocking normal chat. These checks do not provide OS isolation: profiles running as the same OS user may still have unrelated file or terminal capabilities.

Use `layout: two_level` on gateways and shared surfaces. The legacy single-level layout does not enforce sender/platform admission and is suitable only for owner-local CLI use.

The plugin never captures conversations or mirrors memory. Status records contain bounded health, timestamps, latency/freshness, counts, and redacted error categories, never query text or note excerpts.

## Runtime boundary

The load guard intentionally requires the installed first-party Hermes Python 3.14 non-Android runtime and stable `psutil>=6,<8` and `ruamel.yaml>=0.18,<0.20`. Missing, malformed, prerelease, local-version, out-of-range, pre-3.14, or Android runtimes fail plugin import visibly before plugin modules load. These packages are mandatory Hermes runtime dependencies at this boundary; the manifest deliberately does not duplicate them as plugin workspace dependencies. Do not repair a failed guard with ad-hoc `pip` installation or wider ranges.

The verified backend boundary is `@tobilu/qmd` 2.8.3 launched by Bun 1.4.2 with explicit argv such as `["C:/ABSOLUTE/PATH/TO/bun.exe", "C:/ABSOLUTE/PATH/TO/qmd/dist/cli/qmd.js"]`. The reviewed fixture installed QMD with `npm --ignore-scripts`; Bun supplied SQLite without package lifecycle scripts or QMD model acquisition. Node-only execution was not verified. The plugin uses the QMD CLI only and installs no global MCP or HTTP service. Lookup never downloads packages or models, invokes package lifecycle scripts, runs QMD update/embed, or scans the vault.

A prior 180-note synthetic QMD calibration measured semantic p50 5259.12 ms and p95 5407.91 ms on its benchmark CPU. That is resource-planning evidence, not a latency SLA or production-corpus calibration. At thresholds 0.23 through 0.35 the synthetic sweep retained Recall@3 of 1.0 with zero negative injections, but a consented held-out corpus must be measured before changing defaults.

The reviewed QMD dependency tree had zero critical advisories after a narrow `simple-git` 4.0.2/parser 2.0.1 override and four high inherited `braces`-chain availability advisories with no patched version. Fixed generated patterns plus rejection of user braces/brackets reduce the reachable input surface; they are mitigation, not an advisory waiver or independent security sign-off.

## Two-level scope model

`layout: two_level` exposes exactly two scopes:

1. Shared: `<vault_root>/shared`, with owned state at the root Hermes home's `knowledge-base/shared`.
2. Current profile: `<vault_root>/profiles/<profile_id>`, with owned state at the active profile's `knowledge-base/state` (or `knowledge-base/profile` for `default`).

The active profile home must match `profile_id`; a profile cannot select another profile's private scope. `shared_writer_profile` is the only profile allowed to refresh shared state. A non-writer refresh updates only its current private scope. Readers validate the writer-owned shared settings record and configured shared command hash before using shared evidence. Shared and current-profile retrieval run independently, so one failed scope can abstain while the other remains available.

The plugin owns only its derived state roots and `.kb-owner.json` markers. It refuses unrelated, overlapping, reparse-point, or unowned state. `**/private/**` and `**/raw/**` are always excluded. Additional exclusions apply only to the current private scope and cannot rewrite shared settings.

## Portable configuration example

The following is a complete synthetic `alpha` profile example. Every `C:/ABSOLUTE/...` value and every all-zero provenance value is a placeholder that must be replaced with an operator-approved local absolute path and the actual file SHA-256/byte count. The empty sender list intentionally limits this sample to local surfaces and ordinary owner API use.

```yaml
plugins:
  enabled:
    - naresh-kb
  entries:
    naresh-kb:
      settings:
        kb:
          enabled: true
          layout: two_level
          profile_id: alpha
          vault_root: C:/ABSOLUTE/PATH/TO/synthetic-kb-vault
          shared_writer_profile: default
          qmd_command:
            - C:/ABSOLUTE/PATH/TO/bun.exe
            - C:/ABSOLUTE/PATH/TO/qmd-package/node_modules/@tobilu/qmd/dist/cli/qmd.js
          shared_index:
            qmd_command:
              - C:/ABSOLUTE/PATH/TO/bun.exe
              - C:/ABSOLUTE/PATH/TO/qmd-package/node_modules/@tobilu/qmd/dist/cli/qmd.js
            models:
              embed: C:/ABSOLUTE/PATH/TO/embedding-model.gguf
            model_provenance:
              embed:
                sha256: "0000000000000000000000000000000000000000000000000000000000000000"
                bytes: 123456789
            max_note_bytes: 1048576
          mode: hybrid
          rerank_enabled: false
          models:
            embed: C:/ABSOLUTE/PATH/TO/embedding-model.gguf
          model_provenance:
            embed:
              sha256: "0000000000000000000000000000000000000000000000000000000000000000"
              bytes: 123456789
          top_k: 3
          max_context_chars: 6000
          max_note_bytes: 1048576
          timeout_seconds: 10
          refresh_interval_seconds: 60
          min_score: 0.23
          lexical_min_terms: 2
          exclusions: []
          allowed_sender_hashes: []
```

Configuration contract:

- `enabled`: false unless explicitly true.
- `layout`: use `two_level` for the shared/current-profile model above.
- `profile_id`: exact active profile name (`default` for the root profile).
- `vault_root`: existing, approved, absolute Markdown root containing `shared/` and `profiles/<profile_id>/`.
- `shared_writer_profile`: profile that alone publishes the shared index/settings; defaults to `default`.
- `qmd_command`: trusted private-scope argv, represented as a YAML list or JSON argv, never a shell string.
- `shared_index`: canonical shared `qmd_command`, `models`, `model_provenance`, and `max_note_bytes`. The owned schema-2 record stores the command hash and an integrity hash, never executable argv.
- `mode`: `lexical`, `semantic`, or `hybrid`. Exact uppercase IDs force quoted lexical routing. QMD 2.8.3 strips hyphens in quoted search terms, so the adapter searches adjacent identifier components and then verifies the unchanged identifier in canonical source; it never equates `HX-731` with `HX 731` in evidence.
- `models`: absolute approved GGUF paths. Semantic/hybrid with reranking disabled requires `embed`; enabling reranking also requires `rerank`. The typed `vec:`/`lex:` route does not require `generate`.
- `model_provenance`: pinned lowercase SHA-256 plus positive byte count. Two-level embedding provenance is mandatory and verified on provisioning/refresh, not on each lookup.
- `top_k`: 1..10, default 3, shared across both scopes.
- `max_context_chars`: 256..9000, default 6000.
- `max_note_bytes`: 1..4194304, default 1048576.
- `timeout_seconds`: 0.1..30, default 10.
- `refresh_interval_seconds`: 1..86400, default 60.
- `min_score`: 0..1, default 0.23 for raw semantic/hybrid vector relevance. BM25 lexical output does not use it.
- `lexical_min_terms`: 1..10, default 2. Every selected non-ID lexical term must occur in canonical text, so weak lexical matches abstain.
- `exclusions`: at most 64 bounded relative globs. Braces, brackets, backslashes, traversal, NUL, and colons are rejected; mandatory defaults remain in force.
- `allowed_sender_hashes`: optional exact SHA-256 digests for WhatsApp sender IDs. Do not publish real allowlist identities or hashes in examples.

## Local installation, refresh, and activation

1. Copy the eight files from `standalone-kb/` into the selected profile's local plugin directory as `<ALPHA_HERMES_HOME>/plugins/naresh-kb/`. Do not move them into the repository's bundled `plugins/` tree.
2. Store the `kb` mapping above with the supported config surface. For example, serialize only that mapping as JSON and run:

       hermes -p alpha config set --force plugins.entries.naresh-kb.settings.kb '<SYNTHETIC_KB_JSON_OBJECT>'

3. Enable the copied plugin through the supported plugin command:

       hermes -p alpha plugins enable naresh-kb

4. Repeat steps 1–3 for the shared writer, using its exact `profile_id` and the same `shared_index` values. If `default` is the writer, configure `profile_id: default` there before refreshing. Then provision shared plus writer-private state, followed by alpha-private state:

       hermes -p default kb refresh
       hermes -p alpha kb refresh
       hermes -p alpha kb status

5. Start a fresh alpha chat/session to activate the newly enabled hook. Enabling/configuring does not mutate the cached tool or prompt surface of an existing session. A gateway restart is not inherently required, but already-running sessions do not gain the hook retroactively.

`hermes -p alpha kb watch` is an optional, separate foreground maintenance process. Chat never starts it automatically. It polls at half the configured interval and bounds QMD work to the other half. Add/edit/rename/delete visibility depends on scan, embedding, CPU, and vault size; stale or failed indexes report bounded health instead of false success.

Refresh writes only plugin-owned index/config/cache/manifest/status files. It never writes the Markdown vault or Hermes memory. Changed content runs QMD update and, for a validated non-lexical configuration, incremental embed. Pure rename/delete skips embedding; unchanged refresh skips QMD update. Model or index-setting changes require provenance verification before publication.

## Rollback

1. Stop any separate `hermes kb watch` process.
2. Disable only this plugin:

       hermes -p alpha plugins disable naresh-kb

3. Start a fresh session so the disabled hook is absent.
4. If desired, remove only the copied local plugin directory and this plugin's owned index/cache state after preserving any evidence needed for diagnosis.

Do not edit or restore `MEMORY.md`, `USER.md`, memory-provider settings, model-provider settings, unrelated profile configuration, or the Markdown vault as part of activation or rollback.

## Tests

Use the repository's canonical runner. Pure routing, admission, isolation, and health contracts do not require QMD. Optional real-backend tests use only an explicitly supplied synthetic fixture root:

```bash
export QMD_TEST_ROOT=C:/ABSOLUTE/PATH/TO/preprovisioned-synthetic-fixture
export HERMES_PYTHON=C:/ABSOLUTE/PATH/TO/test-python.exe
unset __HERMES_ACTIVATED
export HERMES_TEST_FILE_RETRIES=0
bash scripts/run_tests.sh \
  tests/plugins/test_kb_standalone.py \
  tests/plugins/test_kb_review_regressions.py \
  tests/plugins/test_kb_two_level.py \
  tests/plugins/test_kb_final_review_regressions.py \
  tests/plugins/test_kb_room_gate.py \
  tests/plugins/test_kb_qmd_negation_health.py \
  tests/plugins/test_kb_user_surface.py \
  tests/plugins/test_kb_benchmark_contract.py -v --tb=short -- \
  -p tests.plugins.kb_test_support --qmd-test-root "$QMD_TEST_ROOT"
```

The `-p` bridge carries the explicit `QMD_TEST_ROOT` value through the canonical runner's hermetic child environment. If neither the environment value nor that explicit bridge is supplied, optional real-QMD tests skip with an explicit reason. If a root is supplied, missing QMD, Bun on `PATH`, `store.js`, `models-verified.json`, or `semantic-settings.json` required by a selected test is a failure. Tests never download assets. `semantic-settings.json` is accepted only from that explicit synthetic fixture root, never from production configuration.

The optional benchmark requires a caller-owned output directory and the same explicit fixture root:

    python -m tests.plugins.kb_final_benchmark --output C:/ABSOLUTE/PATH/TO/benchmark-output

It generates only synthetic notes under `--output`, stores real rows for offline threshold sweeps, and is not a production-vault acceptance test.
