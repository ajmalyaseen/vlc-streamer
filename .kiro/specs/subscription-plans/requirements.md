# Requirements Document

## Introduction

This feature adds a membership/subscription system to the existing Telegram "file → VLC stream link" bot (Pyrogram + aiohttp, MongoDB storage, multi-bot worker streaming on Koyeb). The goal is to monetize the bot through three tiers — **Free**, **Plus (₹27/month)**, and **Pro (₹67/month)** — where higher tiers unlock larger file sizes, more daily link generations, and longer link expiry.

The system enforces **only** these limits: daily link-generation count, maximum file size, link expiry duration, and subscription validity. There is **no** concurrent-stream limit, active-stream tracking, session tracking, playback tracking, or queue/priority logic — playback (opening, watching, seeking, or re-opening a link) never affects limits. Only the successful creation of a **new** stream link counts against the daily limit.

Payments are collected via **UPI** (India) using auto-filled UPI deep links, with **manual admin verification** of the UTR reference in a dedicated admin group. Plans, usage counters, and payment requests are stored in the existing free MongoDB Atlas database. Subscription expiry is handled **lazily** (checked at the moment of any plan-gated action), with no scheduler required. The system reuses the current architecture (handlers, db layer, server streaming, worker pool) and must remain configurable (plan limits, prices, UPI ID, admin group) via environment/config.

### Plan Definitions (configurable defaults)

| Plan | Price | Links/day | Max file size | Link expiry |
|------|-------|-----------|---------------|-------------|
| Free | ₹0 | 2 | 2 GB | 6 hours |
| Plus | ₹27/mo | 20 | 4 GB | 24 hours |
| Pro  | ₹67/mo | 100 | 10 GB | 7 days |

## Glossary

- **Plan / Tier:** A subscription level (`free`, `plus`, `pro`) that determines a user's limits.
- **Daily link limit:** Maximum number of NEW stream links a user may generate per calendar day, based on their plan.
- **Link expiry:** How long a generated stream link remains valid before it stops working.
- **Plan-gated action:** Any action that depends on plan state — generating a stream link, opening Premium Plans, viewing My Plan, or using a premium feature.
- **UPI deep link:** A `upi://pay` URL that pre-fills payee, amount, and note so a user can pay via Google Pay/PhonePe/Paytm/BHIM.
- **UTR:** Unique Transaction Reference number from the bank/UPI app; the user submits its last 4 digits as proof.
- **Payment reference:** A bot-generated unique ID (e.g. `P123456`) attached to a pending payment for tracking and admin verification.
- **Admin group:** A Telegram group (`ADMIN_GROUP_ID`) where payment requests are posted for approve/reject.

## Requirements

### Requirement 1: Plan Tiers and Configurable Limits

**User Story:** As the bot owner, I want three configurable subscription tiers with distinct limits, so that I can offer differentiated value and earn revenue.

#### Acceptance Criteria

1. THE system SHALL define three plans: `free`, `plus`, and `pro`.
2. THE system SHALL store, per plan, the following configurable attributes: price, daily link-generation limit, maximum file size, and link expiry duration.
3. WHERE plan values are defined, THE system SHALL read them from configuration (environment/config), defaulting to: Free (2 links/day, 2 GB, 6 h), Plus (₹27, 20 links/day, 4 GB, 24 h), Pro (₹67, 100 links/day, 10 GB, 7 days).
4. WHEN a new user interacts with the bot for the first time, THE system SHALL assign them the `free` plan by default.
5. THE system SHALL NOT implement any concurrent-stream limit, active-stream tracking, session tracking, playback tracking, or queue/priority logic.

### Requirement 2: Subscription Data Model

**User Story:** As the bot owner, I want subscription and usage data persisted, so that limits and plans survive restarts and are enforced reliably.

#### Acceptance Criteria

1. THE system SHALL store, per user: `user_id`, `username`, `plan`, `plan_expires_at`, `links_generated_today`, `last_reset_date`, and `created_at`.
2. THE system SHALL persist this data in the existing MongoDB backend (with the in-memory fallback used only when no `DATABASE_URL` is configured).
3. THE system SHALL store payment requests with at least: `user_id`, `plan`, `amount`, `reference`, `utr_last4`, `status` (pending/approved/rejected), and timestamps.
4. WHEN the data model is introduced, THE system SHALL migrate/backfill existing user records to include the new fields with safe defaults (plan = free).

