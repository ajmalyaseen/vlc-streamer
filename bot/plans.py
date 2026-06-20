"""Subscription plan catalog, built from configurable limits."""
from dataclasses import dataclass

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

VALIDITY_DAYS = 30
GB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class Plan:
    key: str            # "free" | "plus" | "pro"
    name: str           # "Free" | "Plus" | "Pro"
    emoji: str
    price: int          # rupees per 30 days (0 for free)
    daily_links: int    # links/day
    max_file_size: int  # bytes
    expiry_seconds: int # stream-link validity


def _gb_text(n_bytes: int) -> str:
    gb = n_bytes / GB
    return f"{gb:g} GB"


def _expiry_text(seconds: int) -> str:
    hours = seconds // 3600
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        return f"{days} day" + ("s" if days != 1 else "")
    return f"{hours} hour" + ("s" if hours != 1 else "")


def build_plans(cfg) -> dict:
    """Build the {key: Plan} catalog from config values."""
    return {
        "free": Plan(
            "free", "Free", "🆓", 0,
            cfg.free_daily, int(cfg.free_max_gb * GB), cfg.free_expiry_h * 3600,
        ),
        "plus": Plan(
            "plus", "Plus", "⭐", cfg.plus_price,
            cfg.plus_daily, int(cfg.plus_max_gb * GB), cfg.plus_expiry_h * 3600,
        ),
        "pro": Plan(
            "pro", "Pro", "🚀", cfg.pro_price,
            cfg.pro_daily, int(cfg.pro_max_gb * GB), cfg.pro_expiry_h * 3600,
        ),
    }


def plan_line(p: Plan) -> str:
    price = "Free" if p.price == 0 else f"₹{p.price}/month"
    return (
        f"{p.emoji} **{p.name}** — {price}\n"
        f"   • {p.daily_links} links/day\n"
        f"   • {_gb_text(p.max_file_size)} max file\n"
        f"   • {_expiry_text(p.expiry_seconds)} link expiry"
    )


def format_plans_text(plans: dict) -> str:
    return (
        "💎 **Premium Plans**\n\n"
        + "\n\n".join(plan_line(plans[k]) for k in ("free", "plus", "pro"))
    )


def purchase_text(p: Plan) -> str:
    return (
        f"{p.emoji} **{p.name} Plan**\n\n"
        f"**Price:** ₹{p.price}\n"
        f"**Validity:** {VALIDITY_DAYS} Days\n\n"
        "**Features:**\n"
        f"• {p.daily_links} links/day\n"
        f"• {_gb_text(p.max_file_size)} file limit\n"
        f"• {_expiry_text(p.expiry_seconds)} link expiry"
    )


def benefits_text(p: Plan) -> str:
    return (
        f"• {p.daily_links} links/day\n"
        f"• {_gb_text(p.max_file_size)} file uploads\n"
        f"• {_expiry_text(p.expiry_seconds)} link expiry"
    )


def buy_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⭐ Buy Plus", callback_data="buy_plus")],
            [InlineKeyboardButton("🚀 Buy Pro", callback_data="buy_pro")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_home")],
        ]
    )


def upgrade_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⭐ Upgrade to Plus", callback_data="buy_plus"),
                InlineKeyboardButton("🚀 Upgrade to Pro", callback_data="buy_pro"),
            ]
        ]
    )
