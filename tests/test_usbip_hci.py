import asyncio
import unittest

from usbip_hci import SETUP, URB, UsbIpHci


class UsbIpTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_bulk_and_fragmented_interrupt_reply(self):
        finished = asyncio.get_running_loop().create_future()
        requests = []
        data_in = bytes.fromhex('0e0401030c00')

        async def controller(reader, writer):
            try:
                for _ in range(3):
                    request = URB.unpack(await reader.readexactly(URB.size))
                    _, sequence, _, direction, endpoint, _, length, _, _, _, setup = request
                    body = b'' if direction else await reader.readexactly(length)
                    requests.append((endpoint, direction, setup, body))
                    payload = data_in if direction else b''
                    actual_length = len(payload) if direction else len(body)
                    reply = URB.pack(3, sequence, 0, 0, 0, 0, actual_length, 0, -1, 0, bytes(8))
                    writer.write(reply[:21])
                    await writer.drain()
                    await asyncio.sleep(0)
                    writer.write(reply[21:] + payload)
                    await writer.drain()
                finished.set_result(None)
                await reader.read()
            except Exception as error:
                if not finished.done():
                    finished.set_exception(error)
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(controller, '127.0.0.1', 0) as server:
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            async with UsbIpHci(reader, writer, 0x1000a) as transport:
                transport.start(transport.receive())
                reset = bytes.fromhex('030c00')
                acl = bytes.fromhex('01200000')
                setup = SETUP.pack(0x20, 0, 0, 0, len(reset))
                result = await asyncio.wait_for(asyncio.gather(
                    transport.submit(0, data=reset, setup=setup),
                    transport.submit(2, data=acl),
                    transport.submit(1, read_length=260),
                ), 3)
                await asyncio.wait_for(finished, 1)
        self.assertEqual(result, [b'', b'', data_in])
        self.assertEqual(requests, [(0, 0, setup, reset), (2, 0, bytes(8), acl), (1, 1, bytes(8), b'')])
