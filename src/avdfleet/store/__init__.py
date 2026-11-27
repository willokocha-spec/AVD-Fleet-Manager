"""SQLite persistence for AVD Fleet: schema, migrations and repositories.

The only public entry point most callers need is :class:`Store`::

    from avdfleet.store import Store

    store = Store(paths.db_path)
    store.initialize()
    ...
    store.close()

Proxy credentials never enter this package: :class:`ProxyRecord` stores only a
vault reference (``secret_ref``), and the encrypted vault owns the secret value.
"""

from .db import Database, Store
from .errors import (
    StoreClosedError,
    StoreError,
    StoreNotInitializedError,
    StoreSchemaError,
)
from .migrations import MIGRATIONS, SCHEMA_VERSION
from .models import (
    ProxyRecord,
    decode_bool,
    decode_datetime,
    decode_dict,
    decode_json,
    decode_list,
    decode_str_list,
    encode_bool,
    encode_datetime,
    encode_json,
)
from .repositories import (
    AssignmentRepository,
    ImageRepository,
    InstanceRepository,
    OobeRepository,
    ProxyRepository,
    RunRepository,
    ScriptRepository,
    SettingsRepository,
    TemplateRepository,
)

__all__ = [
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "AssignmentRepository",
    "Database",
    "ImageRepository",
    "InstanceRepository",
    "OobeRepository",
    "ProxyRecord",
    "ProxyRepository",
    "RunRepository",
    "ScriptRepository",
    "SettingsRepository",
    "Store",
    "StoreClosedError",
    "StoreError",
    "StoreNotInitializedError",
    "StoreSchemaError",
    "TemplateRepository",
    "decode_bool",
    "decode_datetime",
    "decode_dict",
    "decode_json",
    "decode_list",
    "decode_str_list",
    "encode_bool",
    "encode_datetime",
    "encode_json",
]
