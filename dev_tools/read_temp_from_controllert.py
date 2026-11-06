# read_temp.py  --  run on the Pi:  python3 read_temp.py


#Run using cmd - /opt/temperature-node/venv/bin/python read_temp_from_controllert.py
#Main program need to stop before running this script. Run cmd - systemctl stop temperature-node.service

from pymodbus.client import ModbusSerialClient

# PORT = "/dev/ttyUSB0"   # your RS-485 adapter
# PORT = "/dev/ttySC0"
PORT = "/dev/ttySC1"
BAUD = 9600             # must match controller parameter bd
PARITY = "N"            # must match controller parameter PAr
SLAVE_ID = 1            # must match controller parameter did

client = ModbusSerialClient(PORT, baudrate=BAUD, parity=PARITY, bytesize=8, stopbits=1, timeout=1.0)
if not client.connect():
    raise SystemExit(f"cannot open {PORT}")

# Registers 40001..40006 -> protocol addresses 0..5
rr = client.read_holding_registers(0, count=6, device_id=SLAVE_ID)
client.close()
if rr.isError():
    raise SystemExit(f"read failed: {rr}")

r = rr.registers
if r[0] != 7507 or r[2] != 115:
    raise SystemExit(f"identity check failed: 40001={r[0]} (want 7507), 40003={r[2]} (want 115)")

rs = r[4]                                   # 40005: 0 = 0.1 degC, 1 = 1 degC
raw = r[5]                                  # 40006: control (display) temperature
signed = raw - 0x10000 if raw & 0x8000 else raw

if signed == 1001:
    print("Probe fail (low)")
elif signed == 1002:
    print("Probe fail (high)")
else:
    print(f"Temperature: {signed * (0.1 if rs == 0 else 1.0):.1f} °C")
