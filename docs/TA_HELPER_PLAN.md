# ta-helper — implementation plan

A small local daemon presenting an **OpenAI Chat Completions** endpoint on
`127.0.0.1`, satisfied by whichever subscription the user has. OpenAI/Codex on
day one; Anthropic and others addable later without restructuring. The
TradingAgents pipeline points at it via `backend_url`; no upstream core edits.

---

## 0. Established wire contract (validated — do not re-probe)

```
POST https://chatgpt.com/backend-api/codex/responses
Authorization: Bearer <access_token>
chatgpt-account-id: <from id_token claim>
originator: codex_cli_rs
session_id: <uuid4>
Accept: text/event-stream

{ "model": "gpt-5.6-sol",     // ONLY this; gpt-5 / gpt-5-codex /
  "store": false,             //   codex-mini-latest → 400
  "stream": true,             // both mandatory
  "instructions": "<system>", "input": [<typed items>],
  "tools": [{type:"function", name, description, strict, parameters}],
  "tool_choice": "auto",
  "text": {"format": {type:"json_schema", name, strict, schema}},
  "reasoning": {"effort": "low|medium|high"},
  "include": ["reasoning.encrypted_content"] }
```

SSE events seen: `response.created`, `response.in_progress`,
`response.output_item.added`, `response.output_text.delta`,
`response.function_call_arguments.delta`, `.done`,
`response.output_item.done`, `response.completed` (carries `usage` with
`input_tokens`, `output_tokens`, `output_tokens_details.reasoning_tokens`).

`id_token` claim `https://api.openai.com/auth` →
`chatgpt_account_id`, `chatgpt_plan_type`, `chatgpt_subscription_active_until`.

OAuth: issuer `https://auth.openai.com`, client_id
`app_EMoamEEZ73f0CkXaXp7hrann`, PKCE; Codex CLI redirect
`http://localhost:1455/auth/callback` (other ports UNKNOWN — see U1).

### THREE corrections to the briefing

**(a) No unknown-model warning will fire.** `openai_compatible` is listed in
`_ANY_MODEL_PROVIDERS` in `tradingagents/llm_clients/validators.py`, so
`validate_model("openai_compatible", "gpt-5.6-sol")` → `True`. Verified.
Nothing to handle. (Warnings in earlier test output came from the `openai` and
`deepseek` providers, which do validate.)

**(b) Structured output arrives as a TOOL, not `response_format`.**
`get_capabilities("gpt-5.6-sol").preferred_structured_method ==
"function_calling"` (the default for any unlisted model), and
`LocalCompatibleChatOpenAI.with_structured_output()` forces `tool_choice=None`
on that path. So Trader / Portfolio Manager / Sentiment schemas come through as
an extra entry in `tools`, answered as a `tool_call`. **Tool translation is the
entire hot path.** `response_format` → `text.format` is ~20 lines of
completeness, not critical path. Because `tool_choice` is `None` the model is
not forced to call the schema tool; `invoke_structured_or_freetext()` already
degrades to free text.

**(c) A per-provider `preferred_structured_method` quirk cannot change agent
behaviour.** langchain decides client-side, before the helper sees anything. The
quirk only governs how the helper translates a `response_format` if one
arrives. Do not expect setting it to alter what the agents send.

---

## 1. Architecture

### 1.1 Shape

```
langchain (Chat Completions)
        │
        ▼
  /v1/{provider}/chat/completions        ← provider chosen by URL path
        │
   parse ONCE  ──▶ NormalizedRequest
        │
   Provider = (name, UpstreamAdapter, CredentialSource, ProviderQuirks)
        │
   adapter.send(nreq, creds) ──▶ upstream HTTP ──▶ NormalizedResponse
        │
   render ──▶ Chat Completions JSON
```

Two seams, because they vary **independently** and we have multiple
implementations of each on day one — not speculatively:

| Seam | Day-one implementations |
|---|---|
| `CredentialSource` | `CodexAuthFile` (read-only), `OwnOAuth` (parameterised), `ApiKey` |
| `UpstreamAdapter` | `CodexResponsesAdapter`, `OpenAIChatCompletionsAdapter` |

The cross-product is real: the Codex adapter works with either
`CodexAuthFile` or `OwnOAuth`; the Chat Completions adapter works with
`ApiKey`. That is the justification for the seams — nothing else.

