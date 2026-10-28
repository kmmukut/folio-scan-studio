"""v0.22: driverless printers that CUPS's own generator rejects (an Epson
L3250 does) are set up with cups-filters' generator instead, and the
driverless check also asks printers that answer only over TLS."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import app as web, cupsadmin, ipp  # noqa: E402
from fake_cups import FakeCups, self_signed_certificate  # noqa: E402
import test_builtin_print_server_v015 as v015  # noqa: E402

builtin = v015.builtin  # The built-in print server with a fake CUPS behind it
follow, server_path = v015.follow, v015.server_path

EPSON = {'name': 'Epson', 'connection': 'ipp', 'address': '192.0.2.40', 'driver': 'everywhere'}
CUPS_ANSWER = 'Unable to create PPD: Printer does not support required IPP attributes or document formats.'


def add(admin, fake, body=EPSON):
    started = admin.post(f'{server_path(fake)}/printers', json=body)
    assert started.status_code == 202, started.text
    return follow(admin, started.json()['task'], timeout=30)


def test_a_printer_cups_cannot_describe_is_set_up_with_cups_filters(builtin):
    fake, admin = builtin
    fake.everywhere_fails = True
    task = add(admin, fake)
    assert task['status'] == 'done', task
    queue = fake.printers['Epson']
    assert queue['device-uri'] == 'ipp://192.0.2.40/ipp/print'
    assert queue['ppd-name'] == 'driverless:ipp://192.0.2.40/ipp/print'
    models = [(attrs.get('ppd-name') or [None])[0] for op, attrs, _ in fake.requests if op == ipp.CUPS_ADD_MODIFY_PRINTER]
    assert models == ['everywhere', 'driverless:ipp://192.0.2.40/ipp/print']  # CUPS's own first, then cups-filters'


def test_printers_cups_can_describe_keep_cups_own_setup(builtin):
    fake, admin = builtin
    assert add(admin, fake)['status'] == 'done'
    assert fake.printers['Epson']['ppd-name'] == 'everywhere'
    assert sum(op == ipp.CUPS_ADD_MODIFY_PRINTER for op, _, _ in fake.requests) == 1


def test_when_neither_works_the_half_made_printer_is_removed(builtin):
    fake, admin = builtin
    fake.everywhere_fails, fake.driverless_generator = True, False  # e.g. another CUPS without cups-filters
    task = add(admin, fake)
    assert task['status'] == 'failed' and CUPS_ANSWER in task['error']
    assert 'Epson' not in fake.printers


def test_a_printer_that_already_existed_is_never_removed(builtin):
    fake, _ = builtin  # (Folio's own form refuses existing names; this guards any direct use)
    fake.printers['Epson'] = {'state': 3, 'reasons': [], 'ppd-name': 'everywhere', 'device-uri': 'ipp://192.0.2.40/ipp/print'}
    fake.everywhere_fails, fake.driverless_generator = True, False
    conn = cupsadmin.connection(web.DATA_DIR, fake.server, admin=True)
    with pytest.raises(ipp.IPPError, match='Unable to create PPD'):
        cupsadmin.add_printer(conn, 'Epson', 'ipp://192.0.2.40/ipp/print', cupsadmin.DRIVERLESS)
    assert 'Epson' in fake.printers


def test_a_chosen_driver_is_used_as_it_is(builtin):
    fake, admin = builtin
    fake.everywhere_fails = True  # Only driverless setup ever falls back
    task = add(admin, fake, {**EPSON, 'driver': 'drv:///sample.drv/generic.ppd'})
    assert task['status'] == 'done'
    assert fake.printers['Epson']['ppd-name'] == 'drv:///sample.drv/generic.ppd'
    assert sum(op == ipp.CUPS_ADD_MODIFY_PRINTER for op, _, _ in fake.requests) == 1


def test_the_driverless_check_also_asks_printers_that_only_answer_over_tls(tmp_path):
    certificate = self_signed_certificate(tmp_path, 'printer')
    if not certificate:
        pytest.skip('openssl is not available')
    printer = FakeCups(certificate=certificate)
    try:
        printer.encrypted_only = True
        printer.printers['ipp'] = {'state': 3, 'reasons': [], 'make-and-model': 'EPSON L3250 Series',
                                   'formats': ['application/octet-stream', 'image/pwg-raster', 'application/vnd.epson.escpr']}
        found = cupsadmin.driverless_support(f'ipp://{printer.server}/printers/ipp')
        assert found == {'formats': ['application/octet-stream', 'image/pwg-raster', 'application/vnd.epson.escpr'],
                         'make_model': 'EPSON L3250 Series', 'full': True,
                         'encrypted_only': True}
    finally:
        printer.close()
