from typing import Optional, Iterator, NewType, Sequence, Tuple
from contextlib import contextmanager
from sqlite3 import connect as sqlite3_connect, Connection, OperationalError
from pathlib import Path

from ._exceptions import IsengardDBError
from ._target import ResolvedTargetID


RuleRunID = NewType("RuleRunID", int)


DB_VERSION = 1
# This magic number has two roles:
# - it makes unlikely we mistakenly consider an unrelated database as a legit
#   Isengard database
# - it acts as a constant ID to easily retrieve the single row in the `version` table
VERSION_MAGIC_NUMBER = 76388


# Create tables

SQL_CREATE_VERSION_TABLE = f"""
CREATE TABLE IF NOT EXISTS version(
    magic INTEGER UNIQUE NOT NULL DEFAULT {VERSION_MAGIC_NUMBER},
    value INTEGER NOT NULL
)"""
SQL_CREATE_RULE_RUN_TABLE = """
CREATE TABLE IF NOT EXISTS rule_run(
    _id INTEGER PRIMARY KEY,
    fingerprint BLOB UNIQUE NOT NULL
)"""
SQL_CREATE_TARGET_OUTPUT_TABLE = """
CREATE TABLE IF NOT EXISTS target_output(
    _id INTEGER PRIMARY KEY,
    run INTEGER NOT NULL,
    target TEXT NOT NULL,
    fingerprint BLOB NOT NULL,

    UNIQUE(run, target),
    FOREIGN KEY (run)
       REFERENCES rule_run (_id) 
)"""

# Queries

# No risk of SQL injection given `VERSION_MAGIC_NUMBER` & `DB_VERSION` are constants
SQL_INIT_VERSION_ROW = f"INSERT INTO version(value) VALUES({DB_VERSION})"
SQL_FETCH_VERSION_ROW = f"SELECT value FROM version WHERE magic = {VERSION_MAGIC_NUMBER}"

SQL_FETCH_RULE_RUN = "SELECT _id FROM rule_run WHERE fingerprint = ?"
SQL_UPDATE_RULE_RUN = """
INSERT INTO rule_run(fingerprint) VALUES(?)
ON CONFLICT(fingerprint) DO UPDATE SET fingerprint = excluded.fingerprint
"""
SQL_DELETE_TARGET_OUTPUTS = "DELETE FROM target_output WHERE run = ?"
SQL_INSERT_TARGET_OUTPUT = "INSERT INTO target_output(run, target, fingerprint) VALUES(?, ?, ?)"
SQL_FETCH_TARGET_OUTPUT = "SELECT fingerprint FROM target_output WHERE run = ? AND target = ?"


def init_or_reset_db(path: Path):
    try:
        con = sqlite3_connect(path)
    except OperationalError as exc:
        raise IsengardDBError(f"Cannot open/create database at {path}: {exc}") from exc

    # Optimistic check: the database is already initialized in the correct version
    try:
        cur = con.execute(SQL_FETCH_VERSION_ROW)
        current_db_version,  = cur.fetchone()
    except OperationalError as exc:
        # Just consider the database is invalid
        current_db_version = -1

    if current_db_version != DB_VERSION:
        # DB is not compatible with us, destroy it an restart anew
        con.close()

        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise IsengardDBError(f"Cannot delete incompatible database at {path}: {exc}") from exc

        try:
            con = sqlite3_connect(path)
            with con:
                cur = con.cursor()
                cur.execute(SQL_CREATE_VERSION_TABLE)
                cur.execute(SQL_CREATE_RULE_RUN_TABLE)
                cur.execute(SQL_CREATE_TARGET_OUTPUT_TABLE)
                cur.execute(SQL_INIT_VERSION_ROW)
        except OperationalError as exc:
            raise IsengardDBError(f"Cannot recreate database at {path}: {exc}") from exc

    return con


class DB:
    def __init__(self, path: Path, con: Connection):
        self.path = path
        self.con = con

    @classmethod
    @contextmanager
    def connect(cls, path: Path) -> Iterator["DB"]:
        con = init_or_reset_db(path)
        try:
            yield cls(path, con)
        finally:
            con.close()

    def fetch_rule_previous_run(self, fingerprint: bytes) -> Optional[RuleRunID]:
        row = self.con.execute(SQL_FETCH_RULE_RUN, (fingerprint, )).fetchone()
        return row[0] if row else None

    def set_rule_previous_run(self, fingerprint: bytes, outputs: Sequence[Tuple[ResolvedTargetID, bytes]]) -> RuleRunID:
        with self.con:
            cur = self.con.cursor()
            # TODO: Combine the set+fetch queries together with a RETURNING once SQLite >=3.35
            # is widely available in Python (see https://www.sqlite.org/lang_returning.html)
            cur.execute(SQL_UPDATE_RULE_RUN, (fingerprint, ))
            row = self.con.execute(SQL_FETCH_RULE_RUN, (fingerprint, )).fetchone()
            run_id = row[0]
            assert run_id is not None

            cur.execute(SQL_DELETE_TARGET_OUTPUTS, (run_id, ))
            cur.executemany(SQL_INSERT_TARGET_OUTPUT, [(run_id, *o) for o in outputs])

        return run_id

    def fetch_target_output_fingerprint(self, run_id: RuleRunID, target: ResolvedTargetID) -> Optional[bytes]:
        row = self.con.execute(SQL_FETCH_TARGET_OUTPUT, (run_id, target)).fetchone()
        return row[0] if row else None
