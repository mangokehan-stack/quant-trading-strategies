# -*- coding: utf-8 -*-
# =====================================================================
# Daily 05:00 task runner - drives both download tools in one task
#   step 1: refresh trading calendar   (download_trading_calendar.py)
#   step 2: rebuild risk exclude list  (download_risk_exclude.py)
# Notes:
#   - steps are independent: a calendar failure never blocks the risk list
#   - every step records its own exit code and elapsed seconds
#   - the runner's own log is zt_shrink_daily_task_runner.log
#     step output goes to zt_shrink_daily_task.log (the tool keeps its own file)
#   - no popup window
#   - the calendar tool keeps the old file untouched if all sources fail
# Exit code: 0 = all steps ok or skipped, 1 = at least one step failed
# Changelog:
#   V1.1.0  2026-10-20  split own log vs step log so a long risk-exclude run
#                       can still be tailed while it is writing
# =====================================================================
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PY = r'C:\Program Files\Python311\python.exe'
LOG = os.path.join(BASE, 'zt_shrink_daily_task_runner.log')
STEP_LOG = os.path.join(BASE, 'zt_shrink_daily_task.log')
ACCOUNT = '<acct>'


def wlog(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with open(LOG, 'a', encoding='utf-8', errors='replace') as f:
            f.write(line + '\n')
    except Exception:
        pass
    try:
        sys.stdout.write(line + '\r\n')
        sys.stdout.flush()
    except Exception:
        pass


def run_step(title, script, args, timeout_sec):
    sp = os.path.join(BASE, script)
    if not os.path.exists(sp):
        wlog('[%s][skip] script not found: %s' % (title, sp))
        return (True, None, 0.0)
    cmd = [PY, '-u', sp] + list(args)
    wlog('[%s] start: %s %s' % (title, script, ' '.join(args)))
    t0 = time.time()
    fo = None
    try:
        # 子进程输出直接追加到步骤日志(而不是先缓存在内存里),
        # 这样排雷跑 10 分钟也随时能 tail 到进度
        fo = open(STEP_LOG, 'a', encoding='utf-8', errors='replace')
        fo.write('\n[%s] ---- start %s ----\r\n'
                 % (time.strftime('%Y-%m-%d %H:%M:%S'), script))
        fo.flush()
        r = subprocess.run(cmd, cwd=BASE, stdout=fo, stderr=subprocess.STDOUT,
                           timeout=timeout_sec)
        el = time.time() - t0
        code = r.returncode
        try:
            fo.write('[%s] ---- done exit=%s elapsed=%.1fs ----\r\n'
                     % (time.strftime('%Y-%m-%d %H:%M:%S'), code, el))
            fo.flush()
        except Exception:
            pass
        wlog('[%s] done: exit=%s elapsed=%.1fs' % (title, code, el))
        return (code == 0, code, el)
    except subprocess.TimeoutExpired:
        el = time.time() - t0
        wlog('[%s][FAIL] timeout after %ds' % (title, timeout_sec))
        return (False, 'TIMEOUT', el)
    except Exception as e:
        wlog('[%s][FAIL] exception: %s' % (title, repr(e)[:200]))
        return (False, 'EXC', 0.0)
    finally:
        try:
            if fo is not None:
                fo.close()
        except Exception:
            pass


def main():
    t_all = time.time()
    wlog('=' * 60)
    wlog('[daily-task] start (account=%s) v1.1.0' % ACCOUNT)
    wlog('[daily-task] step log -> %s' % STEP_LOG)

    ok1, c1, e1 = run_step('calendar', 'download_trading_calendar.py', [], 300)
    ok2, c2, e2 = run_step('risk-exclude', 'download_risk_exclude.py',
                           ['--account', ACCOUNT, '--ann-pages', '600'], 1800)

    el = time.time() - t_all
    if ok1 and ok2:
        wlog('[daily-task] all done, total %.1fs' % el)
        return 0
    wlog('[daily-task][WARN] failed steps: calendar=%s risk=%s, total %.1fs'
         % (c1 if not ok1 else 'OK', c2 if not ok2 else 'OK', el))
    return 1


if __name__ == '__main__':
    sys.exit(main())
