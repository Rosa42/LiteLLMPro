# Protocol-Aware Multi-API Gateway Plan

**Target path:** `docs/protocol-aware-multi-api-gateway-plan.md`  
**Status:** Active design — **implementation in progress**  
**Scope:** `E:\LiteLLMPro\local-llm-router`  
**Pinned LiteLLM version:** `v1.90.5`  
**Task tracker:** `docs/tasks.md` (authoritative for Done/TODO)  
**Last progress update:** 2026-07-26

## Implementation status (summary)

| Layer | Status | Notes |
|-------|--------|-------|
| Phase 0 contracts + G0 ADR | **Done** | G0-B metadata integration |
| M1 domain + config + generator + discovery | **Done** | See `docs/tasks.md` §0 |
| M2 capability-aware routing | **Not started** | Core production gap |
| M3 endpoint gates | **Not started** | Chat works; Messages/Responses not gated |
| M4 ops / MVP-GATE | **Not started** | Partial E2E for M1 only |
| Post-MVP conversion (C1–C5) | **Blocked** | After MVP-GATE only |

Key live evidence:

- Phase 0 report: `docs/phase-reports/protocol-gateway-phase-0-compatibility.md`
- ADR: `docs/adr/ADR-protocol-gateway-integration-boundary.md`
- Discovery: `docs/model-capability-discovery.md`
- M1 E2E: `docs/phase-reports/e2e-verification-m1.md`

## Revision Note

Oracle rated the previous draft `PARTIAL`: its goals and hard constraints were
sound, but it overstated what a LiteLLM v1.90.5 custom routing strategy can
observe and control. This revision makes the MVP direct-only, defers Responses
until a verified deployment exists, treats `metadata.protocol` as a candidate
mechanism requiring contract proof, and moves conversion into post-MVP scope.
If metadata does not survive the request path, the implementation must use a
project-owned front gateway or isolated internal protocol lanes.

## 1. Executive Summary

The local router currently exposes an OpenAI-compatible gateway, but its generated LiteLLM configuration hard-codes every upstream deployment as `openai/<model>`. The custom routing layer also has no protocol capability metadata.

That combination caused a Claude `/v1/messages` request to be routed to an OpenCode Go deployment through `/v1/responses`. The upstream returned `404`, and subsequent routing found no usable candidates. The failure was not caused by quota state. It was caused by selecting a deployment that could not serve the requested public protocol.

This plan targets a protocol-aware public gateway with three opt-in endpoints:

- `/v1/chat/completions`
- `/v1/responses`
- `/v1/messages`

The gateway keeps the logical model name independent from the public protocol and upstream provider. Each logical model explicitly declares which public protocols it supports. Each deployment declares which upstream protocols it can serve and which directional conversions are allowed.

Routing follows these rules:

1. Parse and validate the requested public protocol.
2. Resolve the logical model without inferring protocol from the model or provider name.
3. Filter candidates by protocol capability before quota reservation.
4. Prefer same-protocol upstream passthrough.
5. Use an explicitly configured directional conversion only when the conversion is supported and fidelity-safe.
6. Do not use implicit cross-model fallback.
7. Preserve the existing distinction between model groups and quota groups.
8. Preserve stream and retry invariants, especially the prohibition on switching after visible stream output.
9. Keep LiteLLM v1.90.5 integration limited to verified extension points. Do not assume unsupported endpoint hooks or protocol-specific router callbacks exist.

The MVP exposes only endpoints backed by verified direct deployments. Chat
Completions is enabled for current OpenCode Go and Volc deployments. Anthropic
Messages is enabled only after Phase 0 verifies at least one direct
Anthropic-compatible deployment. `/v1/responses` returns a controlled
not-enabled/no-route response until a verified Responses deployment exists.
Cross-protocol conversion is post-MVP and disabled by default.

## 2. Goals

### 2.1 Primary goals

1. Expose the following public protocols through one gateway:
   - OpenAI Chat Completions
   - OpenAI Responses
   - Anthropic Messages

2. Keep the logical model namespace stable and protocol-neutral.

3. Prevent a request from reaching an upstream deployment that cannot serve the requested protocol.

4. Prefer same-protocol passthrough over conversion.

5. Support explicit directional conversion only when:
   - The direction is configured.
   - The upstream capability is declared.
   - The request feature set is supported.
   - The response can be represented without unsafe loss.
   - Streaming behavior is proven safe.

6. Run protocol capability filtering before quota lease acquisition or quota reservation.

7. Preserve existing shared-quota behavior:
   - Same logical model candidates are grouped by `model_group`.
   - Account-level quota and circuit isolation use `quota_group_id`.
   - One failed or exhausted account can affect all deployments in that quota group.
   - Different quota groups remain eligible according to routing policy.

8. Preserve existing stream and retry invariants:
   - No transparent upstream switch after the first visible byte.
   - At most one attempt per quota group for one request.
   - At most three distinct quota groups for one request.
   - No cross-account retry for client errors such as bad requests or content policy failures.

9. Make protocol support opt-in per logical model rather than globally assumed.

10. Keep secrets out of configuration examples, generated documentation, logs, and test fixtures.

### 2.2 Secondary goals

1. Provide clear diagnostics when a logical model exists but has no deployment capable of serving the requested protocol.

2. Distinguish protocol mismatch from:
   - Unknown logical model.
   - Disabled quota group.
   - Exhausted quota group.
   - Provider outage.
   - Deployment cooldown.
   - Invalid request.

3. Make future protocol support additive without changing the logical model contract.

4. Make conversion decisions observable without logging prompts, authorization headers, or API keys.

## 3. Non-Goals

The first version does not attempt to:

1. Infer protocol support from:
   - Model names.
   - Provider names.
   - URL patterns.
   - Arbitrary upstream error messages.

2. Probe upstream endpoints at runtime to discover supported protocols.

3. Treat a `404` as proof that a protocol is unsupported. A `404` may be a route, model, deployment, or provider error and must remain classified according to the existing error policy unless a separate verified capability source exists.

4. Implement default cross-model fallback.

   For example, a request for `kimi-k3` must not silently fall back to `glm-5.2` unless the caller or an explicit policy enables that behavior.

5. Implement quota collectors, browser or Cookie login flows, private balance pages, or predicted remaining quota.

6. Add business logic to the LiteLLM upstream submodule.

7. Depend on undocumented LiteLLM v1.90.5 extension hooks.

8. Guarantee semantic equivalence for unsupported conversion features.

9. Retry after visible stream output.

10. Merge partial output from two models or two protocols into one response.

11. Expose provider credentials or upstream API keys through public responses, logs, metrics, or generated configuration content.

12. Make every model available on every public endpoint by default.

## 4. Verified Current State

This section records facts observed in the repository. It distinguishes current behavior from the target design.

### 4.1 Version pin

`config/versions.env` pins:

```text
LITELLM_VERSION=v1.90.5
LITELLM_GIT_SHA=0430743f2fd4005898506e00bc62dd47bcff6fc9
```

The repository explicitly prohibits production defaults based on `latest`, `main`, `nightly`, `rc`, or `dev`.

### 4.2 Current public gateway

`scripts/llm-router.ps1` starts LiteLLM on:

```text
http://127.0.0.1:4000
```

The client base URL is:

```text
http://127.0.0.1:4000/v1
```

The current smoke command sends requests to:

```text
POST /v1/chat/completions
```

The script configures the LiteLLM process with:

```text
--config config/litellm.yaml
--port 4000
--host 127.0.0.1
```

The current default binding is local-only, which should remain the default for the first implementation.

### 4.3 Current plan declarations

`config/plans.yaml` declares plans with:

- `id`
- `display_name`
- `provider_id`
- `priority`
- `base_url_env`
- `api_key_env`
- `models`

Current examples include:

- `opencode-a`
- `volc-c`
- `newapi-a`

The plan file stores environment variable names for credentials. It does not store credential values in the plan declaration.

### 4.4 Current generated LiteLLM configuration

`config/litellm.yaml` is generated by `scripts/llm-router.ps1 apply`.

Each deployment currently includes:

