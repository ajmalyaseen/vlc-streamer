# Design Document

## Overview

This design adds a subscription/membership layer (Free / Plus / Pro) to the existing Telegram file→VLC bot **without changing the streaming engine**. It introduces a small plans-config module, extends the existing MongoDB-backed user store with subscription + payment data, and adds new Telegram handlers/callbacks for the user-facing menus, UPI purchase flow, and admin verification. Enforcement (file-size, daily-link-limit, expiry) is injected at the single point where links are created today: `send_stream_link()` in `bot/handlers.py`.

Design principles:
- **Reuse, don't rewrite.** The streaming path (`server.py`, `streamer.py`, worker pool, signed links) is untouched.
- **Config-driven.** All plan limits, prices, UPI ID, and admin group are configurable via env vars with sensible defaults.
- **Lazy expiry.** No scheduler; expiry is resolved on each plan-gated action.
- **Storage-agnostic.** New DB methods are added to both the Mongo backend and the in-memory fallback so local testing still works.

## Architecture

```
                ┌─────────────────────── Telegram (Pyrogram) ───────────────────────┐
                │                                                                    │
  /start  ──────┼──► on_start ───► render start menu (plan + usage)                  │
  /plans  ──────┼──► on_plans ───► premium plans page                               │
  file    ──────┼──► on_file ───► EnforcementService.check() ──► send_stream_link    │
  callbacks ────┼──► on_callback ─► buy / pay_now / myplan / upgrade / approve/reject │
  text (UTR) ───┼──► on_text ───► PaymentService.submit_utr                          │
                └────────────────────────────┬───────────────────────────────────────┘
                                              │
                 ┌────────────────────────────┼────────────────────────────┐
                 ▼                            ▼                             ▼
          plans.py (config)          PaymentService               SubscriptionService
          PLAN limits/prices         (refs, UPI link, UTR,         (get/resolve plan,
                                      admin post, approve/reject)   daily usage, expiry)
                                              │                             │
                                              └──────────────┬──────────────┘
                                                             ▼
                                                    db.py  (Mongo / Memory)
                                              users + payments collections
```

New modules:
- **`bot/plans.py`** — immutable plan catalog + helpers (`get_plan`, `plan_limits`, `format_plans_text`).
- **`bot/subscription.py`** — `SubscriptionService`: resolve a user's effective plan (with lazy expiry), read/reset daily usage, enforce limits, mutate plans (admin grants).
- **`bot/payments.py`** — `PaymentService`: create reference, build UPI deep link, attach UTR, post to admin group, approve/reject, single-pending enforcement.

Extended modules:
- **`bot/db.py`** — add subscription + payment persistence to `MongoUserDB` and `MemoryUserDB` (rename conceptually to a user/subscription store; keep class names for compatibility).
- **`bot/config.py`** — add `Config` fields for UPI ID, admin group, plan overrides, prices.
- **`bot/handlers.py`** — new commands/callbacks + enforcement wired into `send_stream_link`/`on_file`.
- **`bot/utils.py`** — `make_payment_reference()`, `build_upi_link()`.
- **`bot/main.py`** — pass services into `register_handlers`; ensure the admin group / payment callbacks are registered.

## Components and Interfaces

### 1. Plans catalog — `bot/plans.py`

```python
@dataclass(frozen=True)
class Plan:
    key: str            # "free" | "plus" | "pro"
    name: str           # "Free" | "Plus" | "Pro"
    price: int          # rupees per 30 days (0 for free)
    daily_links: int    # links/day
    max_file_size: int  # bytes
    expiry_seconds: int # stream-link validity
    emoji: str

PLANS: dict[str, Plan]            # built from config defaults
DEFAULT_PLAN = "free"
VALIDITY_DAYS = 30                # paid plan duration

def get_plan(key: str) -> Plan
def format_plans_text() -> str    # the Premium Plans page body
def upgrade_markup() -> InlineKeyboardMarkup  # Upgrade to Plus / Pro
```

Limits are read from config (env) at construction with the documented defaults.

### 2. Subscription service — `bot/subscription.py`

