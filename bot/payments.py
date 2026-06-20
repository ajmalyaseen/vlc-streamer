"""UPI payment flow: references, deep links, UTR capture, admin approve/reject."""
import datetime as dt
import logging
from dataclasses import dataclass

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .plans import VALIDITY_DAYS
from .utils import build_upi_link, make_payment_reference

log = logging.getLogger("payments")


@dataclass
class PurchaseResult:
    blocked: bool
    reference: str = ""
    upi_link: str = ""
    amount: int = 0


class PaymentService:
    def __init__(self, db, cfg, subs, plans, bot=None) -> None:
        self.db = db
        self.cfg = cfg
        self.subs = subs
        self.plans = plans
        self.bot = bot

    def set_bot(self, bot) -> None:
        self.bot = bot

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.upi_id)

    def upi_link(self, amount: int, reference: str) -> str:
        return build_upi_link(self.cfg.upi_id, "Alaska Stream", amount, reference)

    async def start_purchase(self, user, plan_key: str) -> PurchaseResult:
        pending = await self.db.get_pending_payment(user.id)
        if pending:
            return PurchaseResult(blocked=True)

        plan = self.plans[plan_key]
        reference = make_payment_reference()
        while await self.db.get_payment(reference):  # avoid rare collision
            reference = make_payment_reference()

        await self.db.create_payment({
            "_id": reference,
            "user_id": user.id,
            "username": getattr(user, "username", None),
            "plan": plan_key,
            "amount": plan.price,
            "utr_last4": None,
            "status": "awaiting_utr",
            "created_at": dt.datetime.utcnow(),
            "decided_at": None,
            "decided_by": None,
        })
        return PurchaseResult(
            blocked=False,
            reference=reference,
            upi_link=self.upi_link(plan.price, reference),
            amount=plan.price,
        )

    async def submit_utr(self, user, utr4: str):
        pending = await self.db.get_pending_payment(user.id)
        if not pending or pending["status"] != "awaiting_utr":
            return None
        if not (utr4.isdigit() and len(utr4) == 4):
            return None
        await self.db.update_payment(pending["_id"], {"status": "pending", "utr_last4": utr4})
        pending["status"] = "pending"
        pending["utr_last4"] = utr4
        return pending

    async def post_to_admins(self, payment) -> None:
        text = (
            "💰 **New Upgrade Request**\n\n"
            f"User: @{payment.get('username') or 'N/A'}\n"
            f"User ID: `{payment['user_id']}`\n"
            f"Plan: {payment['plan'].title()}\n"
            f"Amount: ₹{payment['amount']}\n"
            f"Reference: `{payment['_id']}`\n"
            f"UTR Last 4: {payment.get('utr_last4')}"
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment['_id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment['_id']}"),
        ]])
        targets = [self.cfg.admin_group_id] if self.cfg.admin_group_id else list(self.cfg.admins)
        for tid in targets:
            try:
                await self.bot.send_message(tid, text, reply_markup=markup)
            except Exception:
                log.exception("Failed to post payment request to %s", tid)

    async def approve(self, reference: str, admin_id=None):
        payment = await self.db.get_payment(reference)
        if not payment or payment["status"] != "pending":
            return None  # idempotent: only a pending request can be approved
        expires = await self.subs.set_plan(payment["user_id"], payment["plan"], VALIDITY_DAYS)
        await self.db.update_payment(reference, {
            "status": "approved",
            "decided_at": dt.datetime.utcnow(),
            "decided_by": admin_id,
        })
        payment["status"] = "approved"
        payment["expires"] = expires
        return payment

    async def reject(self, reference: str, admin_id=None):
        payment = await self.db.get_payment(reference)
        if not payment or payment["status"] != "pending":
            return None
        await self.db.update_payment(reference, {
            "status": "rejected",
            "decided_at": dt.datetime.utcnow(),
            "decided_by": admin_id,
        })
        payment["status"] = "rejected"
        return payment
