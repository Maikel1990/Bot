from __future__ import annotations

import asyncio
import dataclasses
from collections import defaultdict
from typing import (TYPE_CHECKING, Any, Dict, Generic, Iterable, List, Literal,
                    Optional, Tuple, TypeVar, Union, cast)

from discord.ext import tasks
from sql_athame import sql

import utils


_T = TypeVar("_T")
_DK = TypeVar("_DK")
_CACHE_ITEM = Dict[Literal[
    "channel", "xsaid", "bot_ignore", "auto_join",
    "msg_length", "repeated_chars", "prefix",
    "blocked", "lang", "name", "default_lang"
], Any]

if TYPE_CHECKING:
    from main import TTSBot
    from sql_athame.base import SQLFormatter

    sql: SQLFormatter
    del SQLFormatter


@dataclasses.dataclass
class WriteTask:
    waiter: asyncio.Future[None] = dataclasses.field(default_factory=asyncio.Future)
    changes: _CACHE_ITEM = dataclasses.field(default_factory=dict)


def _unpack_id(identifer: Union[Iterable[_T], _T]) -> Tuple[_T, ...]:
    return tuple(identifer) if isinstance(identifer, Iterable) else (identifer,)

def setup(bot: TTSBot):
    bot.settings, bot.userinfo, bot.nicknames = (
        TableHandler(bot, broadcast=True, default_id=0, pkey_columns=("guild_id",),
        select="SELECT * FROM guilds WHERE guild_id = $1",
        delete="DELETE FROM guilds WHERE guild_id = $1",
        insert="""
            INSERT INTO guilds({})
            VALUES({})

            ON CONFLICT (guild_id)
            DO UPDATE SET ({}) = ({})
        """,
    ), TableHandler(bot, broadcast=False, default_id=0, pkey_columns=("user_id",),
        select="SELECT * FROM userinfo WHERE user_id = $1",
        delete="DELETE FROM userinfo WHERE user_id = $1",
        insert="""
            INSERT INTO userinfo({})
            VALUES({})

            ON CONFLICT (user_id)
            DO UPDATE SET ({}) = ({})
        """,
    ), TableHandler(bot, broadcast=False, default_id=(0, 0), pkey_columns=("guild_id", "user_id"),
        select="SELECT * from nicknames WHERE guild_id = $1 and user_id = $2",
        delete="DELETE FROM nicknames WHERE guild_id = $1 and user_id = $2",
        insert="""
            INSERT INTO nicknames({})
            VALUES({})

            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET ({}) = ({})
        """,
    ))


class TableHandler(Generic[_DK]):
    def __init__(self,
        bot: TTSBot, broadcast: bool,
        select: str, insert: str, delete: str,
        default_id: _DK, pkey_columns: Tuple[str, ...]
    ):
        self.bot = bot
        self.pool = bot.pool

        self.broadcast = broadcast
        self.select_query = select
        self.insert_query = insert
        self.delete_query = delete
        self.default_id = default_id
        self.pkey_columns = tuple(sql(pkey) for pkey in pkey_columns)

        self._not_fully_fetched: List[_DK] = []
        self._cache: Dict[_DK, _CACHE_ITEM] = {}
        self.defaults: Optional[_CACHE_ITEM] = None
        self._write_tasks: defaultdict[_DK, WriteTask] = defaultdict(WriteTask)

        bot.add_listener(self.on_invalidate_cache)


    async def on_invalidate_cache(self, identifier: _DK):
        if isinstance(identifier, list):
            identifier = tuple(identifier) # type: ignore

        self._cache.pop(identifier, None) # type: ignore

    async def _fetch_defaults(self) -> _CACHE_ITEM:
        row = await self.bot.pool.fetchrow(self.select_query, *_unpack_id(self.default_id))
        assert row is not None

        self.defaults = dict(row)
        return self.defaults


    def __getitem__(self, identifer: _DK) -> _CACHE_ITEM:
        if identifer not in self._not_fully_fetched:
            return self._cache[identifer]

        raise KeyError

    def __setitem__(self, identifier: _DK, new_settings: _CACHE_ITEM):
        if identifier not in self._cache:
            self._cache[identifier] = {}
            self._not_fully_fetched.append(identifier)

        self._cache[identifier].update(new_settings)
        self._write_tasks[identifier].changes.update(new_settings)

        if not self.insert_writes.is_running():
            self.insert_writes.start()

    def __delitem__(self, identifier: _DK):
        del self._cache[identifier]
        self.bot.create_task(self.bot.pool.execute(
            self.delete_query, identifier
        ))


    async def get(self, identifer: _DK) -> _CACHE_ITEM:
        try:
            return self[identifer]
        except KeyError:
            return await self._fill_cache(identifer)

    async def set(self, identifer: _DK, new_settings: _CACHE_ITEM):
        self[identifer] = new_settings
        await self._write_tasks[identifer].waiter


    @tasks.loop(seconds=1)
    async def insert_writes(self):
        if not self._write_tasks:
            return self.insert_writes.cancel()

        amount_of_changes = len(self._write_tasks)
        exceptions = [err for err in await asyncio.gather(*(
            self._insert_write(pending_id)
            for pending_id in self._write_tasks.keys()
        ), return_exceptions=True) if err is not None]

        self.bot.logger.debug(f"Inserted {amount_of_changes} change(s) with {len(exceptions)} errors")
        await asyncio.gather(*(self.bot.on_error("insert_writes", err) for err in exceptions))


    async def _insert_write(self, raw_id: _DK):
        task = self._write_tasks.pop(raw_id)

        no_id_settings = [sql(setting) for setting in task.changes.keys()]
        no_id_values = [sql("{}", value) for value in task.changes.values()]
        identifer = [sql("{}", identifer) for identifer in _unpack_id(raw_id)]

        settings = [*self.pkey_columns, *no_id_settings]
        values   = [*identifer, *no_id_values]

        query = sql(self.insert_query,
            sql.list(settings), sql.list(values),
            sql.list(no_id_settings), sql.list(no_id_values)
        )
        self.bot.logger.debug(f"query: {list(query)}")
        await self.pool.execute(*query)

        task.waiter.set_result(None)
        if self.bot.websocket is not None and self.broadcast:
            await self.bot.websocket.send(
                utils.data_to_ws_json("SEND", target="*", **{
                    "c": "invalidate_cache",
                    "a": {"identifer": raw_id},
                })
            )


    async def _fill_cache(self, identifier: _DK) -> _CACHE_ITEM:
        record = await self.pool.fetchrow(self.select_query, *_unpack_id(identifier))
        if record is None:
            self._cache[identifier] = item = self.defaults or await self._fetch_defaults()
        else:
            self._cache[identifier] = item = cast(_CACHE_ITEM, dict(record))

        return item