```python
class SubscriptionService:
    def __init__(self, db, cfg): ...

    async def get_state(self, user) -> UserState
        # ensures user row exists, applies lazy daily reset + lazy expiry,
        # returns: plan(Plan), expires_at, used_today, remaining_today

    async def can_generate(self, user, file_size) -> Decision
        # returns Decision(ok: bool, reason: "ok"|"file_too_big"|"daily_limit",
        #                  plan, limit, used)

    async def record_link(self, user_id) -> None          # +1 daily counter
    async def set_plan(self, user_id, plan_key, days) -> datetime   # admin/approve
    async def remove_plan(self, user_id) -> None
    async def extend_plan(self, user_id, days) -> datetime
    async def analytics(self) -> dict   # totals per plan
```

Lazy logic inside `get_state`:
- If `last_reset_date < today` → set `links_generated_today = 0`, `last_reset_date = today`.
- If `plan != free` and `plan_expires_at < now` → set `plan = free`, clear expiry.
- Persist any change.

### 3. Payment service — `bot/payments.py`

```python
class PaymentService:
    def __init__(self, db, cfg, bot): ...

    async def start_purchase(self, user, plan_key) -> PurchaseResult
        # if a pending/awaiting payment exists -> PurchaseResult(blocked=True)
        # else create payment {status:"awaiting_utr"} + reference, return UPI link

    async def submit_utr(self, user, utr4) -> bool
        # find user's awaiting_utr payment, validate 4 digits, set status="pending",
        # store utr_last4, post request to ADMIN_GROUP_ID with approve/reject buttons

    async def approve(self, reference) -> ApproveResult   # idempotent
    async def reject(self, reference) -> RejectResult

    def upi_link(self, amount, reference) -> str
```

Statuses: `awaiting_utr` → `pending` → `approved` | `rejected`.
"Single pending" = any payment with status in (`awaiting_utr`, `pending`).

### 4. DB layer — `bot/db.py` (extended)

Both `MongoUserDB` and `MemoryUserDB` gain:

```python
# users
async def get_user(user_id) -> dict | None
async def upsert_user(user_id, username, first_name) -> dict   # ensures defaults
async def update_user(user_id, fields: dict) -> None
async def count_by_plan() -> dict   # {"free": n, "plus": n, "pro": n, "total": n}

# payments
async def create_payment(doc: dict) -> None
async def get_pending_payment(user_id) -> dict | None    # status in (awaiting_utr, pending)
async def get_payment(reference) -> dict | None
async def update_payment(reference, fields: dict) -> None
```

Existing `add_user`, `all_users`, `count`, `all_users_detailed` are preserved (used by broadcast). `upsert_user` provides default fields: `plan="free"`, `plan_expires_at=None`, `links_generated_today=0`, `last_reset_date=<today>`, `created_at=<now>`.

Backfill: `upsert_user` uses Mongo `$setOnInsert` for defaults so existing records gain fields lazily on next interaction (Requirement 2.4).

### 5. Config — `bot/config.py` (extended)

New `Config` fields (all optional with defaults):

| Field | Env var | Default |
|-------|---------|---------|
| `upi_id` | `UPI_ID` | `""` (purchase disabled if empty) |
| `admin_group_id` | `ADMIN_GROUP_ID` | `0` |
| `plus_price` / `pro_price` | `PLUS_PRICE` / `PRO_PRICE` | 27 / 67 |
| `free_daily` / `plus_daily` / `pro_daily` | `FREE_DAILY` / `PLUS_DAILY` / `PRO_DAILY` | 2 / 20 / 100 |
| `free_max_gb` / `plus_max_gb` / `pro_max_gb` | `FREE_MAX_GB` / ... | 2 / 4 / 10 |
| `free_expiry_h` / `plus_expiry_h` / `pro_expiry_h` | `FREE_EXPIRY_H` / ... | 6 / 24 / 168 |

`admins` (existing) gates admin commands and approve/reject.

### 6. Handlers/callbacks — `bot/handlers.py`

