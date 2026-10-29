"""Safe transactional SQLite connection lifecycle for Sage code and tests.

Unlike `with sqlite3.connect(...)`, this context manager *always* closes the
connection on exit, so Windows can remove temporary database files immediately.

Used by: utils/rag_catalog.py.
"""

from __future__ import annotations

import sqlite3
from os import PathLike
from typing import Any


class SQLiteSession:
    """Open, commit/rollback, and close a SQLite connection deterministically.

    Successful exit commits the transaction. Exceptional exit rolls it back.
    In all cases it closes the connection and propagates exceptions.

    Example::

        with SQLiteSession(database_path) as conn:
            conn.execute('CREATE TABLE example (id INTEGER)')
    """

    def __init__(self, path: str | PathLike[str], **connect_options: Any) -> None:
        """Remember where and how to connect; the connection opens on ``with``."""
        self.path = path
        self.options = connect_options
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        """Open the connection (one active use at a time)."""
        if self._connection is not None:
            raise RuntimeError('SQLiteSession is already active')
        self._connection = sqlite3.connect(self.path, **self.options)
        return self._connection

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: Any
    ) -> bool:
        """Commit on success, roll back on error, always close; never swallow errors."""
        conn = self._connection
        self._connection = None
        if conn is None:
            return False
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            conn.close()
        return False