```yaml
model_name: <logical-model>
model_info:
  deployment_id: <deployment-id>
  provider_id: <provider-id>
  account_id: <quota-group>
  quota_group_id: <quota-group>
  priority: <priority>
litellm_params:
  model: openai/<model>
  api_base: os.environ/<base-url-env>
  api_key: os.environ/<api-key-env>
  timeout: 300
```

The generator hard-codes:

```text
model: openai/<model>
```

This is the central protocol-awareness gap.

The generated configuration currently has:

```yaml
router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  allowed_fails: 1
  cooldown_time: 30
  retry_after: 1
```

It also registers the shared quota callback:

```yaml
litellm_settings:
  callbacks:
    - shared_quota_callback.callback_instance
  drop_params: true
```

### 4.5 Current model and quota grouping

`plugins/shared_quota_router/models.py` defines:

- `Deployment.model_group`
- `Deployment.quota_group_id`
- `Deployment.provider_id`
- `Deployment.upstream_model`
- `Deployment.priority`
- `Deployment.enabled`

The file also defines `RequestRoutingContext` with these invariants:

- A quota group may be tried only once per request.
- A request may try at most three distinct quota groups.
- Once `first_byte_sent` is true, cross-deployment selection is forbidden.

### 4.6 Current registry behavior

`plugins/shared_quota_router/registry.py` indexes deployments by:

- Deployment ID.
- `model_group`.
- `quota_group_id`.

`deployment_from_model_entry()` obtains:

- The logical model from `model_name`.
- The upstream model from `litellm_params.model`.
- The quota group from `model_info.quota_group_id` or `account_id`.
- The provider from `model_info.provider_id`.

There is currently no protocol capability field in `Deployment`.

### 4.7 Current routing behavior

`plugins/shared_quota_router/strategy.py` filters deployments by:

1. Logical model group.
2. Deployment enabled state.
3. Request retry context.
4. Provider state.
5. Quota group state.
6. Deployment cooldown.

It then ranks candidates by:

1. Affinity.
2. Priority.
3. In-flight quota-group load.
4. Recent success.
5. Deployment ID.

Quota lease acquisition occurs during selection. The target design must insert protocol capability filtering before this reservation step.

The strategy currently uses the LiteLLM v1.90.5 custom routing contract:

```python
get_available_deployment(
    model,
    messages=None,
    input=None,
    specific_deployment=False,
    request_kwargs=None,
)
```

The strategy returns one dictionary from `router.model_list`.

### 4.8 Current callbacks and stream behavior

`plugins/shared_quota_router/callbacks.py` implements LiteLLM `CustomLogger`-compatible hooks.

The callback:

- Marks the first stream event as the first visible byte boundary.
- Refuses cross-deployment retry after the first byte.
- Releases quota leases on success or failure.
- Classifies upstream failures.
- Marks high-confidence shared quota exhaustion at the quota-group level.
- Uses deployment cooldown for ordinary deployment failures.
- Does not treat every `429` as shared quota exhaustion.
- Avoids overriding `async_post_call_streaming_hook` because returning the accumulated string would corrupt the stream format.

These behaviors must remain intact.

### 4.9 Current LiteLLM extension points

`docs/extension-points-v1.90.5.md` verifies:

- `CustomRoutingStrategyBase`.
- `router.set_custom_routing_strategy(strategy_instance)`.
- `LITELLM_WORKER_STARTUP_HOOKS`.
- The delayed registration approach used by `shared_quota_router.bootstrap`.

The verified registration path is:

```text
shared_quota_router.bootstrap:register_proxy_startup
```

LiteLLM uses a proxy-internal `route_type` for `acompletion`, `aresponses`, and
`anthropic_messages`, but that value is not part of the verified custom strategy
contract. The preferred candidate carrier is a serializable
`metadata.protocol` string set by the endpoint adapter. Phase 0 must prove that
it reaches `get_available_deployment()` and callbacks for every enabled
endpoint. `messages` versus `input` is validation evidence only, never the
authoritative protocol signal.

### 4.10 Current registration

`plugins/shared_quota_router/bootstrap.py`:

- Builds Redis-backed state.
- Builds the shared quota strategy.
- Registers the strategy through `router.set_custom_routing_strategy()`.
- Exposes `callback_instance` for LiteLLM logging callbacks.
- Uses a fail-closed Redis substitute when Redis is unavailable.

### 4.11 Verified per-deployment upstream protocol facts

The current `config/plans.yaml` exposes the following logical-model deployments through `config/litellm.yaml`:

| Logical model examples | Plan | Provider | Verified upstream protocol |
|---|---|---|---|
| `kimi-k3`, `kimi-k2.7-code`, `kimi-k2.6`, `kimi-k2.5`, `glm-5.2`, `glm-5.1`, `glm-5`, `minimax-m3`, `minimax-m2.7`, `minimax-m2.5`, `deepseek-v4-pro`, `deepseek-v4-flash`, `qwen3.7-max`, `qwen3.7-plus`, `qwen3.6-plus`, `qwen3.5-plus`, `mimo-v2-pro`, `mimo-v2-omni`, `mimo-v2.5-pro`, `mimo-v2.5`, `hy3-preview`, `grok-4.5` | `opencode-a` | `opencode-go` | `openai_chat` |
| `ark-code-latest`, `glm-5.2`, `glm-latest`, `doubao-seed-2.0-code`, `doubao-seed-2.0-pro`, `doubao-seed-2.0-lite` | `volc-c` | `volcengine` | `openai_chat` |
| `claude-opus-4-8`, `claude-fable-5` | `newapi-a` | `newapi` | Unverified; must be determined by Phase 0 provider contract tests |

Consequences for the design:

1. OpenCode Go deployments are Chat-Completions only. They must not be marked as `openai_responses` in the generated configuration or in documentation examples.
2. Volc Coding Plan deployments are Chat-Completions only in this version. The provider prefix used by LiteLLM (`openai/`) is not evidence of Responses support.
3. NewAPI A carries Claude-named logical models, but model names do not prove
   Anthropic compatibility. It becomes a direct `/v1/messages` candidate only
   after its endpoint, authentication, request, response, tool, usage, and
   streaming contracts are verified.
4. No deployment in the current plan set is verified to speak OpenAI Responses. The first release must keep `/v1/responses` opt-in only for logical models with a deployment whose capability is explicitly declared.

## 5. Problem Statement

The gateway currently treats all deployments as OpenAI protocol deployments because the generator writes:

```yaml
model: openai/<model>
```

This is not a safe representation of the public API surface.

A logical model name identifies a model selection contract. It does not identify:

- The public protocol requested by the client.
- The upstream protocol supported by a provider.
- Whether the request can be converted.
- Whether streaming output can be converted without loss.
- Whether tools, structured output, reasoning content, citations, or usage fields are representable.

The observed bug surface was:

```text
Client:
  POST /v1/messages

Gateway:
  selected logical model deployment
  treated deployment as OpenAI Responses-compatible
  sent request using an incompatible path

Upstream call:
  incompatible route was attempted and happened to return 404
```

The `404` was a symptom, not capability evidence. The corrected behavior must
reject the deployment during capability filtering, before quota reservation and
before an incompatible upstream call. If an upstream call returns `404`, the
existing classifier remains authoritative; status code alone is not a protocol
capability signal.

## 6. Design Principles

### 6.1 Protocol is request metadata

The requested public protocol is derived from the endpoint and request parser:

```text
/v1/chat/completions -> openai_chat
/v1/responses        -> openai_responses
/v1/messages         -> anthropic_messages
```

It must not be inferred from:

- `model`.
- `provider_id`.
- `api_base`.
- A model name containing `claude`, `gpt`, `glm`, `kimi`, or another provider-specific string.

The endpoint adapter establishes the protocol before LiteLLM request
normalization. `metadata.protocol` is the preferred candidate carrier, subject
to Phase 0 contract verification. If it does not survive, the design must use a
project-owned front gateway or isolated protocol lanes; payload-shape inference
is not an acceptable fallback.

### 6.2 Logical model is protocol-neutral

The logical model is the stable client-visible identifier, such as:

```text
glm-5.2
claude-opus-4-8
kimi-k3
```

The logical model does not determine the endpoint. A logical model may opt into one, two, or all three public protocols, subject to explicit deployment capability and conversion policy.

### 6.3 Same-protocol passthrough wins

If a deployment can serve the requested public protocol directly, it must rank ahead of a deployment requiring conversion, all other routing factors being equal.

