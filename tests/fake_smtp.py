"""A minimal in-process mail server that speaks real SMTP, for tests.

It offers STARTTLS or implicit TLS, checks a login with AUTH PLAIN or LOGIN,
can refuse recipients, and keeps every message it accepts.
"""
from __future__ import annotations

import base64
import email
import email.policy
import socketserver
import ssl
import threading


class FakeSmtp:
    def __init__(self, *, username: str | None = None, password: str | None = None, mode: str | None = None,
                 certificate: tuple[str, str] | None = None):
        """``mode``: None (plain), 'starttls' or 'ssl'; TLS needs ``certificate`` (cert, key)."""
        self.username, self.password, self.mode = username, password, mode
        self.messages: list[dict] = []
        self.refuse: set[str] = set()
        self.context = None
        if certificate:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.load_cert_chain(*certificate)
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                fake.converse(self.request)

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def bodies(self) -> list[str]:
        return [m['message'].get_content() for m in self.messages]

    def converse(self, conn):
        try:
            if self.mode == 'ssl':
                conn = self.context.wrap_socket(conn, server_side=True)
            reader = conn.makefile('rb')
            send = lambda text: conn.sendall(text.encode() + b'\r\n')
            send('220 fake.smtp ESMTP ready')
            secure, authed = self.mode == 'ssl', not self.username
            sender, recipients = None, []
            while line := reader.readline():
                command = line.decode().rstrip('\r\n')
                verb = command.split(' ', 1)[0].upper()
                if verb in ('EHLO', 'HELO'):
                    offers = ['fake.smtp']
                    if self.mode == 'starttls' and not secure:
                        offers.append('STARTTLS')
                    if self.username and (secure or self.mode is None):
                        offers.append('AUTH PLAIN LOGIN')
                    offers.append('8BITMIME')
                    send('\r\n'.join(f"250{'-' if i < len(offers) - 1 else ' '}{o}" for i, o in enumerate(offers)))
                elif verb == 'STARTTLS' and self.mode == 'starttls' and not secure:
                    send('220 Go ahead')
                    conn = self.context.wrap_socket(conn, server_side=True)
                    reader, secure = conn.makefile('rb'), True
                elif verb == 'AUTH':
                    parts = command.split()
                    if parts[1].upper() == 'PLAIN':
                        if len(parts) > 2:
                            data = parts[2]
                        else:
                            send('334 ')
                            data = reader.readline().decode().strip()
                        _, user, password = base64.b64decode(data).decode().split('\0')
                    else:  # LOGIN
                        send('334 VXNlcm5hbWU6')
                        user = base64.b64decode(reader.readline().strip()).decode()
                        send('334 UGFzc3dvcmQ6')
                        password = base64.b64decode(reader.readline().strip()).decode()
                    authed = (user, password) == (self.username, self.password)
                    send('235 2.7.0 Authentication successful' if authed else '535 5.7.8 Authentication credentials invalid')
                elif verb == 'MAIL':
                    if not authed:
                        send('530 5.7.0 Authentication required')
                        continue
                    sender, recipients = command.split(':', 1)[1].split()[0].strip('<>'), []
                    send('250 2.1.0 OK')
                elif verb == 'RCPT':
                    address = command.split(':', 1)[1].strip().strip('<>')
                    if address in self.refuse:
                        send('550 5.1.1 No such mailbox')
                    else:
                        recipients.append(address)
                        send('250 2.1.5 OK')
                elif verb == 'DATA':
                    send('354 End data with <CR><LF>.<CR><LF>')
                    data = bytearray()
                    while (chunk := reader.readline()) not in (b'.\r\n', b''):
                        data += chunk[1:] if chunk.startswith(b'..') else chunk
                    self.messages.append({'from': sender, 'to': recipients,
                                          'message': email.message_from_bytes(bytes(data), policy=email.policy.default)})
                    send('250 2.0.0 Queued')
                elif verb == 'RSET':
                    sender, recipients = None, []
                    send('250 OK')
                elif verb == 'NOOP':
                    send('250 OK')
                elif verb == 'QUIT':
                    send('221 Bye')
                    return
                else:
                    send('502 5.5.2 Not implemented')
        except (OSError, ssl.SSLError, ValueError):
            return
