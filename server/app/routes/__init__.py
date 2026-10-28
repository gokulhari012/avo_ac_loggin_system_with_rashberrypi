from flask import Flask


def register_blueprints(app: Flask) -> None:
    from . import admin, api, audit_views, auth, dashboard, logs, monitor, nodes, reports

    for module in (api, auth, dashboard, monitor, logs, nodes, reports, audit_views, admin):
        app.register_blueprint(module.bp)
