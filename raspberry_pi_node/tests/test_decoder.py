import pytest

from src import decoder
from src import registers as R


def base_regs(overrides=None):
    """A valid 40001-40020 block: rS=0, -18.2 degC, compressor on."""
    regs = {
        40001: 7507, 40002: 12, 40003: 115, 40004: 1, 40005: 0, 40006: (-182) & 0xFFFF,
        40007: 0, 40008: (-183) & 0xFFFF, 40009: 0, 40010: 0b100, 40011: 0b01, 40012: 0,
        40013: 0, 40014: 1234, 40015: 0, 40016: 5, 40017: 30, 40018: (-180) & 0xFFFF,
        40019: (-150) & 0xFFFF, 40020: (-200) & 0xFFFF,
    }
    regs.update(overrides or {})
    return regs


class TestAddressTranslation:
    def test_holding_references_are_zero_based(self):
        assert R.holding_address(40001) == 0
        assert R.holding_address(40006) == 5
        assert R.holding_address(40068) == 67

    def test_offset_override(self):
        assert R.holding_address(40006, offset=1) == 6

    def test_coils_are_zero_based(self):
        assert R.coil_address(1) == 0
        assert R.coil_address(5) == 4

    @pytest.mark.parametrize("bad", [6, 30001, 40000, 50000])
    def test_rejects_non_holding_references(self, bad):
        with pytest.raises(ValueError):
            R.holding_address(bad)

    def test_all_reads_respect_50_register_limit(self):
        for _, count in (R.STATUS_BLOCK, R.PARAMETER_BLOCK, *R.STATUS_SEGMENTS):
            assert count <= R.MAX_REGISTERS_PER_READ

    def test_segments_cover_every_documented_status_register(self):
        covered = {s + i for s, n in R.STATUS_SEGMENTS for i in range(n)}
        assert covered == {r.reference for r in R.STATUS_REGISTERS}
        assert not covered & set(R.NA_REFERENCES)

    def test_parameter_block_matches_documented_range(self):
        start, count = R.PARAMETER_BLOCK
        assert (start, start + count - 1) == (40031, 40068)
        assert len(R.PARAMETERS) == 38


class TestIntegerDecoding:
    @pytest.mark.parametrize("raw,expected", [(0, 0), (1, 1), (32767, 32767), (32768, -32768),
                                              (65535, -1), (65354, -182)])
    def test_int16(self, raw, expected):
        assert decoder.to_int16(raw) == expected

    @pytest.mark.parametrize("raw", [-1, 65536])
    def test_int16_rejects_out_of_range(self, raw):
        with pytest.raises(ValueError):
            decoder.to_int16(raw)


class TestTemperatureScaling:
    def test_rs0_tenths(self):
        t = decoder.decode_temperature(253, 0)
        assert (t.value_c, t.status) == (25.3, "ok")

    def test_rs1_whole_degrees(self):
        t = decoder.decode_temperature(25, 1)
        assert (t.value_c, t.status) == (25.0, "ok")

    def test_negative_rs0(self):
        assert decoder.decode_temperature((-182) & 0xFFFF, 0).value_c == -18.2

    def test_negative_rs1(self):
        assert decoder.decode_temperature((-18) & 0xFFFF, 1).value_c == -18.0

    def test_no_float_noise(self):
        assert decoder.decode_temperature((-3) & 0xFFFF, 0).value_c == -0.3

    @pytest.mark.parametrize("rs", [None, 2, -1])
    def test_unknown_resolution_withholds_value(self, rs):
        t = decoder.decode_temperature(100, rs)
        assert t.value_c is None and t.status == "unknown_resolution"

    def test_range_limits(self):
        assert decoder.decode_temperature((-450) & 0xFFFF, 0).value_c == -45.0
        assert decoder.decode_temperature(1000, 0).value_c == 100.0
        assert decoder.decode_temperature((-451) & 0xFFFF, 0).status == "out_of_range"
        assert decoder.decode_temperature(101, 1).status == "out_of_range"


class TestSpecialValues:
    @pytest.mark.parametrize("rs", [0, 1])
    def test_probe_fail_low(self, rs):
        t = decoder.decode_temperature(1001, rs, probe_sentinels=True)
        assert t.value_c is None and t.status == "probe_fail_low"

    @pytest.mark.parametrize("rs", [0, 1])
    def test_probe_fail_high(self, rs):
        t = decoder.decode_temperature(1002, rs, probe_sentinels=True)
        assert t.value_c is None and t.status == "probe_fail_high"

    def test_1001_is_not_a_sentinel_elsewhere_but_out_of_range(self):
        assert decoder.decode_temperature(1001, 0).status == "out_of_range"

    def test_log_reset(self):
        t = decoder.decode_temperature(1007, 0, log_sentinel=True, range_check=False)
        assert t.value_c is None and t.status == "log_reset"


