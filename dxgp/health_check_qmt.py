# -*- coding: utf-8 -*-
"""
QMT 行情接口体检工具  health_check_qmt.py
================================================================
配合母版 zt_shrink_strategy_gbk.py 使用, 在 miniQMT Windows 主机上用【普通 Python】运行,
不需要在 QMT 内执行, 也不导入 xtquant (纯网络 + 进程诊断)。

它回答四个问题:
  1) 行情服务器通不通、延迟多少  -> 逐台 TCP 握手计时 + 多次取样
  2) 有没有多实例在抢同一个账号  -> 扫描所有 XtItClient 进程及其实例目录 + 心跳文件
  3) 网络本身是不是瓶颈          -> 对比外网基准延迟(可选)
  4) 结论与处置建议              -> 直接打印"该关哪个进程"

用法:
    python health_check_qmt.py                      # 完整体检
    python health_check_qmt.py --rounds 5           # 每台服务器测 5 次(默认3)
    python health_check_qmt.py --no-internet        # 不测外网基准
    python health_check_qmt.py --out report.txt     # 结果同时写文件

退出码: 0=健康  1=发现多实例(高危)  2=行情服务器普遍超时  3=两者都有
"""

import argparse
import datetime
import io
import json
import os
import re
import socket
import subprocess
import sys
import time

# 某券商 QMT 行情/交易服务器端口(从实盘连接快照取得, 58000-59100 区间为行情主通道)
QMT_HOSTS = [
    ('119.145.97.213', 59000, '行情主通道(广东电信)'),
    ('183.237.219.239', 59000, '行情主通道(广东移动)'),
    ('183.237.219.239', 56000, '备用通道'),
    ('42.228.16.211', 55300, '行情通道(河南)'),
    ('43.242.96.164', 55300, '行情通道(备用)'),
    ('58.255.39.148', 56000, '行情通道(广东)'),
]
INTERNET_BASE = [('www.baidu.com', 443), ('qt.gtimg.cn', 80)]

# 母版日志里"常态约2秒/批"作为基准, 超过 3 倍即认为异常
BATCH_BASELINE_SEC = 2.0
SLOW_MULT = 3.0


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def tcp_probe(host, port, timeout=5.0):
    """返回 (是否连通, 握手毫秒, 错误描述)"""
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        ms = (time.time() - t0) * 1000.0
        return (True, ms, '')
    except socket.timeout:
        return (False, timeout * 1000.0, 'timeout %.1fs' % timeout)
    except Exception as e:
        return (False, (time.time() - t0) * 1000.0, type(e).__name__)
    finally:
        try:
            s.close()
        except Exception:
            pass


def _ps_process_list():
    """PowerShell 列出 XtItClient 进程(pid|ppid|CreationDate|ExecutablePath)"""
    ps = (
        'powershell.exe -NoProfile -NonInteractive -Command "'
        'Get-CimInstance Win32_Process -Filter \\\"Name=\'XtItClient.exe\'\\\" | '
        'ForEach-Object { \'\' + $_.ProcessId + \'|\' + $_.ParentProcessId + '
        '\'|\' + $_.CreationDate.ToString(\'yyyy-MM-dd HH:mm:ss\') + '
        '\'|\' + $_.ExecutablePath }"'
    )
    try:
        r = subprocess.run(ps, shell=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=40)
        raw = r.stdout or b''
        s = ''
        for enc in ('gbk', 'utf-8'):
            try:
                s = raw.decode(enc)
                break
            except Exception:
                continue
        if not s:
            return ''
        return s.strip() if 'XtItClient' in s else ''
    except Exception:
        return ''


