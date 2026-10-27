"""A minimal in-process CUPS stand-in that speaks real IPP, for tests.

It answers the operations Folio uses, enforces Basic authentication on
administrative requests the way cupsd does, and records what it was asked.
"""
from __future__ import annotations

import base64
import http.server
import ssl
import struct
import threading
import time
from pathlib import Path

from backend import ipp

ADMIN_OPERATIONS = {ipp.CUPS_GET_DEVICES, ipp.CUPS_ADD_MODIFY_PRINTER, ipp.CUPS_DELETE_PRINTER,
                    ipp.PAUSE_PRINTER, ipp.RESUME_PRINTER}


def response(status: int, request_id: int, groups: list) -> bytes:
    """Encode a response; the header layout matches a request's, with a status code."""
    body = ipp.encode(status, [ipp.operation_attributes(*groups[0])] + groups[1:], request_id)
    return body


class FakeCups:
    def __init__(self, username='cupsadmin', password='cups-secret', *, require_tls=False, certificate=None,
                 cancel_needs_auth=False):
        self.username, self.password = username, password
        self.require_tls = require_tls
        self.cancel_needs_auth = cancel_needs_auth
        self.printers: dict[str, dict] = {}
        self.jobs: dict[int, dict] = {}
        self.requests: list[tuple[int, dict, dict]] = []
        self.documents: list[bytes] = []  # Document data sent with each request (e.g. a PPD)
        self.devices = [('direct', 'usb://Samsung/M2020%20Series?serial=Z7', 'Samsung M2020 Series', 'Samsung M2020 Series USB',
                         'MFG:Samsung;MDL:M2020 Series;'),
                        ('network', 'socket', 'Unknown', 'AppSocket/HP JetDirect', '')]
        self.ppds = [('Samsung', 'openprinting-ppds:0/ppd/openprinting/Samsung/PXL/Samsung_M288x_Series_PXL.ppd',
                      'Samsung M288x Series PXL'),
                     ('Samsung', 'foomatic:Samsung-M288x-pxlmono.ppd', 'Samsung M288x Foomatic/pxlmono (recommended)'),
                     ('Samsung', 'openprinting-ppds:0/ppd/openprinting/Samsung/PS/Samsung_C48x_Series.ppd',
                      'Samsung C48x Series PS'),
                     ('Generic', 'drv:///sample.drv/generic.ppd', 'Generic PostScript Printer')]
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                request = ipp.decode(data)
                operation = struct.unpack_from('>H', data, 2)[0]
                attrs = request.group(ipp.OPERATION)
                printer_attrs = request.group(ipp.PRINTER)
                fake.requests.append((operation, attrs, printer_attrs))
                if fake.require_tls and not isinstance(self.connection, ssl.SSLSocket) and operation in ADMIN_OPERATIONS:
                    return self.reply(426, b'')
                authed = self.headers.get('Authorization') == 'Basic ' + base64.b64encode(
                    f'{fake.username}:{fake.password}'.encode()).decode()
                needs_auth = operation in ADMIN_OPERATIONS or (operation == ipp.CANCEL_JOB and fake.cancel_needs_auth)
                if needs_auth and not authed:
                    return self.reply(401, b'')
                fake.documents.append(request.data)
                answer = fake.handle(operation, attrs, printer_attrs, authed)
                status, groups, document = answer if len(answer) == 3 else (*answer, b'')
                self.reply(200, response(status, request.request_id, groups) + document)

            def reply(self, code, body):
                self.send_response(code)
                self.send_header('Content-Type', 'application/ipp')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        if certificate:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*certificate)
            self.plain = self.httpd.socket
            self.httpd.socket = _SniffingSocket(self.plain, context)
        self.server = f'127.0.0.1:{self.httpd.server_address[1]}'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # --- Operations -----------------------------------------------------------

    def queue_of(self, attrs):
        uri = (attrs.get('printer-uri') or [''])[0]
        return uri.rsplit('/printers/', 1)[1] if '/printers/' in uri else None

    def add_job(self, queue, user, title, state=3, pages=0, sheets=0):
        job_id = len(self.jobs) + 1
        self.jobs[job_id] = {'queue': queue, 'user': user, 'title': title, 'state': state,
                             'pages': pages, 'sheets': sheets}
        return job_id

    def job_group(self, job_id, job, show_private):
        values = [(ipp.INTEGER, 'job-id', job_id), (ipp.ENUM, 'job-state', job['state']),
                  (ipp.KEYWORD, 'job-state-reasons', 'none'),
                  (ipp.URI, 'job-printer-uri', f'ipp://{self.server}/printers/{job["queue"]}'),
                  (ipp.INTEGER, 'time-at-creation', 1_700_000_000 + job_id),
                  (ipp.INTEGER, 'job-impressions-completed', job.get('pages', 0)),
                  (ipp.INTEGER, 'job-media-sheets-completed', job.get('sheets', 0))]
        if job['state'] >= 7:
            values.append((ipp.INTEGER, 'time-at-completed', int(time.time())))
        if show_private:  # CUPS hides these from anyone but the owner or an admin
            values += [(ipp.NAME, 'job-name', job['title']), (ipp.NAME, 'job-originating-user-name', job['user'])]
        return (ipp.JOB, values)

    def handle(self, operation, attrs, printer_attrs, authed):
        user = (attrs.get('requesting-user-name') or [None])[0]
        if operation == ipp.CUPS_GET_DEVICES:
            schemes = attrs.get('include-schemes')
            groups = [(ipp.PRINTER, [(ipp.KEYWORD, 'device-class', cls), (ipp.URI, 'device-uri', uri),
                                     (ipp.TEXT, 'device-make-and-model', model), (ipp.TEXT, 'device-info', info),
                                     (ipp.TEXT, 'device-id', device_id)])
                      for cls, uri, model, info, device_id in self.devices
                      if not schemes or uri.split(':')[0] in schemes]
            return 0, [[], *groups]
        if operation == ipp.CUPS_GET_PPDS:
            make = (attrs.get('ppd-make') or [None])[0]
            wanted = attrs.get('requested-attributes') or []
            if wanted == ['ppd-make']:
                return 0, [[], *[(ipp.PRINTER, [(ipp.TEXT, 'ppd-make', m)]) for m in sorted({p[0] for p in self.ppds})]]
            found = [p for p in self.ppds if make is None or p[0].lower() == make.lower()]
            if not found:
                return 0x0406, [[(ipp.TEXT, 'status-message', 'No PPD files found')]]
            return 0, [[], *[(ipp.PRINTER, [(ipp.NAME, 'ppd-name', name), (ipp.TEXT, 'ppd-make-and-model', model),
                                            (ipp.LANGUAGE, 'ppd-natural-language', 'en')]) for _, name, model in found]]
        if operation == ipp.CUPS_GET_PRINTERS:
            if not self.printers:  # What a real CUPS answers when it has no printers
                return 0x0406, [[(ipp.TEXT, 'status-message', 'No destinations added.')]]
            return 0, [[], *[(ipp.PRINTER, [
                (ipp.NAME, 'printer-name', name), (ipp.TEXT, 'printer-info', entry.get('printer-info', name)),
                (ipp.URI, 'device-uri', entry.get('device-uri', 'socket://192.0.2.9')),
                (ipp.BOOLEAN, 'printer-is-shared', entry.get('printer-is-shared', True)),
                (ipp.TEXT, 'printer-make-and-model', 'Fake Laser'), (ipp.TEXT, 'printer-location', entry.get('printer-location', '')),
                (ipp.MIME_TYPE, 'document-format-supported', ['application/octet-stream', 'image/urf', 'application/pdf']),
                (ipp.KEYWORD, 'urf-supported', ['V1.4', 'W8', 'RS300']), (ipp.BOOLEAN, 'color-supported', False),
                (ipp.KEYWORD, 'sides-supported', ['one-sided', 'two-sided-long-edge']),
                (ipp.URI, 'printer-uuid', f'urn:uuid:00000000-0000-0000-0000-{len(name):012d}'),
                (ipp.ENUM, 'printer-state', entry['state']), (ipp.ENUM, 'printer-type', 8400972)])
                for name, entry in self.printers.items()]]
        queue = self.queue_of(attrs)
        if operation == ipp.CUPS_ADD_MODIFY_PRINTER:
            entry = self.printers.setdefault(queue, {'state': 3, 'reasons': []})
            entry.update({k: v[0] for k, v in printer_attrs.items()})
            entry['ppd-name'] = (attrs.get('ppd-name') or [entry.get('ppd-name')])[0]
            if self.documents[-1]:
                entry['ppd-file'] = self.documents[-1]  # A driver sent along, like lpadmin -P
            return 0, [[]]
        if operation == ipp.CUPS_GET_PPD:
            entry = self.printers.get(queue)
            if not entry or not entry.get('ppd-file'):
                return 0x0406, [[(ipp.TEXT, 'status-message', 'No PPD for this printer')]]
            return 0, [[]], entry['ppd-file']
        if operation == ipp.CUPS_DELETE_PRINTER:
            if self.printers.pop(queue, None) is None:
                return 0x0406, [[(ipp.TEXT, 'status-message', 'The printer or class does not exist.')]]
            return 0, [[]]
        if operation in (ipp.PAUSE_PRINTER, ipp.RESUME_PRINTER):
            paused = operation == ipp.PAUSE_PRINTER
            self.printers[queue].update(state=5 if paused else 3, reasons=['paused'] if paused else [])
            return 0, [[]]
        if operation == ipp.GET_PRINTER_ATTRIBUTES:
            entry = self.printers.get(queue)
            if entry is None:
                return 0x0406, [[(ipp.TEXT, 'status-message', 'The printer or class does not exist.')]]
            markers = entry.get('markers', [])  # (name, type, level, colour, low level)
            supplies = [(ipp.NAME, 'marker-names', [m[0] for m in markers]),
                        (ipp.KEYWORD, 'marker-types', [m[1] for m in markers]),
                        (ipp.INTEGER, 'marker-levels', [m[2] for m in markers]),
                        (ipp.NAME, 'marker-colors', [m[3] for m in markers]),
                        (ipp.INTEGER, 'marker-low-levels', [m[4] for m in markers])] if markers else []
            return 0, [[], (ipp.PRINTER, [*supplies, (ipp.ENUM, 'printer-state', entry['state']),
                                          (ipp.KEYWORD, 'printer-state-reasons', entry['reasons'] or ['none']),
                                          (ipp.TEXT, 'printer-state-message', ''),
                                          (ipp.BOOLEAN, 'printer-is-accepting-jobs', True),
                                          (ipp.INTEGER, 'queued-job-count', 0),
                                          (ipp.TEXT, 'printer-make-and-model', entry.get('ppd-name', '')),
                                          (ipp.URI, 'device-uri', entry.get('device-uri', ''))])]
        if operation == ipp.GET_JOBS:
            which = (attrs.get('which-jobs') or ['not-completed'])[0]
            mine = (attrs.get('my-jobs') or [False])[0]
            groups = []
            for job_id, job in self.jobs.items():
                done = job['state'] >= 7
                if (which == 'completed') != done or (queue and job['queue'] != queue) or (mine and job['user'] != user):
                    continue
                groups.append(self.job_group(job_id, job, authed or job['user'] == user))
            return 0, [[], *groups]
        if operation in (ipp.GET_JOB_ATTRIBUTES, ipp.CANCEL_JOB):
            job_id = int((attrs.get('job-uri') or ['/0'])[0].rsplit('/', 1)[1])
            job = self.jobs.get(job_id)
            if job is None:
                return 0x0406, [[(ipp.TEXT, 'status-message', 'Job does not exist.')]]
            if operation == ipp.GET_JOB_ATTRIBUTES:
                return 0, [[], self.job_group(job_id, job, authed or job['user'] == user)]
            if not authed and job['user'] != user:
                return 0x0403, [[(ipp.TEXT, 'status-message', 'Not authorized')]]
            job['state'] = 7
            return 0, [[]]
        return 0x0501, [[(ipp.TEXT, 'status-message', 'Unsupported operation')]]


class _SniffingSocket:
    """Accepts plain HTTP and TLS on one port, like cupsd does on 631."""

    def __init__(self, sock, context):
        self._sock, self._context = sock, context

    def accept(self):
        conn, address = self._sock.accept()
        first = conn.recv(1, 0x02)  # MSG_PEEK
        if first == b'\x16':  # A TLS handshake record
            conn = self._context.wrap_socket(conn, server_side=True)
        return conn, address

    def __getattr__(self, name):
        return getattr(self._sock, name)


def self_signed_certificate(folder: Path, name: str = 'cups') -> tuple[str, str] | None:
    import shutil
    import subprocess
    if not shutil.which('openssl'):
        return None
    cert, key = folder / f'{name}.crt', folder / f'{name}.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', f'/CN={name}',
                    '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
    return str(cert), str(key)
