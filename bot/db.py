"""User tracking + subscription/payment storage (MongoDB or in-memory)."""
import datetime as dt
import logging

log = logging.getLogger("db")


def _today() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d")


def _now() -> dt.datetime:
    return dt.datetime.utcnow()


def _default_user(user_id, username=None, first_name=None) -> dict:
    return {
        "_id": user_id,
        "username": username,
        "first_name": first_name,
        "plan": "free",
        "plan_expires_at": None,
        "links_generated_today": 0,
        "last_reset_date": _today(),
        "created_at": _now(),
    }


class MemoryUserDB:
    def __init__(self) -> None:
        self._users = {}
        self._payments = {}

    async def add_user(self, user_id, username=None, first_name=None) -> None:
        u = self._users.get(user_id)
        if u is None:
            self._users[user_id] = _default_user(user_id, username, first_name)
        else:
            if username is not None:
                u["username"] = username
            if first_name is not None:
                u["first_name"] = first_name

    async def all_users(self) -> list:
        return list(self._users.keys())

    async def all_users_detailed(self) -> list:
        return list(self._users.values())

    async def count(self) -> int:
        return len(self._users)

    async def upsert_user(self, user_id, username=None, first_name=None) -> dict:
        await self.add_user(user_id, username, first_name)
        return dict(self._users[user_id])

    async def get_user(self, user_id):
        u = self._users.get(user_id)
        return dict(u) if u else None

    async def update_user(self, user_id, fields: dict) -> None:
        u = self._users.get(user_id)
        if u is None:
            u = _default_user(user_id)
            self._users[user_id] = u
        u.update(fields)

    async def count_by_plan(self) -> dict:
        counts = {"free": 0, "plus": 0, "pro": 0, "total": 0}
        for u in self._users.values():
            counts["total"] += 1
            plan = u.get("plan", "free") or "free"
            counts[plan] = counts.get(plan, 0) + 1
        return counts

    async def create_payment(self, doc: dict) -> None:
        self._payments[doc["_id"]] = dict(doc)

    async def get_pending_payment(self, user_id):
        for p in self._payments.values():
            if p["user_id"] == user_id and p["status"] in ("awaiting_utr", "pending"):
                return dict(p)
        return None

    async def get_payment(self, reference):
        p = self._payments.get(reference)
        return dict(p) if p else None

    async def update_payment(self, reference, fields: dict) -> None:
        p = self._payments.get(reference)
        if p:
            p.update(fields)


class MongoUserDB:
    def __init__(self, url: str) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        db = AsyncIOMotorClient(url)["vlc_streamer"]
        self._col = db["users"]
        self._pay = db["payments"]

    async def add_user(self, user_id, username=None, first_name=None) -> None:
        await self._col.update_one(
            {"_id": user_id},
            {
                "$set": {"username": username, "first_name": first_name},
                "$setOnInsert": {
                    "plan": "free",
                    "plan_expires_at": None,
                    "links_generated_today": 0,
                    "last_reset_date": _today(),
                    "created_at": _now(),
                },
            },
            upsert=True,
        )

    async def all_users(self) -> list:
        return [doc["_id"] async for doc in self._col.find({}, {"_id": 1})]

    async def all_users_detailed(self) -> list:
        return [doc async for doc in self._col.find({})]

    async def count(self) -> int:
        return await self._col.count_documents({})

    async def upsert_user(self, user_id, username=None, first_name=None) -> dict:
        await self.add_user(user_id, username, first_name)
        return await self.get_user(user_id)

    async def get_user(self, user_id):
        return await self._col.find_one({"_id": user_id})

    async def update_user(self, user_id, fields: dict) -> None:
        await self._col.update_one({"_id": user_id}, {"$set": fields}, upsert=True)

    async def count_by_plan(self) -> dict:
        counts = {"free": 0, "plus": 0, "pro": 0, "total": 0}
        counts["total"] = await self._col.count_documents({})
        for plan in ("plus", "pro"):
            counts[plan] = await self._col.count_documents({"plan": plan})
        counts["free"] = counts["total"] - counts["plus"] - counts["pro"]
        return counts

    async def create_payment(self, doc: dict) -> None:
        await self._pay.insert_one(doc)

    async def get_pending_payment(self, user_id):
        return await self._pay.find_one(
            {"user_id": user_id, "status": {"$in": ["awaiting_utr", "pending"]}}
        )

    async def get_payment(self, reference):
        return await self._pay.find_one({"_id": reference})

    async def update_payment(self, reference, fields: dict) -> None:
        await self._pay.update_one({"_id": reference}, {"$set": fields})


def make_user_db(url: str):
    if url:
        log.info("Using MongoDB for user storage")
        return MongoUserDB(url)
    log.warning("No DATABASE_URL set; using in-memory user storage (resets on restart)")
    return MemoryUserDB()
