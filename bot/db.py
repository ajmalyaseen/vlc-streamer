"""User tracking with an optional MongoDB backend."""
import logging

log = logging.getLogger("db")


class MemoryUserDB:
    def __init__(self) -> None:
        self._users = set()

    async def add_user(self, user_id: int) -> None:
        self._users.add(user_id)

    async def all_users(self) -> list:
        return list(self._users)

    async def count(self) -> int:
        return len(self._users)


class MongoUserDB:
    def __init__(self, url: str) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        self._col = AsyncIOMotorClient(url)["vlc_streamer"]["users"]

    async def add_user(self, user_id: int) -> None:
        await self._col.update_one(
            {"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True
        )

    async def all_users(self) -> list:
        return [doc["_id"] async for doc in self._col.find({}, {"_id": 1})]

    async def count(self) -> int:
        return await self._col.count_documents({})


def make_user_db(url: str):
    if url:
        log.info("Using MongoDB for user storage")
        return MongoUserDB(url)
    log.warning("No DATABASE_URL set; using in-memory user storage (resets on restart)")
    return MemoryUserDB()
