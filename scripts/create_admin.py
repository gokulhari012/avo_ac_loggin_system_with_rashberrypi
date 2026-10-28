"""Create a server user (Administrator by default).

Run with the SERVER virtualenv:
    server\\.venv\\Scripts\\python scripts\\create_admin.py --username admin
    server\\.venv\\Scripts\\python scripts\\create_admin.py --username alice --role Operator
The password is prompted (never pass it on the command line in production).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
os.chdir(SERVER)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--username", required=True)
    ap.add_argument("--full-name", default="")
    ap.add_argument("--role", default="Administrator", choices=["Administrator", "Operator", "Auditor"])
    args = ap.parse_args()

    from click import ClickException

    from app import create_app
    from app.cli import create_user, seed_roles

    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    app = create_app()
    with app.app_context():
        seed_roles()
        try:
            create_user(args.username, password, args.role, args.full_name)
        except ClickException as exc:
            print(f"Error: {exc.message}", file=sys.stderr)
            return 1
    print(f"User {args.username} created with role {args.role}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
