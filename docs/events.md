# Event Catalog

Cross-service communication is **events only** (CLAUDE.md §6). Core owns the
publisher (`app/core/events.py`); services own their subscribers (later phases).

- **Bus:** single custom EventBridge bus `a2z-bus`.
- **`source`** namespaces the producer: `a2z.core`, `a2z.invoicing`, `a2z.omnichannel`.
- **`detail-type`** = the dotted `event_type`.
- **`detail`** always includes `org_id` so subscribers can scope.
- Payloads are versioned implicitly by `event_type`; breaking shapes get a `v2` suffix.

## Events produced by Core (`source = a2z.core`)

| event_type | When | Key `detail` fields |
|---|---|---|
| `member.added` | A user is added to an org | `org_id`, `user_id`, `role`, `inviter_id` |
| `member.removed` | A user is removed from an org | `org_id`, `user_id`, `remover_id` |
| `member.role_changed` | A member's role changes | `org_id`, `user_id`, `old_role`, `new_role` |
| `email.bounced` | SES hard bounce processed | `org_id`, `email`, `bounce_type`, `message_id` |
| `email.complained` | SES complaint processed | `org_id`, `email`, `message_id` |
| `settings.changed` | Org settings updated | `org_id`, `changed_fields` |

> Subscribers are **not** built in Phase 1 — only the publisher is. Services add
> their own producers (`invoice.*`, etc.) and document them here as they land.
