import asyncio
import hashlib
import json
from time import time
from typing import Any, Dict, List, Tuple

import sqlparse
from aioch import Client
from asgiref.sync import async_to_sync
from clickhouse_driver import Client as SyncClient
from clickhouse_pool import ChPool
from django.conf import settings
from django.core.cache import cache
from django.utils.timezone import now
from sentry_sdk.api import capture_exception

from posthog import redis
from posthog.constants import RDBMS
from posthog.settings import (
    CLICKHOUSE_ASYNC,
    CLICKHOUSE_CA,
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_SECURE,
    CLICKHOUSE_USER,
    CLICKHOUSE_VERIFY,
    PRIMARY_DB,
    TEST,
)

CACHE_TTL = 60  # seconds

_save_query_user_id = False

if PRIMARY_DB != RDBMS.CLICKHOUSE:
    ch_client = None  # type: Client
    ch_sync_pool = None  # type: ChPool

    def async_execute(query, args=None):
        return

    def sync_execute(query, args=None):
        return

    def cache_sync_execute(query, args=None, redis_client=None, ttl=None):
        return


else:
    if not TEST and CLICKHOUSE_ASYNC:
        ch_client = Client(
            host=CLICKHOUSE_HOST,
            database=CLICKHOUSE_DATABASE,
            secure=CLICKHOUSE_SECURE,
            user=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD,
            ca_certs=CLICKHOUSE_CA,
            verify=CLICKHOUSE_VERIFY,
        )

        @async_to_sync
        async def async_execute(query, args=None):
            loop = asyncio.get_event_loop()
            task = loop.create_task(ch_client.execute(query, args))
            return task

    else:
        # if this is a test use the sync client
        ch_client = SyncClient(
            host=CLICKHOUSE_HOST,
            database=CLICKHOUSE_DATABASE,
            secure=CLICKHOUSE_SECURE,
            user=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD,
            ca_certs=CLICKHOUSE_CA,
            verify=CLICKHOUSE_VERIFY,
        )

        def async_execute(query, args=None):
            return sync_execute(query, args)

    ch_sync_pool = ChPool(
        host=CLICKHOUSE_HOST,
        database=CLICKHOUSE_DATABASE,
        secure=CLICKHOUSE_SECURE,
        user=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        ca_certs=CLICKHOUSE_CA,
        verify=CLICKHOUSE_VERIFY,
        connections_min=20,
        connections_max=100,
    )

    def cache_sync_execute(query, args=None, redis_client=None, ttl=CACHE_TTL):
        if not redis_client:
            redis_client = redis.get_client()
        key = _key_hash(query, args)
        if redis_client.exists(key):
            result = _deserialize(redis_client.get(key))
            return result
        else:
            result = sync_execute(query, args)
            redis_client.set(key, _serialize(result), ex=ttl)
            return result

    def sync_execute(query, args=None):
        start_time = time()
        try:
            with ch_sync_pool.get_client() as client:
                result = client.execute(query, args)
        finally:
            execution_time = time() - start_time
            if settings.SHELL_PLUS_PRINT_SQL:
                print(format_sql(query, args))
                print("Execution time: %.6fs" % (execution_time,))
            if _save_query_user_id:
                save_query(query, args, execution_time)
        return result


def _deserialize(result_bytes: bytes) -> List[Tuple]:
    results = []
    for x in json.loads(result_bytes):
        results.append(tuple(x))
    return results


def _serialize(result: Any) -> bytes:
    return json.dumps(result).encode("utf-8")


def _key_hash(query: str, args: Any) -> bytes:
    key = hashlib.md5(query.encode("utf-8") + json.dumps(args).encode("utf-8")).digest()
    return key


def format_sql(sql, params):
    substitute_params = (
        ch_client.substitute_params if isinstance(ch_client, SyncClient) else ch_client._client.substitute_params
    )

    sql = substitute_params(sql, params or {})
    sql = sqlparse.format(sql, reindent_aligned=True)
    try:
        import pygments.formatters
        import pygments.lexers

        sql = pygments.highlight(sql, pygments.lexers.get_lexer_by_name("sql"), pygments.formatters.TerminalFormatter())
    except:
        pass

    return sql


def save_query(sql: str, params: Dict, execution_time: float) -> None:
    """
    Save query for debugging purposes
    """

    try:
        key = "save_query_{}".format(_save_query_user_id)
        queries = json.loads(cache.get(key) or "[]")

        queries.insert(
            0, {"timestamp": now().isoformat(), "query": format_sql(sql, params), "execution_time": execution_time}
        )
        cache.set(key, json.dumps(queries), timeout=120)
    except Exception as e:
        capture_exception(e)
