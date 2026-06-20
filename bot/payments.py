"""Payments: Razorpay Standard Checkout (preferred) + manual UPI fallback."""
import datetime as dt
import hashlib
import hmac
import logging
from dataclasses import dataclass

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .plans import VALIDITY_DAYS, benefits_text
from .utils import build_upi_link, make_payment_reference, make_payment_token

log = logging.getLogger("payments")

RZP_ORDERS_URL = "https://api.razorpay.com/v1/orders"


@dataclass
class PurchaseResult:
    blocked: bool = False
    error: bool = False
    provider: str = "upi"      # "upi" | "razorpay"
    reference: str = ""
    upi_link: str = ""
    amount: int = 0
    pay_url: str = ""          # hosted checkout URL (razorpay)


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
    def razorpay_enabled(self) -> bool:
        return bool(self.cfg.razorpay_key_id and self.cfg.razorpay_key_secret)

    @property
    def enabled(self) -> bool:
        return self.razorpay_enabled or bool(self.cfg.upi_id)

    def upi_link(self, amount: int, reference: str) -> str:
        return build_upi_link(self.cfg.upi_id, "Alaska Stream", amount, reference)

    # ---------------- start a purchase ----------------

    async def start_purchase(self, user, plan_key: str) -> PurchaseResult:
        plan = self.plans[plan_key]
        reference = make_payment_reference()
        while await self.db.get_payment(reference):  # avoid rare collision
            reference = make_payment_reference()

        if self.razorpay_enabled:
            await self.db.create_payment({
                "_id": reference,
                "user_id": user.id,
                "username": getattr(user, "username", None),
                "plan": plan_key,
                "amount": plan.price,
                "provider": "razorpay",
                "order_id": None,
                "payment_id": None,
                "status": "created",
                "created_at": dt.datetime.utcnow(),
                "decided_at": None,
                "decided_by": None,
            })
            token = make_payment_token(reference, self.cfg.hash_secret)
            url = f"{self.cfg.base_url}/checkout/{reference}?token={token}"
            return PurchaseResult(provider="razorpay", reference=reference,
                                  amount=plan.price, pay_url=url)

        # manual UPI fallback: enforce single pending request
        pending = await self.db.get_pending_payment(user.id)
        if pending:
            return PurchaseResult(blocked=True)
        await self.db.create_payment({
            "_id": reference,
            "user_id": user.id,
            "username": getattr(user, "username", None),
            "plan": plan_key,
            "amount": plan.price,
            "provider": "upi",
            "utr_last4": None,
            "status": "awaiting_utr",
            "created_at": dt.datetime.utcnow(),
            "decided_at": None,
            "decided_by": None,
        })
        return PurchaseResult(provider="upi", reference=reference,
                              upi_link=self.upi_link(plan.price, reference), amount=plan.price)

    # ---------------- Razorpay ----------------

    async def create_order(self, reference: str):
        """Create a Razorpay order for an existing payment record."""
        payment = await self.db.get_payment(reference)
        if not payment or payment.get("status") == "approved":
            return None
        import aiohttp

        amount_paise = max(100, int(payment["amount"]) * 100)
        auth = aiohttp.BasicAuth(self.cfg.razorpay_key_id, self.cfg.razorpay_key_secret)
        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": reference,
            "notes": {"reference": reference, "user_id": str(payment["user_id"]),
                      "plan": payment["plan"]},
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(RZP_ORDERS_URL, json=payload, auth=auth,
                                  timeout=aiohttp.ClientTimeout(total=20)) as r:
                    status = r.status
                    data = await r.json()
        except Exception:
            log.exception("Razorpay create-order request failed")
            return None
        if status >= 300 or "id" not in data:
            log.error("Razorpay order failed (%s): %s", status, data)
            return None
        await self.db.update_payment(reference, {"order_id": data["id"]})
        return {"order_id": data["id"], "amount": data["amount"], "currency": "INR"}

    async def verify_and_fulfill(self, reference, order_id, payment_id, signature) -> bool:
        """Verify Razorpay client-side signature and activate the plan (idempotent)."""
        payment = await self.db.get_payment(reference)
        if not payment:
            return False
        expected = hmac.new(
            self.cfg.razorpay_key_secret.encode(),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature or ""):
            log.warning("Razorpay signature mismatch for %s", reference)
            return False
        return await self._activate(payment, payment_id)

    async def fulfill_from_webhook(self, body: bytes, signature: str) -> bool:
        """Verify a Razorpay webhook (payment.captured) and activate the plan.

        This is the reliable path: Razorpay calls us server-to-server even if the
        user closed the browser before the client-side verify ran. Idempotent."""
        secret = self.cfg.razorpay_webhook_secret
        if not secret:
            log.warning("Webhook received but RAZORPAY_WEBHOOK_SECRET not set")
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature or ""):
            log.warning("Razorpay webhook signature mismatch")
            return False
        import json

        try:
            event = json.loads(body.decode())
        except Exception:
            log.exception("Webhook body not valid JSON")
            return False
        # The order receipt is our payment reference (set in create_order).
        entity = (
            event.get("payload", {}).get("payment", {}).get("entity", {})
        )
        order_entity = (
            event.get("payload", {}).get("order", {}).get("entity", {})
        )
        reference = (
            (entity.get("notes") or {}).get("reference")
            or order_entity.get("receipt")
        )
        payment_id = entity.get("id")
        if not reference:
            log.warning("Webhook missing reference; event=%s", event.get("event"))
            return False
        payment = await self.db.get_payment(reference)
        if not payment:
            log.warning("Webhook reference %s not found in DB", reference)
            return False
        return await self._activate(payment, payment_id)

    async def _activate(self, payment, payment_id) -> bool:
        """Activate the plan for a verified payment and notify the user. Idempotent."""
        reference = payment["_id"]
        if payment.get("status") == "approved":
            return True  # idempotent: already fulfilled (e.g. client-verify won the race)
        expires = await self.subs.set_plan(payment["user_id"], payment["plan"], VALIDITY_DAYS)
        await self.db.update_payment(reference, {
            "status": "approved",
            "payment_id": payment_id,
            "decided_at": dt.datetime.utcnow(),
        })
        plan = self.plans.get(payment["plan"])
        try:
            await self.bot.send_message(
                payment["user_id"],
                f"🎉 **Payment Successful!**\n\n"
                f"{plan.emoji} **{plan.name} Plan Activated**\n"
                f"Valid Until: {expires.strftime('%d %b %Y')}\n\n"
                f"Benefits:\n{benefits_text(plan)}\n\n"
                "Thank you for supporting Alaska ❤️",
            )
        except Exception:
            log.exception("notify razorpay success failed")
        return True

    # ---------------- manual UPI verification flow ----------------

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
            return None
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