Direct means:

```text
public protocol == upstream protocol
```

The gateway may still normalize headers, authentication, request IDs, or internal metadata, but it must not translate the protocol payload.

### 6.4 Conversion is directional

Conversions are directional capabilities:

```text
openai_chat -> anthropic_messages
anthropic_messages -> openai_chat
openai_responses -> openai_chat
openai_chat -> openai_responses
```

The presence of one direction does not imply the reverse direction.

A conversion entry must identify:

- Source protocol.
- Target protocol.
- Supported request features.
- Supported response features.
- Streaming support.
- Tool-call behavior.
- Usage behavior.
- Error mapping behavior.
- Fidelity classification.

### 6.5 Fidelity safety before availability

A deployment is not eligible merely because a converter exists. It is eligible only if the converter can safely represent the request's feature set.

If the request contains unsupported features, the candidate must be filtered out before quota reservation.

Each feature gate also has a fidelity class: `equivalent`, `lossy_safe`,
`lossy_unsafe`, or `unsupported`. Examples of feature gates include:

- Tools.
- Parallel tool calls.
- Structured output.
- JSON schema response format.
- Reasoning blocks.
- Images or other multimodal content.
- Prompt caching fields.
- Citations.
- Background or asynchronous response controls.
- Provider-specific extensions.
- Streaming events.

For post-MVP conversion, reasoning is conservatively `lossy_unsafe` between
Chat Completions and Anthropic Messages until proven otherwise. Prompt caching
is `unsupported` across conversion in the initial design. Capability validation
must reject semantically required unsupported features before `drop_params:
true` can silently discard them. After compatibility testing, strict
protocol-aware deployments should prefer `drop_params: false`.

### 6.6 No silent semantic downgrade

If a conversion would drop a field or change semantics, the gateway must either:

1. Reject the request with a clear client error, or
2. Use a direct-capable deployment.

It must not silently remove important fields.

### 6.7 Quota semantics remain separate from protocol semantics

`model_group` answers:

> Which deployments serve this logical model?

`quota_group_id` answers:

> Which account-level quota and circuit state applies to this deployment?

Protocol filtering narrows the deployment set. It must not merge, split, or redefine quota groups.

## 7. Target Architecture

```text
Client
  |
  |  POST /v1/chat/completions
  |  POST /v1/responses
  |  POST /v1/messages
  v
Public Protocol Adapters
  |
  |  Parse endpoint-specific request
  |  Normalize to internal RequestEnvelope
  v
Protocol-Aware Routing Context
  |
  |  logical_model
  |  requested_protocol
  |  feature_set
  |  streaming
  |  conversion_policy
  v
Deployment Registry
  |
  |  model_group match
  |  protocol capability filter
  |  feature fidelity filter
  |  provider state filter
  |  quota-group state filter
  |  deployment cooldown filter
  v
Quota Reservation
  |
  |  lease one quota group
  v
Routing Strategy
  |
  |  direct same-protocol preferred
  |  explicit conversion next
  |  priority, affinity, load, recent success
  v
Upstream Adapter
  |
  |  direct passthrough or directional conversion
  v
Response Adapter
  |
  |  target protocol response
  v
Client
```

### 7.1 Components

#### Public protocol adapters

These identify the requested protocol and validate the endpoint-specific request shape.

They should produce a common internal envelope without changing the logical model:

```python
@dataclass(slots=True)
class RequestEnvelope:
    logical_model: str
    public_protocol: PublicProtocol
    features: frozenset[RequestFeature]
    stream: bool
    payload: dict[str, Any]
    request_id: str
    client_metadata: dict[str, Any]
```

The payload remains protocol-specific until the selected route is known. Direct
dispatch is fragmented across LiteLLM protocol entry points: Chat uses the
completion path, Responses uses the Responses path, and Messages uses the
Anthropic Messages path. Phase 0 must verify the exact v1.90.5 signatures,
model-entry selection, callback metadata, and streaming hooks. A custom strategy
returning one `model_list` entry cannot by itself switch protocol entry points.

#### Protocol capability registry

The registry records capabilities declared by configuration. It must not probe upstreams.

Capabilities belong to deployments or deployment-level route descriptors, not to model names.

#### Protocol-aware selector

The selector performs protocol and feature filtering before lease acquisition.

Its input is:

```text
logical model
requested public protocol
request feature set
streaming flag
request routing context
```

Its output is a deployment plus route mode:

```text
selected deployment
direct or conversion
conversion direction, if any
```

#### Conversion layer (post-MVP only)

The conversion layer performs explicit transformations only for configured route pairs.

It must support:

- Request conversion.
- Response conversion.
- Streaming event conversion.
- Error conversion.
- Usage conversion.
- Tool-call conversion.

A conversion that supports non-streaming requests must not automatically be considered valid for streaming requests. No conversion code or conversion
configuration is part of the MVP.

#### Existing shared quota state

The existing Redis-backed state store remains responsible for:

- Provider status.
- Quota-group status.
- Deployment cooldown.
- Request routing context.
- Affinity.
- Lease state.

Protocol capability state is configuration state and should be loaded with the deployment registry. It should not be dynamically invented from upstream responses.

## 8. Configuration Schema

The configuration should evolve from plan-only declarations to plan declarations plus explicit protocol metadata.

The schema below is illustrative. It contains no credentials.

### 8.0 Naming glossary

- `endpoint_path`: public HTTP route.
- `public_protocols`: protocols explicitly exposed for one logical model.
- `upstream_protocol`: one deployment's native protocol.
- `response_format`: response contract associated with an endpoint.

All protocol fields use `openai_chat`, `openai_responses`, or
`anthropic_messages`.

### 8.1 Public protocol names

Use stable internal enum values:

```yaml
protocols:
  - openai_chat
  - openai_responses
  - anthropic_messages
```

These names are internal gateway identifiers. Endpoint mapping is fixed and explicit.

### 8.2 Endpoint mapping

```yaml
public_protocols:
  openai_chat:
    endpoint: /v1/chat/completions
    response_format: openai_chat
  openai_responses:
    endpoint: /v1/responses
    response_format: openai_responses
  anthropic_messages:
    endpoint: /v1/messages
    response_format: anthropic_messages
```

The endpoint mapping should be implemented as code-level constants or a validated configuration section. It must not be inferred from model names.

### 8.3 Plan-level defaults

A plan may provide defaults for its deployments, but defaults must be explicit:

```yaml
plans:
  - id: opencode-a
    display_name: OpenCode Go A
    provider_id: opencode-go
    priority: 10
    base_url_env: OPENCODE_GO_BASE_URL
    api_key_env: OPENCODE_GO_KEY_A

    upstream_defaults:
      protocol: openai_chat
      streaming: true
      features:
        - text
        - tools
        - reasoning

    models:
      - kimi-k3
      - glm-5.2
```

The plan-level protocol is a declared upstream fact. It must not be generated
from `provider_id`, and it never grants public exposure. Exposure requires an
explicit `logical_models[].public_protocols` entry.

### 8.4 Per-model declarations

A logical model must opt into public protocols:

```yaml
logical_models:
  - name: glm-5.2
    public_protocols:
      - openai_chat
    allow_conversion: false
```

For an Anthropic-compatible logical model:

```yaml
  - name: claude-opus-4-8
    public_protocols:
      - anthropic_messages
    allow_conversion: false
```

If a logical model supports a public protocol only through conversion, that must be explicit:

```yaml
  - name: claude-opus-4-8
    public_protocols:
      - anthropic_messages
      - openai_chat
    conversion_policy:
      allowed:
        - from: openai_chat
          to: anthropic_messages
          fidelity: safe_for_text_tools_non_streaming
```

The logical model declaration controls public availability. The deployment declaration controls actual upstream capability.

### 8.5 Deployment-level declarations

A generated deployment should carry protocol metadata in `model_info` or a dedicated internal registry structure.

For a direct Chat-Completions deployment:

```yaml
- model_name: glm-5.2
  model_info:
    deployment_id: opencode-a-glm-5.2
    provider_id: opencode-go
    account_id: opencode-a
    quota_group_id: opencode-a
    priority: 10

    protocol:
      upstream: openai_chat
      capabilities:
        streaming: true
        features:
          - text
          - tools
          - reasoning
      conversions: []

  litellm_params:
    model: openai/glm-5.2
    api_base: os.environ/OPENCODE_GO_BASE_URL
    api_key: os.environ/OPENCODE_GO_KEY_A
    timeout: 300
```