### 1.2 Explicitly NOT doing

- **No dynamic plugin loading.** No entry points, no `importlib` discovery.
- **No config-file-defined providers.** The registry is Python, in-tree.
- **No capability negotiation.** No runtime probing of what an upstream
  supports; capabilities are static data (§1.5).
- **No abstract base-class hierarchy beyond the two Protocols.**

Adding a provider = **one new adapter module + one registry entry.** If it ever
needs more than that, the seam is in the wrong place and should be moved, not
generalised.

### 1.3 `NormalizedRequest`

```python
@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict          # JSON Schema
    strict: bool = False

@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str            # JSON string, never a dict

@dataclass(frozen=True)
class Msg:
    role: Literal["user", "assistant", "tool"]
    content: str | None                       # None, never "" (see §2.4)
    tool_calls: tuple[ToolCall, ...] = ()     # assistant only
    tool_call_id: str | None = None           # tool results only

@dataclass(frozen=True)
class NormalizedRequest:
    model: str
    instructions: str | None                  # all system/developer msgs joined
    messages: tuple[Msg, ...]
    tools: tuple[ToolDef, ...] = ()
    tool_choice: str | ToolChoiceFn | None = None
    json_schema: JsonSchemaSpec | None = None # from response_format
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    stream: bool = False                      # inbound intent; see §2.5
    # Provider-opaque round-trip state (e.g. Codex encrypted reasoning items).
    # Written and read only by the adapter that owns it; never inspected here.
    opaque: Mapping[str, Any] = field(default_factory=dict)
```

`opaque` exists because of a real discovered need (§2.3 item 9): Codex may
require encrypted reasoning items echoed back on later turns of a tool loop.
Normalising them away would lose data the adapter must resend. It is not a
generic extension point — no other component may read it.

### 1.4 `NormalizedResponse`

```python
@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    reasoning_tokens: int = 0
    cached_tokens: int = 0

@dataclass(frozen=True)
class NormalizedResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"]
    usage: Usage
    model: str
    opaque: Mapping[str, Any] = field(default_factory=dict)
```

Rendering to Chat Completions happens in exactly one place, so every provider
gets an identical, correct response envelope for free.

### 1.5 `ProviderQuirks` — data, not conditionals

```python
@dataclass(frozen=True)
class ProviderQuirks:
    valid_models: tuple[str, ...]       # () = any model accepted
    strip_params: frozenset[str]        # {"temperature", "top_p"}
    mandatory_body: Mapping[str, Any]   # {"store": False, "stream": True}
    required_headers: Mapping[str, str] # {"originator": "codex_cli_rs"}
    default_reasoning_effort: str | None
    json_schema_style: Literal["text_format", "response_format", "none"]
```

Day-one registry (the entire configuration surface):

```python
PROVIDERS: dict[str, Provider] = {
    "codex": Provider(
        adapter=CodexResponsesAdapter(
            url="https://chatgpt.com/backend-api/codex/responses"),
        credentials=chain(CodexAuthFile(), OwnOAuth(
            issuer="https://auth.openai.com",
            client_id="app_EMoamEEZ73f0CkXaXp7hrann",
            redirect_uri="http://localhost:1455/auth/callback",
            scopes=("openid", "profile", "email", "offline_access"))),
        quirks=ProviderQuirks(
            valid_models=("gpt-5.6-sol",),
            strip_params=frozenset({"temperature", "top_p"}),
            mandatory_body={"store": False, "stream": True,
                            "include": ["reasoning.encrypted_content"]},
            required_headers={"originator": "codex_cli_rs"},
            default_reasoning_effort="low",
            json_schema_style="text_format"),
    ),
    "openai": Provider(
        adapter=OpenAIChatCompletionsAdapter(
            url="https://api.openai.com/v1/chat/completions"),
        credentials=ApiKey(env="TA_HELPER_OPENAI_API_KEY"),
        quirks=ProviderQuirks(valid_models=(), strip_params=frozenset(),
                              mandatory_body={}, required_headers={},
                              default_reasoning_effort=None,
                              json_schema_style="response_format"),
    ),
}
```

