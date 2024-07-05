"""
Here are the types and functions used to describe a hypothetical scenario in which data is retrieved
from a database and cached.

Both the database and the cache are represented as dictionaries but in practice the database might be a remote API
and cache could be in local memory.
"""

import pprint
from typing import Union
from warnings import warn


type DBKey = str
type DBValue = str

# Making separate types here to indicate that these can go into different place nodes.
type DBKeyValuePair = tuple[DBKey, DBValue]

type Database = dict[DBKey, DBValue]
type Cache = dict[DBKey, DBValue]


class DBOperations:

    def retrieve_key_value_pair(db: Database, key: DBKey) -> DBKeyValuePair:
        return (key, db[key])

    def print_db(db: Database):
        """Just adding this as an example of another function that could be added to the class."""
        pprint.pprint(db)


class CacheOperations:

    def cache_key_value_pair(
        cache: Cache, key_value_pair: DBKeyValuePair, expected_size: int = 100
    ) -> DBKeyValuePair:
        key, value = key_value_pair
        cache[key] = value
        if len(cache) > expected_size:
            warn(f"Expected cache to not exceed {expected_size} items but it now has {len(cache)} items.")
        return key_value_pair

    def retrieve_key_value_pair(cache: Cache, key: DBKey) -> Union[DBKey, DBKeyValuePair]:
        value = cache.get(key)
        if value is None:
            return key
        else:
            return (key, value)
