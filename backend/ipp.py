"""A small IPP client for talking to CUPS (RFC 8010/8011 plus CUPS operations).

The CUPS command-line tools cannot be given an administrator password
without a terminal, so Folio speaks IPP over HTTP itself for administration
and job management. Only what Folio uses is implemented, with no
third-party dependency.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import itertools
import ssl
import struct
from dataclasses import dataclass, field
from typing import Any

# Operations
CANCEL_JOB = 0x0008
GET_JOB_ATTRIBUTES = 0x0009
GET_JOBS = 0x000A
GET_PRINTER_ATTRIBUTES = 0x000B
PAUSE_PRINTER = 0x0010
RESUME_PRINTER = 0x0011
CUPS_GET_PRINTERS = 0x4002
CUPS_ADD_MODIFY_PRINTER = 0x4003
CUPS_DELETE_PRINTER = 0x4004
CUPS_ACCEPT_JOBS = 0x4008
CUPS_GET_DEVICES = 0x400B
CUPS_GET_PPDS = 0x400C
CUPS_GET_PPD = 0x400F

# Delimiter tags (attribute groups)
OPERATION, JOB, END, PRINTER = 0x01, 0x02, 0x03, 0x04
# Value tags
INTEGER, BOOLEAN, ENUM = 0x21, 0x22, 0x23
DATETIME, RESOLUTION, RANGE = 0x31, 0x32, 0x33
BEGIN_COLLECTION, TEXT_LANG, NAME_LANG, END_COLLECTION = 0x34, 0x35, 0x36, 0x37
TEXT, NAME, KEYWORD, URI, URI_SCHEME, CHARSET, LANGUAGE, MIME_TYPE, MEMBER_NAME = (
    0x41, 0x42, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A)
OUT_OF_BAND = range(0x10, 0x20)

JOB_STATES = {3: 'pending', 4: 'held', 5: 'processing', 6: 'stopped',
              7: 'cancelled', 8: 'aborted', 9: 'completed'}
PRINTER_STATES = {3: 'idle', 4: 'processing', 5: 'stopped'}
_request_ids = itertools.count(1)


class IPPError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthRequired(IPPError):
    """The server wants a (different) administrator login."""


class EncryptionRequired(IPPError):
    """The server only accepts this request over TLS."""


class CertificateChanged(IPPError):
    """The server's TLS certificate no longer matches the one first trusted."""


# --- Encoding ----------------------------------------------------------------

def _value(tag: int, value: Any) -> bytes:
    if tag in (INTEGER, ENUM):
        return struct.pack('>i', int(value))
    if tag == BOOLEAN:
        return b'\x01' if value else b'\x00'
    if tag in OUT_OF_BAND:
        return b''
    return str(value).encode('utf-8')


def encode(operation: int, groups: list[tuple[int, list[tuple[int, str, Any]]]],
           request_id: int | None = None) -> bytes:
    """Encode a request. Each attribute is (value-tag, name, value-or-list)."""
    out = bytearray(struct.pack('>BBHI', 1, 1, operation, request_id or next(_request_ids)))
    for group_tag, attributes in groups:
        out.append(group_tag)
        for tag, name, values in attributes:
            values = values if isinstance(values, (list, tuple)) else [values]
            for index, value in enumerate(values):
                raw = _value(tag, value)
                label = name.encode() if index == 0 else b''  # Extra values have no name
                out += struct.pack('>BH', tag, len(label)) + label + struct.pack('>H', len(raw)) + raw
    out.append(END)
    return bytes(out)


def operation_attributes(*extra: tuple[int, str, Any]) -> tuple[int, list]:
    return OPERATION, [(CHARSET, 'attributes-charset', 'utf-8'),
                       (LANGUAGE, 'attributes-natural-language', 'en'), *extra]


# --- Decoding ----------------------------------------------------------------

@dataclass
class Response:
    status: int
    request_id: int
    groups: list[tuple[int, dict[str, list]]] = field(default_factory=list)
    data: bytes = b''  # Document data after the attributes (e.g. a PPD file)

    @property
    def ok(self) -> bool:
        return self.status < 0x0100

    def group(self, tag: int) -> dict[str, list]:
        return next((attrs for t, attrs in self.groups if t == tag), {})

    def all(self, tag: int) -> list[dict[str, list]]:
        return [attrs for t, attrs in self.groups if t == tag]

    @property
    def message(self) -> str:
        values = self.group(OPERATION).get('status-message') or ['']
        return str(values[0] or '')


def _decode_value(tag: int, raw: bytes) -> Any:
    if tag in (INTEGER, ENUM) and len(raw) == 4:
        return struct.unpack('>i', raw)[0]
    if tag == BOOLEAN:
        return bool(raw and raw[0])
    if tag in OUT_OF_BAND:
        return None
    if tag == RANGE and len(raw) == 8:
        return struct.unpack('>ii', raw)
    if tag == RESOLUTION and len(raw) == 9:
        return struct.unpack('>iib', raw)
    if tag == DATETIME:
        return raw
    if tag in (TEXT_LANG, NAME_LANG) and len(raw) >= 4:
        skip = struct.unpack_from('>H', raw, 0)[0]
        size = struct.unpack_from('>H', raw, 2 + skip)[0]
        return raw[4 + skip:4 + skip + size].decode('utf-8', 'replace')
    if 0x30 <= tag <= 0x3F:
        return raw  # Other binary types are not used by Folio
    return raw.decode('utf-8', 'replace')


