"""Standalone async tests for the subscription + payment logic (in-memory DB)."""
import asyncio
import datetime as dt
import sys
import types
from dataclasses import dataclass

# minimal stub so importing bot modules doesn't require pyrogram at test time
if "pyrogram" not in sys.modules:
    pyrogram = types.ModuleType("pyrogram")
    ptypes = types.ModuleType("pyrogram.types")

    class _Btn:
        def __init__(self, *a, **k): pass

    class _Markup:
        def __init__(self, *a, **k): pass

    ptypes.InlineKeyboardButton = _Btn
    ptypes.InlineKeyboardMarkup = _Markup
    pyrogram.types = ptypes
    sys.modules["pyrogram"] = pyrogram
    sys.modules["pyrogram.types"] = ptypes

sys.path.insert(0, ".")

from bot.db import MemoryUserDB           # noqa: E402
from bot.plans import build_plans         # noqa: E402
from bot.subscription import SubscriptionService  # noqa: E402
from bot.payments import PaymentService   # noqa: E402
from bot.utils import make_token, verify_token, make_payment_reference, build_upi_link  # noqa: E402


@dataclass
class Cfg:
    upi_id: str = "alaska@upi"
    admin_group_id: int = 0
    support_link: str = "https://t.me/alaska_in"
    admins: tuple = (999,)
    hash_secret: str = "test-secret"
    base_url: str = "https://test.example"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    plus_price: int = 27
    pro_price: int = 67
    free_daily: int = 2
    plus_daily: int = 20
    pro_daily: int = 100
    free_max_gb: float = 2.0
    plus_max_gb: float = 4.0
    pro_max_gb: float = 10.0
    free_expiry_h: int = 6
    plus_expiry_h: int = 24
    pro_expiry_h: int = 168


@dataclass
class User:
    id: int
    username: str = "tester"
    first_name: str = "Test"


GB = 1024 ** 3
passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


async def main():
    cfg = Cfg()
    plans = build_plans(cfg)
    db = MemoryUserDB()
    subs = SubscriptionService(db, plans)
    pay = PaymentService(db, cfg, subs, plans, bot=None)
    u = User(1)

    print("Plans")
    check("free 2/day", plans["free"].daily_links == 2)
    check("pro 10GB", plans["pro"].max_file_size == 10 * GB)

    print("Subscription get_state defaults")
    st = await subs.get_state(u)
    check("default plan free", st.plan.key == "free")
    check("used 0", st.used_today == 0)

    print("can_generate")
    d = await subs.can_generate(u, 1 * GB)
    check("free 1GB ok", d.ok)
    d = await subs.can_generate(u, 3 * GB)
    check("free 3GB too big", (not d.ok) and d.reason == "file_too_big")

    print("daily limit")
    await subs.record_link(1)
    await subs.record_link(1)
    d = await subs.can_generate(u, 1 * GB)
    check("free at 2/2 blocked", (not d.ok) and d.reason == "daily_limit")

    print("daily reset on date rollover")
    await db.update_user(1, {"last_reset_date": "2000-01-01"})
    st = await subs.get_state(u)
    check("counter reset to 0", st.used_today == 0)

    print("set_plan + expiry")
    exp = await subs.set_plan(1, "plus", 30)
    st = await subs.get_state(u)
    check("plan plus", st.plan.key == "plus")
    check("plus allows 3GB", (await subs.can_generate(u, 3 * GB)).ok)
    check("expiry ~30d", (exp - dt.datetime.utcnow()).days in (29, 30))

    print("lazy expiry downgrade")
    await db.update_user(1, {"plan_expires_at": dt.datetime.utcnow() - dt.timedelta(seconds=5)})
    st = await subs.get_state(u)
    check("expired -> free", st.plan.key == "free")

    print("payments: single pending + UTR + approve idempotent")
    u2 = User(2)
    await subs.get_state(u2)
    r1 = await pay.start_purchase(u2, "plus")
    check("first purchase ok", (not r1.blocked) and r1.reference.startswith("P"))
    r2 = await pay.start_purchase(u2, "pro")
    check("second purchase blocked", r2.blocked)
    bad = await pay.submit_utr(u2, "12")
    check("bad utr rejected", bad is None)
    good = await pay.submit_utr(u2, "6451")
    check("good utr accepted", good is not None and good["status"] == "pending")
    ap = await pay.approve(r1.reference, admin_id=999)
    check("approve activates", ap is not None and ap["status"] == "approved")
    st2 = await subs.get_state(u2)
    check("user2 now plus", st2.plan.key == "plus")
    ap2 = await pay.approve(r1.reference, admin_id=999)
    check("approve idempotent", ap2 is None)
    r3 = await pay.start_purchase(u2, "pro")
    check("can purchase again after decided", not r3.blocked)

    print("reject re-purchase")
    u3 = User(3)
    await subs.get_state(u3)
    rr = await pay.start_purchase(u3, "plus")
    await pay.submit_utr(u3, "1111")
    rej = await pay.reject(rr.reference, admin_id=999)
    check("reject works", rej is not None and rej["status"] == "rejected")
    rr2 = await pay.start_purchase(u3, "plus")
    check("re-purchase after reject", not rr2.blocked)

    print("token expiry")
    now = int(dt.datetime.utcnow().timestamp())
    t = make_token(100, 5, "secret", now + 3600)
    check("valid token", verify_token(100, 5, t, "secret", now + 3600))
    check("wrong exp fails", not verify_token(100, 5, t, "secret", now + 7200))
    legacy = make_token(100, 5, "secret")
    check("legacy token (exp=0)", verify_token(100, 5, legacy, "secret"))

    print("utils")
    refs = {make_payment_reference() for _ in range(100)}
    check("refs unique-ish", len(refs) > 95)
    link = build_upi_link("alaska@upi", "Alaska", 27, "P123")
    check("upi link", link.startswith("upi://pay?") and "pa=alaska%40upi" in link and "am=27" in link)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


asyncio.run(main())