class TestStatusDecoding:
    def test_good_reading(self):
        d = decoder.decode_status(base_regs(), expected_slave_id=1)
        assert d.identity_ok and d.quality == "good" and d.quality_flags == []
        assert d.temperature_c == -18.2
        assert d.actual.value_c == -18.3
        assert d.setpoint.value_c == -18.0
        assert d.log_high.value_c == -15.0 and d.log_low.value_c == -20.0
        assert d.compressor_on is True and d.alarm_on is False
        assert d.compressor_run_hours == 1234
        assert d.resolution_c == 0.1

    def test_rs1_scaling_of_all_temperatures(self):
        d = decoder.decode_status(base_regs({40005: 1, 40006: (-18) & 0xFFFF, 40018: (-20) & 0xFFFF,
                                             40010: 0}))
        assert d.temperature_c == -18.0 and d.setpoint.value_c == -20.0
        assert d.resolution_c == 1.0 and d.quality == "good"

    def test_system_status_bits(self):
        value = (1 << 1) | (1 << 2) | (1 << 5) | (1 << 8) | (1 << 9) | (1 << 10)
        d = decoder.decode_status(base_regs({40010: value}))
        f = d.system_flags
        assert f["keypad_locked"] and f["defrost"] and f["restart_delay"] and f["min_on_delay"]
        assert f["ht_lt_power_on_delay"] and not f["power_off"]

    def test_relay_bits(self):
        d = decoder.decode_status(base_regs({40011: 0b10}))
        assert d.compressor_on is False and d.alarm_on is True

    def test_fault_bits(self):
        d = decoder.decode_status(base_regs({40012: 0b1111100}))
        assert d.faults == ["high_temp", "low_temp", "door_open", "compressor_fault", "manual_compressor_fault"]
        assert d.quality == "good"  # process faults do not make the measurement bad

    def test_probe_fail_sentinel_withholds_temperature(self):
        d = decoder.decode_status(base_regs({40006: 1001, 40012: 0b1}))
        assert d.temperature_c is None and d.quality == "probe_fault"
        assert "probe_fail_low" in d.faults

    def test_probe_fail_bit_without_sentinel_withholds_temperature(self):
        d = decoder.decode_status(base_regs({40012: 0b10}))
        assert d.temperature_c is None and d.quality == "probe_fault"

    def test_identity_mismatch_withholds_everything(self):
        d = decoder.decode_status(base_regs({40001: 0}))
        assert not d.identity_ok and d.quality == "invalid"
        assert d.temperature_c is None and d.setpoint.value_c is None

    def test_unknown_rs_is_invalid(self):
        d = decoder.decode_status(base_regs({40005: 5}))
        assert d.quality == "invalid" and d.temperature_c is None

    def test_resolution_bit_disagreement_is_suspect(self):
        d = decoder.decode_status(base_regs({40010: 0}))  # bit2=0 says 1 degC but rS=0
        assert d.quality == "suspect" and "resolution_mismatch" in d.quality_flags
        assert d.temperature_c == -18.2  # scaling follows rS

    def test_device_id_mismatch_is_suspect(self):
        d = decoder.decode_status(base_regs(), expected_slave_id=7)
        assert "device_id_mismatch" in d.quality_flags and d.quality == "suspect"

    def test_out_of_range_control_temperature_is_invalid(self):
        d = decoder.decode_status(base_regs({40006: 1500}))
        assert d.quality == "invalid" and d.temperature_c is None

    def test_undocumented_fault_bit_flagged(self):
        d = decoder.decode_status(base_regs({40012: 0x80}))
        assert "undocumented_fault_bits" in d.quality_flags


class TestParameters:
    def test_scaling_and_redaction(self):
        regs = {p.reference: 0 for p in R.PARAMETERS}
        regs[40031] = (-180) & 0xFFFF
        regs[40041] = 3
        regs[40065] = 1234
        out = decoder.decode_parameters(regs, 0)
        assert out["SP"]["value"] == -18.0
        assert out["P6"]["value"] == 3 and out["P6"]["unit"] == "min"
        assert out["PA"]["value"] is None and out["PA"]["redacted"] is True
        assert out["P1"]["label"].startswith("Cooling")

    def test_coils(self):
        assert decoder.decode_coils([False, True, False, False, True]) == {
            "power": False, "alarm_relay_mute": True, "manual_fault_reset": False,
            "temp_log_reset": False, "defrost": True,
        }
