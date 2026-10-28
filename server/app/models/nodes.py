from __future__ import annotations

from ..extensions import db
from .common import loads, new_uid, utcnow


class Node(db.Model):
    __tablename__ = "nodes"
    id = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.String(64), unique=True, nullable=False, index=True)  # node.id in the node config
    name = db.Column(db.String(128), nullable=False, default="")
    description = db.Column(db.String(500), nullable=False, default="")
    site = db.Column(db.String(128), nullable=False, default="", index=True)
    location = db.Column(db.String(128), nullable=False, default="")
    equipment = db.Column(db.String(128), nullable=False, default="", index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)

    # Credential: only a SHA-256 hash is stored; the token is shown once.
    token_hash = db.Column(db.String(64), nullable=False)
    token_hint = db.Column(db.String(8), nullable=False, default="")
    token_created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    auth_failures = db.Column(db.Integer, nullable=False, default=0)
    last_auth_failure_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    # Health as last reported (heartbeat) / observed
    last_seen_at = db.Column(db.DateTime, index=True)
    last_heartbeat_at = db.Column(db.DateTime)
    last_heartbeat_json = db.Column(db.Text)
    last_ip = db.Column(db.String(64))
    hostname = db.Column(db.String(128))
    software_version = db.Column(db.String(32))
    reported_config_version = db.Column(db.Integer)
    config_error = db.Column(db.Text)
    last_upload_at = db.Column(db.DateTime)
    last_poll_ok_at = db.Column(db.DateTime)
    queue_pending = db.Column(db.Integer)
    clock_synchronized = db.Column(db.Boolean)
    clock_skew_s = db.Column(db.Float)
    simulation = db.Column(db.Boolean, nullable=False, default=False)
    online = db.Column(db.Boolean, nullable=False, default=False)
    online_changed_at = db.Column(db.DateTime)

    controllers = db.relationship("Controller", back_populates="node", lazy="selectin",
                                  order_by="Controller.uid")

    @property
    def heartbeat(self) -> dict:
        return loads(self.last_heartbeat_json, {})

    @property
    def label(self) -> str:
        return f"{self.name} ({self.uid})" if self.name else self.uid


class Controller(db.Model):
    __tablename__ = "controllers"
    __table_args__ = (db.UniqueConstraint("node_id", "uid", name="uq_controller_node_uid"),)
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, db.ForeignKey("nodes.id"), nullable=False, index=True)
    node = db.relationship("Node", back_populates="controllers")
    uid = db.Column(db.String(64), nullable=False)          # controller id within the node config
    name = db.Column(db.String(128), nullable=False, default="")
    model = db.Column(db.String(64), nullable=False, default="SZ-7526TX-REA")
    bus_name = db.Column(db.String(64))
    slave_id = db.Column(db.Integer)
    poll_interval_s = db.Column(db.Float)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    auto_registered = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    # Latest state, denormalised for the dashboard (history lives in temperature_readings)
    last_reading_at = db.Column(db.DateTime)      # last poll attempt, good or not
    last_good_at = db.Column(db.DateTime)         # last reading with comm_status ok
    last_reading_uid = db.Column(db.String(36))
    last_temperature_c = db.Column(db.Float)
    last_setpoint_c = db.Column(db.Float)
    last_quality = db.Column(db.String(16))
    last_comm_status = db.Column(db.String(32))
    last_comm_error = db.Column(db.String(500))
    last_compressor_on = db.Column(db.Boolean)
    last_alarm_on = db.Column(db.Boolean)
    last_defrost_active = db.Column(db.Boolean)
    last_fault_codes = db.Column(db.String(255))
    last_source = db.Column(db.String(16))
    fault_state_at = db.Column(db.DateTime)       # acquisition time of the last reading used for fault tracking

    parameters_json = db.Column(db.Text)          # latest device parameter snapshot (40031-40068)
    parameters_digest = db.Column(db.String(64))
    parameters_read_at = db.Column(db.DateTime)

    @property
    def label(self) -> str:
        return self.name or self.uid

    @property
    def fault_list(self) -> list[str]:
        return [f for f in (self.last_fault_codes or "").split(",") if f]

    @property
    def parameters(self) -> dict:
        return loads(self.parameters_json, {})


class NodeConfigVersion(db.Model):
    """Versioned desired configuration for a node (the node configuration history)."""
    __tablename__ = "node_config_versions"
    __table_args__ = (db.UniqueConstraint("node_id", "version", name="uq_node_config_version"),)
    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, db.ForeignKey("nodes.id"), nullable=False, index=True)
    node = db.relationship("Node")
    version = db.Column(db.Integer, nullable=False)
    config_json = db.Column(db.Text, nullable=False)
    reason = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by = db.relationship("User")
    # pending -> delivered -> applied | rejected ; or superseded by a newer version
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    delivered_at = db.Column(db.DateTime)
    acked_at = db.Column(db.DateTime)
    ack_errors = db.Column(db.Text)
    ignored_keys = db.Column(db.Text)

    @property
    def config(self) -> dict:
        return loads(self.config_json, {})


class ControllerConfigHistory(db.Model):
    """Changes to a controller's configuration: server-side settings (slave ID,
    poll interval...) and parameter changes detected in device snapshots."""
    __tablename__ = "controller_config_history"
    id = db.Column(db.Integer, primary_key=True)
    controller_id = db.Column(db.Integer, db.ForeignKey("controllers.id"), nullable=False, index=True)
    controller = db.relationship("Controller")
    recorded_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    source = db.Column(db.String(32), nullable=False)  # server_config | device_snapshot
    actor = db.Column(db.String(128), nullable=False, default="")
    config_version = db.Column(db.Integer)
    previous_json = db.Column(db.Text)
    new_json = db.Column(db.Text)
    changed_keys = db.Column(db.Text)
    reason = db.Column(db.String(500), nullable=False, default="")


class DiscoveredNode(db.Model):
    """Unregistered devices that tried to contact the API (never trusted)."""
    __tablename__ = "discovered_nodes"
    id = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.String(64), unique=True, nullable=False)
    last_ip = db.Column(db.String(64))
    hostname = db.Column(db.String(128))
    software_version = db.Column(db.String(32))
    first_seen_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    attempts = db.Column(db.Integer, nullable=False, default=0)


class ControllerCommand(db.Model):
    __tablename__ = "controller_commands"
    id = db.Column(db.Integer, primary_key=True)
    command_uid = db.Column(db.String(36), unique=True, nullable=False, default=new_uid)
    node_id = db.Column(db.Integer, db.ForeignKey("nodes.id"), nullable=False, index=True)
    controller_id = db.Column(db.Integer, db.ForeignKey("controllers.id"), nullable=False, index=True)
    controller = db.relationship("Controller")
    command = db.Column(db.String(32), nullable=False)
    reason = db.Column(db.String(500), nullable=False, default="")
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    requested_by = db.relationship("User")
    requested_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    sent_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    result_json = db.Column(db.Text)

    @property
    def result(self) -> dict:
        return loads(self.result_json, {})
