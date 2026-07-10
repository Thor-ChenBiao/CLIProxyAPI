# API Key Speed Groups Design

## Goal

Allow each authenticated user to choose the default execution speed for each API key. Existing and newly issued keys default to Standard. Users can switch their own keys between Standard and Fast immediately without approval.

This feature sets a default only. An explicit per-request `service_tier` always wins.

## Non-goals

- Do not select speed by model.
- Do not create separate Fast model names or aliases.
- Do not automatically downgrade Fast when account capacity is low.
- Do not change Nginx, NLB, or CLIProxyAPI core behavior.
- Do not require approval or impose a Fast-key count limit.

## Current State

- `model_group` controls model access and must remain independent from speed.
- Key Portal currently has 369 active keys; 361 match a LiteLLM virtual key.
- LiteLLM currently has 608 virtual keys and none has a speed-group marker.
- Key Portal already enforces ownership for key operations.
- New-key issuance has direct and Feishu-approval paths.
- Existing `usr_pool_*` keys are normalized by Nginx to `sk-usr_pool_*` before LiteLLM authentication.

## Data Model

Store the authoritative speed group in `LiteLLM_VerificationToken.metadata`:

```json
{
  "speed_group": "standard"
}
```

Allowed values:

- `standard`: normal execution; default for interactive, offline, batch, and background work unless the user chooses otherwise.
- `fast`: default to the provider priority tier for requests that do not explicitly select a tier.

Missing, empty, or invalid values are interpreted as `standard`. Existing keys therefore require no bulk metadata update. The Key Portal table does not add a second authoritative field; Portal reads the LiteLLM value for display.

## Request Policy

The LiteLLM pre-call hook applies this precedence:

1. If the request contains a non-empty `service_tier`, preserve it unchanged.
2. Otherwise, if the authenticated key has `metadata.speed_group == "fast"`, set `service_tier` to `priority`.
3. Otherwise, leave `service_tier` unset and use Standard behavior.

Examples:

| Key group | Request value | Effective behavior |
| --- | --- | --- |
| Fast | omitted | Fast (`priority`) |
| Fast | `default` | Standard |
| Standard | omitted | Standard |
| Standard | `priority` | Fast |

The hook does not contain a model allowlist and does not inspect capacity. Existing LiteLLM parameter dropping and provider translation remain responsible for provider compatibility.

## Key Creation

The application form adds a required two-option execution-mode control:

- **Standard (default):** required recommendation for offline jobs, batch processing, and background tasks.
- **Fast:** intended for human-interactive work; faster responses consume the shared account pool more quickly.

The request sends `speed_group`, validated server-side. Direct issuance adds it to the existing LiteLLM metadata sent to `/key/generate`.

For approval-required model groups, `speed_group` is stored in the existing approval request `request_payload`. The approval worker passes it to key issuance after approval. Speed selection itself never requires approval.

## Existing-Key Management

The My Keys page displays the effective speed group for each real key and provides an inline Standard/Fast segmented control. A change is saved immediately; the control is disabled while saving and reverts with an inline error if the update fails.

Synthetic usage rows cannot be changed. A regular user can change only keys owned by the current Feishu identity. Existing administrator access rules remain unchanged.

The update endpoint accepts the key and requested group in the JSON request body, never in the URL. It performs:

1. Session and ownership validation.
2. Enum validation for `standard` or `fast`.
3. Canonical LiteLLM key normalization, including `usr_pool_*` to `sk-usr_pool_*`.
4. Existing metadata lookup.
5. Metadata merge that preserves email, model group, issuer, labels, and other fields.
6. LiteLLM `/key/update` call so LiteLLM invalidates its key cache.
7. Authoritative value return without echoing or logging the full key.

## Stored-Key Compatibility

Existing keys with no marker display and behave as Standard. Before release, reconcile the eight Portal keys that do not currently match a LiteLLM token. Each must either be repaired with its existing owner or confirmed obsolete; the UI must not report a successful switch if no LiteLLM token was updated.

LiteLLM keys with no Portal ownership remain Standard and are not user-editable until they are associated through the existing ownership or local-key claim flow.

## Failure Handling

- Invalid group: HTTP 400.
- Missing or synthetic key: HTTP 404 or a non-editable UI state.
- Key owned by another user: HTTP 403.
- LiteLLM lookup/update failure: HTTP 502; retain the previous displayed value.
- Unknown metadata value: treat as Standard and expose a server-side diagnostic without logging secrets.
- No capacity-based fallback is performed.

## Test Coverage

- Hook unit tests for both groups, missing metadata, invalid metadata, and explicit request overrides.
- Endpoint tests for authentication, ownership, administrators, invalid groups, missing tokens, and metadata preservation.
- Creation tests for direct and approval-required flows.
- Compatibility tests for `usr_pool_*` key normalization.
- UI tests for default selection, successful switching, rollback on failure, mobile cards, and synthetic rows.
- Integration checks on both nodes for Standard, Fast, explicit Standard override, and explicit Fast override.

## Rollout

1. Add and test the standalone LiteLLM hook and Portal backend/UI changes without modifying Nginx or CLIProxyAPI.
2. Reconcile the eight unmatched Portal keys.
3. Drain node B, deploy the hook, run the request-policy matrix, and return B to the NLB.
4. Repeat on node A.
5. Deploy Key Portal after both LiteLLM nodes understand the metadata.
6. Verify a speed change made in Portal is honored through both NLB nodes.

Rollback removes the hook registration and hides the Portal controls. Existing metadata is harmless and can remain in Postgres; requests revert to Standard unless the caller explicitly sets `service_tier`.
