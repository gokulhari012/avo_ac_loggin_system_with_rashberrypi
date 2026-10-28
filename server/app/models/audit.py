from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from ..extensions import db
from .common import BigIntPK, loads, new_uid, utcnow


class AuditEvent(db.Model):
    """Append-only audit trail.  Updates and deletes are refused by the ORM
    (see the before_flush hook below) and by database triggers created in the
    migration, so not even an administrator can alter history through the app."""
    __tablename__ = "audit_events"
    __table_args__ = (db.Index("ix_audit_resource", "resource_type", "resource_id"),)
    id = db.Column(BigIntPK, primary_key=True, autoincrement=True)
    event_uid = db.Column(db.String(36), unique=True, nullable=False, default=new_uid)
    occurred_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    actor_type = db.Column(db.String(8), nullable=False)        # user | node | system
    actor_id = db.Column(db.String(64))
    actor_name = db.Column(db.String(128), nullable=False, default="", index=True)
    category = db.Column(db.String(32), nullable=False, index=True)
    action = db.Column(db.String(64), nullable=False, index=True)
    resource_type = db.Column(db.String(32))
    resource_id = db.Column(db.String(64))
    previous_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    outcome = db.Column(db.String(16), nullable=False, default="success", index=True)
    reason = db.Column(db.String(500))
    source_ip = db.Column(db.String(64))
    request_id = db.Column(db.String(64))
    details_json = db.Column(db.Text)

    @property
    def details(self) -> dict:
        return loads(self.details_json, {})

    @property
    def previous(self):
        return loads(self.previous_value)

    @property
    def new(self):
        return loads(self.new_value)


class AuditImmutableError(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def _protect_audit_events(session, _flush_context, _instances):
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, AuditEvent) and (obj in session.deleted or session.is_modified(obj)):
            raise AuditImmutableError("audit events are append-only")


AUDIT_TRIGGERS_SQLITE = (
    "CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
)


class ExportRecord(db.Model):
    __tablename__ = "export_records"
    id = db.Column(db.Integer, primary_key=True)
    export_uid = db.Column(db.String(36), unique=True, nullable=False, default=new_uid)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    username = db.Column(db.String(64), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    completed_at = db.Column(db.DateTime)
    report_type = db.Column(db.String(32), nullable=False, index=True)
    format = db.Column(db.String(8), nullable=False)
    filters_json = db.Column(db.Text)
    date_from = db.Column(db.DateTime)
    date_to = db.Column(db.DateTime)
    record_count = db.Column(db.Integer)
    outcome = db.Column(db.String(24), nullable=False, default="started")
    error = db.Column(db.String(1000))
    file_name = db.Column(db.String(255))
    file_size = db.Column(db.Integer)
    sha256 = db.Column(db.String(64))
    source_ip = db.Column(db.String(64))

    @property
    def filters(self) -> dict:
        return loads(self.filters_json, {})


class SystemSetting(db.Model):
    __tablename__ = "system_settings"
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by = db.Column(db.String(64), nullable=False, default="")
