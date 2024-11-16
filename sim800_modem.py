# Copyright 2024 Joel Ametepeh <JoelAmetepeh@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
import io
import machine
from machine import Pin, UART
import utime
import uasyncio
from micropython import const
from micropython import RingIO


class Sim800Exception(Exception):
    pass


class Sim800lModem(io.BytesIO):
    _instance = None

    def __new__(cls, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls, )
        return cls._instance

    class CmdStatus:

        CMD_TIMEOUT = None
        CMD_ERROR = const(0)
        CMD_SUCCESS = const(1)
        CMD_CONNECT = const(2)

    END_SEQS = (
        (bytearray(b'\r\nOK\r\n'), CmdStatus.CMD_SUCCESS),
        (bytearray(b'\r\nCONNECT\r\n'), CmdStatus.CMD_CONNECT),
        (bytearray(b'\r\nERROR\r\n'), CmdStatus.CMD_ERROR),

        (bytearray(b'\r\n+CME ERROR:'), CmdStatus.CMD_ERROR),
        (bytearray(b'\r\nSTATE:'), CmdStatus.CMD_SUCCESS),
    )

    def __init__(self, config):

        super().__init__()
        self.config = config
        self.uart = UART(
            config["UART_ID"], config["UART_BAUDRATE"],
            rx=Pin(config["UART_RX"]),
            tx=Pin(config["UART_TX"]),
        )
        if config.get("RESET_PIN"):
            self._reset_pin = machine.Pin(config["RESET_PIN"], Pin.OUT, Pin.PULL_UP, value=1)
        else:
            self._reset_pin = None
            self.reset_module = lambda *_: print("No Reset Pin provided. set 'REST_PIN' key in your config")

        self.gprs_connected = False
        self.ip_addr = ''
        self._msg_length: int = 0
        self._recv_buf = bytearray(config["BUFFER_SIZE"])

        self._server_connected = False
        self._in_data_mode = False
        self._set_blocking = False

        self._reader_task = None
        self._ring_buf = RingIO(self._recv_buf)

        self._async_mode = False
        self._reader_flag = False
        self._loop = None

    @property
    def is_connected(self):
        return self._server_connected

    async def reset_module(self):
        """
        Reset the SIM800L module using the defined reset pin if available.
        If no reset pin is provided, it logs a message.

        Raises:
        Exception: If reset fails or is unavailable.
        """
        if not self._reset_pin:
            raise Exception("No Reset Pin provided. set 'REST_PIN' key in your config")

        print("Resetting Sim800l Module")
        self.gprs_connected = False
        self.ip_addr = ''
        self._msg_length = 0
        self._server_connected = False
        self._in_data_mode = False

        self._reset_pin.value(0)
        await uasyncio.sleep(1)
        self._reset_pin.value(1)
        print("Sim800l Module Reset")

    async def send_at_command(self,
                              command, timeout_ms: int = 4000,
                              wait_response: bool = True) -> CmdStatus | int:
        """
        Send an AT command to the modem and wait for a response.
        :param command: The AT command to send.
        :param wait_response: If True, wait until response is received or timeout,
         else returns after AT command is sent.
        :param timeout_ms: Time to wait for a response in millisecond

        :return: "CMD_TIMEOUT" | "CMD_ERROR" | "CMD_SUCCESS" | "CMD_CONNECT"
        """

        self._chk_can_send_at_cmd()
        self.uart.write(command + '\r\n')
        if not wait_response:
            return self.CmdStatus.CMD_SUCCESS

        self._msg_length = 0  # reset msg length
        res = self.CmdStatus.CMD_TIMEOUT

        t0 = utime.ticks_ms()
        while not self.uart.any():
            if utime.ticks_diff(utime.ticks_ms(), t0) > timeout_ms:
                break
            await uasyncio.sleep_ms(0)

        if self.uart.any():
            self._msg_length = self.uart.readinto(self._recv_buf)  # read data

            for seq, status in self.END_SEQS:
                if len(seq) > self._msg_length:  # if we haven't recv a long enough msg, skip checking for end
                    continue
                elif self._buf_contains(seq):  # todo
                    res = status
                    break
        return res

    def _chk_can_send_at_cmd(self, raise_exception=True) -> bool:
        if self._in_data_mode and self._server_connected:
            if raise_exception:
                raise Exception("Cannot send at commands when connected to a server in data mode."
                                " call exit_data_mode or close the connection to enable AT commands")
            return False
        return True

    def get_resp_as_bytes(self, strip=True) -> bytes:
        """
        Read response as a byte string.
        :param strip: If strip is True leading and trailing \r\n is removed
        :return: bytes
        """
        if self._msg_length:
            if strip:
                return bytes(self.get_response_as_mv_slice()).strip(b'\r\n')
            return bytes(self.get_response_as_mv_slice())
        return b''

    def get_response_as_mv_slice(self) -> memoryview:
        """
        Returns the response slice as memoryview to avoid copies
        :return:
        """
        if self._msg_length:
            return memoryview(self._recv_buf[:self._msg_length])
        return memoryview(b'')

    async def check_network_registration(self) -> bool:
        """
        Check if modem is registered to a cellular network
        :return:
        """
        self._chk_can_send_at_cmd()
        print("Checking network registration")
        # register network
        if await self.send_at_command("AT+CREG?"):
            if b"+CREG: 0,1" in self.get_resp_as_bytes():
                print("Registered to the network.")
                return True
        print("Sim800L IS NOT registered to the network")
        return False

    async def check_assigned_ip(self, timeout_ms=6000) -> bool:
        """
        AT command to check if the modem has been assigned an IP Address by the network,
        after gprs is enabled.
        :param timeout_ms:
        :return:
        """
        self._chk_can_send_at_cmd()
        # Get the local IP address
        await self.send_at_command("AT+CIFSR", timeout_ms)
        if b"ERROR" in self.get_resp_as_bytes():
            print(self.get_resp_as_bytes())
            print("ERROR: Failed to get IP address")
            return False
        self.ip_addr = self.get_resp_as_bytes().strip()
        self.gprs_connected = True
        print(f"Connected with IP: {str(self.ip_addr)}")
        return True

    async def get_set_dns(self, dns=None) -> bool | bytes:
        """
        Get or set primary dns. if input is provided, return the current dns address
        :param dns:
        :return:
        """
        self._chk_can_send_at_cmd()
        if not self.gprs_connected:
            raise Exception("Must be connected to gprs to set dns")

        if dns:
            print(f"Setting DNS to {dns}")
            if await self.send_at_command(f'AT+CDNSCFG="{dns}"'):
                return True
        else:
            print("Getting DNS")
            await self.send_at_command(f'AT+CDNSCFG?')
            print(f'{self.get_resp_as_bytes()}')
            return True
        return False

    async def get_addr_info(self, domain: str, timeout_ms=5000) -> str:
        """

        :param domain: domain name to resolve
        :param timeout_ms: how long to wait for dns response
        :return: IP address of the domain
        """
        if not self.gprs_connected:
            raise Exception("Must be connected to gprs to get address info ")
        self._chk_can_send_at_cmd()
        # b'\r\nOK\r\n\r\n+CDNSGIP: 1,"example.com","93.184.215.14"\r\n'
        print(f"Getting IP Address info for {domain}")
        await self.send_at_command(f'AT+CDNSGIP="{domain}"', wait_response=False)

        buf = memoryview(self._recv_buf)
        self._msg_length: int = 0
        _start_found = False
        _end_found = False
        t0 = utime.ticks_ms()
        while utime.ticks_diff(utime.ticks_ms(), t0) < timeout_ms:
            if self.uart.any():
                self._msg_length += self._do_uart_read(buf, self._msg_length)
                if not _start_found and self._buf_contains(b"+CDNSGIP"):
                    _start_found = True
                if not _end_found and self._buf_contains(b'"\r\n'):
                    _end_found = True
                if _start_found and _end_found:
                    break
                t0 = utime.ticks_ms()
            await uasyncio.sleep_ms(0)

        _s = self.get_resp_as_bytes().split(b',')
        if len(_s) > 1:
            ip = _s[-1].strip().strip(b'"').strip(b'"').decode()
            if ip.count('.') == 3:
                print(f"IP ADDRESS FOR {_s[1].decode()} is {ip}")
                return ip
        print(f"Failed to get ip address for {domain}. Got response: {self.get_resp_as_bytes()}")
        return ''

    async def close_gprs_connection(self):
        """
        Closes network connection to GPRS. Modem will still be registered to network.
        :return:
        """
        if self.gprs_connected and self._chk_can_send_at_cmd():
            print("Shutting down gprs context")
            await self.send_at_command("AT+CIPSHUT")
            print("GPRS connection closed")

    async def close_tcp_connection(self):
        """
        Closes a tcp connection to the server. If in data mode,
        data mode will be exited first.
        :return:
        """
        if not self.is_connected:
            print("WARNING: No TCP connection opened")
            return
        if self._async_mode and self._reader_flag:
            self._reader_flag = False
            await uasyncio.sleep(1)
            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()

        if self._in_data_mode:
            await self.exit_data_mode()

        print("Closing TCP connection")
        await self.send_at_command("AT+CIPCLOSE")
        print(f"TCP connection closed.")

    async def shut_down_gprs(self):
        """
        This will terminate any existing TCP connection and disconnect the modem from the GPRS network.
        :return:
        """

        await self.close_tcp_connection()
        await self.close_gprs_connection()

    async def exit_data_mode(self, timeout_ms=5000) -> bool:
        """
        Exit Data mode and enter command mode.
        :param timeout_ms:
        :return:
        """
        print("Exiting data mode")
        t0 = utime.ticks_ms()
        while utime.ticks_diff(utime.ticks_ms(), t0) < timeout_ms and not self.uart.txdone():
            await uasyncio.sleep_ms(5)

        if not self.uart.txdone():
            self.uart.sendbreak()
            await uasyncio.sleep_ms(5)
            self.uart.flush()

        self.uart.write(b"+++")
        t0 = utime.ticks_ms()
        while utime.ticks_diff(utime.ticks_ms(), t0) < timeout_ms:
            if self.uart.any():
                self.uart.read()
                print(f"Data mode exited.")
                self._in_data_mode = False
                return True
            await uasyncio.sleep(0)
        self._in_data_mode = False
        print("INFO: Entering Command Mode")

    async def initialize_modem(self, tries=4) -> bool:
        """
        send AT command and verify OK response. Configures modem for full functionality if successful.
         You can try calling reset modem if initialization fails
        :param tries:
        :return:
        """
        print("Initializing Modem...")
        while tries:
            await self.send_at_command("AT")
            if self.get_resp_as_bytes() == b"OK":
                await self.send_at_command("AT+CFUN=1")
                while self.uart.any():
                    print(self.uart.readline())
                    await uasyncio.sleep_ms(1)
                print("Sim800l Modem Initialized")
                return True
            await uasyncio.sleep(tries)
            tries -= 1
        print("Failed to Initialize Sim800l Modem")
        return False

    async def connect_to_gprs(self, data_mode=False, async_mode=False):
        """
        Connects modem to gprs network and obtains IP address for the device
        :param data_mode: If enabled, modem will enter data mode after successful connection.
         This means AT commands will not work and all data written to the modem will be as considered data to be
         transmitted to server. This mode also prevents URC messages from being delivered.
         you can call 'exit_data_mode' to enter command mode.
        :param async_mode: If enabled, data arriving at the modem is read in the background into a ring buffer using an
        async task. If disabled, you will have to manually wait and read messages arriving at the uart.
        by calling "read_response"(which polls the uart until a timeout) or directly polling the uart yourself.
        if async mode is enabled, you can read available data by calling "read".
        without worrying about losing messages arriving in the modem
        "readline" methods which will return data in the ring buffer.
        Note the same response buffer is used for the ring buffer after the connection is made hence it is not
         safe to read directly into the underlying buffer, as it may corrupt incoming data.
        :return:
        """
        self._chk_can_send_at_cmd()
        if not await self.initialize_modem():
            return False
        if data_mode:
            print("Enabling data mode")
            await self.send_at_command(f'AT+CIPMODE="{1 if data_mode else 0}"')
            self._in_data_mode = True
        else:
            if async_mode:
                raise Sim800Exception("Cannot use async_mode in Command Mode. set data_mode=True to use async mode")

        if async_mode:
            self._async_mode = True

        print("Setting apn")
        await self.send_at_command(f'AT+CSTT="{self.config["SIM800_APN"]}","",""')

        print("Bring up gprs connection")
        await self.send_at_command('AT+CIICR', 85 * 1000)  # up to 85 seconds

        print("Obtaining host ip address")
        await self.check_assigned_ip()
        if self.gprs_connected:
            print("Connected to gprs")

    def _do_uart_read(self, buf, start=0, _max_read=0) -> int:
        """
        returns the number of bytes read into buffer
        :param buf:
        :param start:
        :return:
        """
        # todo limit read to max read to not overflow buffer and corrupt memoryview
        _idx = start
        while self.uart.any():
            buf[_idx:_idx + 1] = self.uart.read(1)
            _idx += 1
        return _idx - start

    def _buf_contains(self, val: bytes) -> bool:
        return val in self._recv_buf[:self._msg_length]

    async def connect_to_server(self, addr: str, port: int, timeout_ms=10000) -> bool:
        """
        Establish a tcp connection to a remote address.
        :param addr: The remote address to connect to . you may obtain the IP address by calling get_addr_info.
        however this method accepts the domain name as well. eg "example.com" or "123.124.125.126"
        :param port: the port to connect to
        :param timeout_ms: how long to wait to establish the connection
        :return:
        """
        if not self.gprs_connected:
            print('Must be connected to gprs to connect to server')
            return False
        self._chk_can_send_at_cmd()

        print(f"Connecting to {addr} on {port=}")
        await self.send_at_command(f'AT+CIPSTART="TCP","{addr}","{port}"', wait_response=False)
        self._msg_length = 0
        t0 = utime.ticks_ms()
        while utime.ticks_diff(utime.ticks_ms(), t0) < timeout_ms:
            if self.uart.any():
                self._msg_length += self._do_uart_read(self._recv_buf, self._msg_length)
                t0 = utime.ticks_ms()

            if self._buf_contains(b'CONNECT') or self._buf_contains(b'ALREADY CONNECT'):
                self._server_connected = True
                if self._async_mode:
                    self._reader_task = uasyncio.create_task(self._resp_stream_reader())

                print(f"Connected to {addr} on {port=}")
                self._msg_length += self._do_uart_read(self._recv_buf, self._msg_length)  # finish read
                return True
            elif self._buf_contains(b'FAIL') or self._buf_contains(b"+CME ERROR"):
                self._msg_length += self._do_uart_read(self._recv_buf, self._msg_length)  # finish read
                break
            await uasyncio.sleep_ms(0)
        print(f"Could not connect to {addr} on {port=}")

    async def _resp_stream_reader(self):
        """
        In async mode, this task waits on the uart for messages and writes the messages into
        an output ring buf.
        :return:
        """
        if not self.is_connected:
            print("Must be connected to server in data_mode and sync mode to start stream reader")

        print("Starting data stream reader")
        _len_buf = len(self._recv_buf)
        for i in range(_len_buf):  # clear buf
            self._recv_buf[i] = 0
        self._reader_flag = True
        self._in_data_mode = True

        while self._reader_flag:
            if self.uart.any():
                avail = _len_buf - self._ring_buf.any()  # check if buff is full
                if avail:
                    self._ring_buf.write(self.uart.read(avail))
            await uasyncio.sleep_ms(0)
        print("Stream reader stopped")
        self._ring_buf = None

    async def send_data_manually(self, data: bytes, timeout_ms=5000) -> bool:
        """
        If not in data mode, you can use this function to send data over the tcp connection manually.
        :param data: The data to send over the TCP connection
        :param timeout_ms:
        :return:
        """

        if self._in_data_mode:
            raise Exception("Cannot call send data manually in data mode."
                            " you can use the 'write' method to send data through the connection"
                            " or exit data mode first")
        if not self.is_connected:
            print("ERROR: Must be connected to a server before sending data ")
            return False
        await self.send_at_command("AT+CIPSEND", timeout_ms, wait_response=False)

        t0 = utime.ticks_ms()
        self._msg_length = 0
        msg_sent = False
        while utime.ticks_diff(utime.ticks_ms(), t0) < timeout_ms:
            if self.uart.any():
                self._msg_length = self._do_uart_read(self._recv_buf, self._msg_length)
                if self._buf_contains(b">"):
                    self.uart.write(data)
                    self.uart.write(b"\x1a")
                    msg_sent = True
                    break
            await uasyncio.sleep_ms(0)

        if not msg_sent:
            print("Send Failed. Could not initiate send")
            return False

        t0 = utime.ticks_ms()
        self._msg_length = 0
        while utime.ticks_diff(utime.ticks_ms(), t0) < timeout_ms:
            if self.uart.any():
                self._msg_length = self._do_uart_read(self._recv_buf, self._msg_length)
                if self._buf_contains(b'SEND OK\r\n'):
                    print("Data sent successfully")
                    return True
                elif self._buf_contains(b'SEND FAIL\r\n'):
                    break
                elif self._buf_contains(b"+CME ERROR"):
                    self._msg_length = self._do_uart_read(self._recv_buf, self._msg_length)
                    break
            await uasyncio.sleep_ms(0)
        print(f"ERROR: Failed to send data {data}")
        return False

    async def read_response_manually(self, as_memory_view=False, timeout_ms=5000,
                                     extend_msg=False) -> bytes | memoryview:
        """
        If not in data mode, use this convenience method to read response from uart.
        This does not mean the entire result will be read within the timeout as the remote
        server can send data at any time.

        NOTE: Each call to read response will reset the internal buffer. set extend_msg to true to prevent this.
        you'll need to store the data already read elsewhere before calling read_response manually

        NOTE: THIS METHOD DOES NOT ENABLE MANUAL READ MODE ON THE MODEM AS T
        HAT OPTION IS NOT AVAILABLE FOR TCP CONNECTIONS

        :param as_memory_view: return a memory view of the internal buffer with the response
        :param timeout_ms: how long to wait until
        :param extend_msg: If extend message is true.
        the incoming data will be read into buffer without resetting the buffer .
        :return:
        """
        if self._in_data_mode:
            raise Exception("Cannot call read_response_manually in data mode."
                            " you can use the 'read' method to read data, in data mode"
                            " or exit data mode first")
        if not self.is_connected:
            raise Exception(
                "ERROR: Must be connected to a server without data mode  before calling read_response_manually ")

        if not extend_msg:
            self._msg_length = 0
        t0 = utime.ticks_ms()
        while True:
            if self.uart.any():  # TODO limit read . check buf size first.
                self._msg_length = self._do_uart_read(self._recv_buf, self._msg_length)
                t0 = utime.ticks_ms()
            if utime.ticks_diff(utime.ticks_ms(), t0) > timeout_ms:
                break
        return self.get_response_as_mv_slice() if as_memory_view else bytes(self.get_response_as_mv_slice())

    async def read_response_manually_into(self, buf, timeout_ms=5000) -> int:
        """
        If not in data mode, use this convenience method to read response from uart.
        This does not mean the entire result will be read within the timeout as the remote
        server can send data at any time.

        :param buf: buffer to hold data
        :param timeout_ms:
        :return: Total number of bytes read
        """
        if self._in_data_mode:
            raise Sim800Exception("Cannot call read_response_manually in data mode."
                                  " you can use the 'read' method to read data, in data mode"
                                  " or exit data mode first")
        if not self.is_connected:
            raise Sim800Exception(
                "ERROR: Must be connected to a server without data mode  before calling read_response_manually ")

        idx = 0
        t0 = utime.ticks_ms()
        while True:
            if self.uart.any():  # TODO limit read . check buf size first.
                idx += self._do_uart_read(buf, idx)
                t0 = utime.ticks_ms()
            if utime.ticks_diff(utime.ticks_ms(), t0) > timeout_ms:
                break
        return idx

    async def get_conn_status(self):  # todo
        print("Getting connection status")
        await self.send_at_command("AT+CIPSTATUS")
        print(self.get_resp_as_bytes())

    async def query_transmit_progress(self):  # todo
        print("Getting transmit progress")
        await self.send_at_command("AT+CIPACK")
        print(self.get_resp_as_bytes())

    # ======================================    SOCKET METHODS  =========================================== #
    def write(self, buf: bytes, timeout_ms=5000) -> int:
        if not self.is_connected:
            raise Sim800Exception("Must be connected to server before calling write")
        if not self.in_data_mode:
            raise Sim800Exception("You must set data_mode=True when connecting to gprs to use the socket api")

        return self.uart.write(buf)

    async def a_write(self, buf: bytes, *_args) -> int:
        if not self.is_connected:
            raise Sim800Exception("Must be connected to server before calling write")

        return self.uart.write(buf)

    async def a_read(self, nbytes=0, *_args, **_kwargs) -> bytes:
        if not self.is_connected:
            raise Sim800Exception("Must be connected to server before calling read")
        if not self.in_data_mode:
            raise Sim800Exception("You must set data_mode=True when connecting to gprs to use the socket api")

        avail = self._ring_buf.any if self._async_mode else self.uart.any
        _read = self._ring_buf.read if self._async_mode else self.uart.read
        if self._set_blocking or nbytes:
            while 1:
                if avail():
                    if not nbytes or avail() >= nbytes:
                        break
                await uasyncio.sleep_ms(0)
        return _read(nbytes) if nbytes else _read()

    def read(self, nbytes=0, *args, **kwargs) -> bytes | None:

        loop = uasyncio.get_event_loop()
        return loop.run_until_complete(self.a_read(nbytes, *args, **kwargs))

    async def a_readline(self, *args):

        if not self.in_data_mode:
            raise Sim800Exception("You must set data_mode=True when connecting to gprs to use the socket api")

        if not self.is_connected:
            raise Sim800Exception("Must be connected to server before calling read")

        avail = self._ring_buf.any if self._async_mode else self.uart.any
        _readline = self._ring_buf.readline if self._async_mode else self.uart.readline
        if self._set_blocking:
            while not avail():
                await uasyncio.sleep_ms(0)
        return _readline(*args)

    def readline(self, *args) -> bytes | None:
        loop = uasyncio.get_event_loop()
        return loop.run_until_complete(self.a_readline(*args))

    async def a_readinto(self, buf, *args):
        if not self.is_connected:
            raise Sim800Exception("Must be connected to server before calling read")

        if not self.in_data_mode:
            raise Sim800Exception("You must set data_mode=True when connecting to gprs to use the socket api")

        avail = self._ring_buf.any if self._async_mode else self.uart.any
        _readinto = self._ring_buf.readinto if self._async_mode else self.uart.readinto
        if self._set_blocking:
            while not avail():
                await uasyncio.sleep_ms(0)

        return _readinto(buf, *args)

    def readinto(self, buf, *args) -> None:
        loop = uasyncio.get_event_loop()
        return loop.run_until_complete(self.a_readinto(buf, *args))

    def any(self):
        if not self.is_connected:
            raise Sim800Exception("Must be connected to server before any")
        return self._ring_buf.any() if self._async_mode else self.uart.any()

    def connect(self, addr: tuple) -> bool | None:
        """
        connect to server on the specified address and port
        :param addr: Tuple with (ADDRESS, PORT)
        :return: True on connect
        """
        loop = uasyncio.get_event_loop()
        return loop.run_until_complete(self.connect_to_server(addr[0], addr[1]))

    async def a_connect(self, addr: tuple):
        """
        connect to server on the specified address and port
        :param addr: Tuple with (ADDRESS, PORT)
        :return: True on connect
        """
        await self.connect_to_server(addr[0], addr[1])

    async def a_close(self, force=False):
        if not self.gprs_connected and not force:
            print("Not connected")
            return
        if self._async_mode:
            self._reader_flag = False
            await uasyncio.sleep(1)
            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()
        if self._in_data_mode:
            await self.exit_data_mode()
        await self.shut_down_gprs()

    def close(self):
        loop = uasyncio.get_event_loop()
        return loop.run_until_complete(self.a_close())

    def setblocking(self, val):
        self._set_blocking = val

    @classmethod
    def getaddrinfo(cls, server: str, port: int) -> list | None:
        """
        Get
        :param server:
        :param port:
        :return:
        """
        if not cls._instance or not cls._instance.gprs_connected:
            print("Gprs service must be activated first")
            return
        loop = uasyncio.get_event_loop()
        # noinspection PyNoneFunctionAssignment
        addr = loop.run_until_complete(cls._instance.get_addr_info(server))
        if addr:
            return [('', (addr, port))]

    @classmethod
    async def a_getaddrinfo(cls, server: str, port: int) -> list | None:
        if not cls._instance or not cls._instance.gprs_connected:
            print("Gprs service must be activated first")
            return
        addr = await cls._instance.get_addr_info(server)
        if addr:
            return [('', (addr, port))]

    @property
    def in_data_mode(self):
        return self._in_data_mode

    @classmethod
    def socket(cls):
        """
        return a socket based on the modem
        :return:
        """
        if cls._instance:
            if not cls._instance.gprs_connected:
                raise Sim800Exception("You must be connected to GPRS to use the socket API.")
            if not cls._instance.in_data_mode:
                raise Sim800Exception("You must set data_mode=True when connecting to gprs to use the socket api")
            return cls._instance
        print("Gprs service must be activated first")
        raise Sim800Exception("an instance of Sim800 should be created before calling socket.")


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
