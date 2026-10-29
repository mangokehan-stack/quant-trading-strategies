# -*- coding: utf-8 -*-
"""
set_daily_buys.py —— 外部禁买/买入计数工具 (GBK 母版 zt_shrink_strategy_gbk.py 配套)
================================================================================
版本:     V1.0.0 (2026-10-19 重建)
配套母版: zt_shrink_strategy_gbk.py V2.6.81
运行方式: miniQMT Windows 主机上, 用普通 Python 直接运行 (不需要 xtquant, 不传入 QMT):
              python set_daily_buys.py                  # 查看当前状态(默认)
              python set_daily_buys.py --mode BLOCK     # 今日起禁止开新仓(手工封锁)
              python set_daily_buys.py --mode CLEAR     # 解除封锁 + 当日买入计数归零
              python set_daily_buys.py --mode SET --n 2 # 把"今日已买笔数"直接设为 2
              python set_daily_buys.py --mode ADD       # "今日已买笔数" +1
              python set_daily_buys.py --paper          # 以上模式改操作 paper(模拟)桶
              python set_daily_buys.py --date 20261020  # 操作指定日期(默认今天)
              python set_daily_buys.py --state 路径.json # 显式指定状态文件

【这个工具干什么】
  在不停止策略的前提下, 从外部控制母版策略的当日买入行为:
    - BLOCK: 往状态文件写入封锁标记, 策略当天不再开新仓(卖出/风控不受影响)
    - CLEAR: 删除封锁标记并把当日已买计数归零, 恢复正常买入
    - SET/ADD: 直接校正"今日已买笔数"(笔数上限 = 母版 MAX_DAILY_BUYS_LIVE=3)

【与母版的对接契约(全部从 V2.6.81 代码提取, 逐字一致)】
  1) 目标文件 = 母版 state_path() 同名文件: 策略同目录的
     zt_shrink_state_<账号后缀>.json (后缀与 zt_shrink_st_hist_*.json 一致,
     例如 zt_shrink_state_<acct>.json); 无后缀时为 zt_shrink_state.json
  2) 文件编码 = UTF-8 (母版 L1044/L1228 明确: 外部工具以 UTF-8 写入中文标记)
  3) 数据结构 = {"buys": {"live": {"date":"YYYYMMDD","n":int,"codes":[...]},
                          "paper": {...}}, ...其余键原样保留...}
  4) 封锁标记 = BUY_BLOCK_TAG = "手工封锁" (母版 L210), 写在 buys[mode]["codes"] 里;
     策略 daily_buy_blocked() 命中该标记即返回封锁, daily_buy_left()=0
  5) 合并规则(母版 _merge_external_daily_buys, L1033): 策略每 300 秒(EXT_MERGE_INTERVAL_SEC)
     主动读盘合并一次, 所以 BLOCK 写入后最多约 5 分钟生效, 不需要重启策略;
     合并只取 max(外部n, 内存n) —— 只增不减, 所以本工具 BLOCK 时不改 n(防止把计数撑爆后
     CLEAR 也救不回来); 封锁标记按日期失效, 次日自动放开
  6) 已知限制: 封锁标记一旦被策略合并进内存, 当天内 CLEAR 只清磁盘不清内存 ——
     如需当天内强行解除, 先停策略 → 运行 --mode CLEAR → 重启策略

【安全设计】
  - 只改 buys 一个字段, 状态文件其余内容(trades/realized/持仓等)原样保留
  - 写盘前自动备份为 <文件>.extbak.<时间戳>.json (该后缀不参与母版 .bak. 轮转, 不会被误删)
  - 先写 .tmp 再 os.replace 原子替换, 策略永远不会读到半个文件
  - 与母版 save_state 一致: 写盘前剔除 names/names_ts 大缓存键, 防止文件膨胀
  - 没有任何改动时不会写盘(幂等)
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import sys

VERSION = 'V1.0.0'
MASTER_VER = 'zt_shrink_strategy_gbk.py V2.6.81'

# ---- 与母版逐字一致的契约常量 (母版 L210 / L604 / L208 / L209) ----
BUY_BLOCK_TAG = '手工封锁'
STATE_FILE = 'zt_shrink_state.json'
MAX_DAILY_BUYS_LIVE = 3
MAX_DAILY_BUYS_PAPER = 3
EXT_MERGE_INTERVAL_SEC = 300          # 母版 L211: 策略主动合并外部变更的周期
MODES = ('live', 'paper')


def _now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _today_str():
    return datetime.datetime.now().strftime('%Y%m%d')


def infer_account_suffix(base_dir):
    """从同目录兄弟数据文件推断母版 _account_suffix() 的返回值。
    依据: st_hist_path()/blacklist_path() 与 state_path() 用同一套后缀规则,
    磁盘上的 zt_shrink_st_hist_<acct>.json / zt_shrink_blacklist_<acct>.json
    即证明运行时后缀为 _<acct>。优先返回非 _dry 后缀(母版 DRY_RUN=False)。"""
    cands = []
    for base in ('zt_shrink_st_hist', 'zt_shrink_blacklist'):
        for p in glob.glob(os.path.join(base_dir, base + '_*.json')):
            n = os.path.basename(p)
            if '.bak.' in n or '.extbak.' in n or n.endswith('.tmp'):
                continue
            suf = n[len(base):-len('.json')]      # 含前导下划线: _<acct> / _noid / _<acct>_dry
            if suf:
                cands.append(suf)
    uniq = sorted(set(cands))
    for s in uniq:
        if not s.endswith('_dry'):
            return s
    return uniq[0] if uniq else ''


def find_state_file(base_dir):
    """定位状态文件。返回 (路径, 说明文字)。
    优先级: ①显式 --state ②带推断后缀的文件 ③任一 zt_shrink_state_*.json(取最新)
    ④无后缀 legacy 文件 ⑤都不存在时给出"将要创建"的目标路径。"""
    suf = infer_account_suffix(base_dir)
    if suf:
        p = os.path.join(base_dir, 'zt_shrink_state%s.json' % suf)
        if os.path.exists(p):
            return p, '账号后缀推断命中: %s' % suf
    cands = []
    for p in glob.glob(os.path.join(base_dir, 'zt_shrink_state_*.json')):
        n = os.path.basename(p)
        if '.bak.' in n or '.extbak.' in n or n.endswith('.tmp'):
            continue
        cands.append(p)
    if cands:
        cands.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        return cands[0], '发现多个状态文件, 取修改时间最新: %s' % os.path.basename(cands[0])
    plain = os.path.join(base_dir, STATE_FILE)
    if os.path.exists(plain):
        return plain, '使用无后缀状态文件(legacy)'
    if suf:
        return os.path.join(base_dir, 'zt_shrink_state%s.json' % suf), \
               '状态文件不存在, 将按推断后缀创建: %s' % suf
    return plain, '状态文件不存在, 将创建无后缀状态文件'


def load_state(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('状态文件顶层不是 JSON 对象(疑似损坏), 已中止, 未做任何修改')
    return data


def normalize_buys(data):
    """与母版 _mode_buckets(L4395)/_merge_external_daily_buys(L1052) 对齐:
    兼容旧版扁平 buys{date,n,codes} -> {'live': 原内容, 'paper': {}}"""
    buys = data.get('buys')
    if not isinstance(buys, dict):
        buys = {}
        data['buys'] = buys
    if 'live' not in buys and 'paper' not in buys and buys:
        buys = {'live': dict(buys), 'paper': {}}
        data['buys'] = buys
    for m in MODES:
        b = buys.get(m)
        if not isinstance(b, dict):
            buys[m] = {'date': '', 'n': 0, 'codes': []}
    return data['buys']


def get_bucket(buys, mode, date):
    """取指定模式当日桶; 日期不符即视为新的一天(重置), 与母版 daily_buy_state 一致"""
    b = buys[mode]
    if b.get('date') != date:
        b['date'] = date
        b['n'] = 0
        b['codes'] = []
    try:
        b['n'] = int(b.get('n', 0) or 0)
    except Exception:
        b['n'] = 0
    if not isinstance(b.get('codes'), list):
        b['codes'] = []
    return b


def atomic_write_json(path, obj):
    """备份 -> 写 .tmp -> os.replace 原子替换"""
    if os.path.exists(path):
        bak = '%s.extbak.%s.json' % (path, datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
        shutil.copyfile(path, bak)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=True, indent=1)
    os.replace(tmp, path)


def show_state(buys, target_date):
    print('=' * 72)
    print('当前买入状态 (目标日期 %s, 今天 %s)' % (target_date, _today_str()))
    print('-' * 72)
    for m in MODES:
        b = buys.get(m) or {}
        date = str(b.get('date') or '')
        n = int(b.get('n', 0) or 0)
        codes = b.get('codes') or []
        cap = MAX_DAILY_BUYS_LIVE if m == 'live' else MAX_DAILY_BUYS_PAPER
        blocked = BUY_BLOCK_TAG in codes
        left = 0 if blocked else (9999 if cap <= 0 else max(0, cap - n))
        fresh = '(今天)' if date == _today_str() else ('(非今天: %s)' % (date or '空'))
        print('[%s] %s' % (m, fresh))
        print('    已买笔数 n = %d / 每日上限 cap = %d -> 今日剩余可买 = %s'
              % (n, cap, ('封锁' if blocked else left)))
        print('    封锁标记 "%s": %s' % (BUY_BLOCK_TAG, '已封锁(当日禁买)' if blocked else '未封锁'))
        print('    codes = %s' % (codes if codes else '[]'))
    print('=' * 72)
    print('说明: 笔数上限与母版 MAX_DAILY_BUYS_LIVE/PAPER 同步(仅展示); '
          '封锁优先级高于笔数上限, 次日自动失效。')


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='set_daily_buys.py',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='外部禁买/买入计数工具 (母版 %s 配套)' % MASTER_VER)
    ap.add_argument('--mode', default='SHOW',
                    choices=['SHOW', 'BLOCK', 'CLEAR', 'SET', 'ADD'],
                    help='SHOW=查看状态(默认); BLOCK=今日手工封锁禁买; '
                         'CLEAR=解除封锁并当日归零; SET=直接设置今日已买笔数; ADD=今日已买笔数增加')
    ap.add_argument('--n', type=int, default=1,
                    help='SET/ADD 模式使用的笔数(SET 必填, ADD 默认 1, 范围 0~999)')
    ap.add_argument('--paper', action='store_true',
                    help='操作 paper(模拟)桶; 不加则操作 live(实盘)桶')
    ap.add_argument('--date', default='',
                    help='操作日期 YYYYMMDD, 默认且仅支持今天(未来日期会被覆盖, 已拒绝); SHOW 可查历史')
    ap.add_argument('--state', default='',
                    help='显式指定状态文件路径(默认自动探测)')
    ap.add_argument('--dir', default='',
                    help='状态文件所在目录(默认: 本脚本所在目录)')
    args = ap.parse_args(argv)

    base_dir = os.path.abspath(args.dir) if args.dir else os.path.dirname(os.path.abspath(__file__))
    if args.state:
        state_path = os.path.abspath(args.state)
        note = '--state 显式指定'
    else:
        state_path, note = find_state_file(base_dir)

    mode = 'paper' if args.paper else 'live'
    target_date = args.date.strip() or _today_str()
    if len(target_date) != 8 or not target_date.isdigit():
        print('[错误] --date 必须是 8 位数字 YYYYMMDD, 当前: %r' % target_date)
        return 2
    # P3-NB2(2026-10-29 修复)：原先 --help 承诺「填未来日期可预先封锁」，但策略侧
    #   save_state 每次都会用当日 date 覆盖 buys 桶，未来日期的封锁会在下一次保存时
    #   被静默擦掉 —— 用户以为封了，实际当天照样买入。这里显式拒绝非今日日期。
    if target_date != _today_str() and args.mode != 'SHOW':
        print('[错误] 本工具只支持操作【今天】(%s) 的封锁/计数。' % _today_str())
        print('       原因: 策略每次保存状态都会按当日日期重建 buys 桶, 预写其它日期')
        print('       会被下次保存静默覆盖, 形成「以为封了、实际没封」的假象。')
        print('       如需当日禁买, 请在当天开盘前运行: --mode BLOCK')
        print('       (仅查看历史请用 --mode SHOW --date YYYYMMDD)')
        return 2

    print('set_daily_buys.py %s | 母版 %s' % (VERSION, MASTER_VER))
    print('目录: %s' % base_dir)
    print('状态文件: %s' % state_path)
    print('定位方式: %s' % note)
    print('操作模式: %s | 目标桶: %s | 目标日期: %s' % (args.mode, mode, target_date))
    print('-' * 72)

    if not os.path.exists(state_path):
        if args.mode == 'SHOW':
            print('[提示] 状态文件不存在(策略尚未首次保存)。无状态可显示。')
            print('       策略首次保存后本文件会自动生成, 届时再运行本工具。')
            return 0
        print('[提示] 状态文件不存在, 将创建仅含 buys 的最小状态文件;')
        print('       策略启动时会自动补齐其余默认字段(trades/realized 等), 安全。')
        data = {}
    else:
        try:
            data = load_state(state_path)
        except Exception as e:
            print('[错误] 读取状态文件失败: %s' % e)
            return 4

    buys = normalize_buys(data)
    before = json.dumps(buys, ensure_ascii=True, sort_keys=True)

    if args.mode == 'SHOW':
        show_state(buys, target_date)
        return 0

    b = get_bucket(buys, mode, target_date)
    changed = False

    if args.mode == 'BLOCK':
        if BUY_BLOCK_TAG not in b['codes']:
            b['codes'].append(BUY_BLOCK_TAG)
            changed = True
        # 注意: 不改 n。母版合并规则只增不减, 把 n 撑爆会导致 CLEAR 也无法恢复。
        print('[BLOCK] %s 桶 %s: 写入封锁标记 "%s" (n 保持 %d 不变)'
              % (mode, target_date, BUY_BLOCK_TAG, b['n']))
    elif args.mode == 'CLEAR':
        if BUY_BLOCK_TAG in b['codes']:
            b['codes'] = [c for c in b['codes'] if c != BUY_BLOCK_TAG]
            changed = True
        if b['n'] != 0:
            b['n'] = 0
            changed = True
        print('[CLEAR] %s 桶 %s: 删除封锁标记 + 当日已买笔数归零' % (mode, target_date))
    elif args.mode == 'SET':
        if not (0 <= args.n <= 999):
            print('[错误] --n 必须在 0~999 之间, 当前: %d' % args.n)
            return 2
        if b['n'] != args.n:
            b['n'] = args.n
            changed = True
        print('[SET] %s 桶 %s: 今日已买笔数设为 %d (剩余可买 = 上限 - %d)'
              % (mode, target_date, args.n, args.n))
    elif args.mode == 'ADD':
        if not (0 <= args.n <= 999):
            print('[错误] --n 必须在 0~999 之间, 当前: %d' % args.n)
            return 2
        b['n'] = max(0, b['n'] + args.n)
        changed = True
        print('[ADD] %s 桶 %s: 今日已买笔数 +%d -> %d' % (mode, target_date, args.n, b['n']))

    after = json.dumps(buys, ensure_ascii=True, sort_keys=True)
    if not changed or before == after:
        print('[完成] 无实际变化, 未写盘(幂等)。')
        show_state(buys, target_date)
        return 0

    # 与母版 save_state 一致: 剔除 names 大缓存再写盘
    data.pop('names', None)
    data.pop('names_ts', None)
    try:
        atomic_write_json(state_path, data)
    except Exception as e:
        print('[错误] 写盘失败(未损坏原文件): %s' % e)
        return 4

    print('[完成] 已写盘: %s (%s)' % (state_path, _now_str()))
    print('       原文件已备份为 %s.extbak.<时间戳>.json' % state_path)
    show_state(buys, target_date)

    if args.mode == 'BLOCK':
        print('生效方式: 策略每 %d 秒主动读盘合并, 最多约 %d 分钟后生效, 无需重启策略;'
              % (EXT_MERGE_INTERVAL_SEC, EXT_MERGE_INTERVAL_SEC // 60 + 1))
        print('          封锁只影响当日开新仓, 卖出/止盈/风控不受影响; 次日自动放开。')
    if args.mode == 'CLEAR':
        print('[注意] 若策略已在运行中合并过该封锁标记(BLOCK 后约 5 分钟内即会合并),')
        print('       仅清磁盘不能解除内存中的标记 —— 需先停止策略 → 运行 --mode CLEAR → 重启策略;')
        print('       或直接编辑状态文件, 删除 codes 列表中的 "%s"。' % BUY_BLOCK_TAG)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('已取消')
        sys.exit(130)
