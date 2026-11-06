"""Cross-platform local setup and consistent backups; no service credentials needed."""

import argparse
from contextlib import closing
from datetime import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def validate_login(username: str, password: str):
    if (not 1 <= len(username) <= 128 or any(character in username for character in ":\r\n")
            or not username.isprintable()):
        raise ValueError("Username must be 1-128 printable characters without a colon")
    if not 12 <= len(password) or len(password.encode("utf-8")) > 1024:
        raise ValueError("Choose a password of at least 12 characters and at most 1024 UTF-8 bytes")


def container_identity() -> tuple[int, int]:
    if os.name != "posix" or os.getuid() == 0:
        return 10001, 10001
    return os.getuid(), os.getgid()


def _permissions(path: Path, mode: int, identity: tuple[int, int] | None = None):
    if os.name == "posix":
        if identity is not None and os.getuid() == 0:
            os.chown(path, *identity)
        path.chmod(mode)


def _write_new(path: Path, content: str, mode: int = 0o600,
               identity: tuple[int, int] | None = None):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    _permissions(path, mode, identity)


def initialize(root: Path, username: str, password: str) -> dict:
    """Create local files without overwriting an existing installation."""
    validate_login(username, password)
    root = root.resolve()
    auth_file = root / "secrets" / "panel-auth.json"
    env_file = root / ".env"
    for path in (root / "data", root / "secrets", auth_file, env_file):
        if path.is_symlink():
            raise ValueError("Setup paths must not be symbolic links")
    if auth_file.exists() or env_file.exists():
        raise ValueError("Authentication or .env already exists; setup does not overwrite an installation")
    root.mkdir(parents=True, exist_ok=True)
    identity = container_identity()
    for name, mode in (("data", 0o750), ("secrets", 0o700)):
        directory = root / name
        directory.mkdir(exist_ok=True)
        _permissions(directory, mode, identity)

    salt = secrets.token_bytes(32)
    credentials = {"username": username, "salt": salt.hex(), "iterations": 600000,
                   "hash": hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600000).hex()}
    _write_new(auth_file, json.dumps(credentials, indent=2) + "\n", 0o400, identity)
    uid, gid = identity
    environment = (
        "COMPOSE_PROJECT_NAME=longfor-auto-checkin\n"
        "RELEASE_TAG=local\nPANEL_PORT=8088\n"
        f"PANEL_UID={uid}\nPANEL_GID={gid}\n"
        "LONGFOR_PUBLIC_ORIGIN=http://127.0.0.1:8088\n"
        "LONGFOR_ROOT_PATH=\nLONGFOR_SCHEDULE_ENABLED=0\n"
        "PIP_INDEX_URL=https://pypi.org/simple\n"
    )
    _write_new(env_file, environment)
    return {"root": str(root), "username": username, "container_uid": uid,
            "container_gid": gid, "schedule_enabled": False}


def backup_database(root: Path, output: Path | None = None) -> Path:
    """SQLite backup API includes committed WAL data while the service is running."""
    root = root.resolve()
    database = root / "data" / "longhu.db"
    if database.is_symlink() or not database.is_file():
        raise ValueError("No regular database found in data/longhu.db")
    destination = output or root / "backups" / f"longhu-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
    destination = destination.absolute()
    if destination.is_symlink() or destination.exists():
        raise ValueError("Backup destination already exists; it will not be overwritten")
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        _permissions(destination.parent, 0o700)
    # Exclusive creation also handles two concurrent requests for the same filename.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15)) as source:
        with closing(sqlite3.connect(destination, timeout=15)) as target:
            source.backup(target, pages=256, sleep=0.1)
    _permissions(destination, 0o600)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Initialize Longfor Auto Check-in or back up its local data")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create authentication and a disabled local Compose setup")
    init.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Installation directory (default: project root)")
    init.add_argument("--username", default="admin", help="Panel username (default: admin)")
    init.add_argument("--generate-password", action="store_true", help="Generate a password and display it once")
    backup = commands.add_parser("backup", help="Create a consistent SQLite backup, including committed WAL data")
    backup.add_argument("--root", type=Path, default=PROJECT_ROOT)
    backup.add_argument("--output", type=Path, help="Destination file; defaults to backups/ with a timestamp")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            if args.generate_password:
                password = secrets.token_urlsafe(24)
            else:
                if not sys.stdin.isatty():
                    raise ValueError("Interactive setup needs a terminal; use --generate-password for non-interactive setup")
                password = getpass.getpass("Panel password (at least 12 characters): ")
                if password != getpass.getpass("Repeat panel password: "):
                    raise ValueError("Passwords do not match")
            result = initialize(args.root, args.username, password)
            print(f"Initialized {result['root']}")
            print(f"Username: {result['username']}")
            if args.generate_password:
                print(f"Password (displayed once): {password}")
            print("Automatic scheduling is OFF. Start with: docker compose up -d --build")
            print("Open http://127.0.0.1:8088 and use the panel credentials above.")
        else:
            output = backup_database(args.root, args.output)
            print(f"Consistent backup saved: {output}")
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Never print a credential or SQL statement in exception diagnostics.
        print(f"Setup/backup failed: {type(exc).__name__}. {exc if isinstance(exc, ValueError) else 'Check file permissions and available disk space.'}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
