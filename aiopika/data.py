# Copyright
#  (c) 2009-2019, Tony Garnock-Jones, Gavin M. Roy, Pivotal Software, Inc and others.
# All rights reserved.

# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:

#  * Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#  * Neither the name of the Pika project nor the names of its contributors may be used
#    to endorse or promote products derived from this software without specific
#    prior written permission.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


"""AMQP Table Encoding/Decoding"""
import struct
import decimal
import calendar
import warnings

from datetime import datetime

from . import exceptions


def encode_short_string(pieces, value):
    """Encode a string value as short string and append it to pieces list
    returning the size of the encoded value.

    :param list pieces: Already encoded values
    :param str value: String value to encode
    :rtype: int

    """
    if isinstance(value, str):
        value = value.encode('UTF-8')
    encoded_value = value
    length = len(encoded_value)

    # 4.2.5.3
    # Short strings, stored as an 8-bit unsigned integer length followed by zero
    # or more octets of data. Short strings can carry up to 255 octets of UTF-8
    # data, but may not contain binary zero octets.
    # ...
    # 4.2.5.5
    # The server SHOULD validate field names and upon receiving an invalid field
    # name, it SHOULD signal a connection exception with reply code 503 (syntax
    # error).
    # -> validate length (avoid truncated utf-8 / corrupted data), but skip null
    # byte check.
    if length > 255:
        raise exceptions.ShortStringTooLong(encoded_value)

    pieces.append(struct.pack('B', length))
    pieces.append(encoded_value)
    return 1 + length


def decode_short_string(encoded, offset):
    """Decode a short string value from ``encoded`` data at ``offset``.
    """
    length = struct.unpack_from('B', encoded, offset)[0]
    offset += 1
    value = encoded[offset:offset + length]
    try:
        value = value.decode('utf8')
    except UnicodeDecodeError:
        pass
    offset += length
    return value, offset


def encode_table(pieces, table):
    """Encode a dict as an AMQP table appending the encded table to the
    pieces list passed in.

    :param list pieces: Already encoded frame pieces
    :param dict table: The dict to encode
    :rtype: int

    """
    table = table or {}
    length_index = len(pieces)
    pieces.append(None)  # placeholder
    tablesize = 0
    for (key, value) in table.items():
        tablesize += encode_short_string(pieces, key)
        tablesize += encode_value(pieces, value)

    pieces[length_index] = struct.pack('>I', tablesize)
    return tablesize + 4


def encode_value(pieces, value): # pylint: disable=R0911
    """Encode the value passed in and append it to the pieces list returning
    the the size of the encoded value.

    :param list pieces: Already encoded values
    :param any value: The value to encode
    :rtype: int

    """
    if isinstance(value, str):
        value = value.encode('utf-8')
        pieces.append(struct.pack('>cI', b'S', len(value)))
        pieces.append(value)
        return 5 + len(value)

    if isinstance(value, bytes):
        pieces.append(struct.pack('>cI', b'x', len(value)))
        pieces.append(value)
        return 5 + len(value)

    if isinstance(value, bool):
        pieces.append(struct.pack('>cB', b't', int(value)))
        return 2
    if isinstance(value, int):
        if -(2 ** 64 / 2) < value < (2 ** 64 / 2) - 1:
            pieces.append(struct.pack('>cq', b'l', value))
            return 9
        else:
            with warnings.catch_warnings():
                warnings.filterwarnings('error')
                try:
                    packed = struct.pack('>ci', b'I', value)
                    pieces.append(packed)
                    return 5
                except (struct.error, DeprecationWarning):
                    packed = struct.pack('>cq', b'l', int(value))
                    pieces.append(packed)
                    return 9
    elif isinstance(value, decimal.Decimal):
        value = value.normalize()
        if value.as_tuple().exponent < 0:
            decimals = -value.as_tuple().exponent
            raw = int(value * (decimal.Decimal(10)**decimals))
            pieces.append(struct.pack('>cBi', b'D', decimals, raw))
        else:
            # per spec, the "decimals" octet is unsigned (!)
            pieces.append(struct.pack('>cBi', b'D', 0, int(value)))
        return 6
    elif isinstance(value, datetime):
        pieces.append(
            struct.pack('>cQ', b'T', calendar.timegm(value.utctimetuple())))
        return 9
    elif isinstance(value, dict):
        pieces.append(struct.pack('>c', b'F'))
        return 1 + encode_table(pieces, value)
    elif isinstance(value, list):
        list_pieces = []
        for val in value:
            encode_value(list_pieces, val)
        piece = b''.join(list_pieces)
        pieces.append(struct.pack('>cI', b'A', len(piece)))
        pieces.append(piece)
        return 5 + len(piece)
    elif value is None:
        pieces.append(struct.pack('>c', b'V'))
        return 1
    else:
        raise exceptions.UnsupportedAMQPFieldException(pieces, value)