For a direct Anthropic deployment:

```yaml
- model_name: claude-opus-4-8
  model_info:
    deployment_id: newapi-a-claude-opus-4-8
    provider_id: newapi
    account_id: newapi-a
    quota_group_id: newapi-a
    priority: 30

    protocol:
      upstream: anthropic_messages
      capabilities:
        streaming: true
        features:
          - text
          - tools
      conversions: []
```

The `litellm_params.model` provider prefix remains a LiteLLM integration detail. It must not be used as the source of public protocol truth.

### 8.6 Conversion schema (post-MVP; not implemented in the first version)

```yaml
protocol:
  upstream: openai_chat
  capabilities:
    streaming: true
    features:
      - text
      - tools
  conversions:
    - from: openai_chat
      to: anthropic_messages
      fidelity: safe_for_text_tools_non_streaming
      streaming: false
      features:
        request:
          - text
          - tools
        response:
          - text
          - tool_calls
          - usage
```

The configuration validator must reject:

- Duplicate conversion directions.
- Conversions whose source does not match a supported public or internal protocol.
- `streaming: true` without a tested streaming adapter.
- `fidelity` values outside the supported enum.
- Public model protocol opt-in without a route path.
- A route declaring a conversion while `allow_conversion` is false.
- A logical model listing a public protocol without a direct or explicit
  converted route.
- A conversion whose target protocol has no capable deployment.
- A logical model with no `public_protocols` declaration being exposed
  implicitly.

### 8.7 Recommended first schema transition

The first implementation may preserve `plans.yaml` compatibility by adding optional fields:

```yaml
  - id: opencode-a
    display_name: OpenCode Go A
    provider_id: opencode-go
    priority: 10
    base_url_env: OPENCODE_GO_BASE_URL
    api_key_env: OPENCODE_GO_KEY_A
    upstream_protocol: openai_chat
    upstream_features:
      - text
      - tools
      - reasoning
    models:
      - kimi-k3
      - glm-5.2
```

A separate `config/protocols.yaml` may be preferable if protocol policy should not be mixed with account credentials and plan membership.

The generator must fail closed when protocol metadata is missing for a deployment that is intended to serve a protocol-aware public endpoint. It must not silently default every deployment to OpenAI Chat.

## 9. Internal Capability Model

### 9.1 Protocol enum

```python
class PublicProtocol(str, Enum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"
```

### 9.2 Request features

```python
class RequestFeature(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    TOOLS = "tools"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    STRUCTURED_OUTPUT = "structured_output"
    JSON_SCHEMA = "json_schema"
    REASONING = "reasoning"
    CITATIONS = "citations"
    PROMPT_CACHE = "prompt_cache"
    STREAMING = "streaming"
```

The actual enum can be smaller initially. The important requirement is that the selector can distinguish a simple text request from a request whose semantics may not survive conversion.

### 9.3 Upstream capability

```python
@dataclass(frozen=True, slots=True)
class UpstreamCapability:
    protocol: PublicProtocol
    features: frozenset[RequestFeature]
    streaming: bool
```

### 9.4 Conversion capability (post-MVP)

```python
@dataclass(frozen=True, slots=True)
class ConversionCapability:
    source: PublicProtocol
    target: PublicProtocol
    request_features: frozenset[RequestFeature]
    response_features: frozenset[RequestFeature]
    streaming: bool
    fidelity: str
```

The names `source` and `target` must be defined consistently. Recommended convention:

```text
source = public protocol received by the gateway
target = upstream protocol sent to the deployment
```

For response conversion, the adapter reverses the direction internally when converting the upstream result back to the public response format.

### 9.5 Route mode

```python
class RouteMode(str, Enum):
    DIRECT = "direct"
    CONVERT = "convert"
```

### 9.6 Deployment extension

The current `Deployment` model should gain protocol metadata without changing model and quota semantics:

```python
@dataclass(slots=True)
class Deployment:
    deployment_id: str
    model_group: str
    upstream_model: str
    provider_id: str
    quota_group_id: str
    priority: int = 100
    weight: int = 1
    enabled: bool = True
    api_base: str | None = None
    api_key_env: str | None = None
    protocol_capability: UpstreamCapability | None = None
    conversions: tuple[ConversionCapability, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)
```

Missing capability metadata must not be treated as universal support.

## 10. Routing Algorithm

### 10.1 Request processing

For every public request:

1. Determine the public protocol from the endpoint.
2. Parse the request into a protocol-specific request object.
3. Extract the logical model.
4. Extract request features.
5. Determine whether streaming is requested.
6. Resolve the request routing context.
7. Resolve deployments by `model_group`.
8. Filter by public model protocol opt-in.
9. Filter by direct capability in the MVP; post-MVP may add explicit conversion capability.
10. Filter by request features and streaming compatibility.
11. Filter by provider and deployment state.
12. Filter by quota-group state.
13. Rank eligible direct routes; post-MVP ranks direct ahead of conversion.
14. Acquire a quota lease only for the selected eligible deployment.
15. Dispatch through the verified protocol-specific LiteLLM entry point.
16. Post-MVP only: convert the response when an explicit adapter is selected.
17. Record success or failure through existing callbacks.
18. Release the lease.

### 10.2 Candidate filtering order

The required order is:

```text
logical model
  -> public protocol opt-in
  -> direct/conversion capability
  -> feature fidelity
  -> stream compatibility
  -> request retry context
  -> provider status
  -> quota-group status
  -> deployment cooldown
  -> ranking
  -> quota reservation
```

Protocol filtering must happen before quota reservation.

This prevents a protocol-incompatible candidate from consuming a lease and then failing with an avoidable upstream error.

### 10.3 Direct route eligibility

A deployment is direct-eligible when:

```text
deployment.upstream_protocol == request.public_protocol
```

and:

```text
request.features ⊆ deployment.upstream_features
```

and:

```text
not request.stream or deployment.streaming is true
```

### 10.4 Converted route eligibility

A deployment is conversion-eligible when:

1. The logical model opts into the requested public protocol.
2. A conversion capability exists for the exact direction.
3. The request feature set is a subset of the conversion request features.
4. The expected response feature set is representable.
5. Streaming is supported if requested.
6. The conversion fidelity is accepted by policy.
7. The deployment's upstream capability supports the converted request.

### 10.5 Ranking

The recommended ranking key is:

```text
(
    route_mode_rank,          # direct = 0, conversion = 1
    affinity_rank,
    plan_priority,
    quota_group_inflight,
    recent_success_rank,
    deployment_id
)
```

The existing Fill First, affinity, in-flight, and recent-success behavior remains in place after capability filtering. Affinity is consulted only after capability
filtering. If the affinity target is absent from the filtered set, ranking
continues over the remaining eligible deployments; affinity never overrides
protocol or feature eligibility.

If policy requires a conversion route for a particular client, that policy must be explicit in the request or logical-model configuration. The default remains direct first.

### 10.6 No implicit cross-model fallback

Candidate resolution must remain:

```text
requested logical model -> deployments for that model_group
```

It must not become:

```text
requested logical model -> any model with similar provider or name
```

Cross-model fallback remains opt-in and out of the first version.

### 10.7 No runtime endpoint probing

The router must not send test requests to:

- `/v1/messages`
- `/v1/responses`
- `/v1/chat/completions`

to discover support.

Capabilities are configuration declarations validated during startup and contract testing.

### 10.8 No protocol inference from names

The following are invalid decisions:

```text
if "claude" in model:
    use anthropic_messages

if provider_id == "opencode-go":
    use openai_responses

if litellm_params.model.startswith("openai/"):
    expose openai_chat
```

The configured protocol metadata is the only source of capability truth.

## 11. Direct and Conversion Matrix

### 11.1 Target public protocols and MVP state

| Public protocol | Endpoint | Internal name |
|---|---|---|
| OpenAI Chat Completions | `/v1/chat/completions` | `openai_chat` |
| OpenAI Responses | `/v1/responses` | `openai_responses` (disabled until a verified deployment exists) |
| Anthropic Messages | `/v1/messages` | `anthropic_messages` |