### Requirement 3: Enhanced Start Menu

**User Story:** As a user, I want the start screen to show my plan, usage, and an always-visible upgrade button, so that I understand my limits and how to upgrade.

#### Acceptance Criteria

1. WHEN a user sends `/start`, THE system SHALL display a welcome message including the current plan name and today's usage (e.g. "Today's Usage: 0 / 2 links used").
2. THE system SHALL show inline buttons: "📂 Generate Link", "💎 Premium Plans", "📊 My Plan", and "ℹ️ Help".
3. THE system SHALL always display the "💎 Premium Plans" button on the start menu regardless of the user's current plan.

### Requirement 4: Premium Plans Page

**User Story:** As a user, I want to view all plans and their perks, so that I can decide which to buy.

#### Acceptance Criteria

1. WHEN a user taps "💎 Premium Plans" or sends `/plans`, THE system SHALL display all three tiers with their price, links/day, max file size, and expiry.
2. THE system SHALL show inline buttons "⭐ Buy Plus" and "🚀 Buy Pro" on the plans page.

### Requirement 5: Daily Usage Tracking and Reset

**User Story:** As the bot owner, I want daily link-generation counted and auto-reset, so that per-day limits are enforced fairly.

#### Acceptance Criteria

1. WHEN a user successfully generates a NEW stream link, THE system SHALL increment that user's `links_generated_today` by exactly 1.
2. THE system SHALL NOT increment the daily counter for any playback activity (opening, watching, seeking, forwarding, or re-opening an existing link).
3. WHEN a user performs an action AND their `last_reset_date` is before the current day, THE system SHALL reset `links_generated_today` to 0 and update `last_reset_date`.
4. THE system SHALL evaluate the daily limit based on the user's current plan's links/day value.

### Requirement 6: Daily Limit Reached Upgrade Flow

**User Story:** As a user who hit my daily link limit, I want a clear upgrade prompt, so that I know how to continue.

#### Acceptance Criteria

1. WHEN a user attempts to generate a link AND they have reached their plan's daily limit, THE system SHALL block link generation.
2. THE system SHALL display a "Daily limit reached" message stating how many links were used and the plan name.
3. THE system SHALL show upgrade options (Plus and Pro perks) with inline buttons "⭐ Upgrade to Plus" and "🚀 Upgrade to Pro".

### Requirement 7: File Size Limit Enforcement and Upgrade Flow

**User Story:** As a user, I want clear feedback when my file is too large for my plan, so that I can upgrade if needed.

#### Acceptance Criteria

1. WHEN a user sends a file larger than their plan's maximum file size, THE system SHALL refuse to generate a link.
2. THE system SHALL display a message showing the current plan, the maximum allowed size, and an upgrade prompt.
3. THE system SHALL show inline buttons "⭐ Upgrade to Plus" and "🚀 Upgrade to Pro".

### Requirement 8: Purchase Flow

**User Story:** As a user, I want to start buying a plan from the bot, so that I can upgrade easily.

#### Acceptance Criteria

1. WHEN a user taps "⭐ Buy Plus" or "🚀 Buy Pro", THE system SHALL show the selected plan's price, validity (30 days), and feature list.
2. THE system SHALL show inline buttons "💳 Pay Now" and "🔙 Back" on the purchase page.

### Requirement 9: UPI Payment and Deep Link

**User Story:** As a user, I want a one-tap UPI payment with details pre-filled, so that paying is frictionless.

#### Acceptance Criteria

1. THE system SHALL use a configurable UPI ID (e.g. `alaska@upi`).
2. WHEN a user taps "💳 Pay Now" AND has no pending request, THE system SHALL generate a unique payment reference (e.g. `P123456`) and store it as a pending request.
3. THE system SHALL generate a UPI deep link that auto-fills the UPI ID, the plan amount, and the payment note/reference, openable by Google Pay, PhonePe, Paytm, and BHIM.

### Requirement 10: Payment Confirmation via UTR

**User Story:** As a user, after paying I want to submit proof, so that an admin can verify and activate my plan.

#### Acceptance Criteria

1. AFTER presenting the UPI deep link, THE system SHALL ask the user to enter the last 4 digits of their UTR.
2. WHEN the user submits the UTR, THE system SHALL validate the format (exactly 4 digits) and store it against the payment reference.
3. AFTER UTR submission, THE system SHALL send a confirmation showing the plan, reference, masked UTR (`****6451`), and that an admin will verify shortly.

