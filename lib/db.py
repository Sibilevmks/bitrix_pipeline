#!/usr/bin/env python3

from pathlib import Path
from typing import Any, Dict, Tuple

from env_loader import env_get


def _require_db_env(user: str, password: str, database: str) -> None:
    if not user or not password or not database:
        raise RuntimeError("ENV missing DB_USER, DB_PASS or DB_NAME")


def _db_config(env: Dict[str, str], autocommit: bool) -> Tuple[Dict[str, Any], int]:
    user = env_get(env, "DB_USER")
    password = env_get(env, "DB_PASS")
    database = env_get(env, "DB_NAME")
    charset = env_get(env, "DB_CHARSET", "utf8mb4")
    db_socket = env_get(env, "DB_SOCKET", "/var/run/mysqld_native/mysqld.sock")
    db_host = env_get(env, "DB_HOST", "localhost")
    db_port = int(env_get(env, "DB_PORT", "3306") or "3306")
    connect_timeout = int(env_get(env, "DB_CONNECT_TIMEOUT", "10") or "10")

    _require_db_env(user, password, database)

    cfg: Dict[str, Any] = {
        "user": user,
        "password": password,
        "database": database,
        "charset": charset,
        "autocommit": autocommit,
    }

    if db_socket and Path(db_socket).exists():
        cfg["unix_socket"] = db_socket
    else:
        cfg["host"] = db_host
        cfg["port"] = db_port

    return cfg, connect_timeout


def mysql_connect(env: Dict[str, str]):
    import mysql.connector

    cfg, connect_timeout = _db_config(env, autocommit=False)
    cfg["use_unicode"] = True
    cfg["connection_timeout"] = connect_timeout

    return mysql.connector.connect(**cfg)


def pymysql_connect(env: Dict[str, str], dict_cursor: bool = False, autocommit: bool = True):
    import pymysql

    cfg, connect_timeout = _db_config(env, autocommit=autocommit)
    cfg["connect_timeout"] = connect_timeout
    if dict_cursor:
        cfg["cursorclass"] = pymysql.cursors.DictCursor

    return pymysql.connect(**cfg)