def decode_table(encoded, offset):
    """Decode the AMQP table passed in from the encoded value returning the
    decoded result and the number of bytes read plus the offset.

    :param str encoded: The binary encoded data to decode
    :param int offset: The starting byte offset
    :rtype: tuple

    """
    result = {}
    tablesize = struct.unpack_from('>I', encoded, offset)[0]
    offset += 4
    limit = offset + tablesize
    while offset < limit:
        key, offset = decode_short_string(encoded, offset)
        value, offset = decode_value(encoded, offset)
        result[key] = value
    return result, offset


def decode_value(encoded, offset): # pylint: disable=R0912,R0915
    """Decode the value passed in returning the decoded value and the number
    of bytes read in addition to the starting offset.

    :param str encoded: The binary encoded data to decode
    :param int offset: The starting byte offset
    :rtype: tuple
    :raises: pika.exceptions.InvalidFieldTypeException

    """
    # slice to get bytes in Python 3 and str in Python 2
    kind = encoded[offset:offset + 1]
    offset += 1

    # Bool
    if kind == b't':
        value = struct.unpack_from('>B', encoded, offset)[0]
        value = bool(value)
        offset += 1

    # Short-Short Int
    elif kind == b'b':
        value = struct.unpack_from('>B', encoded, offset)[0]
        offset += 1

    # Short-Short Unsigned Int
    elif kind == b'B':
        value = struct.unpack_from('>b', encoded, offset)[0]
        offset += 1

    # Short Int
    elif kind == b'U':
        value = struct.unpack_from('>h', encoded, offset)[0]
        offset += 2

    # Short Unsigned Int
    elif kind == b'u':
        value = struct.unpack_from('>H', encoded, offset)[0]
        offset += 2

    # Long Int
    elif kind == b'I':
        value = struct.unpack_from('>i', encoded, offset)[0]
        offset += 4

    # Long Unsigned Int
    elif kind == b'i':
        value = struct.unpack_from('>I', encoded, offset)[0]
        offset += 4

    # Long-Long Int
    elif kind == b'L':
        value = int(struct.unpack_from('>q', encoded, offset)[0])
        offset += 8

    # Long-Long Unsigned Int
    elif kind == b'l':
        value = int(struct.unpack_from('>Q', encoded, offset)[0])
        offset += 8

    # Float
    elif kind == b'f':
        value = int(struct.unpack_from('>f', encoded, offset)[0])
        offset += 4

    # Double
    elif kind == b'd':
        value = int(struct.unpack_from('>d', encoded, offset)[0])
        offset += 8

    # Decimal
    elif kind == b'D':
        decimals = struct.unpack_from('B', encoded, offset)[0]
        offset += 1
        raw = struct.unpack_from('>i', encoded, offset)[0]
        offset += 4
        value = decimal.Decimal(raw) * (decimal.Decimal(10)**-decimals)

    # https://github.com/pika/pika/issues/1205
    # Short Signed Int
    elif kind == b's':
        value = struct.unpack_from('>h', encoded, offset)[0]
        offset += 2

    # Long String
    elif kind == b'S':
        length = struct.unpack_from('>I', encoded, offset)[0]
        offset += 4
        value = encoded[offset:offset + length]
        try:
            value = value.decode('utf8')
        except UnicodeDecodeError:
            pass
        offset += length

    elif kind == b'x':
        length = struct.unpack_from('>I', encoded, offset)[0]
        offset += 4
        value = encoded[offset:offset + length]
        offset += length

    # Field Array
    elif kind == b'A':
        length = struct.unpack_from('>I', encoded, offset)[0]
        offset += 4
        offset_end = offset + length
        value = []
        while offset < offset_end:
            val, offset = decode_value(encoded, offset)
            value.append(val)

    # Timestamp
    elif kind == b'T':
        value = datetime.utcfromtimestamp(
            struct.unpack_from('>Q', encoded, offset)[0])
        offset += 8

    # Field Table
    elif kind == b'F':
        (value, offset) = decode_table(encoded, offset)

    # Null / Void
    elif kind == b'V':
        value = None
    else:
        raise exceptions.InvalidFieldTypeException(kind)

    return value, offset