### 11.2 First-version supported route matrix

The first version should support the following paths only when each route is explicitly configured and contract-tested.

| Public request | Upstream protocol | Route mode | First version |
|---|---|---:|---:|
| OpenAI Chat Completions | OpenAI Chat Completions | Direct | Supported |
| OpenAI Responses | OpenAI Responses | Direct | Deferred; no current verified deployment |
| Anthropic Messages | Anthropic Messages | Direct | Supported |
| OpenAI Chat Completions | OpenAI Responses | Conversion | Deferred unless separately proven |
| OpenAI Responses | OpenAI Chat Completions | Conversion | Deferred unless separately proven |
| OpenAI Chat Completions | Anthropic Messages | Conversion | Deferred, non-streaming only if proven |
| Anthropic Messages | OpenAI Chat Completions | Conversion | Deferred, non-streaming only if proven |
| Anthropic Messages | OpenAI Responses | Conversion | Deferred |
| OpenAI Responses | Anthropic Messages | Conversion | Deferred |

The first release should not advertise conversion merely because a general-purpose LiteLLM translation may exist elsewhere. Each path needs repository-level verification against LiteLLM v1.90.5 and the actual upstream behavior.

### 11.3 Recommended first production matrix

A conservative first production matrix is:

| Public protocol | Direct upstream protocols | Conversion |
|---|---|---|
| `openai_chat` | `openai_chat` | Disabled by default |
| `openai_responses` | none in current plan set | Endpoint disabled/no-route |
| `anthropic_messages` | `anthropic_messages` | Disabled by default |

This matrix solves the immediate routing correctness problem while preventing unsafe compatibility claims.

### 11.4 Current plan set vs first production matrix

Mapping the verified deployment facts to the first production matrix:

| Logical model family | Verified deployments | First-version public availability |
|---|---|---|
| `glm-*`, `kimi-*`, `minimax-*`, `deepseek-*`, `qwen*`, `mimo-*`, `hy3-preview`, `grok-4.5` | OpenCode Go plan as `openai_chat` | `openai_chat` only |
| `ark-code-latest`, `glm-latest`, `doubao-seed-*` | Volc Coding Plan plan as `openai_chat` | `openai_chat` only |
| `claude-opus-4-8`, `claude-fable-5` | NewAPI A protocol unverified | No public protocol until Phase 0 verification; then explicit opt-in only |

Consequences:

1. No logical model in the current plan set exposes `/v1/responses` in the first version.
2. `/v1/messages` remains controlled disabled/no-route until at least one direct
   Anthropic-compatible deployment is verified; model names alone do not opt in.
3. Cross-protocol conversion is not available for the current plan set without an explicit, separately proven route.
4. Adding a Responses-capable deployment later must go through configuration and contract tests before it is exposed publicly.

### 11.5 Deferred conversion paths

The following paths may be evaluated later:

1. `openai_chat -> anthropic_messages`
2. `anthropic_messages -> openai_chat`
3. `openai_chat -> openai_responses`
4. `openai_responses -> openai_chat`

Each path needs separate tests for:

- Basic text.
- System instructions.
- Multi-turn messages.
- Tools.
- Tool results.
- Parallel tools.
- Structured output.
- Reasoning content.
- Images.
- Usage.
- Stop conditions.
- Error responses.
- Non-streaming.
- Streaming.
- Client cancellation.
- Timeouts.
- Retry boundaries.

## 12. Protocol Adapter Contracts

### 12.1 Public request adapter

Each public endpoint adapter must:

1. Parse JSON.
2. Validate the required `model`.
3. Resolve the logical model exactly as provided.
4. Detect `stream`.
5. Extract supported features.
6. Preserve protocol-specific fields in the payload.
7. Reject malformed or unsupported fields before routing where possible.
8. Avoid logging full request bodies.

### 12.2 Direct passthrough contract

For direct routes:

- The request body must remain protocol-native.
- The upstream endpoint must be selected from the declared upstream protocol.
- The response must remain protocol-native.
- Only internal metadata, authentication, and transport headers may be added or normalized.

### 12.3 Conversion contract (post-MVP appendix material)

For converted routes:

```text
public request
  -> source protocol parser
  -> feature validation
  -> directional request converter
  -> upstream request
  -> upstream response parser
  -> directional response converter
  -> public response
```

The converter must return a structured result containing:

```python
@dataclass(slots=True)
class ConvertedRequest:
    payload: dict[str, Any]
    warnings: list[str]
    dropped_fields: list[str]
```

A route must be rejected if `dropped_fields` contains fields not explicitly allowed by policy.

### 12.4 Streaming conversion contract (post-MVP appendix material)

Streaming conversion is a separate capability. It must not reuse non-streaming conversion code unless the event-level behavior is proven.

The adapter must preserve:

- Event ordering.
- Incremental text.
- Tool-call deltas.
- Finish reason.
- Usage event behavior.
- Error event format.
- End-of-stream marker.
- Client cancellation behavior.

No conversion route may be marked streaming-capable until these invariants pass.

## 13. Capability Filter Before Quota Reservation

This is a hard requirement.

The existing selector currently filters candidates and then acquires a lease for the selected deployment. Protocol-aware filtering must be added to the candidate filter before the selector reaches the lease acquisition loop.

Conceptually:

```python
def filter_candidates(
    self,
    model_group: str,
    context: RequestRoutingContext,
    *,
    public_protocol: PublicProtocol,
    request_features: frozenset[RequestFeature],
    stream: bool,
) -> list[RouteCandidate]:
    deployments = self.registry.get_by_model_group(model_group)

    candidates = []
    for deployment in deployments:
        route = self.capabilities.resolve_route(
            deployment=deployment,
            public_protocol=public_protocol,
            request_features=request_features,
            stream=stream,
        )
        if route is None:
            continue

        if not context.can_try_quota_group(deployment.quota_group_id):
            continue

        if not self.provider_is_available(deployment.provider_id):
            continue

        if not self.quota_group_is_available(deployment.quota_group_id):
            continue

        if not self.deployment_is_available(deployment.deployment_id):
            continue

        candidates.append(RouteCandidate(deployment=deployment, route=route))

    return candidates
```

The lease must be acquired only after a route candidate has passed every capability and state filter.

Affinity is evaluated against this already-filtered set. An affinity target that
is protocol- or feature-incompatible is ignored without consuming a retry or a
quota lease.

This ordering avoids:

- Wasted quota leases.
- False pressure on unavailable quota groups.
- Incorrect retry accounting.
- Protocol mismatch being reported as quota failure.
- A `404` caused by an incompatible route.

## 14. Error and Circuit Isolation

### 14.1 Error classes

Protocol-aware routing introduces a distinction between route capability errors and upstream runtime errors.

#### Client request errors

Examples:

- Malformed JSON.
- Missing model.
- Unsupported request feature.
- Unsupported conversion.
- Invalid protocol-specific field.

Behavior:

- Return a client error.
- Do not retry across quota groups.
- Do not modify quota-group state.
- Do not open a circuit.

#### Capability mismatch

Examples:

- Logical model is not opted into the requested public protocol.
- Deployment has no matching direct capability.
- Deployment has no allowed conversion direction.
- Conversion does not support a request feature.
- Conversion does not support streaming.

Behavior:

- Exclude the candidate before lease acquisition.
- Do not call the upstream.
- Do not mark the quota group exhausted.
- Do not treat the condition as provider outage.

If no candidates remain, return a protocol-aware no-route error that identifies the logical model and requested protocol without exposing credentials.

#### Upstream model or route error

A `404` from an upstream must not automatically be classified as protocol mismatch. It may indicate:

- Wrong upstream path.
- Unsupported model.
- Provider route behavior.
- Account-specific deployment issue.
- Actual protocol mismatch.

The existing classifier policy remains authoritative. A future classifier enhancement may add a high-confidence route mismatch classification, but it must be based on verified provider-specific evidence rather than status code alone.

#### Provider outage

Behavior:

- Apply provider cooldown according to existing policy.
- Keep quota-group semantics unchanged.
- Permit another eligible quota group if retry policy allows and no visible stream output exists.

#### Deployment failure

Behavior:

