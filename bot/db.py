"""User tracking with an optional MongoDB backend."""
import logging

log = logging.getLogger("db")


class MemoryUserDB:
    def __init__(self) -> None:
        self._users = {}

    async def add_user(self, user_id, username=None, first_name=None) -> None:
        self._users[user_id] = {
            "_id": user_id,
            "username": username,
            "first_name": first_name,
        }

    async def all_users(self) -> list:
        return list(self._users.keys())

    async def all_users_detailed(self) -> list:
        return list(self._users.values())

    async def count(self) -> int:
        return len(self._users)


class MongoUserDB:
    def __init__(self, url: str) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        self._col = AsyncIOMotorClient(url)["vlc_streamer"]["users"]

    async def add_user(self, user_id, username=None, first_name=None) -> None:
        await self._col.update_one(
            {"_id": user_id},
            {"$set": {"username": username, "first_name": first_name}},
            upsert=True,
        )

    async def all_users(self) -> list:
        return [doc["_id"] async for doc in self._col.find({}, {"_id": 1})]

    async def all_users_detailed(self) -> list:
        return [doc async for doc in self._col.find({})]

    async def count(self) -> int:
        return await self._col.count_documents({})


def make_user_db(url: str):
    if url:
        log.info("Using MongoDB for user storage")
        return MongoUserDB(url)
    log.warning("No DATABASE_URL set; using in-memory user storage (resets on restart)")
    return MemoryUserDB()
