# ta-helper — implementation plan

A small local daemon presenting an **OpenAI Chat Completions** endpoint on
`127.0.0.1`, satisfied by whichever subscription the user has. OpenAI/Codex on
day one; Anthropic and others addable later without restructuring. The
TradingAgents pipeline points at it via `backend_url`; no upstream core edits.

> **Revision 3** — incorporates the Codex review (22 findings, 9 BLOCKER).
> Dispositions in Appendix A. Milestones re-derived in §10: **M0–M2 grew
> 2.5 d → 5.0 d.** Two review claims were themselves wrong and are corrected
> below with evidence (§0e, §0f).

---

## 0. Established facts

### 0.1 Wire contract (validated — do not re-probe)

```
POST https://chatgpt.com/backend-api/codex/responses
Authorization: Bearer <access_token>
chatgpt-account-id: <from id_token claim>
originator: codex_cli_rs
session_id: <uuid4 — per request, see §2.7>
Accept: text/event-stream

{ "model": "gpt-5.6-sol",     // ONLY this; gpt-5 / gpt-5-codex /
  "store": false,             //   codex-mini-latest → 400
  "stream": true,             // both mandatory
  "instructions": "<system>", "input": [<typed items>],
  "tools": [{type:"function", name, description, parameters}],
  "text": {"format": {type:"json_schema", name, strict, schema}},
  "reasoning": {"effort": "low|medium|high"},
  "include": ["reasoning.encrypted_content"] }
```

SSE events seen: `response.created`, `response.in_progress`,
`response.output_item.added`, `response.output_text.delta`,
`response.function_call_arguments.delta`, `.done`,
`response.output_item.done`, `response.completed` (carries `usage`).

`id_token` claim `https://api.openai.com/auth` → `chatgpt_account_id`,
`chatgpt_plan_type`, `chatgpt_subscription_active_until`.

OAuth: issuer `https://auth.openai.com`, client_id
`app_EMoamEEZ73f0CkXaXp7hrann`, PKCE; Codex CLI redirect
`http://localhost:1455/auth/callback` (other ports UNKNOWN — U1).

### 0.2 Verified repo facts (each checked, not assumed)

**(a) No unknown-model warning.** `openai_compatible` is in
`_ANY_MODEL_PROVIDERS` (`llm_clients/validators.py`), so
`validate_model("openai_compatible","gpt-5.6-sol")` → `True`. Nothing to handle.

**(b) Structured output arrives as a TOOL, not `response_format`.**
`get_capabilities("gpt-5.6-sol").preferred_structured_method ==
"function_calling"`, and `LocalCompatibleChatOpenAI.with_structured_output()`
sets `tool_choice=None`. **Tool translation is the entire hot path.**

