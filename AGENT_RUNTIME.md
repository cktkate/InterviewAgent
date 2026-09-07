# Agent Runtime Harness

InterviewAgent wraps the compiled LangGraph workflow with a runtime layer. The
runtime does not alter API, WebSocket, CLI, or Agent business outputs.

## Runtime identity

- `session_id` identifies the complete interview.
- `runtime_run_id` identifies one graph invocation.
- `runtime_trace_id` identifies the OpenTelemetry trace for that invocation.

The IDs are carried in `InterviewState` and attached to checkpoint metadata.

## Bounded planning loop

Question preparation uses a bounded `Plan -> Tool -> Review -> Replan` loop:

1. The planning LLM produces candidate questions and may request up to two
   additional `rag_retrieve` actions when evidence is insufficient.
2. The Harness accepts only the allowlisted read-only RAG action. User identity,
   metadata isolation, permissions, idempotency key and call limits are injected
   by trusted orchestration code rather than accepted from the model.
3. The deterministic Reviewer checks required fields, duplicates, job relevance
   and evidence bindings. A rejected artifact is returned as structured feedback
   for one bounded replanning attempt.
4. If the retry budget is exhausted, the Coordinator installs a deterministic
   recovery plan so the interview remains available without an unbounded loop.

`planning_history`, `planning_actions` and `planning_decision` are checkpointed
with the interview state and summarized into the run trace.

## Local trace

Sanitized spans are written to:

```text
logs/agent_traces.jsonl
```

The trace contains operational metadata such as node name, status, latency,
LLM model, prompt/response character counts, token usage when supplied by the
model provider, and exception type. It must not contain prompt text, resume
text, answers, access tokens, API keys, or database credentials.

## Security boundary

- Tool permissions are derived by deterministic code from the Agent role and
  tool allowlist; model output cannot grant itself permissions or change the
  authenticated user identity.
- JD, resumes, candidate answers, RAG documents and derived history are marked
  as untrusted data before entering prompts. XML-like delimiters are escaped so
  retrieved text cannot close its container and impersonate an instruction.
- Injection-like content is not blindly blocked because security terminology
  may be legitimate interview material. It is isolated as data, recorded only
  by source, signal, length and digest, and added to Bad Case review through
  trace metrics.
- Observability export applies a final recursive redaction pass for credentials
  and sensitive content. HTTP 500 responses do not expose exception details.

To forward traces to an OpenTelemetry Collector, set an HTTP/protobuf endpoint:

```env
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
```

## Checkpoint persistence

LangGraph checkpoints use a configurable backend. Local development defaults
to:

```text
data/langgraph_checkpoints.sqlite3
```

The runtime automatically uses `session_id` as LangGraph's `thread_id`. Local
SQLite persistence is intended for development and single-process deployment.
For multi-instance deployment set `LANGGRAPH_CHECKPOINT_BACKEND=mysql`; the
SQLAlchemy saver creates `langgraph_checkpoints` and
`langgraph_checkpoint_writes` in the configured MySQL database. A configured
shared backend fails fast instead of silently falling back to a local file.

Tool rate-limit windows, circuit state and idempotency results are kept in
memory by default. Set `TOOL_RUNTIME_STATE_BACKEND=redis` to share them between
API workers. The Redis implementation uses atomic Lua scripts for rate limiting
and circuit transitions and stores hashes instead of raw idempotency keys.

## Configuration

```env
OBSERVABILITY_ENABLED=true
TRACE_JSONL_ENABLED=true
TRACE_JSONL_PATH=logs/agent_traces.jsonl
OTEL_SERVICE_NAME=interview-agent
OTEL_EXPORTER_OTLP_ENDPOINT=
LANGGRAPH_CHECKPOINT_ENABLED=true
LANGGRAPH_CHECKPOINT_BACKEND=sqlite
LANGGRAPH_CHECKPOINT_PATH=data/langgraph_checkpoints.sqlite3
LANGGRAPH_CHECKPOINT_DATABASE_URL=
TOOL_RUNTIME_STATE_BACKEND=memory
TOOL_RUNTIME_REDIS_DB=3
TOOL_RUNTIME_STATE_PREFIX=iam:tool-runtime
TOOL_RUNTIME_IDEMPOTENCY_TTL_SECONDS=3600
AGENT_MAX_LLM_CALLS=12
AGENT_MAX_TOOL_CALLS=6
AGENT_MAX_TOTAL_TOKENS=30000
AGENT_DEADLINE_SECONDS=120
AGENT_PLANNING_MAX_ATTEMPTS=2
AGENT_PLANNING_MAX_EVIDENCE_ACTIONS=2
```

Set `OBSERVABILITY_ENABLED=false` to bypass tracing. Set
`LANGGRAPH_CHECKPOINT_ENABLED=false` to compile the graph without a persistent
checkpointer.
