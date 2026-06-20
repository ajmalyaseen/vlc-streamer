# Implementation Plan

## Overview

Incremental, test-driven build of the Free/Plus/Pro membership system on top of the existing bot. Work proceeds bottom-up: config + plans catalog, then the data layer, then services (subscription + payments), then per-plan link expiry, then the user menus, enforcement, purchase flow, admin verification, wiring, and tests. Each task is self-contained and references the requirements it satisfies. The streaming engine is not modified except for an optional, backward-compatible link-expiry check.

## Tasks

- [ ] 1. Add plan configuration module and config fields
  - Create `bot/plans.py` with a frozen `Plan` dataclass and a `PLANS` catalog (free/plus/pro) built from config with documented defaults; add `get_plan`, `format_plans_text`, and `upgrade_markup` helpers.
  - Extend `bot/config.py` `Config` with: `upi_id`, `admin_group_id`, `plus_price`, `pro_price`, per-plan `daily`, `max_gb`, `expiry_h`; load from env in `load_config` with defaults (2/20/100, 2/4/10 GB, 6/24/168 h, ₹27/₹67).
  - Update `.env.example` with the new optional variables and comments.
  - _Requirements: 1.1, 1.2, 1.3, 17.4_

- [ ] 2. Extend the data layer for subscriptions and payments
- [ ] 2.1 Add user subscription persistence to both DB backends
  - In `bot/db.py`, add to `MongoUserDB` and `MemoryUserDB`: `upsert_user` (defaults via `$setOnInsert`: plan=free, expiry=None, links_generated_today=0, last_reset_date=today, created_at=now), `get_user`, `update_user`, `count_by_plan`.
  - Preserve existing `add_user`, `all_users`, `count`, `all_users_detailed`.
  - _Requirements: 2.1, 2.2, 2.4_
- [ ] 2.2 Add payments persistence to both DB backends
  - Add `create_payment`, `get_pending_payment(user_id)` (status in awaiting_utr/pending), `get_payment(reference)`, `update_payment(reference, fields)`.
  - _Requirements: 2.3, 17.1, 17.3_
- [ ] 2.3 Unit-test the DB layer with the in-memory backend
  - Test default backfill on upsert, plan counts, pending lookup returns only open statuses, payment update transitions.
  - _Requirements: 2.1, 2.3, 2.4_

- [ ] 3. Implement SubscriptionService (lazy reset + expiry + enforcement)
- [ ] 3.1 Create `bot/subscription.py` with `get_state` and `can_generate`
  - `get_state(user)`: ensure row, apply lazy daily reset (date rollover) and lazy expiry (paid+expired → free), persist, return plan/expiry/used/remaining.
  - `can_generate(user, file_size)`: decision ok / file_too_big / daily_limit with plan + limit + used. `record_link(user_id)`: +1 daily counter.
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 6.1, 7.1, 14.1, 14.2, 15.1, 15.2, 15.3_
- [ ] 3.2 Add plan mutation + analytics methods
  - `set_plan(user_id, plan_key, days)` → set plan + expiry = now+days; `remove_plan`; `extend_plan(days)`; `analytics()` → counts per plan.
  - _Requirements: 12.1, 16.1, 16.2, 16.4, 16.5, 16.6_
- [ ] 3.3 Unit-test SubscriptionService
  - Date-rollover reset; expired paid → free; can_generate matrix; set/extend expiry math.
  - _Requirements: 5.3, 5.4, 6.1, 7.1, 14.1, 12.1, 16.4_

- [ ] 4. Add per-plan link expiry to tokens and the stream endpoint
- [ ] 4.1 Extend token signing/verification in `bot/utils.py`
  - Add optional `expires_at` to `make_token`/`verify_token` (signed into the HMAC); keep absent/0 fully backward compatible. Unit-test valid/expired/legacy.
  - _Requirements: 1.2, 1.3_
- [ ] 4.2 Honor `exp` in `bot/server.py` stream/watch handlers
  - Parse optional `exp` query param; pass to `verify_token`; if set and in the past, return HTTP 410 before streaming.
  - _Requirements: 1.2, 1.3_

- [ ] 5. Implement payment helpers in `bot/utils.py`
  - Add `make_payment_reference()` ("P" + short base36) and `build_upi_link(upi_id, name, amount, note)` → valid `upi://pay?...`. Unit-test format/uniqueness and query encoding.
  - _Requirements: 9.1, 9.2, 9.3_

- [ ] 6. Implement PaymentService
- [ ] 6.1 Create `bot/payments.py` with purchase + UTR + decisions
  - `start_purchase` (block if pending exists else create awaiting_utr + reference, return UPI link), `submit_utr` (validate 4 digits, set pending + utr_last4), `approve` (idempotent), `reject`, `upi_link`.
  - _Requirements: 9.2, 10.1, 10.2, 12.1, 12.4, 13.1, 13.2, 17.1, 17.2, 17.3_
- [ ] 6.2 Unit-test PaymentService
  - Single-pending enforcement; UTR validation; approve activates exactly once + idempotent; reject allows re-purchase.
  - _Requirements: 17.1, 17.2, 17.3, 12.4_

- [ ] 7. Build user-facing menus and commands in `bot/handlers.py`
- [ ] 7.1 Enhanced `/start` menu with plan + usage
  - Welcome with current plan and used/limit today; buttons Generate Link / Premium Plans / My Plan / Help (Premium always shown).
  - _Requirements: 3.1, 3.2, 3.3_
