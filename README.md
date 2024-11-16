# Sim800 Modem Driver

A simple async/sync libray for connecting to
the internet with a sim800l GSM module.
It can be used as a basic drop-in replacemwnt for the socket API as well.

## Quick Demo

```python
from sim800_modem import Sim800lModem
import utime, uasyncio

# Create a config object
CONFIG = {
    "UART_ID": 1,
    "UART_RX": 5,
    "UART_TX": 4,
    "RESET_PIN": 11,
    "UART_BAUDRATE": 115200,
    "BUFFER_SIZE": 1024,
    "SIM800_APN": 'internet',
}


async def demo():
    # create a modem object
    modem = Sim800lModem(config=CONFIG)

    # connect modem to gprs network in async mode
    await modem.connect_to_gprs(data_mode=True, async_mode=True)
    if not modem.gprs_connected:
        print("Could not connect to gprs")
        return

    # connect to server
    await modem.connect_to_server('example.com', port=80)
    if not modem.is_connected:
        print("Connection failed")
        return

    # send data
    await modem.a_write(b'GET / HTTP/1.1\r\nHost: example.com\r\n\r\n')

    # read response
    timeout_ms = 5000
    t0 = utime.ticks_ms()
    while utime.ticks_diff(utime.ticks_ms(), t0) < timeout_ms:
        if modem.any():
            # read data from server
            print(await modem.a_read())
        await uasyncio.sleep_ms(0)

    # close server and gprs connection
    await modem.a_close()

```

## Socket API demo
This library can serve as a simple dropin replacement for the socket/usocket library.
This is convenient for boards like the Pico r2040 whose firmware ships without a socket library.
This makes it to use libraries like the mqtt libray without having to modify the library or compile new firmware.

```python
from sim800_modem import Sim800lModem

# you must connect the modem to gprs first before socket()
CONFIG=...
modem = Sim800lModem(config=CONFIG)
# connect modem to gprs network in async mode
await modem.connect_to_gprs(data_mode=True, async_mode=False)


def connect_to_mqtt(clean_session=True):
    from sim800_modem import Sim800lModem as socket # import modem as socket

    sock = socket.socket()
    res = socket.getaddrinfo('mqtt.io', 1883)
    addr = res[0][-1]
    sock.connect(addr)
    sock.write(b'...')
    sock.setblocking(False)
    sock.read(2)
    sock.setblocking(True)
    sock.readinto(buf, size)
    

```
> [!NOTE]
> Note: All sockeet methods are available as async methods as well. They are prepeded by the prefix "a_" for example:

```python

    await sock.a_connect(addr)
    await sock.a_write(b'...')
    sock.setblocking(False)
    await sock.a_read(2)
    sock.setblocking(True)
    await sock.a_readinto(buf, size)
```

## Usage 

This libray provides a simple sync/async api while allowing flexibility 
to intereact with the modem manually if preferred.

### Data Mode
Under "normal" mode, the sim800 is usually in "Command Mode". 
In this mode, the modem responds to AT Commands and can 
receive URC(unsolicited response code) from the network at any time.
In manual mode, all data sent to the modem must be valid AT commands.

When connecting to a server, the sim800l can switch into "Data Mode". 
In data mode, ALL data sent to the modem is treated as raw data to be sent to the server. 
This means that the modem will not respond to AT commands and certain URC codes might be missed.

If you are using the sim900l for other functions such as sending SMS and Voice, 
while occasionally oppening TCP connections, Command mode might be preferred. 
However, you can exit data mode at any time by calling exit_data_mode, to send other AT commands.
#### Command mode example
```python
    # setting data mode to false keeps the modem in command mode.
    await modem.connect_to_gprs(data_mode=False)  # In COMMAND MODE
    
    # connect to server
    await modem.connect_to_server('example.com', port=80)
    
    # send request mannually (Uses AT commands)
    await modem.send_data_manually(b'GET / HTTP/1.1\r\nHost: example.com\r\n\r\n')
    
    # check if response has arrived
    response = modem.read_response_manually(timeout_ms=1000)
    modem.read_response_manually_into(buf, timeout_ms=1000)
    

```
> [!WARNING]
> Socket methods are not available in manual mode. 
> Async mode can not be used in command mode.

### Async Mode
Due to the inherrently asynchronous nature of cellular communication, messages may arrive in the modem at any time. 
We could handle these messages using IRQs, however the in micropython interrupts are usualy
"soft interrupts" with various limitations. For instance, variants of micropythono expose differing 
interrupt triggers with different characteristics.
Here  are a few of the "execptions" on just the UART from [the micropython docs](https://docs.micropython.org/en/latest/library/machine.UART.html#machine-uart)

> * The rp2 port’s UART.IRQ_TXIDLE is only triggered when the message is longer than 5 characters and the trigger happens when still 5 characters are to be sent.
> * The rp2 port’s UART.IRQ_BREAK needs receiving valid characters for triggering again.
> * The SAMD port’s UART.IRQ_TXIDLE is triggered while the last character is sent.
> * On STM32F4xx MCU’s, using the trigger UART.IRQ_RXIDLE the handler will be called once after the first character and then after the end of the message, when the line is idle.

Due to these "irregularities",
it is unreliable and prone to fail in ways that are difficult to
debug and cause data loss.

Hence, Async mode. An asyncio task waits on the uart and writes the data received to a Ring buffer. 
this ensures that data is read immediately it becomes available preventing the
uart fifos from being flushed before all data is received. 
The data can then be read when convenient. 

>The ammount of data that will be read before blocking is determined by the BUFFER_SIZE property in your config.
when the buffer gets full, you will have to read or flush the buffer by calling read() .