### Requirement 11: Admin Verification Group

**User Story:** As an admin, I want payment requests delivered to a group with approve/reject controls, so that I can verify payments efficiently.

#### Acceptance Criteria

1. THE system SHALL post each new payment request to a configurable `ADMIN_GROUP_ID`.
2. THE request message SHALL include username, user ID, plan, amount, reference, and UTR last 4 digits.
3. THE request message SHALL include inline buttons "✅ Approve" and "❌ Reject".

### Requirement 12: Approval Workflow

**User Story:** As an admin, when I approve a payment I want the plan activated automatically, so that the user gets access without manual steps.

#### Acceptance Criteria

1. WHEN an admin taps "✅ Approve", THE system SHALL activate the user's subscription, set `plan_expires_at` to now + 30 days, and update the database.
2. THE system SHALL notify the user that their plan is activated, including the plan name, valid-until date, and benefits.
3. THE system SHALL mark the payment request as approved.
4. IF the payment request is already approved, THE system SHALL ignore a repeated approval (no duplicate activation or expiry extension).

### Requirement 13: Rejection Workflow

**User Story:** As an admin, when I reject a payment I want the user notified, so that they can retry or contact support.

#### Acceptance Criteria

1. WHEN an admin taps "❌ Reject", THE system SHALL mark the request rejected and notify the user that verification failed with guidance to retry or contact support.
2. AFTER a request is rejected, THE system SHALL allow the user to create a new payment request.

### Requirement 14: Lazy Subscription Expiry

**User Story:** As the bot owner, I want plans to auto-expire without a scheduler, so that users revert to Free with no manual action.

#### Acceptance Criteria

1. WHEN a user performs any plan-gated action (generate link, open Premium Plans, view My Plan, or use a premium feature) AND their `plan_expires_at` is in the past, THE system SHALL downgrade them to `free`, reset their plan benefits, and update the database.
2. THE system SHALL NOT require a scheduler or background cron for expiry; a periodic cleanup task is optional and not required.

### Requirement 15: Feature Enforcement Before Link Generation

**User Story:** As the bot owner, I want all limits checked before a link is issued, so that plan rules are reliably enforced.

#### Acceptance Criteria

1. BEFORE generating any stream link, THE system SHALL check, in this order: subscription expiry (lazy downgrade if expired), file-size limit, and daily usage limit.
2. IF any check fails, THE system SHALL block generation and show the appropriate upgrade or limit prompt.
3. THE system SHALL only increment the daily counter after a link is successfully generated.

### Requirement 16: Admin Management Commands

**User Story:** As an admin, I want commands to manage plans and view analytics, so that I can operate the service.

#### Acceptance Criteria

1. THE system SHALL provide `/stats` returning total users and counts of free, plus, and pro users.
2. THE system SHALL provide `/plans_stats` returning subscription analytics.
3. THE system SHALL provide `/user USER_ID` returning that user's plan, expiry, and daily usage.
4. THE system SHALL provide `/addplan USER_ID plus DAYS` and `/addplan USER_ID pro DAYS` to grant a plan for a number of days.
5. THE system SHALL provide `/removeplan USER_ID` to revert a user to Free.
6. THE system SHALL provide `/extend USER_ID DAYS` to extend an existing plan.
7. THE system SHALL restrict all admin commands to configured admin user IDs.

### Requirement 17: Single Pending Payment, Edge Cases, and Configurability

**User Story:** As the bot owner, I want the system robust and configurable, so that it behaves correctly under abuse and is easy to tune.

#### Acceptance Criteria

1. IF a user already has a payment request with status `pending` AND taps "💳 Pay Now" again, THE system SHALL NOT create another reference or pending request, and SHALL show: "⚠️ You already have a payment request awaiting verification. Please wait for an admin to review your current request before creating a new one."
2. AFTER the pending request is approved or rejected, THE system SHALL allow the user to create a new payment request.
3. THE system SHALL prevent duplicate approvals of the same payment request.
4. THE system SHALL make plan limits, prices, UPI ID, and admin group ID configurable.
5. THE system SHALL log key events (link generation, limit blocks, payment created, approved, rejected, expiry downgrade) for observability.
6. THE system SHALL be implemented asynchronously and integrate with the existing handlers, db layer, and streaming server without breaking current functionality (file → link, VLC streaming, force-sub, worker pool).