- Apply deployment cooldown.
- Do not disable the whole quota group unless the classifier identifies a quota-group or account-level condition.

#### Shared quota exhaustion

Behavior:

- Require high confidence.
- Mark the entire `quota_group_id` exhausted.
- Remove all deployments in that quota group from candidate selection.
- Preserve other quota groups for the same `model_group`.
- Use recovery probing according to existing passive recovery behavior.

#### Authentication failure or account disabled

Behavior:

- Disable the quota group.
- Clear affinity to the quota group.
- Do not retry client errors.
- Emit an alert without credentials.

### 14.2 Circuit state boundaries

Circuit isolation remains:

```text
provider outage      -> provider scope
deployment failure   -> deployment scope
shared quota exhaust -> quota_group scope
logical model        -> no automatic circuit scope
public protocol      -> no automatic circuit scope
```

Protocol mismatch should not melt an account circuit. A configuration error must remain a configuration or route eligibility error.

### 14.3 Redis failure

Existing fail-closed behavior remains mandatory.

If Redis state cannot be read:

- Do not treat all providers as available.
- Do not treat all quota groups as available.
- Do not blind-select a deployment.
- Do not bypass request retry context.
- Return a controlled gateway failure.

## 15. Streaming and Retry Invariants

### 15.1 Before first visible byte

Before the first visible byte:

- A failed attempt may be followed by another eligible deployment.
- The next deployment must serve the same requested public protocol directly or through an explicitly safe conversion.
- The request must not change logical model unless explicit cross-model fallback is enabled.
- A quota group may be tried at most once.
- The request may try at most three distinct quota groups.
- Client errors do not trigger cross-account retry.

### 15.2 After first visible byte

After the first visible byte:

- No upstream switch.
- No protocol switch.
- No model switch.
- No output concatenation.
- No transparent retry.
- No second attempt under another quota group.

The existing `RequestRoutingContext.first_byte_sent` flag remains the hard gate.

### 15.3 First byte definition

For direct routes, the first byte boundary is the first response chunk delivered to the client.

For converted routes, the boundary is the first converted response event delivered to the client, not merely the first upstream byte received.

This distinction matters because a converter may buffer upstream data before producing a valid public event.

Post-MVP converters that buffer upstream events must hold the lease throughout
the buffering window. Each route declares a configurable maximum buffering
latency established by contract and load tests; no fixed threshold is assumed.

### 15.4 Retry classification

Retry eligibility must consider:

```text
client error?
first byte sent?
same quota group already tried?
maximum quota groups reached?
candidate supports requested protocol?
candidate supports request features?
```

A protocol-incompatible candidate must never consume a retry attempt because it should have been filtered before the first attempt.

### 15.5 Lease lifecycle

The selected quota-group lease must:

1. Be acquired only after capability filtering.
2. Be associated with the request ID.
3. Be released on success.
4. Be released on failure.
5. Remain bounded by the existing timeout behavior.
6. Not be acquired again for the same quota group during the same request.

## 16. LiteLLM v1.90.5 Constraints

### 16.1 Verified usable extension points

The repository verifies these LiteLLM v1.90.5 integration points:

- `CustomRoutingStrategyBase`-compatible methods.
- `router.set_custom_routing_strategy()`.
- `CustomLogger`-compatible callbacks.
- `LITELLM_WORKER_STARTUP_HOOKS`.
- Delayed registration after the proxy router is ready.

The implementation should remain within these boundaries where possible.

### 16.2 No unsupported endpoint hook claims

The design must not assume LiteLLM v1.90.5 provides:

- A documented public endpoint-specific custom router callback.
- A documented protocol capability callback.
- A documented per-request upstream protocol override.
- A documented streaming conversion hook.
- A documented conversion policy registry.

If those behaviors are needed, they must be implemented in the project-owned gateway or adapter layer, or verified through a compatibility test before use.

### 16.3 Integration options

There are two viable implementation shapes.

#### Option A: Protocol-aware front adapter before LiteLLM routing

The project-owned adapter receives the public request, resolves protocol and capabilities, and invokes LiteLLM using the selected deployment.

Advantages:

- Clear protocol boundary.
- Explicit control of conversions.
- No dependence on undocumented endpoint hooks.

Risks:

- Requires careful integration with the existing proxy request lifecycle.
- May need additional transport and error handling code.

#### Option B: Protocol-aware custom routing metadata through verified LiteLLM request kwargs

The preferred candidate is a string value in `metadata.protocol`. It is not a
verified solution until contract tests prove that endpoint adapters can set it
and LiteLLM preserves it through custom routing and callbacks.

Advantages:

- Keeps the existing custom strategy central.
- Smaller architectural change if request metadata survives the path.

Risks:

- Must verify actual v1.90.5 behavior.
- Must not rely on fields that are dropped or serialized unexpectedly.
- The current code explicitly avoids attaching non-serializable objects to request kwargs.

The implementation phase must choose one option based on a focused v1.90.5 compatibility test. If Option B fails, Option A or isolated internal protocol lanes
is mandatory. The design does not assume either option is already available.

### 16.4 Upstream submodule rule

No protocol business logic should be added to `upstream/litellm`.

Project-owned logic belongs in:

```text
plugins/shared_quota_router/
```

A minimal registration patch may be considered only if a verified LiteLLM integration gap cannot be solved through the existing registration API. Any such patch must contain no quota business logic.

## 17. Generator Changes

The generator in `scripts/llm-router.ps1` currently creates every LiteLLM model entry with `model: openai/<model>`.

The target generator behavior is:

1. Read explicit upstream protocol metadata.
2. Write protocol metadata into generated model information.
3. Preserve `model_group`, `quota_group_id`, `provider_id`, `account_id`, and `priority`.
4. Never write credential values into generated documentation or logs.
5. Fail if a protocol-aware deployment lacks required capability metadata.
6. Keep generated YAML ASCII-safe if the existing Windows encoding constraint remains.
7. Preserve environment variable references such as:
   - `os.environ/<BASE_URL_ENV>`
   - `os.environ/<API_KEY_ENV>`

The generator must not transform protocol declarations based on provider or model names.

In the verified plan set:

1. OpenCode Go plan deployments must be generated with `upstream: openai_chat`.
2. Volc Coding Plan plan deployments must be generated with `upstream: openai_chat`.
3. NewAPI A deployments must not be assigned an upstream protocol until Phase 0
   provider contract tests establish it; generated configuration must fail
   closed or keep those deployments disabled meanwhile.
4. No deployment should be generated with `upstream: openai_responses` unless an explicit, separately verified capability source exists.

## 18. First-Version Rollout

### Phase 0: Compatibility and inventory

Tasks:

1. Record the proxy-internal `route_type` enum from pinned v1.90.5 source and
   verify it is not part of the custom strategy contract.
2. Run `tests/contract/test_c0_routing.py` against the pinned package and record
   command output.
3. Add a contract fixture that sets `metadata.protocol` for Chat, Responses,
   and Messages, then assert what reaches strategy `request_kwargs` and
   callbacks.
4. Record strategy `messages` and `input` values for each endpoint as
   validation-only evidence, not protocol inference.
5. Verify protocol-specific LiteLLM entry points, selected model metadata,
   structured errors, and first-visible-event callback behavior.
6. Choose metadata integration only if it passes; otherwise select and test the
   project-owned front gateway or isolated-lane boundary.

Deliverable:

```text
A v1.90.5 compatibility report with command-backed tests.
```

### Phase 1: Capability model and direct routing

Tasks:

1. Add protocol enums and capability data classes.
2. Add explicit protocol fields to deployment metadata.
3. Add public protocol opt-in per logical model.
4. Add capability filtering before lease acquisition.
5. Implement direct same-protocol routing.
6. Keep conversion disabled.
7. Add protocol-aware no-route errors.
8. Preserve existing quota and retry behavior.

Acceptance target:

- A request cannot select a deployment without the requested protocol capability.
- A Claude `/v1/messages` request cannot be routed to an OpenCode deployment declared as `openai_chat`.

### Phase 2: Public endpoint coverage

Tasks:

1. Enable `/v1/chat/completions`.
2. Keep `/v1/responses` disabled until a verified direct deployment is added.
3. Enable `/v1/messages`.
4. Add direct fixtures for Chat and Messages; add Responses only when a verified
   deployment exists.
