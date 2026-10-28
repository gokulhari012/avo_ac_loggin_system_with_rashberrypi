"""Server entry point.

Production (Windows):   python run.py                      (Waitress WSGI server)
Development only:       python run.py --dev
Flask CLI:              flask --app run db upgrade | seed | create-admin

Host/port come from HOST/PORT in server/.env (default 127.0.0.1:8000 = this PC
only).  Use HOST=0.0.0.0 to accept Raspberry Pi nodes from the LAN.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import sys

from app import __version__, create_app
from app.extensions import db

app = create_app()
log = logging.getLogger("server")


def prepare_database() -> None:
    from flask_migrate import upgrade

    from app.cli import seed_roles

    with app.app_context():
        if app.config["AUTO_MIGRATE"]:
            upgrade()
        seed_roles()


def _audit_lifecycle(action: str, details: dict) -> None:
    from app.services.audit import audit

    with app.app_context():
        try:
            audit("system", action, actor_type="system", actor_name="server", details=details, commit=True)
        except Exception:  # noqa: BLE001 - never block start/stop on auditing
            db.session.rollback()
            log.exception("could not record %s", action)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Temperature monitoring server")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--threads", type=int, default=int(os.environ.get("WAITRESS_THREADS", "8")))
    ap.add_argument("--dev", action="store_true", help="Flask development server (never for production)")
    ap.add_argument("--no-monitor", action="store_true", help="do not start the offline-detection thread")
    args = ap.parse_args(argv)

    prepare_database()
    details = {"version": __version__, "host": args.host, "port": args.port, "pid": os.getpid()}
    _audit_lifecycle("server_started", details)
    atexit.register(_audit_lifecycle, "server_stopped", {"version": __version__, "pid": os.getpid()})

    if not args.no_monitor:
        from app.services.monitor import Monitor
        Monitor(app).start()

    log.info("Temperature server %s listening on http://%s:%s", __version__, args.host, args.port)
    if args.dev:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        return 0
    from waitress import serve

    proxy = os.environ.get("TRUSTED_PROXY")  # e.g. 127.0.0.1 when behind Caddy/IIS
    kwargs = {"trusted_proxy": proxy, "trusted_proxy_headers": "x-forwarded-for x-forwarded-proto"} if proxy else {}
    try:
        serve(app, host=args.host, port=args.port, threads=args.threads, ident="TemperatureServer",
              max_request_body_size=app.config["MAX_CONTENT_LENGTH"], **kwargs)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
