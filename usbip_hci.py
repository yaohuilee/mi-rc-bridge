"""Access the Intel Bluetooth controller through the local Windows USB/IP host."""

import asyncio
from contextlib import suppress
from itertools import count
import socket
import struct

from bumble.transport.common import BaseSource, Transport
from bumble.transport.usb import AclPacketSplitter, EventPacketSplitter

USBIP_HEADER = struct.Struct('!HHI')
URB = struct.Struct('!IIIIIiiiii8s')
DEVICE = struct.Struct('!256s32sIIIHHHBBBBBB')
SETUP = struct.Struct('<BBHHH')
USBIP_VERSION = 0x0111


class HciSource(BaseSource):
    def receive(self, packet_type, packet):
        if self.sink:
            self.sink.on_packet(bytes((packet_type,)) + packet)


class UsbIpHci(Transport):
    def __init__(self, reader, writer, device_id):
        super().__init__(HciSource(), self)
        self.reader = reader
        self.writer = writer
        self.device_id = device_id
        self.sequence = count(1)
        self.pending = {}
        self.outgoing = asyncio.Queue()
        self.tasks = []
        self.error = None

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        task.add_done_callback(self.check_task)
        self.tasks.append(task)

    def check_task(self, task):
        if not task.cancelled() and (error := task.exception()) is not None and self.error is None:
            self.error = error
            for _, _, future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.source.on_transport_lost()

    async def submit(self, endpoint, *, read_length=0, data=b'', setup=bytes(8)):
        if self.error:
            raise self.error
        direction = int(read_length != 0)
        length = read_length or len(data)
        sequence = next(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.pending[sequence] = direction, length, future
        self.writer.write(URB.pack(1, sequence, self.device_id, direction, endpoint,
                                   0, length, 0, -1, 0, setup) + data)
        return await future

    async def receive(self):
        while True:
            fields = URB.unpack(await self.reader.readexactly(URB.size))
            command, sequence, _, _, _, status, actual_length, _, packets, _, _ = fields
            if command != 3 or sequence not in self.pending:
                raise OSError('Unexpected USB/IP reply.')
            direction, length, future = self.pending[sequence]
            if not 0 <= actual_length <= length or packets not in (-1, 0):
                raise OSError('Invalid USB/IP transfer length.')
            data = await self.reader.readexactly(actual_length) if direction else b''
            del self.pending[sequence]
            if not future.done():
                if status:
                    future.set_exception(OSError(f'USB/IP transfer failed ({status}).'))
                else:
                    future.set_result(data)

    async def receive_endpoint(self, endpoint, length, splitter):
        while True:
            splitter.feed(await self.submit(endpoint, read_length=length))

    def on_packet(self, packet):
        self.outgoing.put_nowait(packet)

    async def send(self):
        while True:
            packet = await self.outgoing.get()
            kind, data = packet[0], packet[1:]
            if kind == 1:
                await self.submit(0, data=data, setup=SETUP.pack(0x20, 0, 0, 0, len(data)))
            elif kind == 2:
                await self.submit(2, data=data)
            else:
                raise ValueError(f'Unexpected HCI packet type {kind}.')

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for _, _, future in self.pending.values():
            future.cancel()
        self.pending.clear()
        self.writer.close()
        with suppress(OSError):
            await self.writer.wait_closed()


async def open_usbip_hci(bus_id):
    reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', 3240), 5)
    transport = None
    try:
        writer.get_extra_info('socket').setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        writer.write(USBIP_HEADER.pack(USBIP_VERSION, 0x8003, 0) + struct.pack('32s', bus_id.encode('ascii')))
        version, command, status = USBIP_HEADER.unpack(
            await asyncio.wait_for(reader.readexactly(USBIP_HEADER.size), 15)
        )
        if (version, command, status) != (USBIP_VERSION, 3, 0):
            raise OSError(f'Local USB/IP server rejected Bluetooth attachment ({status}).')
        _, reported_bus, bus, address, _, vid, pid, _, _, _, _, config, _, _ = DEVICE.unpack(
            await asyncio.wait_for(reader.readexactly(DEVICE.size), 5)
        )
        if reported_bus.rstrip(b'\0').decode('ascii') != bus_id or (vid, pid) != (0x8087, 0x0026):
            raise OSError('USB/IP returned an unexpected controller.')
        transport = UsbIpHci(reader, writer, (bus << 16) | address)
        transport.start(transport.receive())
        await asyncio.wait_for(transport.submit(0, setup=SETUP.pack(0, 9, config or 1, 0, 0)), 5)
        events = EventPacketSplitter(lambda packet: transport.source.receive(4, packet))
        acl = AclPacketSplitter(lambda packet: transport.source.receive(2, packet))
        # Intel 8087:0026 exposes events on 0x81 and ACL on 0x82/0x02.
        transport.start(transport.receive_endpoint(1, 260, events))
        transport.start(transport.receive_endpoint(2, 4096, acl))
        transport.start(transport.send())
        return transport
    except BaseException:
        if transport:
            await transport.close()
        else:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        raise
