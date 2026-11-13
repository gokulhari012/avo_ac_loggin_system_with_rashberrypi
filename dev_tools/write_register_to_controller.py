from pymodbus.client import ModbusSerialClient

client = ModbusSerialClient(
    port="/dev/ttySC0",
    baudrate=9600,
    parity="N",
    stopbits=1,
    bytesize=8,
    timeout=2
)

SLAVE_ID = 1

if client.connect():
    value = 0  # Replace with the value you want to write

    response = client.write_register(
        address=63,  #40064 Register address to set the Temperature Resolution.
        value=value,
        device_id=SLAVE_ID
    )

    if not response.isError():
        print("Register 40064 written successfully")

        # Read back to verify
        result = client.read_holding_registers(
            address=63,
            count=1,
            device_id=SLAVE_ID
        )

        if not result.isError():
            print("Register 40064 value:", result.registers[0])
    else:
        print("Write failed:", response)

    client.close()
else:
    print("Unable to connect to Modbus device")