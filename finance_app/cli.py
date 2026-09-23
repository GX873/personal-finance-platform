import argparse
import sys
from getpass import getpass

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from finance_app.auth.models import User
from finance_app.auth.service import hash_password
from finance_app.db import get_session_factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finance")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init-admin", "change-password"):
        command = commands.add_parser(name)
        command.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    if not args.username.strip() or len(args.username) > 128:
        print("Username must contain 1 to 128 characters.", file=sys.stderr)
        return 1
    with get_session_factory()() as db:
        if not inspect(db.get_bind()).has_table("users"):
            print(
                "Database is not initialized. Run: python -m alembic upgrade head",
                file=sys.stderr,
            )
            return 1
        user = db.scalar(select(User).where(User.username == args.username))
        if args.command == "init-admin":
            if (
                db.scalar(select(User.id).where(User.is_active.is_(True))) is not None
                or user is not None
            ):
                print(
                    "An active administrator or this username already exists.",
                    file=sys.stderr,
                )
                return 1
        elif user is None or not user.is_active:
            print("Active user not found.", file=sys.stderr)
            return 1
        try:
            password = getpass("Password: ")
            confirmation = getpass("Repeat password: ")
            if password != confirmation:
                raise ValueError("Passwords do not match.")
            password_hash = hash_password(password)
        except (ValueError, EOFError, KeyboardInterrupt) as exc:
            print(str(exc) or "Password entry cancelled.", file=sys.stderr)
            return 1
        if args.command == "init-admin":
            db.add(User(username=args.username, password_hash=password_hash))
        else:
            assert user is not None
            user.password_hash = password_hash
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            print(
                "User could not be saved; the username may already exist.",
                file=sys.stderr,
            )
            return 1
    print(
        "Administrator created."
        if args.command == "init-admin"
        else "Password changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
