# LiteLLM Anthropic Messages SpendLogs issue

## Summary

LiteLLM 1.85.1 can successfully serve Anthropic Messages requests through the OpenAI Responses API adapter while failing the asynchronous success logging path. The user request returns `200 OK`, but SpendLogs are not written for affected `/v1/messages?beta=true` requests.

Observed error:

```text
LiteLLM.Success_Call Error: 1 validation error for AnthropicResponse
Input should be a valid dictionary or instance of AnthropicResponse
input_type=ResponseCompletedEvent
```

## Impact

- Claude Code traffic using `/v1/messages?beta=true` may not appear in `LiteLLM_SpendLogs`.
- Key Portal realtime speed and usage panels that read SpendLogs can show zero usage even while requests are succeeding.
- Other request paths, such as `/v1/responses`, can continue writing SpendLogs, so the database and Key Portal queries may look partially healthy.

## Root cause

The Anthropic Messages to Responses API adapter forwards a LiteLLM logging object with the wrong `call_type`.

Affected upstream file:

```text
litellm/llms/anthropic/experimental_pass_through/responses_adapters/handler.py
```

Problematic code:

```python
# Reclassify as acompletion so the success handler doesn't try to
# validate the Responses API event as an AnthropicResponse.
# (Mirrors the pattern used in LiteLLMMessagesToCompletionTransformationHandler.)
setattr(value, "call_type", CallTypes.anthropic_messages.value)
```

The comment says the logging object should be reclassified as `acompletion`, but the code keeps it as `anthropic_messages`. When the Responses API streaming path later passes a `ResponseCompletedEvent` into the success logging handler, LiteLLM routes it through Anthropic Messages logging and attempts:

```python
AnthropicResponse.model_validate(result)
```

That validation fails because the object is a Responses API event, not an Anthropic response.

## Verification performed

On node-b, a direct streaming request to LiteLLM reproduced the issue without changing the production service:

```text
POST /litellm/v1/messages?beta=true HTTP/1.1 200 OK
LiteLLM.Success_Call Error ... input_type=ResponseCompletedEvent
```

Then a temporary copied venv was patched with the one-line call type change and run on an alternate local port. The same request returned `200 OK`, no `AnthropicResponse` validation error was emitted, and a successful SpendLogs row was written.

## Hotfix

Change the adapter line from:

```python
setattr(value, "call_type", CallTypes.anthropic_messages.value)
```

to:

```python
setattr(value, "call_type", CallTypes.acompletion.value)
```

After applying the patch, restart `litellm-proxy.service` on the node.

## Production rollout on 2026-05-24

Patched and restarted:

- node-a (`172.31.17.144`)
- node-b (`172.31.26.28`)
- node-c (`172.31.16.7`)
- node-d (`172.31.24.86`)

Each patched file was backed up beside the original with a `.bak-calltype-<timestamp>` suffix before modification.

Verification requests to node-a, node-b, node-c, and node-d returned `200 OK` for `/litellm/v1/messages?beta=true` and did not emit the previous `AnthropicResponse` / `ResponseCompletedEvent` logging error.

## Longer-term fix

This is a site-packages hotfix and can be overwritten by a LiteLLM upgrade or venv rebuild. Prefer one of these durable fixes:

1. Upgrade to an upstream LiteLLM release that changes this call type correctly.
2. Submit or track an upstream PR for the one-line fix.
3. Build and deploy an internally patched LiteLLM wheel until upstream includes the fix.

Before removing the hotfix, verify that `/v1/messages?beta=true` streaming requests both return `200 OK` and write successful `LiteLLM_SpendLogs` rows.