**Why quirks are separate from the adapter:** one adapter can serve several
providers with different rules — `OpenAIChatCompletionsAdapter` also fronts a
local vLLM or an OpenRouter relay, which differ only in model lists and
parameter support. Adapter = protocol; quirks = deployment. Keeping them
separate is what stops the second OpenAI-compatible provider from becoming a
subclass.

### 1.6 Seam Protocols

```python
class CredentialSource(Protocol):
    name: str
    def available(self) -> bool: ...
    def get(self) -> Credentials: ...    # bearer + account metadata; refreshes
    def invalidate(self) -> None: ...    # called on 401

@dataclass(frozen=True)
class Credentials:
    bearer: str
    headers: Mapping[str, str] = ()   # e.g. chatgpt-account-id
    plan: str | None = None
    expires_at: int | None = None
    source: str = ""

class UpstreamAdapter(Protocol):
    async def send(self, nreq: NormalizedRequest, creds: Credentials,
                   quirks: ProviderQuirks) -> NormalizedResponse: ...
```

`chain(...)` tries sources in order and picks the first `available()`.

### 1.7 Proof the seam is in the right place — `AnthropicMessagesAdapter`

Not built; sketched to show what a second adapter costs.

- `POST https://api.anthropic.com/v1/messages`, headers
  `anthropic-version`, `x-api-key` **or** `Authorization: Bearer` for
  subscription auth.
- `nreq.instructions` → top-level `system` (already separated — free).
- `nreq.messages` → `messages[]` with `content` blocks;
  assistant `tool_calls` → `tool_use` blocks; tool results → `tool_result`
  blocks keyed by `tool_use_id` (our `ToolCall.id` carries straight over).
- `nreq.tools` → `{name, description, input_schema}` — `ToolDef` maps 1:1.
- `stop_reason: "tool_use"` → `finish_reason: "tool_calls"`.
- `usage.{input_tokens,output_tokens}` → `Usage` directly.

Everything above is a pure function of `NormalizedRequest`. **No changes to
the router, renderer, relay, idempotency cache, or checkpointing.** That is the
test the design has to pass, and it does. Credentials would be a second
`OwnOAuth` instance with different parameters — which is why issuer, client_id
and redirect are constructor arguments rather than constants.

### 1.8 Provider selection by route path (verified)

`/v1/{provider}/chat/completions`. The pipeline selects a provider purely by
`backend_url`, using the per-run field it already has — **no new plumbing in
the repo.**

Verified against the installed `openai` 2.33.0 SDK: a path-prefixed
`base_url` joins correctly, with or without a trailing slash —
`base_url="http://127.0.0.1:8899/v1/codex"` and `".../v1/codex/"` both produce
`http://127.0.0.1:8899/v1/codex/chat/completions`. (The classic
`urljoin` last-segment-drop gotcha does not apply; the SDK appends rather than
resolves.)

One helper instance can therefore serve several subscriptions at once, and a
run's provider is a per-run config value.

`GET /v1/providers` lists configured providers with credential status, so the
frontend can show what is usable.

### 1.9 Model names are data and they die

`gpt-5` worked for Codex and now 400s. `valid_models` lives in the quirks
record; an unknown model returns:

```
400 {"error": {"code": "model_not_supported",
      "message": "Model 'gpt-5' is not available for provider 'codex'.
                  Valid models: gpt-5.6-sol"}}
```

Empty `valid_models` means accept anything (correct for API-key providers,
where the upstream is the authority).

---

## 2. Chat Completions ⇄ NormalizedRequest ⇄ Codex Responses

### 2.1 Inbound parse (once, provider-independent)

| Chat Completions | NormalizedRequest |
|---|---|
| `messages[role=system\|developer]` | `instructions`, blank-line joined, wherever they appear |
| other `messages` | `messages` |
| `tools[].function` | `tools` (`ToolDef`) |
| `tool_choice` | `tool_choice` |
| `response_format.json_schema` | `json_schema` |
| `max_tokens` / `max_completion_tokens` | `max_output_tokens` |
| `reasoning_effort` | `reasoning_effort` |
| `temperature`, `top_p` | dropped per `quirks.strip_params` |

### 2.2 `CodexResponsesAdapter` — NormalizedRequest → upstream

