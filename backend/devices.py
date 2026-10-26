"""Persisted scanner registry and read-only CUPS printer discovery for Folio.

No route gives the web app lpadmin privileges. Device and queue selections are
strictly validated server-side; raw commands are never supplied by browsers.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from typing import Any, Callable
from backend import printing

REGISTRY_MUTEX = threading.RLock()

# Device queries spawn lpstat/lpoptions/scanimage processes and may wait on
# the network. Cache them so page loads and print submissions don't repeat the
# same probes, and back off from unreachable CUPS servers instead of waiting
# for a timeout on every request. Explicit "Refresh" actions clear the cache.
DEVICE_CACHE = os.getenv('FOLIO_DEVICE_CACHE', 'on').strip().lower() not in ('off','false','no','0')
QUEUE_TTL = 15          # Queue ready/stopped state
CAPABILITY_TTL = 300    # lpoptions -l: sizes, trays, duplex, color
SANE_LIST_TTL = 20      # scanimage -L
SCANNER_CAPS_TTL = 600  # scanimage --all-options for a responsive scanner
FAILED_PROBE_TTL = 15   # Retry soon after a scanner failed to answer
CUPS_BACKOFF = 60       # Skip a CUPS server this long after a timeout
_cache: dict[tuple, tuple[float, Any]] = {}
_cache_locks: dict[tuple, threading.Lock] = {}
_cache_mutex = threading.Lock()
_cups_down: dict[str, float] = {}


def cached(key: tuple, ttl: float | Callable[[Any], float], compute: Callable[[], Any]) -> Any:
    """Return a fresh cached value, computing it at most once concurrently.

    ``ttl`` may be a function of the computed value, so failed probes can
    expire sooner than successful ones.
    """
    if not DEVICE_CACHE:
        return compute()
    with _cache_mutex:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        key_lock = _cache_locks.setdefault(key, threading.Lock())
    with key_lock:
        with _cache_mutex:
            hit = _cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        value = compute()
        lifetime = ttl(value) if callable(ttl) else ttl
        with _cache_mutex:
            _cache[key] = (time.monotonic() + lifetime, value)
        return value


def invalidate(kind: str | None = None) -> None:
    """Forget cached device answers: all of them, or one kind ('cups', 'sane')."""
    with _cache_mutex:
        for key in list(_cache):
            if kind is None or key[0] == kind:
                del _cache[key]
        if kind in (None, 'cups'):
            _cups_down.clear()


def mark_cups_reachable(server: str) -> None:
    _cups_down.pop(server, None)


def mark_cups_down(server: str) -> None:
    _cups_down[server] = time.monotonic() + CUPS_BACKOFF


def cups_backoff(server: str) -> float:
    """Seconds until an unreachable CUPS server is probed again (0 = now)."""
    until = _cups_down.get(server, 0)
    return max(0.0, until - time.monotonic())
QUEUENAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
DEVICEID = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')
DEVICE_NAME = re.compile(r'^[A-Za-z0-9][\w .-]{0,75}$')
SANE_OPT = re.compile(r'^[ \t]*--(source|mode|resolution)[ \t]+([^\r\n]*)', re.I | re.M)
SANE_LISTED = re.compile(r'^device [`\']([^`\']+)[`\'] is a (.*)$', re.M)
NETWORK_PROTOCOLS = {'WSD':(8018,'/wsd/scan'), 'eSCL':(80,'/eSCL')}


def path_for(data_dir: Path) -> Path:
    return Path(data_dir) / 'devices.json'


def initial_config(default_scanner: str = '', default_queue: str = '') -> dict:
    """Clean Folio installs have zero scanners until an admin adds one.

    An explicit SCANNER_DEVICE is accepted only for older deployments which
    intentionally opt into the legacy SANE device. It is never required.
    """
    default_scanner = (default_scanner or '').strip()
    default_queue = (default_queue or '').strip()
    scanners = ([{'id':'original',
                  'name':default_scanner.split(':', 2)[-1] if ':' in default_scanner else 'Existing scanner',
                  'kind':'existing', 'hint':default_scanner, 'enabled':True}]
                if default_scanner else [])
    return {'version':2,
            'default_scanner':'original' if scanners else None,
            'default_printer':default_queue or None,
            'scanners':scanners,
            'printers':({default_queue:{'enabled':True, 'guest':bool(scanners),
                                       'member':True, 'printer':True}}
                        if default_queue else {})}


def get_config(data_dir: Path, default_scanner: str, default_queue: str) -> dict:
    """Initialize exactly once; later restarts never overwrite administrator edits."""
    with REGISTRY_MUTEX:
        filename = path_for(data_dir)
        if not filename.exists():
            filename.parent.mkdir(parents=True, exist_ok=True)
            state = initial_config(default_scanner, default_queue)
            save_config(data_dir, state)
            return state
        return json.loads(filename.read_text(encoding='utf-8'))


def save_config(data_dir: Path, state: dict) -> None:
    """Atomic write prevents a partial JSON file on container restart."""
    with REGISTRY_MUTEX:
        file = path_for(data_dir)
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.devices-', dir=file.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, file)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def scanner_endpoint(address: str, port: int, path: str) -> str:
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError as exc:
        raise ValueError('Enter a valid scanner IP address') from exc
    if ip.version != 4:
        raise ValueError('IPv4 scanner addresses are supported in this wizard')
    if not (1 <= int(port) <= 65535):
        raise ValueError('Invalid scanner port')
    if not re.fullmatch(r'/[A-Za-z0-9._/%-]{0,127}', path) or '..' in path or '//' in path:
        raise ValueError('Invalid scanner endpoint path')
    return f'http://{ip}:{int(port)}{path}'


def make_scanner(name: str, address: str, protocol: str, port: int | None,
                 path: str | None, existing_id: str | None = None) -> dict:
    name=name.strip()
    if not DEVICE_NAME.fullmatch(name):
        raise ValueError('Scanner name should use letters, numbers, spaces, dot or dash')
    if protocol not in NETWORK_PROTOCOLS:
        raise ValueError('Use WSD or eSCL scanning')
    default_port, default_path=NETWORK_PROTOCOLS[protocol]
    endpoint=scanner_endpoint(address, port or default_port, path or default_path)
    identifier=existing_id or ('net-' + hashlib.sha256(endpoint.encode()).hexdigest()[:12])
    return {'id':identifier, 'name':name, 'kind':'managed', 'protocol':protocol,
            'ip':address.strip(), 'port':port or default_port,'path':path or default_path,
            'endpoint':endpoint,'enabled':True}


def write_managed_sane_config(registry: dict, conf_path: Path) -> None:
    """Write the complete Folio scanner config without restarting Docker.

    A new scanimage process reads the persisted scanner endpoints via the
    /etc/sane.d/airscan.d/folio-managed.conf symlink.
    """
    lines=['# Generated by Folio; edit scanner endpoints in Devices, not here.', '[devices]']
    for entry in registry.get('scanners', []):
        if entry.get('kind') != 'managed' or not entry.get('enabled', True):
            continue
        endpoint = scanner_endpoint(entry['ip'], entry['port'], entry['path'])
        display = f"Folio-{entry['id']} {entry['name']}"
        lines.append(f'"{display}" = {endpoint}, {entry["protocol"]}')
    # Use discovery across the local subnet only when explicitly enabled.
    # On the user's VLAN-heavy Docker host, multicast probing many Docker
    # bridges can be slow and can exhaust multicast group resources.
    if os.getenv('FOLIO_AIRSCAN_DISCOVERY', 'off').strip().lower() not in ('on','true','yes','1'):
        lines.extend(['', '[options]', 'discovery = disable'])
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    content='\n'.join(lines)+'\n'
    fd, temp = tempfile.mkstemp(prefix='.folio-sane-', dir=conf_path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, conf_path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def listed_sane_devices(timeout: int = 14) -> list[dict]:
    return cached(('sane','list'), SANE_LIST_TTL, lambda: read_sane_devices(timeout))


def read_sane_devices(timeout: int = 14) -> list[dict]:
    try:
        completed=subprocess.run(['scanimage','-L'], capture_output=True,text=True,timeout=timeout)
    except (OSError,subprocess.TimeoutExpired):
        return []
    return [{'sane_name':name, 'description':desc} for name,desc in SANE_LISTED.findall(completed.stdout)]


def match_scanner(entry: dict, seen: list[dict]) -> dict | None:
    if entry.get('kind') == 'managed':
        needle=f"Folio-{entry['id']} "
        return next((s for s in seen if needle in s['sane_name']),None)
    hint=entry.get('hint','')
    found=next((s for s in seen if s['sane_name']==hint),None)
    if found:
        return found
    # airscan:w0 changes when devices are added: match the stable final name.
    if hint.startswith('airscan:') and ':' in hint:
        suffix=hint.split(':',2)[-1].strip().lower()
        matches=[s for s in seen if s['sane_name'].startswith('airscan:')
                 and s['sane_name'].split(':',2)[-1].strip().lower()==suffix]
        if len(matches)==1:
            return matches[0]
    return None


def scanner_capabilities(device: str, timeout: int = 12) -> dict:
    return cached(('sane','caps',device),
                  lambda caps: SCANNER_CAPS_TTL if caps['available'] else FAILED_PROBE_TTL,
                  lambda: read_scanner_capabilities(device, timeout))


def read_scanner_capabilities(device: str, timeout: int = 12) -> dict:
    """Read actual source, mode, DPI lists from SANE, without hardcoding a model."""
    result={'sources':[], 'modes':[], 'resolutions':[], 'available':False}
    try:
        completed=subprocess.run(['scanimage','-d',device,'--all-options'],
                                 capture_output=True,text=True,timeout=timeout)
    except (OSError,subprocess.TimeoutExpired):
        return result
    if completed.returncode != 0:
        result['error'] = (completed.stderr or 'SANE option query failed').strip()[:300]
        return result
    for match in SANE_OPT.finditer(completed.stdout):
        key, expr=match.groups()
        key=key.lower()
        expr=expr.split(' [',1)[0].strip()
        if key=='resolution':
            values=[int(n) for n in re.findall(r'\d+',expr)]
            # For continuous ranges use common DPIs, respecting the backend bounds.
            if '..' in expr and len(values)>=2:
                lo,hi=values[:2]
                values=[n for n in (75,100,150,200,300,400,600,1200,2400,4800) if lo<=n<=hi]
            result['resolutions']=[v for v in values if 50<=v<=4800][:30]
        else:
            options=[x.lstrip('*').strip() for x in expr.split('|')]
            result['sources' if key=='source' else 'modes']=[o for o in options
                if re.fullmatch(r'[\w .+/-]{1,40}',o)]
    result['available']=bool(result['sources'] and result['modes'] and result['resolutions'])
    if not result['available']:
        result['error'] = 'SANE did not report a source, mode, and resolution. Check scanimage --all-options.'
    return result


def scanners_state(registry: dict, *, probe: bool = True) -> list[dict]:
    seen=listed_sane_devices() if probe else []
    output=[]
    for entry in registry['scanners']:
        detected=match_scanner(entry,seen) if probe else None
        output.append({**entry, 'connected':bool(detected),
                       'sane_name':detected['sane_name'] if detected else None,
                       'description':detected['description'] if detected else '',
                       'capabilities':scanner_capabilities(detected['sane_name']) if detected and entry.get('enabled',True) else {'sources':[],'modes':[],'resolutions':[], 'available':False}})
    return output


def validate_queue(queue: str) -> str:
    if not QUEUENAME.fullmatch(queue):
        raise ValueError('Invalid CUPS queue name')
    return queue


def cups_run(args: list[str], timeout: int = 8) -> subprocess.CompletedProcess:
    server = args[args.index('-h')+1] if '-h' in args[:-1] else None
    if server and cups_backoff(server):
        return subprocess.CompletedProcess(args,1,'',f'CUPS server {server} is not responding; retrying shortly')
    try:
        return subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    except subprocess.TimeoutExpired:
        if server:
            _cups_down[server] = time.monotonic() + CUPS_BACKOFF
        return subprocess.CompletedProcess(args,1,'','CUPS unavailable')
    except OSError:
        return subprocess.CompletedProcess(args,1,'','CUPS unavailable')


def list_cups_queues(server: str) -> list[dict]:
    result=cups_run(['lpstat','-h',server,'-p'])
    accepting=cups_run(['lpstat','-h',server,'-a'])
    found=[]
    accepts={n for n in re.findall(r'^(\S+) accepting requests',accepting.stdout,re.M)}
    for line in result.stdout.splitlines():
        m=re.match(r'^printer\s+(\S+)\s+(.*)$',line)
        if not m or not QUEUENAME.fullmatch(m.group(1)):
            continue
        queue=m.group(1)
        if queue in [q['queue'] for q in found]:
            continue
        found.append({'queue':queue,'ready':'disabled' not in m.group(2).lower() and queue in accepts,
                      'status':m.group(2)[:160]})
    return found[:64]


# CUPS advertises either the IPP media-source option or PPD-specific names.
# Keep this ordered: prefer the standard IPP attribute when both appear.
PAPER_SOURCE_KEYS = ('media-source', 'inputslot', 'mediasource',
                     'mediaposition', 'papersource', 'paperinput',
                     'paper-source', 'inputtray', 'mediaslot', 'papertray')
SAFE_SOURCE_VALUE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')


def cups_choice_values(raw: str) -> list[dict]:
    """Parse lpoptions -l choices without guessing unsupported tray names.

    Legacy PPDs can display options as ``*Tray1/Tray 1`` (labels contain
    whitespace). We take the actual token before `/` and drop label words.
    For options without per-choice labels, each space-separated token is a
    choice. The leading `*` marks the driver's default.
    """
    chunks=raw.split()
    if any('/' in chunk for chunk in chunks):
        first=next(i for i, chunk in enumerate(chunks) if '/' in chunk)
        chunks=[*chunks[:first],*(chunk for chunk in chunks[first:]
                                  if '/' in chunk or chunk.startswith('*'))]
    result=[]
    seen=set()
    for choice in chunks:
        text=choice.split('/',1)[0]
        default=text.startswith('*')
        value=text.lstrip('*')
        if not SAFE_SOURCE_VALUE.fullmatch(value) or value.lower() in seen:
            continue
        result.append({'value':value, 'default':default})
        seen.add(value.lower())
    return result


def paper_source_label(value: str) -> str:
    """Readable, stable labels for vendor-specific raw CUPS option values."""
    key=re.sub(r'[-_.\s]','',value).lower()
    labels={
        'auto':'Automatic selection', 'autoselect':'Automatic selection',
        'automatic':'Automatic selection', 'printerdefault':'Printer default',
        'main':'Main tray', 'maintray':'Main tray',
        'manual':'Manual feed', 'manualfeed':'Manual feed',
        'multipurpose':'Multipurpose tray', 'multipurposetray':'Multipurpose tray',
        'mptray':'Multipurpose / manual feed', 'mp':'Multipurpose tray',
        'multitray':'Multipurpose / manual feed',
        'bypass':'Bypass tray', 'bypasstray':'Bypass tray',
        'alternate':'Alternate tray', 'upper':'Upper tray',
        'lower':'Lower tray', 'middle':'Middle tray',
        'top':'Top tray', 'bottom':'Bottom tray', 'envelope':'Envelope feeder',
        'largecapacity':'Large capacity tray',
    }
    if key in labels:
        return labels[key]
    tray=re.fullmatch(r'(?:tray|cassette|drawer|source|bin)(\d+)',key)
    if tray:
        return 'Tray '+tray.group(1)
    clean=re.sub(r'([a-z])([A-Z])',r'\1 \2',value.replace('_',' ').replace('-',' '))
    clean=re.sub(r'(?<=[A-Za-z])(?=\d)', ' ', clean)
    return clean[:80]


def cups_capabilities(server: str, queue: str) -> dict:
    validate_queue(queue)
    return cached(('cups','caps',server,queue),
                  lambda caps: CAPABILITY_TTL if caps['known'] else FAILED_PROBE_TTL,
                  lambda: read_cups_capabilities(server, queue))


def read_cups_capabilities(server: str, queue: str) -> dict:
    validate_queue(queue)
    result=cups_run(['lpoptions','-h',server,'-p',queue,'-l'])
    caps={'known':False, 'media':[], 'duplex':None, 'color':None,
          'color_modes':['auto','gray','bw'], 'trays':[], 'resolution':[],
          'source_option':None, 'paper_sources':[], 'source_default':None,
          'media_option':None, 'media_map':{},
          'envelope_type_option':None, 'envelope_type_value':None, 'raw':{}}
    if result.returncode != 0:
        return caps
    parsed={}
    source_rows={}
    for line in result.stdout.splitlines():
        if ':' not in line:
            continue
        key=line.split(':',1)[0].split('/',1)[0].strip()
        # lpoptions commonly returns InputSlot/Paper Source, but older PPDs
        # and some custom queues use InputSlot without a descriptive /label.
        # In both cases we keep the exact option name CUPS understands.
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', key):
            continue
        raw=line.split(':',1)[1].strip()
        key_lower=key.lower()
        parsed[key_lower]=[token.lstrip('*').split('/',1)[0] for token in raw.split()]
        source_rows[key_lower]=(key,raw)
    caps['known']=bool(parsed)
    caps['raw']={k:v for k,v in parsed.items()
                 if k in ('pagesize','duplex','sides','colormodel',
                          'print-color-mode','inputslot','resolution',
                          'media-source','mediasource','mediaposition','papersource')}
    # Use the actual PageSize/IPP media tokens. Older Samsung PPDs often
    # advertise 'Env10' instead of CUPS's generic 'COM10'.
    option_name=next((source_rows[key][0] for key in ('pagesize','media')
                      if key in source_rows), None)
    options_raw=next((source_rows[key][1] for key in ('pagesize','media')
                      if key in source_rows), '')
    sizes=[item['value'] for item in cups_choice_values(options_raw)] if options_raw else []
    if not sizes:
        sizes=parsed.get('pagesize',[]) or parsed.get('media',[])
    caps['media']=sizes
    caps['media_option']=option_name
    caps['media_map']=printing.match_media_choices(sizes)
    # Choose Envelope media type only if the queue explicitly advertises it.
    media_type_name=source_rows.get('mediatype', (None,''))[0]
    media_types=cups_choice_values(source_rows.get('mediatype',(None,''))[1])
    envelope_type=next((item['value'] for item in media_types
                        if re.sub(r'[^a-z0-9]', '', item['value'].lower()) == 'envelope'),None)
    if envelope_type:
        caps['envelope_type_option']=media_type_name
        caps['envelope_type_value']=envelope_type
    duplex_opts=parsed.get('sides',[])+parsed.get('duplex',[])
    caps['duplex']=any(x.lower() not in ('none','one-sided','false','off')
                        and ('two-sided' in x.lower() or 'duplex' in x.lower()) for x in duplex_opts) if duplex_opts else None
    mode_opts=parsed.get('print-color-mode',[])+parsed.get('colormodel',[])
    color_options=('color','rgb','cmyk','srgb','adobergb','devicecolor')
    caps['color']=any(x.lower() in color_options for x in mode_opts) if mode_opts else None
    if caps['color'] is not False:
        caps['color_modes']=['auto','color','gray','bw']
    for option in PAPER_SOURCE_KEYS:
        if option not in source_rows:
            continue
        actual_key,raw=source_rows[option]
        values=cups_choice_values(raw)
        if not values:
            continue
        caps['source_option']=actual_key  # Exact option name, case preserved
        caps['paper_sources']=[{'value':v['value'],
                                'label':paper_source_label(v['value']),
                                'is_default':v['default']}
                               for v in values]
        caps['source_default']=next((v['value'] for v in values if v['default']),None)
        caps['trays']=[v['value'] for v in values]
        break
    caps['resolution']=parsed.get('resolution',[])
    return caps


def printer_settings(registry: dict, printer_id: str, legacy_queue: str = '') -> dict:
    # Scoped ID prevents duplicate queue names on different servers colliding.
    # Previously persisted queue-only permissions apply only to the first server
    # and only if explicitly migrated by the caller.
    saved = registry.get('printers', {})
    return dict(saved.get(printer_id, {'enabled':True, 'guest':False,
                                       'member':True, 'printer':True}))


def visible_queues(registry: dict, queues: list[dict], role: str, legacy_queue: str = '') -> list[dict]:
    result=[]
    for record in queues:
        flags=printer_settings(registry,record['id'],legacy_queue)
        if not flags['enabled']:
            continue
        if role=='guest' and not flags['guest']:
            continue
        if role in ('member','printer') and not flags[role]:
            continue
        result.append({**record,'guest':bool(flags['guest']), 'allowed':True})
    return result



# CUPS is used only through supported client CLI flags; Folio cannot change
# queues, install drivers, or choose a server supplied by a browser.
CUPS_SERVER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?$')


def parse_cups_servers(raw: str) -> list[str]:
    servers=[]
    for value in (raw or '').split(','):
        server=value.strip()
        if not server:
            continue
        if not CUPS_SERVER_RE.fullmatch(server) or '..' in server:
            raise ValueError(f'Invalid CUPS server: {server[:80]}')
        if ':' in server and not 1 <= int(server.rsplit(':',1)[1]) <= 65535:
            raise ValueError('CUPS port must be between 1 and 65535')
        if server not in servers:
            servers.append(server)
    if len(servers)>16:
        raise ValueError('Configure no more than 16 CUPS servers')
    return servers


def validate_printer_reference(identity: str) -> None:
    if '|' in identity:
        server, queue = identity.split('|', 1)
        if not CUPS_SERVER_RE.fullmatch(server):
            raise ValueError('Invalid printer server')
    else:
        queue = identity
    validate_queue(queue)


def printer_id(server: str, queue: str) -> str:
    validate_queue(queue)
    return f'{server}|{queue}'


def all_cups_queues(servers: list[str]) -> list[dict]:
    def server_queues(server: str) -> list[dict]:
        return cached(('cups','queues',server), QUEUE_TTL, lambda: list_cups_queues(server))
    # Query servers concurrently so one slow server doesn't delay the others.
    if len(servers) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(servers))) as pool:
            listings = list(pool.map(server_queues, servers))
    else:
        listings = [server_queues(s) for s in servers]
    found=[]
    for server, queues in zip(servers, listings):
        for record in queues:
            found.append({**record, 'server':server,
                          'id':printer_id(server,record['queue']),
                          'label':f"{record['queue']} ({server})"})
    return found


def resolve_printer(identity: str | None, records: list[dict]) -> dict | None:
    if not identity:
        return None
    exact=next((r for r in records if r['id']==identity),None)
    if exact:
        return exact
    # Support the unqualified queue name used in v0.9.1 databases only when
    # there is exactly one match. Never silently choose between duplicates.
    if '|' not in identity and QUEUENAME.fullmatch(identity):
        candidates=[r for r in records if r['queue']==identity]
        if len(candidates)==1:
            return candidates[0]
    return None


def migrate_legacy_printers(registry: dict, records: list[dict], legacy_queue: str = '') -> bool:
    """Migrate queue-only permission/default keys when unambiguous."""
    changed=False
    flags=registry.setdefault('printers', {})
    for key in list(flags):
        if '|' in key:
            continue
        matching=[r for r in records if r['queue']==key]
        if len(matching)==1:
            flags.setdefault(matching[0]['id'], flags[key])
            del flags[key]
            changed=True
    old=registry.get('default_printer')
    if old and '|' not in old:
        found=resolve_printer(old, records)
        if found:
            registry['default_printer']=found['id']
            changed=True
    # A legacy install could have guest permission implicitly tied to a single
    # CUPS_QUEUE. Only preserve that for an explicitly configured legacy queue.
    if legacy_queue:
        matched=resolve_printer(legacy_queue,records)
        if matched and matched['id'] not in flags and not registry.get('printers'):
            flags[matched['id']]={'enabled':True,'guest':True,'member':True,'printer':True}
            changed=True
    return changed
