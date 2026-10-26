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
                status, groups = fake.handle(operation, attrs, printer_attrs, authed)
                self.reply(200, response(status, request.request_id, groups))

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

    def add_job(self, queue, user, title, state=3):
        job_id = len(self.jobs) + 1
        self.jobs[job_id] = {'queue': queue, 'user': user, 'title': title, 'state': state}
        return job_id

    def job_group(self, job_id, job, show_private):
        values = [(ipp.INTEGER, 'job-id', job_id), (ipp.ENUM, 'job-state', job['state']),
                  (ipp.KEYWORD, 'job-state-reasons', 'none'),
                  (ipp.URI, 'job-printer-uri', f'ipp://{self.server}/printers/{job["queue"]}'),
                  (ipp.INTEGER, 'time-at-creation', 1_700_000_000 + job_id)]
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
        queue = self.queue_of(attrs)
        if operation == ipp.CUPS_ADD_MODIFY_PRINTER:
            entry = self.printers.setdefault(queue, {'state': 3, 'reasons': []})
            entry.update({k: v[0] for k, v in printer_attrs.items()})
            entry['ppd-name'] = (attrs.get('ppd-name') or [entry.get('ppd-name')])[0]
            return 0, [[]]
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
            return 0, [[], (ipp.PRINTER, [(ipp.ENUM, 'printer-state', entry['state']),
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
