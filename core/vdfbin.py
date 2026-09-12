"""Binary KeyValues (VDF) reader/writer.

Only the subset used by Steam's shortcuts.vdf is implemented, but the codec is
lossless: load() followed by dumps() reproduces the input byte for byte (this
is enforced by a test).  Key order is preserved because Python dicts keep
insertion order.
"""

import struct

TYPE_NESTED = 0x00
TYPE_STRING = 0x01
TYPE_INT32 = 0x02
TYPE_UINT64 = 0x07
TYPE_END = 0x08
TYPE_END_ALT = 0x09


class UInt64(int):
    """Marker type so a 64-bit unsigned int round-trips as type 0x07."""


def loads(data: bytes):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("loads() needs bytes")
    obj, pos = _read_map(data, 0)
    if pos != len(data):
        raise ValueError(f"trailing bytes after root map: {len(data) - pos}")
    return obj


def load(path):
    with open(path, "rb") as fh:
        return loads(fh.read())


def _read_cstring(data, pos):
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", "surrogateescape"), end + 1


def _read_map(data, pos):
    result = {}
    while True:
        if pos >= len(data):
            raise ValueError("unexpected end of data inside map")
        type_byte = data[pos]
        pos += 1
        if type_byte in (TYPE_END, TYPE_END_ALT):
            return result, pos
        key, pos = _read_cstring(data, pos)
        if type_byte == TYPE_NESTED:
            value, pos = _read_map(data, pos)
        elif type_byte == TYPE_STRING:
            value, pos = _read_cstring(data, pos)
        elif type_byte == TYPE_INT32:
            value = struct.unpack_from("<i", data, pos)[0]
            pos += 4
        elif type_byte == TYPE_UINT64:
            value = UInt64(struct.unpack_from("<Q", data, pos)[0])
            pos += 8
        else:
            raise ValueError(f"unsupported VDF type 0x{type_byte:02x} at offset {pos - 1}")
        result[key] = value


def dumps(obj) -> bytes:
    return _write_map(obj, None)


def dump(obj, path):
    payload = dumps(obj)
    with open(path, "wb") as fh:
        fh.write(payload)
    return len(payload)


def _write_cstring(buf: bytearray, value: str):
    buf += value.encode("utf-8", "surrogateescape")
    buf += b"\x00"


def _write_map(obj, key) -> bytes:
    buf = bytearray()
    if key is not None:
        buf += bytes([TYPE_NESTED])
        _write_cstring(buf, key)
    for k, v in obj.items():
        if isinstance(v, dict):
            buf += _write_map(v, k)
        elif isinstance(v, str):
            buf += bytes([TYPE_STRING])
            _write_cstring(buf, k)
            _write_cstring(buf, v)
        elif isinstance(v, UInt64):
            buf += bytes([TYPE_UINT64])
            _write_cstring(buf, k)
            buf += struct.pack("<Q", int(v))
        elif isinstance(v, bool):
            buf += bytes([TYPE_INT32])
            _write_cstring(buf, k)
            buf += struct.pack("<i", int(v))
        elif isinstance(v, int):
            buf += bytes([TYPE_INT32])
            _write_cstring(buf, k)
            buf += struct.pack("<i", v)
        else:
            raise TypeError(f"cannot serialize {type(v).__name__} for key {k!r}")
    buf += bytes([TYPE_END])
    return bytes(buf)