- [ ] 7.2 Premium Plans page (`/plans` + callback) and `/myplan`
  - Plans page shows all tiers + Buy Plus/Buy Pro; My Plan shows plan, usage, expiry; apply lazy `get_state` on open.
  - _Requirements: 4.1, 4.2, 14.1_

- [ ] 8. Wire enforcement into file handling
  - In `on_file`: after force-sub, `get_state` then `can_generate(file_size)`; on file_too_big show file-size upgrade prompt; on daily_limit show daily-limit prompt; else `send_stream_link` then `record_link`. Pass plan `expiry_seconds` into link creation (`exp`).
  - _Requirements: 6.1, 6.2, 6.3, 7.1, 7.2, 7.3, 15.1, 15.2, 15.3_

- [ ] 9. Implement purchase + UTR + admin-post flow
- [ ] 9.1 Buy/Pay callbacks
  - `buy_plus`/`buy_pro` → purchase page (price, 30-day validity, features, Pay Now / Back); `pay_*` → `start_purchase` → UPI deep link + ask UTR; blocked → "already awaiting verification".
  - _Requirements: 8.1, 8.2, 9.1, 9.2, 9.3, 17.1_
- [ ] 9.2 UTR text capture and admin-group posting
  - Private non-command text handler: if user has awaiting_utr payment, `submit_utr` → post to `ADMIN_GROUP_ID` (fallback DM admins) with Approve/Reject + confirm to user (masked UTR).
  - _Requirements: 10.1, 10.2, 10.3, 11.1, 11.2, 11.3_

- [ ] 10. Implement admin verification and management
- [ ] 10.1 Approve/Reject callbacks (admin-only, idempotent)
  - `approve_<ref>` → activate 30 days, edit admin message, DM user activation (valid-until + benefits); `reject_<ref>` → reject + DM user failure; ignore non-admins.
  - _Requirements: 12.1, 12.2, 12.3, 12.4, 13.1, 13.2_
- [ ] 10.2 Admin management commands
  - Extend `/stats` (totals + per-plan); add `/plans_stats`, `/user`, `/addplan`, `/removeplan`, `/extend`; gate by `cfg.admins`.
  - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7_

- [ ] 11. Wire services into startup
  - In `bot/main.py`, construct `SubscriptionService` + `PaymentService` and pass into `register_handlers`; add `/plans`, `/myplan` to `set_bot_commands`; confirm login/server/force-sub/worker pool unaffected.
  - _Requirements: 17.5, 17.6_

- [ ] 12. Integration tests (in-memory backend)
  - Full purchase (start → UTR → approve → active + expiry); reject → re-purchase allowed; enforcement blocks oversize/over-limit and increments only on success; expired link → 410.
  - _Requirements: 6.1, 7.1, 10.1, 12.1, 13.1, 15.1, 17.1, 17.2_

- [ ] 13. Final verification and docs
  - Run tests; `python -m py_compile` all modules; update README/.env.example with plan/payment config and admin command reference.
  - _Requirements: 17.4, 17.5, 17.6_

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "rationale": "Plans catalog + config underpin everything." },
    { "wave": 2, "tasks": ["2.1", "2.2", "4.1", "5"], "rationale": "Data layer, token expiry, and payment helpers depend only on config/plans." },
    { "wave": 3, "tasks": ["2.3", "3.1", "4.2", "6.1"], "rationale": "Services and endpoint expiry build on the data layer and token changes." },
    { "wave": 4, "tasks": ["3.2", "3.3", "6.2"], "rationale": "Service mutations/analytics and their unit tests." },
    { "wave": 5, "tasks": ["7.1", "7.2", "8"], "rationale": "User menus and file-handling enforcement need the subscription service + expiry." },
    { "wave": 6, "tasks": ["9.1", "9.2", "10.1", "10.2"], "rationale": "Purchase/UTR flow and admin verification need payment service + menus." },
    { "wave": 7, "tasks": ["11"], "rationale": "Wire services into startup once all handlers exist." },
    { "wave": 8, "tasks": ["12"], "rationale": "End-to-end integration tests after wire-up." },
    { "wave": 9, "tasks": ["13"], "rationale": "Final verification and docs." }
  ]
}
```

Visual summary:
```
1
├─ 2.1, 2.2, 4.1, 5
│   ├─ 2.3, 3.1, 4.2, 6.1
│   │   ├─ 3.2, 3.3, 6.2
│   │   │   ├─ 7.1, 7.2, 8
│   │   │   │   ├─ 9.1, 9.2, 10.1, 10.2
│   │   │   │   │   └─ 11 ── 12 ── 13
```

## Notes

- Build and test against the in-memory DB backend so no live MongoDB/Telegram is required during development; the Mongo backend mirrors the same method signatures.
- The streaming path stays intact; the only streaming-side change is the optional, backward-compatible `exp` link-expiry check (tasks 4.1/4.2).
- Keep all plan limits, prices, UPI ID, and admin group configurable via env (no hardcoded values in logic).
- After each service task, run its unit tests before moving on; wire-up (task 11) is the first point the full bot is exercised end-to-end.
- Do not re-introduce any concurrent-stream/queue/playback tracking — out of scope per requirements.
