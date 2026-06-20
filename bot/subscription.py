"""Subscription state with lazy daily-reset, lazy expiry, and limit checks."""
import datetime as dt
import logging
from dataclasses import dataclass

from .plans import Plan

log = logging.getLogger("subscription")


def _today() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d")


def _now() -> dt.datetime:
    return dt.datetime.utcnow()


@dataclass
class UserState:
    plan: Plan
    expires_at: object        # datetime | None
    used_today: int
    remaining_today: int


@dataclass
class Decision:
    ok: bool
    reason: str               # "ok" | "file_too_big" | "daily_limit"
    plan: Plan
    limit: int
    used: int


class SubscriptionService:
    def __init__(self, db, plans: dict) -> None:
        self.db = db
        self.plans = plans

    def plan_of(self, key) -> Plan:
        return self.plans.get(key or "free", self.plans["free"])

    async def get_state(self, user) -> UserState:
        """Resolve a user's effective plan, applying lazy reset + lazy expiry."""
        user_id = user.id if hasattr(user, "id") else int(user)
        username = getattr(user, "username", None)
        first_name = getattr(user, "first_name", None)
        rec = await self.db.upsert_user(user_id, username, first_name) or {}

        changes = {}
        if rec.get("last_reset_date") != _today():
            rec["links_generated_today"] = 0
            rec["last_reset_date"] = _today()
            changes["links_generated_today"] = 0
            changes["last_reset_date"] = _today()

        plan_key = rec.get("plan", "free") or "free"
        expires = rec.get("plan_expires_at")
        if plan_key != "free" and expires is not None and expires < _now():
            plan_key = "free"
            rec["plan"] = "free"
            rec["plan_expires_at"] = None
            changes["plan"] = "free"
            changes["plan_expires_at"] = None

        if changes:
            await self.db.update_user(user_id, changes)

        plan = self.plan_of(plan_key)
        used = rec.get("links_generated_today", 0) or 0
        return UserState(
            plan=plan,
            expires_at=rec.get("plan_expires_at"),
            used_today=used,
            remaining_today=max(0, plan.daily_links - used),
        )

    async def can_generate(self, user, file_size: int) -> Decision:
        state = await self.get_state(user)
        plan = state.plan
        if file_size > plan.max_file_size:
            return Decision(False, "file_too_big", plan, plan.max_file_size, state.used_today)
        if state.used_today >= plan.daily_links:
            return Decision(False, "daily_limit", plan, plan.daily_links, state.used_today)
        return Decision(True, "ok", plan, plan.daily_links, state.used_today)

    async def record_link(self, user_id) -> None:
        rec = await self.db.get_user(user_id) or {}
        used = (rec.get("links_generated_today", 0) or 0) + 1
        await self.db.update_user(user_id, {"links_generated_today": used})

    async def set_plan(self, user_id, plan_key: str, days: int) -> dt.datetime:
        expires = _now() + dt.timedelta(days=days)
        await self.db.update_user(user_id, {"plan": plan_key, "plan_expires_at": expires})
        return expires

    async def remove_plan(self, user_id) -> None:
        await self.db.update_user(user_id, {"plan": "free", "plan_expires_at": None})

    async def extend_plan(self, user_id, days: int) -> dt.datetime:
        rec = await self.db.get_user(user_id) or {}
        base = _now()
        cur = rec.get("plan_expires_at")
        if cur and cur > base:
            base = cur
        expires = base + dt.timedelta(days=days)
        await self.db.update_user(user_id, {"plan_expires_at": expires})
        return expires

    async def analytics(self) -> dict:
        return await self.db.count_by_plan()