```
instructions            → "instructions"
Msg(user)               → {type:"message", role:"user",
                           content:[{type:"input_text", text}]}
Msg(assistant, text)    → {type:"message", role:"assistant",
                           content:[{type:"output_text", text}]}
Msg(assistant, calls)   → one {type:"function_call", name, arguments, call_id}
                           per call (plus a message item only if content too)
Msg(tool)               → {type:"function_call_output", call_id, output:<str>}
tools                   → [{type:"function", name, description,
                            parameters, strict:false}]
json_schema             → text.format (json_schema_style == "text_format")
reasoning_effort        → {"reasoning": {"effort": ...}}
quirks.mandatory_body   → merged last, always wins
```

- `call_id` = the Chat Completions `tool_calls[].id`, verbatim both ways.
- `arguments` is a JSON **string**; `output` is a **string** (`json.dumps`
  anything else).
- `strict:true` only when the schema is `additionalProperties:false` with all
  keys required — langchain's usually are not, and a mismatch is a 400.

### 2.3 Edge cases (one fixture test each)

1. Assistant turn with **both** content and tool_calls → message item first,
   then one `function_call` per call, order preserved.
2. **Multi-tool-call** turn → N `function_call` items, then N matching
   `function_call_output` items.
3. **Empty assistant content** (`content:null`, tool_calls only) → emit no
   message item; an empty `content:[]` is a 400 risk.
4. Tool results arrive after the assistant turn — preserve arrival order, never
   sort.
5. **Refusal** → ordinary assistant content, `finish_reason:"stop"`. Nothing in
   this pipeline reads OpenAI's `refusal` field.
6. Consecutive same-role messages → keep separate, do not merge.
7. **System message not first** (Msg-Clear nodes reorder) → collect all.
8. Multimodal content parts → unused here; reject with a clear 400 rather than
   silently dropping.
9. **Reasoning echo (OPEN — U2).** If Codex requires encrypted reasoning items
   echoed on later tool-loop turns, they travel in `nreq.opaque` and are
   re-emitted by the adapter. Capture them from
   `response.output_item.done` (`item.type=="reasoning"`) from day one, even
   before we know they are required.

### 2.4 SSE → NormalizedResponse

Accumulate: `response.output_text.delta` → text buffer;
`output_item.added` (`function_call`) → open slot keyed by `call_id`, record
`name`; `function_call_arguments.delta` → append; `.done` → prefer the terminal
`arguments` field over the concatenation; `output_item.done` (`reasoning`) →
stash into `opaque`; `response.completed` → `usage` + status.

- `finish_reason` = `tool_calls` if any slot filled; `length` on signalled
  truncation; else `stop`.
- `text` must be `None`, never `""` — langchain may treat an empty string as a
  valid final answer and stop a tool loop early.
- Stream ends without `response.completed` → **502**. Never return a partial
  assembly as complete: a truncated `arguments` string that happens to parse
  would silently corrupt an agent's decision.

### 2.5 Downstream streaming is out of scope for v1

The agents call `.invoke()`, never `.stream()` — `graph.stream()` streams
*nodes*, not tokens. Buffer upstream, return one JSON body. `nreq.stream` is
recorded but unused; revisit only if a token-streaming UI is wanted.

### 2.6 Error mapping (provider-independent, in the router)

| Upstream | Helper |
|---|---|
| 401 / invalid refresh | `credentials.invalidate()`, refresh once, retry once, then `401 {"code":"reauth_required"}` |
| 400 model rejected | `400` naming valid models (§1.9) |
| 429 | passthrough + `Retry-After`; the pipeline's `llm_max_retries` backs off |
| 5xx / aborted stream | `502` |

---

## 3. Credential tiers