5. Add endpoint-specific request validation.
6. Add response and error contract tests.

Acceptance target:

- Each endpoint serves only logical models explicitly opted into that protocol.
- Direct routes preserve protocol-native request and response structures.

### Phase 3 and later: Post-MVP conversion framework

Tasks:

1. Implement conversion capability declarations.
2. Implement directional converter interfaces.
3. Implement feature gating.
4. Implement non-streaming conversion tests.
5. Keep all conversions disabled in production configuration.

Acceptance target:

- Converters can be tested independently.
- Unsupported fields are rejected rather than silently dropped.
- No conversion route is selected without explicit configuration.

### Phase 4: Limited conversion pilot

Tasks:

1. Select one conversion direction.
2. Limit it to text-only non-streaming requests.
3. Add explicit allowlisting for selected logical models.
4. Add metrics for direct versus converted routing.
5. Run shadow or isolated tests.
6. Do not enable streaming conversion initially.

Acceptance target:

- All supported fixtures pass.
- Unsupported features fail before quota reservation.
- Error mapping is stable.
- No credentials or prompt bodies appear in logs.

### Phase 5: Streaming conversion evaluation

Streaming conversion should be treated as a separate project milestone.

It requires:

- Event-level adapter tests.
- Backpressure tests.
- Cancellation tests.
- First-byte boundary tests.
- Failure-after-first-byte tests.
- Tool-call delta tests.
- Usage event tests.

No streaming conversion should be enabled until these tests pass against the actual upstream protocols.

## 19. Test Plan

### 19.1 Configuration tests

1. Missing upstream protocol metadata fails configuration validation.
2. Unknown protocol value fails validation.
3. Unknown conversion direction fails validation.
4. Public protocol opt-in without a route fails validation.
5. Duplicate deployment IDs fail validation.
6. Duplicate conversion directions fail validation.
7. Credential values are never required in protocol metadata.
8. Generated YAML retains environment references rather than secret values.
9. Model and quota group fields remain unchanged by protocol metadata generation.

### 19.2 Registry tests

1. Deployment registry indexes by `model_group`.
2. Deployment registry indexes by `quota_group_id`.
3. Protocol metadata is parsed from model entries.
4. Missing capability metadata does not imply universal capability.
5. Multiple deployments for one model group retain separate quota groups.
6. One quota group can contain multiple model groups.
7. A quota-group lookup returns every deployment under that account boundary.

### 19.3 Public protocol tests

For each endpoint:

1. Valid request parses successfully.
2. Missing model is rejected.
3. Unknown logical model is rejected.
4. Non-opted-in protocol is rejected.
5. Supported logical model reaches only compatible deployments.
6. Protocol mismatch does not invoke the upstream.
7. Error responses use the requested public protocol's error shape.
8. Authentication headers are not forwarded to logs.
9. A logical model with no `public_protocols` declaration is unavailable on all
   public endpoints without acquiring a quota lease.

### 19.4 Direct routing tests

1. `openai_chat` request selects `openai_chat` deployment directly.
2. `openai_responses` request selects `openai_responses` deployment directly.
3. `anthropic_messages` request selects `anthropic_messages` deployment directly.
4. Direct route ranks ahead of conversion route.
5. Model names do not determine protocol.
6. Provider names do not determine protocol.
7. LiteLLM provider prefixes do not determine public protocol.

### 19.5 Capability filter ordering tests

1. Protocol-incompatible candidate is removed before lease acquisition.
2. Unsupported conversion feature is removed before lease acquisition.
3. Streaming-incompatible candidate is removed before lease acquisition.
4. A protocol mismatch does not mark the quota group exhausted.
5. A protocol mismatch does not consume the request's quota-group retry budget.
6. A valid candidate in another quota group can be selected after an invalid candidate is filtered out.
7. If all candidates are incompatible, the result is a protocol-aware no-route error.

### 19.6 Conversion tests

For every enabled conversion direction:

1. Basic text request.
2. System instructions.
3. Multi-turn conversation.
4. Tool declaration.
5. Tool call.
6. Tool result.
7. Stop or finish reason.
8. Usage.
9. Error mapping.
10. Unsupported feature rejection.
11. Field preservation.
12. Explicitly allowed field omission.
13. No silent semantic downgrade.
14. No credential leakage.
15. No prompt leakage in logs.
16. Enabling `openai_chat -> anthropic_messages` does not enable the reverse;
   the reverse request fails before lease acquisition.

### 19.7 Streaming tests

1. First chunk marks the request context.
2. First visible public event, not merely upstream receipt, is the conversion boundary.
3. Failure before first byte permits eligible retry.
4. Failure after first byte forbids retry.
5. No output from two deployments is concatenated.
6. Direct streams preserve event framing.
7. Converted streams preserve event ordering.
8. Client cancellation releases the lease.
9. Tool-call streams do not produce malformed JSON.
10. End-of-stream markers are correct.

### 19.8 Quota and circuit tests

1. Protocol filtering occurs before lease acquisition.
2. Same quota group is tried at most once.
3. At most three distinct quota groups are tried.
4. Shared quota exhaustion disables every deployment in that quota group.
5. Deployment failure affects only the deployment.
6. Provider outage affects the provider scope.
7. Content policy errors do not trigger cross-account retry.
8. Bad requests do not trigger cross-account retry.
9. Low-confidence `429` is not shared quota exhaustion.
10. Redis failure remains fail-closed.
11. Affinity does not override protocol eligibility.
12. Affinity to an incompatible deployment is ignored.

### 19.9 Regression tests for the observed incident

Create a regression test representing:

```text
public request: /v1/messages
logical model: a Claude logical model
candidate A: OpenCode Go, upstream protocol openai_chat
candidate B: Anthropic-compatible deployment, upstream protocol anthropic_messages
```

Expected result:

```text
candidate A is filtered before lease acquisition
candidate B is selected directly
no request is sent to the OpenCode endpoint
```

Also test the no-capable-candidate case:

```text
public request: /v1/messages
logical model: only has openai_chat deployments
```

Expected result:

```text
protocol-aware no-route error
no upstream call
no quota-group circuit mutation
```

## 20. Observability

Metrics should identify protocol and route mode without logging sensitive content.

Recommended dimensions:

```text
public_protocol
upstream_protocol
route_mode
logical_model
provider_id
quota_group_id
deployment_id
result
failure_kind
```

Recommended metrics:

```text
protocol_route_selection_total
protocol_route_rejected_total
protocol_conversion_total
protocol_conversion_failure_total
protocol_capability_mismatch_total
protocol_stream_conversion_total
protocol_no_capable_deployment_total
```

Do not include:

- Authorization headers.
- API keys.
- Full prompt content.
- Full response content.
- Secret environment values.

Logs should contain identifiers such as:

```text
logical_model=claude-opus-4-8
public_protocol=anthropic_messages
deployment_id=newapi-a-claude-opus-4-8
quota_group_id=newapi-a
route_mode=direct
```

Identifiers must still be reviewed for accidental inclusion of secret material.
For shared or multi-tenant operation, `logical_model`, `deployment_id`, and
similar labels are PII-conditional. Export hashed surrogates or suppress them
unless a separately managed metrics salt is configured. Local-only operation
may keep raw operational labels.

## 21. Acceptance Criteria

The design is accepted for implementation when all criteria below are met.

### Public API

- [ ] `/v1/chat/completions` is exposed.
- [ ] `/v1/responses` is disabled with a controlled response until at least one
      verified Responses deployment exists; once one exists, exposure is opt-in.
- [ ] `/v1/messages` is exposed.
- [ ] Each endpoint maps to an explicit internal protocol identifier.
- [ ] Logical models opt into public protocols explicitly.
- [ ] Non-opted-in protocol requests are rejected before upstream dispatch.

### Routing correctness

- [ ] Protocol capability filtering occurs before quota reservation.
- [ ] Same-protocol direct routing ranks above conversion.
- [ ] Conversion is directional and explicit.
- [ ] Conversion is disabled unless configured and tested.
- [ ] Protocol is never inferred from a model name.
- [ ] Protocol is never inferred from a provider name.
- [ ] No runtime endpoint probing is used.
- [ ] No default cross-model fallback is introduced.

### Quota semantics