New message handlers:
- `/start` → enhanced menu (plan + usage + buttons). Replaces current `on_start`.
- `/plans` → premium plans page.
- `/myplan` → current plan + usage + expiry.
- `/plans_stats`, `/user`, `/addplan`, `/removeplan`, `/extend` → admin (extend existing `/stats`).
- `on_text` (private, non-command) → if user has an `awaiting_utr` payment, treat text as UTR.

`on_callback` new `data` cases:
- `menu_generate` → instruct user to send a file.
- `menu_plans` / `plans` → plans page.
- `menu_myplan` → my plan.
- `buy_plus` / `buy_pro` → purchase page (price, validity, Pay Now / Back).
- `pay_plus` / `pay_pro` → `PaymentService.start_purchase` → UPI link + ask UTR (or blocked message).
- `approve_<ref>` / `reject_<ref>` → admin-only, calls PaymentService; edits the admin message to show outcome.
- `menu_home` / `back` → start menu.

**Enforcement wiring** (Requirement 15): inside `on_file`, after force-sub passes and before `send_stream_link`:
```python
state = await subs.get_state(m.from_user)            # lazy reset + expiry
decision = await subs.can_generate(m.from_user, media.file_size)
if not decision.ok and reason == "file_too_big": -> file-size upgrade prompt; return
if not decision.ok and reason == "daily_limit":  -> daily-limit upgrade prompt; return
await send_stream_link(...)                          # uses plan.expiry_seconds for link TTL
await subs.record_link(user_id)                      # +1 only on success
```

Link expiry: the signed token currently never expires. To honor per-plan expiry, the token will embed an expiry timestamp and `verify_token` will reject expired links (see Utils + server change below). This is the only streaming-side change.

### 7. Utils — `bot/utils.py` (extended)

```python
def make_payment_reference() -> str          # "P" + 6 base36 chars, unique-ish
def build_upi_link(upi_id, name, amount, note) -> str
    # upi://pay?pa=<id>&pn=<name>&am=<amount>&cu=INR&tn=<note>
```

Token expiry (per-plan link expiry, Requirement 1/4):
```python
def make_token(chat_id, message_id, secret, expires_at: int = 0) -> str
def verify_token(chat_id, message_id, token, secret, expires_at: int = 0) -> bool
```
The stream URL gains an `&exp=<unix>` query param; `server.py` passes `exp` into `verify_token` and returns 410 (Gone) if `exp` is set and in the past. Links with `exp=0`/absent remain permanent (backward compatible).

## Data Models

### users collection (extends current `{_id, username, first_name}`)
```
{
  _id: <user_id:int>,
  username: str | null,
  first_name: str | null,
  plan: "free" | "plus" | "pro",        # default "free"
  plan_expires_at: datetime | null,     # null for free
  links_generated_today: int,           # default 0
  last_reset_date: "YYYY-MM-DD",         # default today
  created_at: datetime
}
```

### payments collection (new)
```
{
  _id: <reference:str>,        # "P123456"
  user_id: int,
  username: str | null,
  plan: "plus" | "pro",
  amount: int,                 # rupees
  utr_last4: str | null,
  status: "awaiting_utr" | "pending" | "approved" | "rejected",
  created_at: datetime,
  decided_at: datetime | null,
  decided_by: int | null       # admin id
}
```

Indexes (Mongo): `payments.user_id` for pending lookups; `_id` is the reference.

## Key Flows

### Purchase + approval (sequence)
```
User taps Buy Plus → buy_plus page (price/validity/features) → Pay Now
  PaymentService.start_purchase:
    has pending? → "already awaiting verification" (stop)
    else create payment(awaiting_utr) + reference → reply UPI deep link + "send UTR last 4"
User sends "6451" (on_text):
    PaymentService.submit_utr → validate 4 digits → status=pending, store utr_last4
       → post to ADMIN_GROUP_ID [Approve|Reject]
       → user: "payment submitted, awaiting admin"
Admin taps Approve (approve_<ref>):
    PaymentService.approve (idempotent):
       payment.status -> approved; SubscriptionService.set_plan(user, plan, 30d)
       → edit admin msg "Approved by X"; DM user "Plan activated, valid until DATE"
Admin taps Reject:
    payment.status -> rejected; edit admin msg; DM user "verification failed"
```

