from __future__ import annotations

from ..extensions import db
from .common import BigIntPK, loads


class TemperatureReading(db.Model):
    """One poll attempt of one controller, exactly as reported by the node.

    Rows are never updated or overwritten.  ``temperature_c`` is NULL when the
    controller could not be read or reported a probe fault - never invented.
    """
    __tablename__ = "temperature_readings"
    __table_args__ = (
        db.Index("ix_readings_controller_time", "controller_id", "acquired_at"),
        db.Index("ix_readings_node_time", "node_id", "acquired_at"),
    )

    id = db.Column(BigIntPK, primary_key=True, autoincrement=True)
    reading_uid = db.Column(db.String(36), unique=True, nullable=False)   # generated on the node
    node_id = db.Column(db.Integer, db.ForeignKey("nodes.id"), nullable=False)
    controller_id = db.Column(db.Integer, db.ForeignKey("controllers.id"), nullable=False)
    node = db.relationship("Node")
    controller = db.relationship("Controller")

    acquired_at = db.Column(db.DateTime, nullable=False, index=True)       # node clock, UTC
    received_at = db.Column(db.DateTime, nullable=False)                   # server clock, UTC
    backfilled = db.Column(db.Boolean, nullable=False, default=False, index=True)
    source = db.Column(db.String(16), nullable=False, index=True)          # controller | simulated
    config_version = db.Column(db.Integer)

    comm_status = db.Column(db.String(32), nullable=False, index=True)
    comm_error = db.Column(db.String(500))
    quality = db.Column(db.String(16), nullable=False, index=True)
    quality_flags = db.Column(db.String(255), nullable=False, default="")

    temperature_c = db.Column(db.Float)            # control (display) temperature, 40006
    actual_temperature_c = db.Column(db.Float)     # 40008 (scaling assumption - see docs)
    setpoint_c = db.Column(db.Float)               # 40018
    temp_log_high_c = db.Column(db.Float)          # 40019
    temp_log_low_c = db.Column(db.Float)           # 40020
    resolution_rs = db.Column(db.SmallInteger)     # 40005

    compressor_on = db.Column(db.Boolean)          # 40011 bit0
    alarm_on = db.Column(db.Boolean)               # 40011 bit1
    defrost_active = db.Column(db.Boolean)         # 40010 bit5
    keypad_locked = db.Column(db.Boolean)          # 40010 bit1
    fault_flags = db.Column(db.Integer)            # 40012 raw
    fault_codes = db.Column(db.String(255), nullable=False, default="")
    system_status = db.Column(db.Integer)          # 40010 raw
    relay_status = db.Column(db.Integer)           # 40011 raw
    compressor_run_hours = db.Column(db.Integer)   # 40014

    raw_registers = db.Column(db.Text)             # JSON {"40001": 7507, ...} as received
    extra_json = db.Column(db.Text)                # delays, balanced defrost, warnings, slave id, ...

    @property
    def faults(self) -> list[str]:
        return [f for f in self.fault_codes.split(",") if f]

    @property
    def flags(self) -> list[str]:
        return [f for f in self.quality_flags.split(",") if f]

    @property
    def raw(self) -> dict:
        return loads(self.raw_registers, {})

    @property
    def extra(self) -> dict:
        return loads(self.extra_json, {})
