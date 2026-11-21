"""Buttons from the Xiaomi remote's keyboard input report."""

from enum import IntFlag
import struct


class Key(IntFlag):
    CONFIRM = 1
    BACK = 2
    UP = 4
    DOWN = 8


def decode_keys(report):
    if len(report) != 8:
        return None
    usages = struct.unpack_from('<3H', report, 2)
    keys = Key(0)
    for code, key in ((0x28, Key.CONFIRM), (0x65, Key.BACK), (0x52, Key.UP), (0x51, Key.DOWN)):
        if code in usages:
            keys |= key
    return keys