### Link generation with enforcement
```
file received → force-sub ok → get_state (lazy reset+expiry)
  file_size > plan.max_file_size → file-size upgrade prompt
  used_today >= plan.daily_links → daily-limit upgrade prompt
  else → send_stream_link (token exp = now + plan.expiry_seconds) → record_link (+1)
```

## Error Handling

- **No `DATABASE_URL`:** subscription data lives in the in-memory store; works for testing, resets on restart (same trade-off as today's user tracking).
- **UPI not configured (`UPI_ID` empty):** buy buttons show "Payments are not enabled yet" instead of generating a reference.
- **Admin group not configured:** payment requests fall back to DMing the configured `admins`; if none, log an error and tell the user to contact support.
- **Invalid UTR:** if not exactly 4 digits, re-prompt without changing state.
- **Duplicate approve/reject:** `approve`/`reject` check current status and no-op if already decided (Requirement 12.4, 17.3).
- **Expired link hit in VLC:** server returns HTTP 410; player stops. (Link can be regenerated, counting against the daily limit.)
- **Pyrogram FloodWait on admin/user notifications:** caught and retried briefly; failure to notify does not roll back an approval.

## Testing Strategy

- **Unit (pure logic):**
  - `plans.get_plan`, limit lookups, config overrides.
  - `SubscriptionService.get_state` lazy reset (date rollover) and lazy expiry (downgrade to free).
  - `can_generate` matrix: under/over file size, under/at/over daily limit, per plan.
  - `make_payment_reference` uniqueness/format; `build_upi_link` query encoding; token expiry accept/reject.
  - Single-pending rule; idempotent approve.
- **Integration (with in-memory DB):**
  - Full purchase flow: start → UTR → admin approve → plan active + expiry set.
  - Reject flow; re-purchase allowed after decision.
  - Enforcement in `on_file` blocks oversize / over-limit and increments only on success.
- **Manual smoke (Telegram):** /start menu, /plans, buy → UPI link opens an app, admin group receives request, approve activates, expired link returns 410.

Tests use the in-memory backend so no live MongoDB/Telegram is required for CI.

## Correctness Properties

These invariants must always hold and should be covered by tests.

### Property 1: Counter only on new-link success
No playback request (open/watch/seek/re-open) ever changes `links_generated_today`; only a successfully generated new link increments it by 1.
**Validates: Requirements 5.1, 5.2, 15.3**

### Property 2: Daily reset before check
After a calendar-date rollover, `links_generated_today` resets to 0 before any limit check on that day.
**Validates: Requirements 5.3, 5.4**

### Property 3: Lazy expiry to Free
Once `plan_expires_at < now`, the next plan-gated action yields `plan == free` and `plan_expires_at == null`; a user is never treated as paid past expiry.
**Validates: Requirements 14.1, 14.2**

### Property 4: Authoritative gates
A link is generated only if `file_size <= plan.max_file_size` AND `used_today < plan.daily_links` at the moment of generation.
**Validates: Requirements 6.1, 7.1, 15.1, 15.2**

### Property 5: At most one open payment
At any time a user has zero or one payment with status in {`awaiting_utr`, `pending`}.
**Validates: Requirements 17.1, 17.2**

### Property 6: Idempotent approval
Approving a reference activates the plan exactly once; repeated approvals do not extend expiry or re-notify.
**Validates: Requirements 12.4, 17.3**

### Property 7: Definite expiry on grant
After approval or `/addplan`, `plan_expires_at == now + days` and `plan` matches the purchased/granted tier.
**Validates: Requirements 12.1, 16.4**

### Property 8: Link TTL equals plan expiry
A link's `exp` equals creation time + the plan's `expiry_seconds`; after `exp`, the stream endpoint refuses the link.
**Validates: Requirements 1.2, 1.3**

### Property 9: Backward compatibility
Links without an `exp` parameter and all existing streaming behavior continue to work unchanged.
**Validates: Requirements 19.6** *(per requirements R17.6: integrate without breaking current functionality)*

### Property 10: Configurability
Changing a plan limit, price, UPI ID, or admin group via config changes behavior with no code edits.
**Validates: Requirements 1.3, 17.4**
