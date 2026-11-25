"""Unified ASIO-only output policy for Tracks, Notes, and Score.

Priority:
  1. Explicitly saved/selected registered ASIO driver (if any).
  2. Registered physical hardware ASIO drivers (device_catalog kind == 'hardware').
  3. Registered ASIO4ALL driver as fallback.
Never falls back to WASAPI (shared or RAW) or Qt MediaPlayer.
"""
from . import device_catalog

ENGINE_RATE = 48000


def _hardware_sort_key(row):
    name = (row.get('name') or '').lower()
    # Prefer USB hardware interfaces before Thunderbolt/PCIe when both vendor
    # drivers are installed, while still trying every registered hardware driver.
    if 'usb' in name:
        rank = 0
    elif 'thunderbolt' in name or 'pcie' in name:
        rank = 2
    else:
        rank = 1
    return (rank, name)


def _is_asio4all(row):
    text = ((row.get('name') or '') + ' ' + (row.get('description') or '')).lower()
    return 'asio4all' in text


def ordered_asio_candidates(setup=None, drivers=None):
    """Return ordered list of registered ASIO driver names: physical first, ASIO4ALL fallback."""
    rows = device_catalog.asio_drivers() if drivers is None else list(drivers)
    registered = [r for r in rows if r.get('state') == 'registered' and isinstance(r.get('name'), str) and r['name'].strip()]
    by_name = {r['name']: r for r in registered}
    ordered = []

    def add(name):
        if name and name in by_name and name not in ordered:
            ordered.append(name)

    if setup is not None and getattr(setup, 'backend', None) == 'asio' and getattr(setup, 'device', None):
        add(setup.device)

    hardware = sorted((r for r in registered if r.get('kind') == 'hardware'), key=_hardware_sort_key)
    for row in hardware:
        add(row['name'])

    for row in sorted((r for r in registered if _is_asio4all(r)), key=lambda r: r['name'].lower()):
        add(row['name'])

    return ordered


def resolve_asio_device_route(setup=None, choices=None, active_endpoint=None):
    """Build a verified ASIO device_route dict with ordered fallback candidates.

    Reuses active_endpoint.device directly if it is already open and satisfies
    the requested setup. Never returns WASAPI.
    """
    sample_rate = getattr(setup, 'sample_rate', None) or ENGINE_RATE
    buffer_frames = 0 if (setup is None or getattr(setup, 'buffer_frames', None) is None) else int(setup.buffer_frames)
    explicit_asio = setup is not None and getattr(setup, 'backend', None) == 'asio' and getattr(setup, 'device', None)

    if active_endpoint is not None and getattr(active_endpoint, 'handle', None) and not getattr(active_endpoint, 'closing', False):
        ep_dev = getattr(active_endpoint, 'device', None)
        ep_info = getattr(active_endpoint, 'info', None)
        if isinstance(ep_dev, dict) and ep_dev.get('backend') == 'asio' and ep_dev.get('sample_rate') == sample_rate:
            if ep_dev.get('buffer_frames') == buffer_frames:
                ep_name = (ep_info or {}).get('device') or (ep_dev.get('native_selector') or {}).get('exact_device')
                if not explicit_asio or ep_name == setup.device:
                    return dict(ep_dev)

    if choices is None:
        runtime = device_catalog.asio_runtime()
        if not runtime['available']:
            raise ValueError(runtime['note'] or 'ASIO 运行时不可用。')
        asio_rows = device_catalog.asio_drivers()
        asio_choices = [('asio', row) for row in asio_rows if row.get('state') == 'registered']
    else:
        asio_choices = [c for c in choices if c[0] == 'asio']
        asio_rows = [c[1] for c in asio_choices]

    candidates = ordered_asio_candidates(setup, asio_rows)
    if not candidates:
        raise ValueError('未找到可用的物理 ASIO 或 ASIO4ALL 驱动（当前策略已禁用 WASAPI）。')

    primary_name = candidates[0]
    match = next((c for c in asio_choices if c[1].get('name') == primary_name), None)
    if match is None:
        raise ValueError(f'找不到已注册的 ASIO 驱动：{primary_name}')

    from .vst_project import device_route
    route = device_route(match, sample_rate, buffer_frames, [0, 1])
    route['asio_candidates'] = tuple(candidates)
    return route