def _read_attribute(data: bytes, pos: int) -> tuple[int, str, bytes, int]:
    tag = data[pos]
    name_len = struct.unpack_from('>H', data, pos + 1)[0]
    name = data[pos + 3:pos + 3 + name_len].decode('utf-8', 'replace')
    pos += 3 + name_len
    value_len = struct.unpack_from('>H', data, pos)[0]
    return tag, name, data[pos + 2:pos + 2 + value_len], pos + 2 + value_len


def _read_collection(data: bytes, pos: int) -> tuple[dict, int]:
    members: dict[str, list] = {}
    key = None
    while True:
        tag, _, raw, pos = _read_attribute(data, pos)
        if tag == END_COLLECTION:
            return members, pos
        if tag == MEMBER_NAME:
            key = raw.decode('utf-8', 'replace')
            members.setdefault(key, [])
            continue
        value, pos = _read_collection(data, pos) if tag == BEGIN_COLLECTION else (_decode_value(tag, raw), pos)
        if key is not None:
            members[key].append(value)


def decode(data: bytes) -> Response:
    if len(data) < 9:
        raise IPPError('Truncated IPP response from CUPS')
    _, _, status, request_id = struct.unpack_from('>BBHI', data, 0)
    response = Response(status, request_id)
    current: dict[str, list] | None = None
    name = None
    pos = 8
    try:
        while pos < len(data):
            tag = data[pos]
            if tag == END:
                response.data = data[pos + 1:]
                break
            if tag < 0x10:  # A new attribute group
                current = {}
                response.groups.append((tag, current))
                pos += 1
                continue
            tag, label, raw, pos = _read_attribute(data, pos)
            value, pos = _read_collection(data, pos) if tag == BEGIN_COLLECTION else (_decode_value(tag, raw), pos)
            if label:
                name = label
                if current is not None:
                    current.setdefault(name, [])
            if current is not None and name is not None:
                current[name].append(value)
    except (IndexError, struct.error) as exc:
        raise IPPError('Malformed IPP response from CUPS') from exc
    return response


# --- Transport ---------------------------------------------------------------

def split_server(server: str) -> tuple[str, int]:
    host, _, port = server.rpartition(':') if ':' in server else (server, '', '631')
    return host, int(port or 631)


@dataclass
class Connection:
    """One CUPS server. ``tls`` and ``pin`` (SHA-256 of its certificate) are
    learned on first contact; a later certificate change is refused."""
    server: str
    username: str | None = None
    password: str | None = None
    tls: bool = False
    pin: str | None = None
    timeout: float = 15

    def printer_uri(self, queue: str | None = None) -> str:
        host, port = split_server(self.server)
        return f'ipp://{host}:{port}/printers/{queue}' if queue else f'ipp://{host}:{port}/'

    def job_uri(self, job_id: int) -> str:
        host, port = split_server(self.server)
        return f'ipp://{host}:{port}/jobs/{int(job_id)}'

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        host, port = split_server(self.server)
        if not self.tls:
            return http.client.HTTPConnection(host, port, timeout=timeout)
        # CUPS uses self-signed certificates; trust is anchored by the pin.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)

    def request(self, operation: int, attributes: list[tuple[int, str, Any]] = (),
                resource: str = '/', printer_attributes: list | None = None,
                timeout: float | None = None, authenticate: bool = True, data: bytes = b'') -> Response:
        """Send one request; ``data`` is document data sent after the attributes."""
        groups = [operation_attributes(*attributes)]
        if printer_attributes:
            groups.append((PRINTER, printer_attributes))
        body = encode(operation, groups) + data
        headers = {'Content-Type': 'application/ipp'}
        if authenticate and self.username and self.password is not None:
            token = base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()
            headers['Authorization'] = f'Basic {token}'
        connection = self._connect(timeout or self.timeout)
        try:
            connection.connect()
            if self.tls:
                # Check the certificate before any credentials are sent.
                certificate = connection.sock.getpeercert(binary_form=True) or b''
                fingerprint = hashlib.sha256(certificate).hexdigest()
                if self.pin is None:
                    self.pin = fingerprint
                elif fingerprint != self.pin:
                    raise CertificateChanged(f'The TLS certificate of CUPS server {self.server} has changed. '
                                             'If that is expected, reconnect it in Devices.')
            connection.request('POST', resource, body, headers)
            reply = connection.getresponse()
            data = reply.read()
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, ssl.SSLError):
                raise IPPError(f'TLS connection to CUPS server {self.server} failed') from exc
            raise IPPError(f'Cannot reach CUPS server {self.server}') from exc
        finally:
            connection.close()
        if reply.status == 401:
            raise AuthRequired('CUPS did not accept the administrator login' if 'Authorization' in headers
                               else 'This needs a CUPS administrator login', 401)
        if reply.status == 426:
            raise EncryptionRequired('CUPS requires an encrypted connection', 426)
        if reply.status == 403:
            raise AuthRequired('This CUPS account is not allowed to administer printers', 403)
        if reply.status != 200:
            raise IPPError(f'CUPS answered HTTP {reply.status}', reply.status)
        response = decode(data)
        if not response.ok:
            if response.status in (0x0402, 0x0403):  # not-authenticated, not-authorized
                raise AuthRequired(response.message or 'CUPS refused this administrator account', response.status)
            raise IPPError(response.message or f'CUPS refused the request (status 0x{response.status:04x})',
                           response.status)
        return response
