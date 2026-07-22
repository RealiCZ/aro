#!/usr/bin/env python3
import datetime
import os
import pathlib
import pexpect
import signal
import time

RUN = pathlib.Path('/nvme2/mega-engineer/workspace/aro/.aro-runs/a6ee3a9b-livelock-investigation-20260721')
BIN = (RUN / 'test-binary.txt').read_text().strip()
OUTDIR = RUN / 'gdb' / 'retry-until-hung'
OUTDIR.mkdir(parents=True, exist_ok=True)

def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def setup(attempt):
    env = os.environ.copy()
    env['NUM_DATA_BUCKETS'] = '2'
    env['BUCKET_RESIZE_LOAD_FACTOR_PCT'] = '1'
    child = pexpect.spawn('gdb', ['-q', '-nx', '--args', BIN, '--nocapture'], env=env,
                          encoding='utf-8', timeout=60)
    log = (OUTDIR / f'attempt-{attempt:02d}.gdb.log').open('w', encoding='utf-8')
    child.logfile = log
    child.expect_exact('(gdb) ')
    for cmd in ['set pagination off', 'set confirm off', 'set print thread-events off',
                'set print demangle off', 'set print asm-demangle off',
                'set print frame-arguments none']:
        child.sendline(cmd)
        child.expect_exact('(gdb) ')
    return child, log

for attempt in range(1, 11):
    child, log = setup(attempt)
    log.write(f'=== ATTEMPT_{attempt}_START {utc()} ===\n')
    child.sendline('run')
    try:
        child.expect_exact('(gdb) ', timeout=20)
    except pexpect.TIMEOUT:
        log.write(f'=== HANG_WINDOW_CONFIRMED {utc()} ===\n')
        for snap in range(1, 4):
            os.kill(child.pid, signal.SIGINT)
            child.expect_exact('(gdb) ', timeout=30)
            log.write(f'=== SNAPSHOT_{snap} {utc()} ===\n')
            child.sendline('info threads')
            child.expect_exact('(gdb) ', timeout=60)
            child.sendline('thread apply all bt 12')
            child.expect_exact('(gdb) ', timeout=120)
            if snap < 3:
                child.sendline('continue')
                time.sleep(30)
        child.sendline('kill')
        child.expect_exact('(gdb) ', timeout=30)
        child.sendline('quit')
        child.expect(pexpect.EOF, timeout=30)
        log.close()
        (OUTDIR / 'successful-attempt.txt').write_text(f'{attempt}\n')
        print(OUTDIR / f'attempt-{attempt:02d}.gdb.log')
        raise SystemExit(0)
    else:
        log.write(f'=== COMPLETED_WITHIN_20S {utc()} ===\n')
        child.sendline('quit')
        child.expect(pexpect.EOF, timeout=30)
        log.close()

(OUTDIR / 'successful-attempt.txt').write_text('none\n')
raise SystemExit('no hang reproduced in 10 attempts')