**(c) FOUR schema-bound agents, not three** (review #3, confirmed):
`trader.py` (`TraderProposal`), `research_manager.py` (`ResearchPlan`),
`portfolio_manager.py` (`PortfolioDecision`), `sentiment_analyst.py`
(sentiment schema). All four need fixtures and contract tests.

**(d) `preferred_structured_method` as a quirk cannot change agent behaviour** —
langchain decides client-side before the helper sees anything. The quirk governs
only how an inbound `response_format` is translated.

**(e) CORRECTION to review #4's evidence — Codex was RIGHT, the counter-claim
was wrong.** I verified empirically. Binding `PortfolioDecision` on a
`LocalCompatibleChatOpenAI` yields:

```
bound kwargs: ['ls_structured_output_format', 'parallel_tool_calls', 'tools']
  tool_choice         = <absent from kwargs>
  parallel_tool_calls = False        ← IS sent
  strict              = None
  tool[0].strict      = <absent>
```

`with_structured_output(method="function_calling")` sets
`"parallel_tool_calls": False` in `bind_kwargs` (langchain_openai
`base.py:2402`), and our subclass only overrides `tool_choice`, so it survives.
`base.py:2157` (`if parallel_tool_calls is not None`) governs the *explicit
argument* path, not this one. **So `parallel_tool_calls: false` is on the wire
for all four schema-bound agents** and the adapter must handle it.

**(f) CORRECTION to my own earlier draft — do not hardcode `strict`.** Empirical
evidence above: langchain sends **no** `strict` field, and `tool_choice` is
absent entirely (not `null`). Previous drafts said "always `strict:false`" and
"assume `tool_choice:auto`". Both would add fields the caller never sent.
**Omit what is absent** (review #5, accepted).

**(g) Review #6 confirmed, and one of its two proposed remedies is
unavailable.** `_convert_message_to_dict` in langchain_openai builds the
outbound message from a fixed allowlist — `content`, `name`, `role`,
`tool_calls` (further filtered to `{id, type, function}`), `function_call`,
`audio`. There is **no arbitrary `additional_kwargs` passthrough**. So a
"langchain-preserved extension field" does not exist. Only helper-side
conversation state is viable (§2.8).

**(h) `thread_id(ticker, date, signature)` has no user_id** — collides across
users, and the API deliberately allows same-ticker runs from different users
(review #15 confirmed).

**(i) The API path streams `stream_mode: "values"`** (`propagation.py:78-83`),
so per-node metadata is NOT in the stream — `last_completed_node` cannot be
derived from it (review #19 confirmed).

**(j) `RunStatus` has no `interrupted`** (`schemas.py:20`) (review #18
confirmed).

**(k) `create_run(config=request.model_dump())` writes `config_json`** — an
`api_key` on `RunRequest` would land in SQLite in plaintext (review #1
confirmed).

**(l) One kwargs dict feeds both LLM clients** (`trading_graph.py:95-112`), so
config cannot express per-tier reasoning effort (review #2 confirmed).

**(m) Route-path provider selection works.** Verified on the installed `openai`
2.33.0: `base_url="http://127.0.0.1:8899/v1/codex"` and `".../v1/codex/"` both
produce `.../v1/codex/chat/completions`. The SDK appends rather than
`urljoin`-resolves.

**(n) Exactly one agent hand-builds a system message** (`trader.py`); the other
prompt-bearing agents use `ChatPromptTemplate`. Empirically the system message
is always the leading message — relevant to §2.2.

---

## 1. Architecture

### 1.1 Shape

```
langchain (Chat Completions)
        │  local bearer token required (§3)
        ▼
  /v1/{provider}/chat/completions      ← provider chosen by URL path
        │
   parse ONCE  ──▶ NormalizedRequest
        │
   Provider = (name, adapter, credentials, quirks, model_aliases)
        │
   adapter.send(nreq, creds, quirks, ctx) ──▶ upstream ──▶ NormalizedResponse
        │
   render ──▶ Chat Completions JSON
```

### 1.2 Two seams — and an honest statement of the guarantee

| Seam | Day-one implementations |
|---|---|
| `CredentialSource` | `CodexAuthFile` (read-only), `OwnOAuth`, `ApiKey` |
| `UpstreamAdapter` | `CodexResponsesAdapter`, `OpenAIChatCompletionsAdapter` |

**Restated guarantee (review #20 accepted).** The earlier "one adapter module +
one registry entry" claim was aspirational and does not hold for authenticated
providers: OAuth varies in discovery, grant parameters, token shape, refresh
rotation, entitlement claims and required headers. The accurate guarantee is:

> Adding a provider means **one new provider package** —
> `providers/<name>/{adapter.py, auth.py, quirks.py}` — plus **one registry
> entry**. It must not require edits to the router, the inbound parser, the
> renderer, the relay, the idempotency layer, or checkpointing.

That is the property the `EchoAdapter` seam test enforces (§7). Auth is
colocated with its adapter because the two are coupled in practice.

`CredentialSource.get()` is **async** — it may perform a network refresh.

### 1.3 Explicitly NOT doing

- No dynamic plugin loading (no entry points, no `importlib` discovery).
- No config-file-defined providers. The registry is Python, in-tree.
- No capability negotiation. Capabilities are static data.
- No abstract base classes beyond the two Protocols.

### 1.4 `NormalizedRequest`

```python
@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict
    strict: bool | None = None      # None = caller omitted it; do NOT default

@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str                  # JSON string, never a dict

ABSENT = object()                   # distinguishes "no content" from ""

@dataclass(frozen=True)
class Msg:
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None | Absent = ABSENT
    name: str | None = None         # review #7: was being dropped
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

@dataclass(frozen=True)
class NormalizedRequest:
    model: str                      # as received, pre-alias
    resolved_model: str             # after alias mapping (§5.3)
    instructions: str | None        # leading system run only (§2.2)
    messages: tuple[Msg, ...]
    tools: tuple[ToolDef, ...] = ()
    tool_choice: ToolChoice | None = None   # full shape, see §2.3
    json_schema: JsonSchemaSpec | None = None
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    # Review #4 ACCEPTED: normalize every semantic parameter so an adapter can
    # deliberately strip or reject it. The parser must never silently drop.
    temperature: float | None = None
    top_p: float | None = None
    parallel_tool_calls: bool | None = None
    seed: int | None = None
    stop: tuple[str, ...] = ()
```

`opaque` is **removed** — §0.2(g) proves it cannot round-trip. Conversation
state is handled in §2.8 instead.

### 1.5 `NormalizedResponse`

```python
@dataclass(frozen=True)
class Usage:
    input_tokens: int; output_tokens: int; total_tokens: int
    reasoning_tokens: int = 0; cached_tokens: int = 0

@dataclass(frozen=True)
class NormalizedResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: Literal["stop","tool_calls","length","content_filter"]
    usage: Usage
    model: str
    upstream_id: str | None = None      # review #22: response id/created
    created: int | None = None
    incomplete_reason: str | None = None  # review #9, diagnostics only
```

Refusals map to `finish_reason="content_filter"` with the refusal text in
`text`. No separate refusal channel — nothing in this pipeline reads OpenAI's
`refusal` field (review #9, ACCEPT-WITH-CHANGES).

### 1.6 `ProviderQuirks`

```python
@dataclass(frozen=True)
class ProviderQuirks:
    valid_models: tuple[str, ...]        # () = accept anything
    model_aliases: Mapping[str, ModelAlias]   # §5.3
    reject_params: frozenset[str]        # 400 rather than silent drop
    strip_params: frozenset[str]         # dropped, logged once
    mandatory_body: Mapping[str, Any]
    static_headers: Mapping[str, str]
    json_schema_style: Literal["text_format","response_format","none"]
    max_body_bytes: int = 8 * 1024 * 1024
    request_timeout_s: float = 180.0
```

**`reject_params` vs `strip_params` (review #4).** `temperature` on Codex is
*stripped* (the pipeline sets it from `DEFAULT_CONFIG`/env and cannot be told
not to), and stripping is logged once per process. Anything semantically
load-bearing that an upstream cannot honour belongs in `reject_params` so it
fails loudly rather than changing meaning silently.

### 1.7 Request context (review #12 ACCEPTED)

```python
@dataclass
class RequestCtx:
    deadline_at: float                  # monotonic
    cancel: asyncio.Event
    req_id: str
    trace: str                          # for logs, no payloads
```

`adapter.send(..., ctx)` must abort the upstream HTTP stream when `cancel` is
set or the deadline passes, and on client disconnect. **Documented limitation:**
graph-level cancellation stays cooperative — `_CancelToken` is polled between
chunks, so today an in-flight LLM call keeps billing. Wiring cancel through to
`ctx.cancel` is part of M5, not M1; until then the helper's own deadline is the
only bound.

---

## 2. Translation

### 2.1 Inbound parse (provider-independent)

All semantic parameters are captured (§1.4). Nothing is dropped here — dropping
is an adapter decision driven by quirks.

### 2.2 `instructions` vs message roles (review #7 ACCEPT-WITH-CHANGES)

Hoist **only a leading contiguous run** of `system`/`developer` messages into
`instructions`. Any later system/developer message stays in `input` as a message
item with its role preserved, so late instructions keep their precedence.

Rejected the stronger "never hoist" form: §0.2(n) shows the system message is
always leading in this pipeline, and Codex requires `instructions` to be
populated, so unconditional non-hoisting would send an empty `instructions` for
every call. `Msg.name` is now preserved and `ABSENT` distinguishes missing
content from `""`.

### 2.3 `tool_choice` — every shape mapped (review #5 ACCEPTED)

| Inbound | Codex Responses |
|---|---|
| absent | omit (**do not synthesise `"auto"`**) |
| `"auto"` / `"none"` / `"required"` | passthrough |
| `{"type":"function","function":{"name":N}}` | `{"type":"function","name":N}` |
| named tool not in `tools` | **400** `tool_choice_unknown_tool` |

`strict` is passed through when present and **omitted when absent** — never
defaulted (§0.2(f)). `parallel_tool_calls` maps to the Responses field of the
same name; if a future provider cannot express it, it goes in `reject_params`.

### 2.4 `CodexResponsesAdapter` — item shapes

```
Msg(user)              → {type:"message", role:"user",
                          content:[{type:"input_text", text}]}
Msg(assistant, text)   → {type:"message", role:"assistant",
                          content:[{type:"output_text", text}]}
Msg(assistant, calls)  → one {type:"function_call", name, arguments, call_id}
                          per call (+ a message item only if content present)
Msg(tool)              → {type:"function_call_output", call_id, output:<str>}
Msg(system|developer)  → non-leading only: message item, role preserved
```

`arguments` is a JSON **string**; `output` is a **string**.
`quirks.mandatory_body` merges last and always wins.

### 2.5 Edge cases (one fixture test each)

1. Assistant turn with both content and tool_calls → message item, then calls.
2. Multi-tool-call turn → N `function_call`, then N `function_call_output`.
3. `content` ABSENT vs `""` vs `null` → three distinct behaviours.
4. Tool results in arrival order; never sorted.
5. Refusal → `finish_reason="content_filter"`.
6. Consecutive same-role messages kept separate.
7. Non-leading system message → stays inline (§2.2).
8. Multimodal parts → 400, not silently dropped.
9. `parallel_tool_calls:false` present on all four schema-bound agents.
10. Unknown `tool_call_id` on a tool message → 400, never fabricate a `call_id`.

### 2.6 SSE → NormalizedResponse (reviews #8, #9 ACCEPTED)

**Index by `(output_index, item_id)`, not `call_id`.** Argument delta events
identify their target by item, and a table keyed only on `call_id` cannot
assemble parallel calls safely. Each slot retains its `call_id` for the
response, validates the terminal `arguments` against the concatenation,
rejects duplicate item ids, and emits in `output_index` order.

**Every terminal state modelled:**

| Terminal | Result |
|---|---|
| `response.completed` | success; `usage` |
| `response.incomplete` | `finish_reason="length"` + `incomplete_reason` |
| `response.failed` | error path (§2.9) |
| top-level `error` event | error path (§2.9) |
| stream ends with no terminal event | **502**, treated as aborted |

Never return a partial assembly as complete: a truncated `arguments` string that
happens to parse would silently corrupt an agent decision.

`text` must be `None`, never `""` — langchain may treat `""` as a valid final
answer and end a tool loop early.

### 2.7 `session_id` and per-request fields (review #22 ACCEPTED)

`session_id` is **dynamic**, so it cannot live in `static_headers`. The adapter
owns per-request header construction: `session_id` (uuid4 per HTTP request),
`Authorization` and `chatgpt-account-id` from `Credentials`, and
`quirks.static_headers` merged in. Response `id`/`created` come from
`response.created` where available, else generated. Bodies over
`quirks.max_body_bytes` → 413.

### 2.8 Encrypted-reasoning echo — U2 gates M1 (review #6 ACCEPTED)

`opaque` on the response is impossible (§0.2(g)). **Resolve U2 before M1** and
build from the answer:

- **If echo is NOT required** (expected): capture reasoning items, discard them,
  no state. Cheapest, and nothing further to build.
- **If echo IS required**: helper-side conversation state keyed by a
  **conversation-prefix digest** — `sha256` over the canonicalized inbound
  messages *excluding the newest turn*, plus provider, resolved model and
  credential principal. langchain resends the full history each turn, so the
  digest is deterministic and needs no client cooperation. After responding,
  store the reasoning items under the digest of (history + this response).

  A miss degrades rather than crashes **only if** echo is optional-but-helpful.
  If echo is strictly required, a miss is a hard failure, so state must be
  durable (SQLite next to the helper, TTL 1 h) rather than in-memory. **Which of
  those two we build depends entirely on U2 — do not pre-build either.**

### 2.9 Error mapping (review #11 ACCEPT-WITH-CHANGES)

Preserve upstream code/message/status where safe; never collapse everything to
502.

| Condition | Helper |
|---|---|
| HTTP 401 / invalid refresh | `invalidate()`, refresh once, retry once, then 401 `reauth_required` |
| HTTP 403 | 403, upstream message preserved |
| HTTP 400 — model rejected | 400 `model_not_supported`, listing `valid_models` |
| HTTP 400 — other | 400, upstream code/message preserved verbatim |
| HTTP 429 **or** rate-limit inside a 200 SSE stream | 429 **with `Retry-After` preserved** |
| terminal SSE `error`/`response.failed` | mapped by upstream code, not blanket 502 |
| deadline / `ctx.cancel` | 499 (client closed) — distinct from upstream failure |
| stream aborted with no terminal event | 502 |

Rate-limit and auth errors arriving inside an HTTP-200 stream are the subtle
case: the status line is 200, so only the SSE terminal event reveals them.
Mapping those to 502 would destroy `Retry-After` and defeat the pipeline's
`llm_max_retries` backoff.

### 2.10 Inbound `stream:true` (review #10 ACCEPTED)

Return **501 `streaming_not_supported`** with a clear message. Do not
record-and-ignore: silently answering a streaming request with a single JSON
body violates Chat Completions. The pipeline calls `.invoke()`, never
`.stream()` (`graph.stream()` streams *nodes*), so nothing regresses.

---

## 3. Local security (review #21 BLOCKER — in M1, not deferred)

**Binding to `127.0.0.1` is not authorization.** Any local process — including a
web page doing `fetch("http://127.0.0.1:8899/...")` — could otherwise spend the
user's subscription or read account status.

1. **Local bearer token.** 32 bytes from `secrets.token_urlsafe`, generated on
   first run, stored `0600` at `~/.ta-helper/token`. Required on **every**
   endpoint except `/healthz`. Compared with `secrets.compare_digest`.
   The pipeline passes it as the `api_key` (§5.2) — the field langchain already
   sends as `Authorization: Bearer`, so no new plumbing.
2. **Origin/CSRF.** Reject any request carrying `Origin` or `Referer` unless it
   is on an explicit allowlist (empty by default). A browser cannot suppress
   `Origin` on cross-origin requests, so this blocks the drive-by case even if
   the token leaks into a page.
3. **Bind address** `127.0.0.1` only; refuse `0.0.0.0` unless
   `--i-know-what-im-doing` is passed, and log loudly.
4. **Body cap** `quirks.max_body_bytes` (8 MiB) → 413. **Tool-schema cap** 256
   KiB. **Concurrency cap** 8 in-flight upstream requests, 429 beyond.
5. **`/status` requires the token** — it exposes plan type, account id suffix
   and expiry. `/healthz` returns only `{"ok":true}`.
6. **Never log** tokens, prompts, tool arguments or completions. Log
   `req_id`, provider, resolved model, status, duration, token counts.

---

## 4. Credentials

**`CodexAuthFile` (read-only).** `~/.codex/auth.json` →
`tokens.{access_token,refresh_token,account_id}`, `auth_mode=="chatgpt"`.
**Never written** — Codex CLI writes it too and a concurrent write can log the
user out of real Codex. On expiry `available()` → `False` and the chain falls
through; do not refresh in place (rotation could invalidate Codex's copy — U3).

**`OwnOAuth`.** Async. PKCE S256, loopback callback, browser open, code
exchange. Derives `chatgpt_account_id`, `chatgpt_plan_type`,
`chatgpt_subscription_active_until` from the `id_token`. **Gates on plan** — no
active subscription refuses to serve *before* a 6-minute run starts. Storage: OS
keychain (`keyring`), fallback `~/.ta-helper/auth.json` `0600` in a `0700` dir.
Colocated with its provider package (§1.2), because grant parameters, token
shape and entitlement claims are provider-specific rather than parameters.

**`ApiKey`.** Env or flag; paired with `OpenAIChatCompletionsAdapter`.

**Refresh.** Driven by `access_token.exp`: under 5 min remaining, and on 401.
**Single-flight** — 12 agents would otherwise stampede. Rotated tokens persisted
atomically (temp + `os.replace`).

---

## 5. Repo integration — zero core edits

### 5.1 Provider selection

`llm_provider="openai_compatible"`,
`backend_url="http://127.0.0.1:8899/v1/codex"`. Verified in §0.2(m).
`openai_compatible` has `require_base_url=True`, `key_optional=True`,
`chat_class=LocalCompatibleChatOpenAI`, and never enables
`use_responses_api`.

### 5.2 Credential injection WITHOUT `RunRequest` (review #1 BLOCKER)

`create_run(config=request.model_dump())` persists `config_json`, so a token on
`RunRequest` would sit in SQLite in plaintext, and would also reach
`run.started` SSE events (`_redact_config`) and report headers.

**Design:** the local bearer token never enters `RunRequest`.

1. `build_graph_for_request(request, *, user_id, credential: str | None)` — a
   keyword argument, resolved by the caller from server-side state (env for
   local; the relay session for hosted), never from the request body.
2. Injected into the in-memory config *after* `_build_config()` returns, so it
   is absent from the dict that was persisted.
3. `HelperBackedGraph._get_provider_kwargs()` forwards it to the client.
4. A test asserts the token appears in **no** `config_json` row, **no** event
   payload and **no** rendered report.

```python
class HelperBackedGraph(TradingAgentsGraph):
    """Forward config['api_key'] to the LLM client.

    Upstream reads keys from os.environ only. Per-user credentials must not go
    through the environment: WEBAPP_CONCURRENCY>1 shares one process, so one
    user's token would leak into another's run.
    """
    def _get_provider_kwargs(self):
        kwargs = super()._get_provider_kwargs()
        if key := self.config.get("api_key"):
            kwargs["api_key"] = key
        return kwargs
```

### 5.3 Per-tier reasoning effort via model aliases (review #2 ACCEPTED)

§0.2(l): one kwargs dict feeds both clients, so config cannot express per-tier
effort. Instead the **model id carries it**, which config *can* express
per-tier (`shallow_thinker` / `deep_thinker` are separate fields):

```python
model_aliases = {
    "gpt-5.6-sol-low":  ModelAlias("gpt-5.6-sol", reasoning_effort="low"),
    "gpt-5.6-sol-high": ModelAlias("gpt-5.6-sol", reasoning_effort="high"),
}
```

`shallow_thinker="gpt-5.6-sol-low"`, `deep_thinker="gpt-5.6-sol-high"`. 10 of
12 agents use the quick client; on the probe 63 of 105 output tokens were
reasoning tokens, so this is the dominant cost lever. Aliases also give U5 a
natural home: if `gpt-5.6-terra`/`luna` work, they become additional aliases
with no code change.

`NormalizedRequest` keeps both `model` (as received) and `resolved_model` so
errors can name what the caller asked for.

### 5.4 Config endpoint

`routes/config.py` `_PROVIDERS` lacks `openai_compatible`. Add it with the alias
model ids; the catalog only offers `("Custom model ID","custom")` there.

---

## 6. Resume and robustness

### 6.1 The hard constraint

`store:false` is mandatory → no server-side conversation state, no
`previous_response_id`. **Resumption is impossible at the LLM layer.**

### 6.2 Idempotency (reviews #13, #14)

**#13 ACCEPTED.** A provider-agnostic content hash is an invalid key. The
logical invocation identity is the **relay `req_id`** (stable across retries of
the same call), bound to a fingerprint so a reused id cannot return another
deployment's answer:

```
key         = req_id
fingerprint = sha256(provider, upstream_url, adapter_version,
                     resolved_model, credential_principal, request_digest)
```

A `req_id` hit whose fingerprint differs is a bug → 409, never a cache hit.

**#14 ACCEPT-WITH-CHANGES.** Added: per-key **single-flight** so concurrent
duplicates collapse to one upstream call. **Rejected for v1:** durable result
storage — over-engineered at this scale. The guarantee is therefore narrowed and
stated honestly:

> Retry is free **only** for a call that completed and whose result is still
> held by the same helper process (LRU 256, 10-min TTL). A helper restart, or a
> disconnect between upstream completion and caching, costs one re-billed call.

### 6.3 Run-level resume (reviews #15, #16, #17, #18, #19)

Currently `checkpoint_enabled` is a **no-op in the API path**: checkpointing
lives in `propagate()`, which `runner.py` bypasses. Fixing it properly needs
five things, not one.

**#15 — namespace per run, not per ticker+date.** `thread_id(ticker,date,sig)`
omits `user_id` (§0.2(h)). Persist a run-specific namespace
`f"{user_id}:{run_id}"` in a new `checkpoint_thread_id` column and reuse it only
for resumes **of that run**. Never re-derive it from ticker+date.

**#16 — resume is a distinct execution path.** `stream(None, config)`, not
`stream(init_state, config)` — resending the initial state restarts the graph.
The resume path must also:
- seed `ChunkTranslator` from persisted state (completed analysts, debate
  lengths, `_processed_message_ids`) or sequence numbers restart and collide
  with stored events;
- start `seq` from `store.latest_seq(run_id)+1`;
- suppress re-emission of already-persisted content.

**#17 — persist the effective config, not the request.** `config_json` is
`RunRequest.model_dump()`; `_build_config()` re-applies a live
`DEFAULT_CONFIG` derived from the environment, so a restart can silently change
`temperature`, `llm_max_retries` or paths. Add `effective_config_json` — the
post-`_build_config` dict **with credentials removed** — plus `code_version`
(git sha). Validate both on resume; mismatch → refuse, explain, offer a fresh
run.

**#18 — `interrupted` end-to-end.** Add to `RunStatus`, the store, terminal-state
handling and the frontend. Sweep **both** `queued` and `running` on startup
(queued jobs are lost on restart too). Resume must be an **atomic**
`interrupted → running` transition (`UPDATE ... WHERE status='interrupted'`,
check `rowcount`) so a double-click cannot start two resumes.

**#19 ACCEPT-WITH-CHANGES.** Own the checkpointer context in the runner's
`try/finally` so error, cancel and early-return paths cannot leak the SQLite
handle. Clear checkpoints on success and cancel; retain only resumable
interruptions. **Rejected:** changing `stream_mode` to capture node names —
§0.2(i) shows the API path uses `"values"`, and adding `"updates"` changes what
the translator receives, a real regression risk for cosmetic data. **Drop the
`last_completed_node` column**; resume is driven by the checkpoint itself.

**Cap `resume_count` at 3** so a poison run cannot loop.

### 6.4 Provider-agnostic vs provider-specific

**Agnostic:** relay protocol, correlation, ack/deadline/reconnect, idempotency
(the fingerprint *names* the provider but the mechanism is shared), all
checkpoint/resume work, job store, SSE-to-frontend, cancellation, renderer,
inbound parser, local security.

**Provider-specific:** `UpstreamAdapter` and `CredentialSource` only — one
provider package each.

So M4 and M5 need no revisiting when a provider is added.

### 6.5 Not recoverable

A mid-call interruption loses that call's output. Cost: one call's tokens unless
the idempotency cache hits.

---

## 7. Testing without burning quota

**Fixtures.** Capture one real SSE transcript per scenario **once**, tokens
scrubbed, into `tests/helper/fixtures/`: text-only; single tool call; multi
tool call; **all four** schema-bound agents (`TraderProposal`, `ResearchPlan`,
`PortfolioDecision`, sentiment); 400 bad model; 429; `response.incomplete`;
`response.failed`; mid-stream abort; rate-limit-inside-200-stream.

**Fake upstream** serving those fixtures over a local `http.server`.

**Unit tests (no network):** inbound parse per §2.5 edge case; `tool_choice`
matrix (§2.3); `strict`/`parallel_tool_calls` preserved exactly as received
(§0.2(e,f)); `mandatory_body` overrides caller values; `strip_params` vs
`reject_params`; SSE assembly indexed by `(output_index,item_id)` incl. parallel
calls and duplicate ids; every terminal state; truncated stream → 502;
`Retry-After` survives a rate limit inside a 200 stream; renderer emits
`content:null` not `""`; credential tiers incl. **asserting Codex's
`auth.json` is never opened for writing**; single-flight; fingerprint mismatch →
409; security — missing/wrong token → 401, cross-origin `Origin` → 403,
oversized body → 413, `/healthz` open.

**Contract test with real langchain (no network).** Point
`LocalCompatibleChatOpenAI` at the fake upstream and assert `bind_structured`
round-trips **all four** schemas. Highest-value single test — it proves the
§0.2(b) tool-based path end to end.

**Credential-leak test (review #1).** Create a run with a credential, then
assert it appears in no `config_json`, no event payload, no report.

**Seam test.** A ~30-line `providers/echo/` package under `/v1/echo` proving the
§1.2 guarantee and guarding it against erosion.

**Rejected:** contract tests for *every* item in review #22 — gold-plating.
Tests exist for the ones that can silently corrupt output (headers, timeouts,
body caps, error envelope), not for cosmetics.

**Needs live calls** (`@pytest.mark.live`, skipped by default, ~200 tokens
each): auth smoke; one custom function tool honoured; refresh grant returns a
usable token.

---

## 8. Open unknowns

| # | Unknown | Experiment | Gates |
|---|---|---|---|
| U1 | Redirect URI other than `http://localhost:1455/auth/callback` allowed? 1455 collides with a live `codex login`. | Build the authorize URL with port 1456 and open it — an unregistered URI errors before any token is issued. Free. | M3 |
| U2 | Must encrypted reasoning items be echoed on later tool-loop turns? | Two-turn run (tool call → result → second call), with and without the reasoning item. ~400 tokens. | **M1** (§2.8) |
| U3 | Does the refresh grant rotate the refresh token, invalidating Codex CLI's copy? | Refresh with a copy; compare the returned `refresh_token`. | M3 |
| U4 | Rate limits for a 12-agent run on Pro. | One depth-1 single-analyst run; count 429s. | M2 |
| U5 | Do `gpt-5.6-terra` / `luna` work? Cheap quick-thinker. | Repeat the 400-probe per name; ~0 tokens on rejection. | M2 (aliases, §5.3) |

---

## 9. File layout

```
apps/helper/
  pyproject.toml
  ta_helper/
    __main__.py            # serve | login | status | logout | providers | token
    server.py              # routing, local auth, origin/body/concurrency caps
    security.py            # token gen/compare, origin policy
    normalize.py           # NormalizedRequest/Response
    inbound.py             # Chat Completions -> NormalizedRequest
    render.py              # NormalizedResponse -> Chat Completions
    registry.py            # PROVIDERS, Provider, ProviderQuirks, ModelAlias
    context.py             # RequestCtx (deadline, cancel)
    idempotency.py         # req_id + fingerprint, single-flight
    conversation_state.py  # only if U2 says echo is required
    providers/
      codex/{adapter.py, auth.py, quirks.py}
      openai/{adapter.py, auth.py, quirks.py}
      echo/                # seam test
    relay_client.py        # M5
tests/helper/
docker-compose.local.yml
```

**Language: Python** — reuses this repo's toolchain, tests and idioms.
Packaging as a signed desktop artifact is a separate decision.

---

## 10. Milestones — re-derived

| # | Deliverable | Was | Now | Δ |
|---|---|---|---|---|
| M0 | U1 + U5 probes, **U2 resolution** (gates M1), fixture capture incl. 4 schemas + error/terminal cases | 0.5 | **1.0** | +0.5 |
| M1 | Normalize/inbound/render with all semantic params, registry + quirks + aliases, `CodexResponsesAdapter` (full `tool_choice`, item-indexed SSE, all terminal states), `OpenAIChatCompletionsAdapter`, `CodexAuthFile` + `ApiKey`, **local security (§3)**, error mapping (§2.9), `RequestCtx`, fake-upstream + seam tests | 1.5 | **3.0** | +1.5 |
| M2 | Wire local pipeline: `HelperBackedGraph`, **credential injection outside `RunRequest`** + leak test, model aliases, `docker-compose.local.yml`, one real end-to-end analysis, U4 | 0.5 | **1.0** | +0.5 |
| M3 | `OwnOAuth` (async, colocated), keychain store, single-flight refresh | 1.0 | **1.25** | +0.25 |
| M4 | Resume redesign: per-run namespace, `stream(None,config)` path, translator/seq seeding, `effective_config_json` + `code_version`, `interrupted` end-to-end, atomic transition, `try/finally` teardown + retention | 1.5 | **3.0** | +1.5 |
| M5 | Relay: WS endpoint, registry, loopback shim, cancel → `ctx.cancel`, idempotency fingerprint + single-flight, backpressure | 2.5 | **3.0** | +0.5 |
| | **Total** | 7.5 | **12.25** | **+4.75** |

**M0–M2: 2.5 d → 5.0 d.** What moved, and why:

- **+1.5 d in M1** is mostly the security work (§3), which was entirely absent
  and is a genuine blocker, plus the SSE state machine growing from "happy path
  + abort" to every terminal state, and full `tool_choice`/parameter fidelity.
- **+0.5 d in M0** is U2. It gates M1 because §2.8's design branches on the
  answer, and building the wrong branch is worse than waiting.
- **+0.5 d in M2** is credential injection done safely — a keyword argument
  path plus a leak test, rather than a field on `RunRequest`.
- **+1.5 d in M4** is the honest cost of resume being wrong three independent
  ways; it was previously scoped as if only the checkpointer wiring were
  missing.
- **M5 +0.5 d** for the idempotency fingerprint and single-flight.

Recommended stop-and-review after M2 (now day 5).

---

## Appendix A — Review round 1 dispositions

| # | Sev | Disposition | Reason |
|---|---|---|---|
| 1 | BLOCKER | **ACCEPT** | Confirmed: `create_run(config=request.model_dump())` → plaintext token in `config_json`. §5.2 |
| 2 | SHOULD | **ACCEPT** | Confirmed: one kwargs dict feeds both clients. Model aliases adopted. §5.3 |
| 3 | SHOULD | **ACCEPT** | Confirmed: four schema-bound agents incl. `ResearchPlan`. §0.2(c), §7 |
| 4 | BLOCKER | **ACCEPT**, evidence **VINDICATED** | Recommendation adopted (§1.4). Codex's `parallel_tool_calls=false` claim is **true** — verified empirically; the counter-claim was wrong. §0.2(e) |
| 5 | BLOCKER | **ACCEPT** | `tool_choice` was unmapped; `strict` was being invented. §2.3, §0.2(f) |
| 6 | BLOCKER | **ACCEPT** | `opaque` removed. Verified no langchain passthrough exists, so only helper-side state is viable. U2 gates M1. §0.2(g), §2.8 |
| 7 | SHOULD | **ACCEPT-WITH-CHANGES** | Leading-run hoist only; `name` and ABSENT added. Rejected "never hoist" — Codex needs `instructions` populated. §2.2 |
| 8 | SHOULD | **ACCEPT** | Index by `(output_index,item_id)`. §2.6 |
| 9 | SHOULD | **ACCEPT-WITH-CHANGES** | All terminal states modelled; refusal → `content_filter`, no separate channel (nothing reads it). §1.5, §2.6 |
| 10 | SHOULD | **ACCEPT** | 501 on inbound `stream:true`. §2.10 |
| 11 | SHOULD | **ACCEPT-WITH-CHANGES** | Full mapping incl. rate-limit-inside-200-stream; `Retry-After` preserved. §2.9 |
| 12 | SHOULD | **ACCEPT** | `RequestCtx` with deadline + cancel; cooperative graph cancellation documented. §1.7 |
| 13 | BLOCKER | **ACCEPT** | `req_id` + fingerprint; mismatch → 409. §6.2 |
| 14 | BLOCKER | **ACCEPT-WITH-CHANGES** | Single-flight added; guarantee narrowed. **Rejected** durable storage for v1 as over-engineered. §6.2 |
| 15 | BLOCKER | **ACCEPT** | Confirmed `thread_id` lacks `user_id`. Per-run namespace. §6.3 |
| 16 | BLOCKER | **ACCEPT** | `stream(None,config)` + translator/seq seeding. §6.3 |
| 17 | BLOCKER | **ACCEPT** | Confirmed `_build_config` re-applies env defaults. `effective_config_json` + `code_version`. §6.3 |
| 18 | SHOULD | **ACCEPT** | Confirmed `interrupted` absent from `RunStatus`. Atomic transition added. §6.3 |
| 19 | SHOULD | **ACCEPT-WITH-CHANGES** | `try/finally` teardown + retention accepted. **Rejected** the `stream_mode` change; **dropped** `last_completed_node` instead. §6.3 |
| 20 | SHOULD | **ACCEPT** | Guarantee restated as provider *package* + registry entry; credentials async, auth colocated. §1.2 |
| 21 | BLOCKER | **ACCEPT** | Loopback ≠ authorization. Token + origin + caps, in M1. §3 |
| 22 | SHOULD | **ACCEPT-WITH-CHANGES** | `session_id`, timeouts, response ids, body caps made explicit. **Rejected** contract tests for every item as gold-plating. §2.7, §7 |

**Net:** 15 ACCEPT, 7 ACCEPT-WITH-CHANGES, 0 outright REJECT — but four
sub-recommendations rejected inside those: durable idempotency storage (#14),
the `stream_mode` change and `last_completed_node` (#19), "never hoist
instructions" (#7), and exhaustive contract tests (#22). One review claim was
**vindicated against the counter-claim** (#4).
