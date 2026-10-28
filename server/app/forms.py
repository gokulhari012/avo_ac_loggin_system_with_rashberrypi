"""WTForms definitions (CSRF protected through Flask-WTF)."""

from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import (BooleanField, DecimalField, FieldList, FormField, HiddenField, IntegerField, PasswordField,
                     SelectField, StringField, TextAreaField)
from wtforms.validators import DataRequired, EqualTo, Length, NumberRange, Optional, Regexp, ValidationError

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
ID_MESSAGE = "Use 1-64 letters, digits, '.', '_' or '-' (start with a letter or digit)."


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=256)])


class PasswordChangeForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    new_password = PasswordField("New password", validators=[DataRequired(), Length(max=256)])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("new_password",
                                                                                         "Passwords differ.")])


class UserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=64),
                                                   Regexp(r"^[A-Za-z0-9_.@-]+$", message="Invalid characters.")])
    full_name = StringField("Full name", validators=[Optional(), Length(max=128)])
    email = StringField("Email", validators=[Optional(), Length(max=255)])
    role_id = SelectField("Role", coerce=int)
    is_active = BooleanField("Active", default=True)
    password = PasswordField("Password", validators=[Optional(), Length(max=256)],
                             description="Required for new users; leave blank to keep the current password.")
    reason = StringField("Reason for change", validators=[Optional(), Length(max=500)])


class ReasonForm(FlaskForm):
    reason = StringField("Reason", validators=[Optional(), Length(max=500)])


class ControllerEntryForm(FlaskForm):
    class Meta:
        csrf = False

    uid = StringField("Controller ID", validators=[Optional(), Regexp(ID_PATTERN, message=ID_MESSAGE)])
    name = StringField("Name", validators=[Optional(), Length(max=128)])
    slave_id = IntegerField("Slave ID", validators=[Optional(), NumberRange(1, 250)])
    poll_interval_s = DecimalField("Poll interval (s)", places=1, validators=[Optional(), NumberRange(1, 3600)])
    parameter_read_interval_s = IntegerField("Parameter read (s)", validators=[Optional(), NumberRange(0, 604800)])
    address_offset = SelectField("Address offset", choices=[("0", "0"), ("-1", "-1"), ("1", "+1")],
                                 default="0")
    read_strategy = SelectField("Read strategy", choices=[("auto", "auto"), ("block", "block"),
                                                          ("segments", "segments")], default="auto")
    enabled = BooleanField("Enabled", default=True)


class NodeDetailsMixin:
    name = StringField("Friendly name", validators=[DataRequired(), Length(max=128)])
    description = StringField("Description", validators=[Optional(), Length(max=200)])
    site = StringField("Site", validators=[Optional(), Length(max=128)])
    location = StringField("Location", validators=[Optional(), Length(max=128)])
    equipment = StringField("Equipment", validators=[Optional(), Length(max=128)])


class NodeConfigForm(NodeDetailsMixin, FlaskForm):
    server_url = StringField("Server URL override", validators=[Optional(), Length(max=200)],
                             description="Leave blank to keep the URL in the node's local file. If set, the node "
                                         "tests the new URL before switching.")
    upload_interval_s = DecimalField("Upload interval (s)", places=1, default=5, validators=[NumberRange(1, 3600)])
    batch_size = IntegerField("Upload batch size", default=200, validators=[NumberRange(1, 1000)])
    heartbeat_interval_s = DecimalField("Heartbeat interval (s)", places=1, default=30,
                                        validators=[NumberRange(5, 3600)])
    config_sync_interval_s = DecimalField("Config check interval (s)", places=1, default=60,
                                          validators=[NumberRange(10, 86400)])
    request_timeout_s = DecimalField("Request timeout (s)", places=1, default=10, validators=[NumberRange(1, 120)])
    backoff_max_s = DecimalField("Max retry delay (s)", places=1, default=300, validators=[NumberRange(5, 3600)])

    bus_name = StringField("Bus name", default="rs485-1", validators=[DataRequired(), Regexp(ID_PATTERN,
                                                                                             message=ID_MESSAGE)])
    port = StringField("Serial port", default="/dev/ttyUSB0", validators=[DataRequired(), Length(max=200)])
    baudrate = SelectField("Baud rate", coerce=int, choices=[(9600, "9600"), (19200, "19200"), (38400, "38400")])
    parity = SelectField("Parity", choices=[("N", "None"), ("E", "Even"), ("O", "Odd")])
    timeout_s = DecimalField("Response timeout (s)", places=2, default=1.0, validators=[NumberRange(0.05, 10)])
    retries = IntegerField("Retries", default=2, validators=[NumberRange(0, 10)])
    inter_request_delay_ms = IntegerField("Inter-request delay (ms)", default=50, validators=[NumberRange(0, 5000)])

    controllers = FieldList(FormField(ControllerEntryForm), min_entries=1)

    retention_days_delivered = DecimalField("Keep delivered records (days)", places=1, default=7,
                                            validators=[NumberRange(0, 3650)])
    max_pending_records = IntegerField("Max buffered records", default=2_000_000,
                                       validators=[NumberRange(1000, 50_000_000)])
    full_behavior = SelectField("When buffer is full", choices=[("drop_oldest", "Drop oldest (report gap)"),
                                                                ("stop_collecting", "Stop collecting (report gap)")])
    min_free_disk_mb = IntegerField("Min free disk (MB)", default=100, validators=[NumberRange(0, 100_000)])
    log_level = SelectField("Log level", choices=[(x, x) for x in ("DEBUG", "INFO", "WARNING", "ERROR")],
                            default="INFO")
    diagnostic_registers = BooleanField("Diagnostic register logging")

    advanced_json = TextAreaField("Advanced: full configuration JSON", validators=[Optional(), Length(max=100_000)],
                                  description="If filled, this JSON is used instead of the fields above "
                                              "(needed for several RS-485 buses).")
    reason = StringField("Reason for change", validators=[DataRequired(message="A reason is required."),
                                                          Length(max=500)])


class NodeRegisterForm(NodeConfigForm):
    uid = StringField("Node ID", validators=[DataRequired(), Regexp(ID_PATTERN, message=ID_MESSAGE)],
                      description="Must equal node.id in the Raspberry Pi's config.yaml.")
    reason = StringField("Reason", default="Initial registration", validators=[Optional(), Length(max=500)])


class CommandForm(FlaskForm):
    command = SelectField("Command", choices=[
        ("identity_check", "Identity check (read-only)"),
        ("read_parameters", "Read parameters 40031-40068 (read-only)"),
        ("alarm_mute", "Mute alarm relay (coil 2 <- 0)"),
        ("fault_reset", "Manual fault reset (coil 3 <- 0)"),
        ("temp_log_reset", "Reset temperature log (coil 4 <- 1)"),
        ("defrost_start", "Start defrost (coil 5 <- 1)"),
        ("defrost_stop", "Stop defrost (coil 5 <- 0)"),
    ])
    reason = StringField("Reason", validators=[DataRequired(message="A reason is required."), Length(max=500)])
    confirm = StringField("Type the controller ID to confirm", validators=[DataRequired()])
    nonce = HiddenField()

    def validate_confirm(self, field):
        expected = getattr(self, "_expected_confirm", None)
        if expected and field.data.strip() != expected:
            raise ValidationError("Confirmation does not match the controller ID.")
