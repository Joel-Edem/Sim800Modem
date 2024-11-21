from sim800_modem import Sim800lModem
import utime
import uasyncio

CONFIG = {
    "UART_ID": 1,
    "UART_RX": 5,
    "UART_TX": 4,
    "RESET_PIN": 11,
    "UART_BAUDRATE": 115200,
    "BUFFER_SIZE": 2048,
    "SIM800_APN": 'internet',
}


async def demo(domain=b'example.com', path=b'/', timeout_ms=5000):
    modem = Sim800lModem(config=CONFIG)
    await modem.connect_to_gprs(data_mode=True, async_mode=True)

    if not modem.gprs_connected:
        print("Could not connect to gprs")
        return

    await modem.get_set_dns('1.1.1.1')
    print(await modem.get_addr_info('example.com'))
    try:
        res = await modem.connect_to_server('example.com', 80)
        if not res:
            print("Connection failed")
            return
        req = b'GET %s HTTP/1.1\r\nHost: %s\r\n\r\n' % (path, domain)
        await modem.a_write(req)
        t0 = utime.ticks_ms()
        while utime.ticks_diff(utime.ticks_ms(), t0) < timeout_ms:
            if modem.any():
                print(await modem.a_read())
            await uasyncio.sleep_ms(0)
    finally:
        await modem.a_close()


if __name__ == "__main__":
    try:
        uasyncio.run(demo())
    finally:
        uasyncio.new_event_loop()