- [ ] `model_group` remains the logical-model candidate boundary.
- [ ] `quota_group_id` remains the account-level quota and circuit boundary.
- [ ] A quota-group exhaustion event disables all deployments in that quota group.
- [ ] Protocol mismatch does not mark quota exhausted.
- [ ] Protocol filtering does not consume a quota lease.
- [ ] Redis failure remains fail-closed.

### Streaming and retries

- [ ] No retry occurs after visible stream output.
- [ ] No upstream output is concatenated with another deployment's output.
- [ ] Same quota group is attempted at most once per request.
- [ ] At most three distinct quota groups are attempted.
- [ ] Client errors do not trigger cross-account retry.
- [ ] Converted streaming routes are disabled unless event-level tests pass.

### LiteLLM compatibility

- [ ] The implementation works with LiteLLM v1.90.5.
- [ ] Only verified extension points are used.
- [ ] No unsupported v1.90.5 endpoint hook is claimed.
- [ ] No business logic is added to the upstream submodule.
- [ ] Existing custom routing and callback contracts remain valid.

### Security

- [ ] No credentials appear in the document or fixtures.
- [ ] No credentials appear in generated public metadata.
- [ ] Authorization headers are not logged.
- [ ] Full prompts are not logged.
- [ ] Public binding remains local-only by default.
- [ ] Secret environment references remain indirect.

## 22. Risks

### 22.1 LiteLLM request-context uncertainty

LiteLLM v1.90.5 may not preserve every desired protocol field through all proxy paths.

Mitigation:

- Add compatibility tests before implementation.
- Use a project-owned protocol adapter if necessary.
- Avoid relying on undocumented fields.

### 22.2 Converter fidelity loss

Protocol conversion may lose semantics around:

- Tool calls.
- Reasoning.
- Structured output.
- Multimodal content.
- Usage.
- Caching.
- Citations.
- Stop behavior.

Mitigation:

- Direct routes first.
- Feature-level capability filters.
- Explicit directional conversion declarations.
- Reject unsupported feature combinations.
- Keep conversions disabled by default.

### 22.3 Streaming corruption

A converter may produce malformed event framing or incomplete tool-call deltas.

Mitigation:

- Separate streaming capability from non-streaming capability.
- Test public event framing.
- Mark the first visible converted event as the retry boundary.
- Never switch after the boundary.

### 22.4 Configuration drift

A plan may be updated without protocol metadata, or generated YAML may become stale.

Mitigation:

- Validate required metadata during `apply`.
- Regenerate configuration from source declarations.
- Add configuration contract tests.
- Fail closed rather than defaulting to OpenAI Chat.

### 22.5 Incorrect provider assumptions

The same provider may expose different protocols for different plans or accounts.

Mitigation:

- Store capability per deployment or explicit plan declaration.
- Never infer protocol from provider identity.
- Keep account-level quota grouping independent.

### 22.6 Error classification confusion

A provider may return `404` for several unrelated reasons.

Mitigation:

- Do not classify every `404` as protocol mismatch.
- Preserve existing failure classifier behavior.
- Add provider-specific evidence only after verification.

### 22.7 Operational complexity

Three public protocols increase testing and incident diagnosis requirements.

Mitigation:

- Start with direct routes only.
- Use a narrow supported matrix.
- Add protocol and route-mode metrics.
- Roll out conversions separately.

## 23. Open Verification Items

These items must be verified before implementation claims compatibility.

1. **Verified from source:** `route_type` is proxy-internal and is not part of
   the custom strategy contract.

2. **Contract test required:** does endpoint-injected `metadata.protocol`
   survive Chat, Responses, and Messages paths into strategy `request_kwargs`
   and callbacks?

3. If metadata propagation fails, which project-owned boundary is selected:
   front gateway or isolated protocol lanes?

4. Does the selected model entry's custom `model_info` reach all success and failure callbacks for each public endpoint?

5. Does `async_log_stream_event` fire consistently for:
   - Direct Chat Completions.
   - Direct Responses.
   - Direct Messages.
   - Any project-owned converted stream.

6. Can LiteLLM v1.90.5 dispatch a direct Anthropic Messages request using the configured deployment metadata without a provider-specific undocumented hook?

7. Can a project-owned adapter invoke a selected LiteLLM deployment while preserving:
   - Request IDs.
   - Callback metadata.
   - Quota lease ownership.
   - Error classification.

8. Which protocol-specific fields are dropped by `drop_params: true` in the current configuration?

9. Does the current LiteLLM v1.90.5 proxy path preserve structured error responses for all three public endpoints?

10. Does endpoint routing happen before or after the custom routing strategy in every target path?

11. `/v1/models` should keep one entry per logical model and add
    `metadata.public_protocols` where the selected integration boundary permits
    it. Presence in `/v1/models` alone never guarantees endpoint availability;
    exact v1.90.5 model-list metadata preservation remains a contract test.

12. What is the exact behavior when a logical model is public-protocol-enabled but all matching deployments are in cooldown or exhausted?

13. What is the exact response shape for a protocol-aware no-route error?

14. Can conversion warnings be returned safely without violating the target protocol or leaking internal route details?

15. Which conversion directions can be proven fidelity-safe for the first release?

16. Which existing deployments in `config/plans.yaml` can be confirmed to support `/v1/responses` rather than `/v1/chat/completions`? Until verified, none.

## 24. Rollback Plan

Rollback must be possible without changing quota state semantics.

### 24.1 Configuration rollback

This is the catastrophic fallback, not the primary rollback path. Every
successful `apply` must create a timestamped backup of generated
`config/litellm.yaml` before replacement.

1. Disable protocol-aware public endpoint exposure.
2. Restore the previous generated `config/litellm.yaml`.
3. Restart the local router using the pinned LiteLLM v1.90.5 environment.
4. Keep `plans.yaml` and quota state intact.
5. Do not delete Redis state unless required by a separate incident procedure.

### 24.2 Feature rollback

This feature flag is the primary rollback mechanism.

Protocol-aware routing should be feature-gated:

```text
PROTOCOL_AWARE_GATEWAY_ENABLED=false
```

When disabled:

- Existing Chat Completions behavior remains available according to the previous configuration.
- New endpoints should return a controlled not-enabled response rather than silently selecting incompatible deployments.
- Shared quota callbacks and circuit state remain active.

### 24.3 Conversion rollback

Conversion can be disabled independently:

```text
PROTOCOL_CONVERSION_ENABLED=false
```

When disabled:

- Direct routes remain available.
- Converted candidates are filtered out before quota reservation.
- No conversion-specific circuit state is changed.
- Public models without direct capability return a protocol-aware no-route error.

### 24.4 Emergency route quarantine

If a specific deployment causes protocol failures:

1. Disable that deployment in configuration.
2. Regenerate LiteLLM configuration.
3. Restart the router.
4. Preserve the quota group for other deployments unless the failure classifier identifies an account-level problem.
5. Review the deployment's declared protocol capability and upstream path.

### 24.5 Rollback verification

After rollback, verify:

- `/v1/chat/completions` responds as expected.
- No incompatible deployment is selected.
- Redis remains reachable and authenticated.
- Existing quota-group state is readable.
- No public logs contain credentials.
- No retries occur after visible stream output.

## 25. Final Decision

Implement a protocol-aware gateway target with:

```text
/v1/chat/completions
/v1/responses
/v1/messages
```

MVP state:

```text
/v1/chat/completions  enabled for explicitly opted-in openai_chat models
/v1/messages          disabled until a verified direct anthropic_messages deployment exists
/v1/responses         controlled disabled/no-route response until a verified deployment exists
```

Use explicit public protocol opt-in per logical model. Keep logical model identity separate from protocol and provider identity. Filter candidates by protocol and request feature capability before quota reservation. Prefer direct same-protocol passthrough. Conversion is post-MVP and remains disabled until each direction is explicitly declared and proven fidelity-safe.

Preserve the existing shared quota architecture:

```text
model_group       = logical model candidate set
quota_group_id    = account-level quota and circuit boundary
```

Preserve existing retry and streaming invariants. Do not infer protocol from names. Do not probe upstream endpoints at runtime. Do not add unsupported LiteLLM v1.90.5 extension assumptions. Do not introduce default cross-model fallback.