def scan_instances():
    """扫描所有 XtItClient 进程, 判断是否多实例"""
    out = []
    # Windows 11 已移除 wmic, 先试 PowerShell Get-CimInstance
    txt = _ps_process_list()
    if txt:
        for ln in txt.splitlines():
            ln = ln.strip()
            if not ln or '|' not in ln:
                continue
            parts = [x.strip() for x in ln.split('|')]
            if len(parts) < 4 or 'XtItClient' not in parts[3]:
                continue
            out.append({'pid': parts[0], 'ppid': parts[1],
                        'created': parts[2], 'exe': parts[3]})
        return out
    try:
        r = subprocess.run(
            ['wmic', 'process', 'where', "name='XtItClient.exe'", 'get',
             'ProcessId,ParentProcessId,ExecutablePath,CreationDate', '/format:csv'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        raw = r.stdout or b''
        txt = ''
        for enc in ('gbk', 'utf-8'):
            try:
                txt = raw.decode(enc)
                break
            except Exception:
                continue
        if not txt:
            txt = raw.decode('gbk', errors='replace')
        for ln in txt.splitlines():
            ln = ln.strip()
            if not ln or ln.lower().startswith('node'):
                continue
            parts = ln.split(',')
            if len(parts) < 5:
                continue
            exe = parts[-1]
            if 'XtItClient' not in exe:
                continue
            pid = parts[-2]
            ppid = parts[-3]
            cd = parts[-4]
            out.append({'pid': pid, 'ppid': ppid, 'exe': exe, 'created': cd})
    except Exception as e:
        out.append({'error': repr(e)[:160]})
    return out


def hb_dirs_of(root):
    """心跳文件落在策略脚本所在目录(bin.x64), 故返回实例目录+一级子目录"""
    out = []
    for d in (root, os.path.join(root, 'bin.x64'),
              os.path.join(root, 'userdata_mini')):
        if d and os.path.isdir(d):
            out.append(d)
    return out


def read_json_safe(p):
    try:
        with io.open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def scan_heartbeats(dirs):
    """扫描各实例目录下的策略心跳文件, 判断哪个实例在跑策略"""
    found = []
    real = []
    for r in dirs:
        for d in hb_dirs_of(r):
            if d not in real:
                real.append(d)
    for d in real:
        if not d or not os.path.isdir(d):
            continue
        try:
            for fn in os.listdir(d):
                if not fn.startswith('zt_shrink_heartbeat_') or not fn.endswith('.json'):
                    continue
                p = os.path.join(d, fn)
                try:
                    st = os.stat(p)
                    age = time.time() - st.st_mtime
                    m = re.search(r'_(\d+)\.json$', fn)
                    found.append({'file': fn, 'pid': (m.group(1) if m else '?'),
                                  'age_sec': age, 'dir': d, 'size': st.st_size})
                except Exception:
                    pass
        except Exception:
            pass
    return sorted(found, key=lambda x: x['age_sec'])


def instance_root(exe):
    """从 exe 路径推出实例根目录(去掉 \\bin.x64\\XtItClient.exe)"""
    try:
        return os.path.dirname(os.path.dirname(exe))
    except Exception:
        return ''


def fmt_age(sec):
    if sec < 60:
        return '%.0f秒前' % sec
    if sec < 3600:
        return '%.0f分钟前' % (sec / 60.0)
    if sec < 86400:
        return '%.1f小时前' % (sec / 3600.0)
    return '%.1f天前' % (sec / 86400.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=3, help='每台服务器探测次数(默认3)')
    ap.add_argument('--timeout', type=float, default=5.0, help='单次握手超时秒(默认5)')
    ap.add_argument('--no-internet', action='store_true', help='跳过外网基准测试')
    ap.add_argument('--out', default='', help='结果同时写入该文件')
    args = ap.parse_args()

    buf = []

    def P(s=''):
        buf.append(s)
        try:
            sys.stdout.write(s + '\n')
            sys.stdout.flush()
        except Exception:
            pass

    P('=' * 74)
    P('QMT 行情接口体检  %s' % now_str())
    P('=' * 74)
    P()

    # ---------- 1) 多实例检测 ----------
    P('【1】多实例检测(最关键的一项)')
    inst = scan_instances()
    real = [x for x in inst if 'exe' in x]
    if not real:
        P('  ! 没能列出 XtItClient 进程(wmic 不可用或被拦), 请手工在任务管理器确认')
        for e in inst:
            if 'error' in e:
                P('    原因: %s' % e['error'])
    else:
        P('  发现 %d 个 XtItClient 进程:' % len(real))
        roots = {}
        for x in real:
            root = instance_root(x['exe'])
            roots.setdefault(root, []).append(x)
            P('    PID=%s  实例目录=%s' % (x['pid'], root or '?'))
        P()
        if len(roots) > 1:
            P('  >>> [高危] 检测到 %d 个不同实例目录同时运行 QMT <<<' % len(roots))
            P('      多实例会各自向后端行情服务器订阅同一批数据, 触发券商侧限流,')
            P('      表现就是"取数每批从 2 秒变 30~40 秒"(实测慢 15~21 倍)。')
            P('      处置: 只保留运行策略的那一个, 其余全部退出(见下方【4】)。')
        else:
            P('  [正常] 只有一个实例目录在运行')
        P('  各实例目录下的策略心跳:')
        hb = scan_heartbeats(list(roots.keys()))
        if not hb:
            P('    (无心跳文件)')
        for h in hb:
            alive = '活跃(策略在线)' if h['age_sec'] < 120 else '已退出'
            P('    %s | PID=%s | 写入: %s | %s' % (h['file'], h['pid'],
                                               fmt_age(h['age_sec']), alive))
            P('        目录: %s' % h['dir'])
    P()

    # ---------- 2) 行情服务器延迟 ----------
    P('【2】行情服务器握手延迟(每台 %d 次)' % args.rounds)
    slow_hosts = 0
    dead_hosts = 0
    for host, port, note in QMT_HOSTS:
        results = []
        for _ in range(max(1, args.rounds)):
            ok, ms, err = tcp_probe(host, port, args.timeout)
            results.append((ok, ms, err))
            time.sleep(0.15)
        okc = sum(1 for r in results if r[0])
        if okc == 0:
            dead_hosts += 1
            P('  [不通] %-16s:%-5d %-18s 全部 %d 次失败(%s)'
              % (host, port, note, len(results), results[0][2]))
        else:
            good = [r[1] for r in results if r[0]]
            avg = sum(good) / len(good)
            mx = max(good)
            flag = ''
            if avg > 200:
                flag = '  <== 偏慢'
                slow_hosts += 1
            P('  [%s] %-16s:%-5d %-18s 成功%d/%d 平均%.0fms 最慢%.0fms%s'
              % ('OK' if avg <= 200 else '慢', host, port, note,
                 okc, len(results), avg, mx, flag))
    P()

    # ---------- 3) 外网基准 ----------
    if not args.no_internet:
        P('【3】外网基准(判断是本机网络问题还是券商侧问题)')
        for host, port in INTERNET_BASE:
            ok, ms, err = tcp_probe(host, port, args.timeout)
            if ok:
                verdict = '本机网络正常' if ms < 150 else '本机网络偏慢'
                P('  %-20s:%-4d %.0fms  (%s)' % (host, port, ms, verdict))
            else:
                P('  %-20s:%-4d 失败(%s)  <== 本机网络或防火墙异常' % (host, port, err))
        P()

    # ---------- 4) 结论 ----------
    P('【4】结论与处置')
    multi = (len(real) > 1 and len({instance_root(x['exe']) for x in real}) > 1)
    if multi:
        P('  根因: 多实例争用(高置信度)')
        # 心跳文件名里带 PID, 直接和进程 PID 对, 谁在写谁就是策略宿主
        live = []
        for h in scan_heartbeats([instance_root(x['exe']) for x in real]):
            if h['age_sec'] < 120:
                live.append(h)
        keep = None
        if live:
            best = live[0]
            P('  判定依据: 心跳文件 %s 于 %s 写入(该目录下的策略正在跑)'
              % (best['file'], fmt_age(best['age_sec'])))
            for x in real:
                if x['pid'] == best['pid']:
                    keep = x
                    break
            if keep is None:
                # 心跳 PID 对不上进程(策略跑在 QMT 内嵌解释器里, PID 可能不同)
                for x in real:
                    if instance_root(x['exe']) == os.path.dirname(best['dir']) \
                            or instance_root(x['exe']) == best['dir']:
                        keep = x
                        break
                if keep is not None:
                    P('    心跳 PID=%s 与进程 PID 不同(策略跑在内嵌解释器), '
                      '按目录匹配认定为 PID=%s' % (best['pid'], keep['pid']))
        if keep:
            P('  结论: 应保留 PID=%s (%s)' % (keep['pid'], instance_root(keep['exe'])))
            extra = [x for x in real if x['pid'] != keep['pid']]
            if not extra:
                P('  其余实例目录下没有活跃心跳, 属于纯行情客户端(没跑策略)')
            for x in extra:
                P('    应关闭 PID=%s (%s)' % (x['pid'], instance_root(x['exe'])))
            P('  PowerShell 关闭命令(确认无误后再执行):')
            for x in extra:
                P('       Stop-Process -Id %s -Force' % x['pid'])
        else:
            P('  3. ! 没找到任何活跃心跳, 无法自动判定; 请手工看任务管理器')
            P('     (若策略此刻没在跑, 两个实例都只是行情客户端, 关掉不用的那个即可)')
        P('  预期: 关掉多余实例后, 取数应回到每批 2 秒左右(全市场约 40 秒)')
    elif dead_hosts >= len(QMT_HOSTS) - 1:
        P('  结论: 行情服务器整体不可达 -> 检查本机网络/防火墙/是否挂了 VPN')
    elif slow_hosts >= 2:
        P('  结论: 行情服务器握手普遍偏慢(可能本机网络问题, 看第3段外网基准)')
    else:
        P('  未发现明显异常。若策略日志仍显示每批 30 秒以上, 请:')
        P('    - 检查 QMT 客户端左下角行情连接状态是否显示"已连接"')
        P('    - 检查是否在 QMT 里开了多个策略实例(同一账号)')
        P('    - 检查券商端是否对 5226 只全市场订阅做了限流')
    P()
    P('=' * 74)

    if args.out:
        try:
            with io.open(args.out, 'w', encoding='utf-8') as f:
                f.write('\n'.join(buf) + '\n')
            P('结果已写入: %s' % args.out)
        except Exception as e:
            P('写文件失败: %r' % e)

    if multi:
        return 3 if dead_hosts else 1
    if dead_hosts >= len(QMT_HOSTS) - 1:
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