**`CodexAuthFile` (read-only).** `~/.codex/auth.json` →
`tokens.{access_token,refresh_token,account_id}`, `auth_mode=="chatgpt"`.
**Never written.** Codex CLI writes it too; a concurrent write races its
refresh and can log the user out of real Codex. On expiry, do **not** refresh
in place — `available()` returns `False` and the chain falls through to
`OwnOAuth` (a refresh may rotate the token and invalidate Codex's copy — U3).

**`OwnOAuth` (default path).** Parameterised by `issuer`, `client_id`,
`redirect_uri`, `scopes` — so an Anthropic instance is a second construction,
not a second class. PKCE S256, loopback callback, browser open, code exchange.
Derives `chatgpt_account_id` → header, plus `chatgpt_plan_type` and
`chatgpt_subscription_active_until` from the `id_token`. **Gates on plan**:
no active subscription → refuse to serve with a plain message, before a
6-minute run starts. Storage: OS keychain (`keyring`), fallback
`~/.ta-helper/auth.json` at `0600` in a `0700` directory. Never log token
material — only `exp` and plan.

**`ApiKey`.** Env or flag; paired with `OpenAIChatCompletionsAdapter` for a
near-passthrough path. Makes the helper useful to anyone and gives a fallback
when the Codex path breaks.

**Refresh.** Driven by `access_token.exp`: refresh under 5 minutes remaining
and on any 401. **Single-flight** — 12 agents firing concurrently would
otherwise stampede. Rotated refresh tokens persisted atomically (temp file +
`os.replace`).

**`GET /status`** → `{provider, tier, plan, account_id_suffix, expires_in_s,
model}`; drives the frontend chip and makes support tractable.

---

## 4. Resumability and robustness

### 4.1 The hard constraint

`store:false` is mandatory, so there is no server-side conversation state and
no `previous_response_id`. **Resumption is impossible at the LLM layer.** Every
call is self-contained. Two other layers carry it.

### 4.2 Layer 1 — call-level (relay only)

- **Correlation id** per call (`uuid4`); relay holds `{req_id: Future}`.
- **Idempotency key** = `sha256(canonical NormalizedRequest)`. Helper caches
  `key → NormalizedResponse` (LRU 256, 10-min TTL). A retry after reconnect
  returns the cached body instead of re-billing quota. Highest-value robustness
  feature: it makes retry free. **Provider-agnostic** — the key is computed
  over the normalized form.
- **Reconnect buffering.** Helper drops mid-call → Future stays pending to
  `deadline` (180 s/call). On reconnect the server re-sends unacked `req_id`s;
  the idempotency cache answers instantly if the call had finished. Past the
  deadline the call fails into the pipeline's normal retry.
- **Ack protocol**: `request` → `ack` (≤5 s) → `result`/`error`. No ack →
  re-dispatch; the socket may be a zombie.
- Reconnect backoff 1 s → 30 s jittered, indefinitely.

### 4.3 Layer 2 — run-level (LangGraph checkpointing)

Survives a server restart or a laptop closing, and closes an existing gap.

**Current state:** `checkpoint_enabled` is a **no-op in the API path**.
Checkpointing lives inside `TradingAgentsGraph.propagate()`, but
`apps/api/jobs/runner.py` calls `graph.graph.stream(init_state, **args)`
directly; `stream_args.config` is only `{recursion_limit, callbacks}`. The UI
toggle does nothing (TASKS.md §7).

Fork-side, no core edits:
1. In `graph_factory`, when `checkpoint_enabled`: hold
   `get_checkpointer(config["data_cache_dir"], ticker)` for the run's life,
   `graph.graph = graph.workflow.compile(checkpointer=saver)`, and inject
   `thread_id(ticker, str(date), graph._run_signature(asset_type))` into
   `args["config"]["configurable"]["thread_id"]`. Reusing `_run_signature()`
   prevents resuming the wrong graph shape after an analyst/depth change
   (upstream #1089).
2. New `runs` columns: `checkpoint_thread_id`, `run_signature`, `resume_count`,
   `last_completed_node`.
3. `POST /api/runs/{id}/resume` rebuilds with the identical signature and
   streams again; reject if a freshly computed signature differs.
4. On process start, sweep `running` → `interrupted` (not `failed`) and offer
   Resume.

**Required for resume to be real:** byte-identical `request` JSON (already in
`config_json`), `checkpoint_thread_id` + `run_signature`, `user_id` (routes to
the right helper), the per-chunk `final_state` snapshot (already — a partial
report survives even if the checkpoint is unusable), and `resume_count` capped
at 3 so a poison run cannot loop.

### 4.4 Provider-agnostic vs provider-specific

**Provider-agnostic (nearly everything):** the relay protocol, correlation,
ack/deadline/reconnect logic, the idempotency cache and its key, all
checkpointing and resume work, the job store, SSE translation to the frontend,
cancellation, the Chat Completions renderer, and the inbound parser.

**Provider-specific (only two places):** the `UpstreamAdapter` (request
building + response decoding) and the `CredentialSource`. Both are single
modules behind Protocols.

Consequence: **M4 and M5 need no revisiting when a provider is added.** That is
the payoff of normalising in the middle, and the reason to do it before M5
rather than after.

### 4.5 Not recoverable

A mid-call interruption loses that call's output — no server-side state, by
construction. Cost is one call's tokens unless the idempotency cache hits.
Acceptable; pretending otherwise would be the mistake.

---

## 5. Repo integration — zero core edits

1. `llm_provider="openai_compatible"`,
   `backend_url="http://127.0.0.1:8899/v1/codex"`,
   `shallow_thinker=deep_thinker="gpt-5.6-sol"`.
   `openai_compatible` has `require_base_url=True`, `key_optional=True`,
   `chat_class=LocalCompatibleChatOpenAI`; no warning fires (§0a); and
   `use_responses_api` stays off because that spec never enables it.
2. **`api_key` injection** (a relay/session token — the helper holds the real
   credential). `api_key` *is* in `_PASSTHROUGH_KWARGS` but
   `_get_provider_kwargs()` never populates it:

   ```python
   # apps/api/integrations/graph_factory.py — fork-only wrap
   class HelperBackedGraph(TradingAgentsGraph):
       """Forward config['api_key'] to the LLM client.

       Upstream reads keys from os.environ only. Per-user keys must not go
       through the environment: WEBAPP_CONCURRENCY>1 shares one process, so
       one user's key would leak into another's run.
       """
       def _get_provider_kwargs(self):
           kwargs = super()._get_provider_kwargs()
           if key := self.config.get("api_key"):
               kwargs["api_key"] = key
           return kwargs
   ```
   Record in `FORK_PATCHES.md` as a wrap, not a patch.
3. **Reasoning effort per tier.** 10 of 12 agents use `quick_thinking_llm`;
   only Research Manager and Portfolio Manager use `deep_thinking_llm`. The
   user's `config.toml` sets `high`. Default quick → `low`, deep → `high`: on
   the probe, 63 of 105 output tokens were reasoning tokens, making this the
   dominant cost lever.
4. **Config endpoint.** `apps/api/routes/config.py` `_PROVIDERS` lacks
   `openai_compatible` (10 of upstream's 17 — TASKS.md §7). Add it with a
   `gpt-5.6-sol` option; the catalog only offers
   `("Custom model ID", "custom")` there.
5. `temperature` stripping is the helper's job (`quirks.strip_params`), not the
   fork's — `_get_provider_kwargs()` forwards it whenever
   `config["temperature"]` is set and `.env.example` invites setting it.

---

## 6. Testing without burning quota

**Fixtures.** Capture one real SSE transcript per scenario **once** (text-only,
single tool call, multi tool call, structured-output-as-tool, 400 bad model,
429, mid-stream abort) into `tests/fixtures/codex_sse/*.txt`, tokens scrubbed.
All translation tests replay from disk.

**Fake upstream.** A `pytest` fixture serving those fixtures over a local
`http.server`, exercising the helper end-to-end — headers, refresh, assembly,
error mapping — with no real calls.

**Unit tests (no network):**
- inbound parse → `NormalizedRequest` for every §2.3 edge case
- `CodexResponsesAdapter` request building, incl. `mandatory_body` overriding
  caller values and `strip_params` removing `temperature`
- SSE → `NormalizedResponse` per fixture; truncated stream → 502
- renderer: `NormalizedResponse` → Chat Completions, `content:null` not `""`
- credential tiers: expiry maths, single-flight refresh, keychain/file
  fallback, and **assert Codex's `auth.json` is never opened for writing**
- registry: unknown provider path → 404; unknown model → 400 listing valid ones
- idempotency: identical request → one upstream call
- relay: correlation, deadline, reconnect replay, cancel resolves futures

**Contract test with real langchain (no network).** Point
`LocalCompatibleChatOpenAI` at the fake upstream and assert
`bind_structured(llm, PortfolioDecision, "PM").invoke(...)` returns a populated
model. This proves the §0b tool-based structured-output path end to end and is
worth more than any other single test.

**Seam test.** A 30-line `EchoAdapter` + `StaticCredentials` registered under
`/v1/echo` proves a new provider needs one module and one registry entry, and
guards the seam against erosion. Cheap, and it is the executable version of the
architectural claim.

**Needs live calls (3 only, `@pytest.mark.live`, skipped by default, ~200
tokens each):** auth smoke; one custom function tool honoured; refresh grant
returns a usable token.

---

## 7. Open unknowns and the cheapest experiment for each

| # | Unknown | Experiment |
|---|---|---|
| U1 | Is a redirect URI other than `http://localhost:1455/auth/callback` allowed? Blocks `OwnOAuth`, and 1455 collides with a live `codex login`. | Build the authorize URL with port 1456 and open it — an unregistered URI errors on OpenAI's page before any token is issued. Free. **Do first.** |
| U2 | Must encrypted reasoning items be echoed back on later tool-loop turns? | One two-turn run (tool call → result → second call), with and without the reasoning item. ~400 tokens. Decides §2.3.9. |
| U3 | Does the refresh grant rotate the refresh token, invalidating Codex CLI's copy? | Refresh with a copy, compare the returned `refresh_token`. If rotated, `CodexAuthFile` stays read-only-until-expiry then hands off. |
| U4 | Rate limits for a 12-agent run on Pro. | One depth-1 single-analyst run; count 429s. |
| U5 | Do `gpt-5.6-terra` / `gpt-5.6-luna` work? Would give a cheap quick-thinker and cut cost sharply. | Repeat the 400-probe per name; ~0 tokens on rejection. |

---

## 8. File layout

```
apps/helper/
  pyproject.toml                   # standalone installable: `ta-helper`
  ta_helper/
    __main__.py                    # serve | login | status | logout | providers
    server.py                      # /v1/{provider}/chat/completions, /status,
                                   #   /v1/providers, /healthz
    registry.py                    # PROVIDERS dict, Provider, ProviderQuirks
    normalize.py                   # NormalizedRequest/Response dataclasses
    inbound.py                     # Chat Completions -> NormalizedRequest
    render.py                      # NormalizedResponse -> Chat Completions
    adapters/
      base.py                      # UpstreamAdapter Protocol
      codex_responses.py           # day one
      openai_chat.py               # day one (API-key passthrough)
      # anthropic_messages.py      # later: one module, one registry entry
    credentials/
      base.py                      # CredentialSource Protocol, chain()
      codex_file.py                # read-only
      own_oauth.py                 # parameterised PKCE + refresh
      api_key.py
      store.py                     # keychain + 0600 file fallback
    idempotency.py
    relay_client.py                # M5 only
tests/helper/
docker-compose.local.yml           # api+web -> host.docker.internal:8899
```

**Language: Python.** Reuses this repo's toolchain, test suite and idioms, and
the translation logic resembles code already here. A TS/Bun helper would ship a
smaller binary but adds a second toolchain for one component — worth it only if
the helper must be distributed as a signed desktop artifact to non-Python
users, which is a packaging decision, not an implementation one.

---

## 9. Milestones

| # | Deliverable | Est. | Δ |
|---|---|---|---|
| M0 | U1 + U5 probes; fixture capture harness | 0.5 d | — |
| M1 | Normalize/inbound/render, registry + quirks, `CodexResponsesAdapter`, `OpenAIChatCompletionsAdapter`, `CodexAuthFile` + `ApiKey`, fake-upstream tests, `/status`, seam test | **1.5 d** | +0.5 d |
| M2 | Wire the local pipeline: subclass, `openai_compatible`, `docker-compose.local.yml`, one real end-to-end analysis on the Pro subscription | 0.5 d | — |
| M3 | `OwnOAuth` PKCE, keychain store, single-flight refresh | 1 d | — |
| M4 | Run-level checkpoint resume (§4.3) — closes the TASKS.md §7 no-op | 1.5 d | — |
| M5 | Hosted relay: WS endpoint, registry, loopback shim, cancellation, idempotency | 2.5 d | — |

**Estimate change, stated explicitly:** M1 grows **1 d → 1.5 d** (+0.5 d) for
the normalization layer, registry, quirks record and seam test. Every other
milestone is unchanged: M4 and M5 are provider-agnostic (§4.4), so the extra
structure costs nothing there and saves a rewrite when the second provider
lands. **M0–M2 is now ~2.5 days** (was ~2) to reach real analyses running on
the ChatGPT subscription locally.

Recommended stop-and-review after M2.
