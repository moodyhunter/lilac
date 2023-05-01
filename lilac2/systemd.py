import os
import subprocess
from typing import Generator, Any
import select
import time
import logging
import threading

from .typing import Cmd, RUsage

logger = logging.getLogger(__name__)

_available = None
_check_lock = threading.Lock()


def available() -> bool:
    global _available

    with _check_lock:
        if _available is None:
            _available = _check_availability()
            logger.debug('systemd availability: %s', _available)
    return _available


def _check_availability() -> bool:
    if 'DBUS_SESSION_BUS_ADDRESS' not in os.environ:
        dbus = f'/run/user/{os.getuid()}/bus'
        if not os.path.exists(dbus):
            return False
        os.environ['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={dbus}'
    p = subprocess.run(['systemd-run', '--quiet', '--user',
                       '-u', 'lilac-check', 'true'])
    return p.returncode == 0


def start_cmd(
    name: str, cmd: Cmd,
    setenv: dict[str, str] = {},
    **kwargs: Any,  # can't use P.kwargs here because there is no place for P.args
) -> subprocess.Popen:
    # don't use --collect here because it will be immediately collected when
    # failed
    cmd_s: Cmd = [
        'systemd-run', '--pipe', '--quiet', '--user',
        '--wait', '--remain-after-exit', '-u', name,
        '-p', 'CPUWeight=100',
    ]

    if cwd := kwargs.pop('cwd', None):
        cmd_s += [f'--working-directory={str(cwd)}']  # type: ignore

    cmd_setenv = [f'--setenv={k}={v}' for k, v in setenv.items()]
    cmd_s = cmd_s + cmd_setenv + ['--'] + cmd  # type: ignore
    logger.debug('running %s', subprocess.list2cmdline(cmd_s))
    return subprocess.Popen(cmd_s, **kwargs)


def _get_service_info(name: str) -> tuple[int, str, str]:
    '''return pid and control group path'''
    out = subprocess.check_output([
        'systemctl', '--user', 'show', f'{name}.service',
        '--property=MainPID',
        '--property=ControlGroup',
        '--property=SubState',
    ], text=True)
    pid = 0
    cgroup = ''
    state = ''
    for l in out.splitlines():
        k, v = l.split('=', 1)
        if k == 'MainPID':
            pid = int(v)
        elif k == 'ControlGroup':
            cgroup = v
        elif k == 'SubState':
            state = v
    return pid, cgroup, state


def _poll_cmd(pid: int) -> Generator[None, None, None]:
    try:
        pidfd = os.pidfd_open(pid)
    except OSError as e:
        if e.errno == 22:
            return

    poll = select.poll()
    poll.register(pidfd, select.POLLIN)

    try:
        while True:
            ret = poll.poll(1_000)
            if ret:
                logger.debug('worker exited')
                return
            yield
    finally:
        os.close(pidfd)


def poll_rusage(name: str, deadline: float) -> tuple[RUsage, bool]:
    timedout = False
    done_state = ['exited', 'failed']

    try:
        time_start = time.monotonic()
        while True:
            pid, cgroup, state = _get_service_info(name)
            if (not pid or not cgroup) and state not in done_state:
                if time.monotonic() - time_start > 60:
                    logger.error(
                        '%s.service not started in 60s, giving up.', name)
                    raise Exception(
                        'systemd error: service not started in 60s')
                logger.debug('%s.service state: %s, waiting', name, state)
                time.sleep(0.1)
            else:
                break

        if state in done_state:
            logger.warning('%s.service already finished: %s', name, state)
            return RUsage(0, 0), False

        mem_file = f'/sys/fs/cgroup{cgroup}/memory.peak'

        mem_max = 0
        for _ in _poll_cmd(pid):
            with open(mem_file) as f:
                mem_cur = int(f.read().rstrip())
                mem_max = max(mem_cur, mem_max)
            if time.time() > deadline:
                timedout = True
                break

        # systemd will remove the cgroup as soon as the process exits
        # instead of racing with systemd, we just ask it for the data
        nsec = 0
        out = subprocess.check_output([
            'systemctl', '--user', 'show', f'{name}.service',
            '--property=CPUUsageNSec',
        ], text=True)
        for l in out.splitlines():
            k, v = l.split('=', 1)
            if k == 'CPUUsageNSec':
                nsec = int(v)

    finally:
        if timedout:
            logger.debug('killing worker service')
            subprocess.run(['systemctl', '--user', 'kill',
                           '--signal=SIGKILL', name])
        logger.debug('stopping worker service')
        # stop whatever may be running (even from a previous batch)
        subprocess.run(['systemctl', '--user', 'stop', '--quiet', name])
        if cgroup:
            # if we actually got the cgroup (i.e. service was started when we looked)
            wait_cgroup_disappear(cgroup)

        p = subprocess.run(
            ['systemctl', '--user', 'is-failed', '--quiet', name])
        if p.returncode == 0:
            subprocess.run(
                ['systemctl', '--user', 'reset-failed', '--quiet', name])
    return RUsage(nsec / 1_000_000_000, mem_max), timedout


def wait_cgroup_disappear(cgroup: str) -> None:
    d = f'/sys/fs/cgroup/{cgroup}'
    if not os.path.exists(d):
        return

    while os.path.exists(d):
        logger.warning('waiting %s to disappear...', cgroup)
        time.sleep(1)
