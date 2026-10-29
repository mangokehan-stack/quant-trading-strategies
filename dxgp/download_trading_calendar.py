# -*- coding: utf-8 -*-
"""
download_trading_calendar.py —— A股交易日历刷新工具 (GBK 母版 zt_shrink_strategy_gbk.py 配套)
================================================================================
版本:     V1.0.0 (2026-10-19 重建)
配套母版: zt_shrink_strategy_gbk.py V2.6.81
运行方式: miniQMT Windows 主机上, 用普通 Python 直接运行 (不需要 xtquant, 不传入 QMT):
              python download_trading_calendar.py            # 联网刷新并写盘
              python download_trading_calendar.py --check    # 只看不写: 报告覆盖情况
              python download_trading_calendar.py --no-forward  # 只合并历史, 不生成未来日期
              python download_trading_calendar.py --years-ahead 2  # 未来日期延伸到 2 年后年底
              python download_trading_calendar.py --out 路径.json # 显式指定输出文件

【这个工具干什么】
  刷新策略同目录的 trading_calendar.json (母版策略的静态交易日历缓存)。
  母版 refresh_calendar() 的取数优先级: ①本地 trading_calendar.json ②QMT 官方接口
  C.get_trading_dates ③上证指数日线兜底。本地日历只有在"最后日期 >= 今天"时才会被采信
  (母版 V2.6.70 的 _not_cover_today 检查), 所以本工具除了抓取历史, 还会向前生成未来
  交易日的日期, 保证日历始终覆盖今天、让本地缓存持续可用(保留 1990 年以来的完整历史,
  母版的多处"回溯 N 个交易日"计算都依赖这份长历史)。

【数据来源(全部公开接口, 按优先级尝试, 成功即用, 全部失败则保留原文件不动)】
  1) 东方财富 push2his 日K接口(上证指数 000001.SH) —— 指数有日K的日子就是交易日
  2) 腾讯 ifzq.gtimg 日K接口(同上) —— 备用源
  3) AKShare tool_trade_date_hist_sina() —— 仅当主机已安装 akshare 时可用
  与磁盘上已有文件取并集: 只增不减, 历史日期永远不会丢失。

【未来日期怎么生成】
  历史抓取只到"最近一个已交易日"; 从那天之后到"当前年份+1 年的 12 月 31 日",
  按周一~周五生成, 再扣除内置的高置信度法定节假日表(_HOLIDAYS, 2026/2027 两年)。
  设计取舍(重要):
    - 节假日表只收录高置信度日期; 不确定的节假日(如 2027 端午/中秋的具体日期)
      故意不收 —— 母版对"日历覆盖今天但今天不在日历里"的日期会权威判定为休市
      (母版 L4955-4973), 漏收节假日的最坏结果只是当天不开市(正确行为), 而错把
      交易日当节假日才会漏掉一个真实交易日且当天无法自我纠正。
    - 周末调休上班日(如春节前后的周六补班)不会生成 —— 母版有当日分钟行情探测兜底
      (母版 L4981), 真开了市会自动补进日历。
    - 国务院每年 11 月左右发布次年节假日安排后, 请更新下面的 _HOLIDAYS 表。

【输出文件格式(母版 load_local_calendar 读取的键, 一个不多一个不少)】
  {"source": "eastmoney", "sources": [...], "last_update": "YYYY-MM-DD HH:MM:SS",
   "last_update_iso": "...", "first_date": "19901219", "last_date": "20271231",
   "count": 9000, "trading_days": ["19901219", ...]}
  母版校验: trading_days 必须是列表且有效日期 >= 100 个, 否则整个文件被忽略。
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import sys
import time
import urllib.request

VERSION = 'V1.0.0'
MASTER_VER = 'zt_shrink_strategy_gbk.py V2.6.81'
CAL_FILE = 'trading_calendar.json'
HIST_BEG = '19901201'          # 上证指数 1990-12-19 上市, 与现有文件 first_date 一致
MIN_DAYS = 100                 # 母版 load_local_calendar 的硬性下限

# ---- 内置高置信度法定节假日表(仅 2026/2027; 不确定的日期故意不收, 见文件头说明) ----
# 2026 元旦: 1/1(周四) 法定1天(1/2周五是否放假存疑, 保守不收)
# 2026 春节: 初一 2/17(周二), 除夕 2/16(周一); 通知窗口 2/15~2/22, 2/16~2/20 全在窗口内(高置信)
# 2026 清明: 4/4(周六) 4/5(周日) 4/6(周一) -> 4/6 高置信
# 2026 劳动节: 5/1~5/5 法定(2025年新规) -> 5/1(周五) 5/4(周一) 5/5(周二)
# 2026 端午: 6/19(周五, 农历五月初五)
# 2026 中秋: 9/25(周五, 农历八月十五)
# 2026 国庆: 10/1~10/7 法定 -> 10/1(周四) 10/2(周五) 10/5(周一) 10/6 10/7
_HOLIDAYS = {
    '2026': [
        '20260101',
        '20260216', '20260217', '20260218', '20260219', '20260220',
        '20260406',
        '20260501', '20260504', '20260505',
        '20260619',
        '20260925',
        '20261001', '20261002', '20261005', '20261006', '20261007',
    ],
    '2027': [
        '20270101',                                    # 元旦 1/1(周五)
        '20270205', '20270208', '20270209', '20270210', '20270211', '20270212',  # 春节: 初一2/6(周六) 除夕2/5(周五)
        '20270405',                                    # 清明 4/5(周一)
        '20270503', '20270504', '20270505',            # 劳动节 5/1~5/5
        '20271001', '20271004', '20271005', '20271006', '20271007',              # 国庆 10/1~10/7
        # 2027 端午(约6/9)/中秋(约9/15)具体日期未发布, 故意不收; 漏收由母版休市判定兜底
    ],
}


def _now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _now_iso():
    return datetime.datetime.now().isoformat(timespec='seconds')


def _http_get_json(url, timeout):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) trading_calendar_tool/1.0',
        'Accept': 'application/json, text/plain, */*',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode('utf-8', 'replace'))


def _norm_days(seq):
    out = set()
    for x in seq or []:
        d = ''.join(ch for ch in str(x) if ch.isdigit())
        if len(d) == 8:
            out.add(d)
    return out


def fetch_eastmoney(end_ymd, timeout):
    """东方财富日K(上证指数 000001.SH): data.klines = ['1990-12-19,开,收,...', ...]"""
    url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get'
           '?secid=1.000001&fields1=f1,f2,f3,f4,f5,f6'
           '&fields2=f51,f52,f53,f54,f55,f56,f57,f58'
           '&klt=101&fqt=1&beg=%s&end=%s&lmt=1000000' % (HIST_BEG, end_ymd))
    j = _http_get_json(url, timeout)
    klines = (j.get('data') or {}).get('klines') or []
    days = set()
    for k in klines:
        d = str(k).split(',')[0].replace('-', '').strip()
        if len(d) == 8 and d.isdigit():
            days.add(d)
    return days, url


def fetch_tencent(end_ymd, timeout):
    """腾讯日K(上证指数 sh000001)按年分块抓取。
    注意: 该接口单次 count 上限约 1000(传 10000 会直接 param error),
    按年请求(一年最多约 243 个交易日)最稳, 单次约 0.2 秒。
    返回 data['sh000001']['day'] = [['1990-12-19', ...], ...]"""
    y1 = int(end_ymd[:4])
    days = set()
    nreq = 0
    for y in range(1990, y1 + 1):
        s = '%d-01-01' % y if y > 1990 else '1990-12-01'
        e = '%d-12-31' % y
        url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
               '?param=sh000001,day,%s,%s,320,qfq' % (s, e))
        nreq += 1
        try:
            j = _http_get_json(url, timeout)
            node = (j.get('data') or {}).get('sh000001') or {}
            rows = node.get('day') or node.get('qfqday') or []
            for r in rows:
                try:
                    d = str(r[0]).replace('-', '').strip()
                except Exception:
                    continue
                if len(d) == 8 and d.isdigit():
                    days.add(d)
        except Exception:
            continue          # 单年失败不致命, 继续后续年份
    return days, 'tencent(按年分块 1990~%d, 共 %d 次请求)' % (y1, nreq)


def fetch_akshare(timeout):
    """AKShare 备用源(仅当主机已安装 akshare)"""
    try:
        import akshare as ak
    except Exception:
        return set(), '(akshare 未安装, 跳过)'
    df = ak.tool_trade_date_hist_sina()
    col = 'trade_date' if 'trade_date' in getattr(df, 'columns', []) else df.columns[0]
    return _norm_days(df[col].tolist()), 'akshare.tool_trade_date_hist_sina'


def load_existing(path):
    if not os.path.exists(path):
        return set(), None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            j = json.load(f)
        days = _norm_days(j.get('trading_days'))
        return days, j
    except Exception as e:
        print('[告警] 现有日历文件读取失败(将按全新文件处理, 原文件会先备份): %s' % e)
        return set(), None


def gen_forward(last_hist_day, end_ymd, with_holidays=True):
    """从 last_hist_day 次日生成到 end_ymd(含)的工作日, 按设定扣除节假日表"""
    holidays = set()
    if with_holidays:
        for yr, ds in _HOLIDAYS.items():
            holidays.update(ds)
    try:
        d0 = datetime.date(int(last_hist_day[:4]), int(last_hist_day[4:6]), int(last_hist_day[6:]))
        d1 = datetime.date(int(end_ymd[:4]), int(end_ymd[4:6]), int(end_ymd[6:]))
    except Exception:
        return set()
    out = set()
    d = d0 + datetime.timedelta(days=1)
    while d <= d1:
        if d.weekday() < 5:
            k = d.strftime('%Y%m%d')
            if k not in holidays:
                out.add(k)
        d += datetime.timedelta(days=1)
    return out


def atomic_write_json(path, obj):
    if os.path.exists(path):
        bak = '%s.extbak.%s.json' % (path, datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
        shutil.copyfile(path, bak)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=True, indent=1)
    os.replace(tmp, path)


def report(path, days, extra=''):
    today = datetime.datetime.now().strftime('%Y%m%d')
    year_end = '%s1231' % datetime.datetime.now().strftime('%Y')
    print('-' * 72)
    print('日历文件: %s%s' % (path, (' (%s)' % extra) if extra else ''))
    if not days:
        print('  (无数据)')
        print('-' * 72)
        return
    dl = sorted(days)
    in_year = [d for d in dl if d.startswith(datetime.datetime.now().strftime('%Y'))]
    print('  交易日总数 : %d' % len(dl))
    print('  覆盖范围   : %s ~ %s' % (dl[0], dl[-1]))
    print('  本年度日期 : %d 个 (%s ~ %s)' % (len(in_year), in_year[0] if in_year else '-',
                                              in_year[-1] if in_year else '-'))
    print('  覆盖今天   : %s (今天 %s)' % ('是' if today in dl else '否 —— 母版将跳过本地缓存改走 QMT 官方接口', today))
    print('  覆盖今年底 : %s (%s)' % ('是' if dl[-1] >= year_end else '否', year_end))
    if dl[-1] < year_end:
        print('  建议       : 运行 python download_trading_calendar.py 刷新, 延伸到明年年底')
    print('-' * 72)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='download_trading_calendar.py',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='A股交易日历刷新工具 (母版 %s 配套)' % MASTER_VER)
    ap.add_argument('--check', action='store_true',
                    help='只报告当前日历覆盖情况, 不联网不写盘')
    ap.add_argument('--no-forward', action='store_true',
                    help='只合并历史抓取结果, 不向前生成未来交易日')
    ap.add_argument('--years-ahead', type=int, default=1,
                    help='未来日期延伸到"当前年份+N"的 12 月 31 日 (默认 1)')
    ap.add_argument('--out', default='',
                    help='输出文件路径 (默认: 环境变量 TRADING_CALENDAR_PATH, 否则本脚本同目录 trading_calendar.json)')
    ap.add_argument('--dir', default='',
                    help='输出目录 (默认: 本脚本所在目录; --out 优先于 --dir)')
    ap.add_argument('--timeout', type=int, default=20,
                    help='每个数据源 HTTP 超时秒数 (默认 20)')
    ap.add_argument('--retries', type=int, default=2,
                    help='每个数据源失败重试次数 (默认 2, 应对瞬时断连)')
    args = ap.parse_args(argv)

    if args.out:
        out_path = os.path.abspath(args.out)
    else:
        env_path = os.environ.get('TRADING_CALENDAR_PATH')
        if env_path:
            out_path = os.path.abspath(env_path)
            print('[提示] 命中环境变量 TRADING_CALENDAR_PATH, 输出到: %s' % out_path)
        else:
            base_dir = os.path.abspath(args.dir) if args.dir else os.path.dirname(os.path.abspath(__file__))
            out_path = os.path.join(base_dir, CAL_FILE)

    print('download_trading_calendar.py %s | 母版 %s' % (VERSION, MASTER_VER))

    old_days, old_json = load_existing(out_path)
    if args.check:
        report(out_path, old_days, '只读检查, 未做任何修改')
        return 0 if old_days else 3

    print('输出文件: %s' % out_path)
    print('现有日历: %d 个交易日' % len(old_days))
    print('-' * 72)

    # ---- 抓取历史(多源并集; 某源已拿到 >=5000 天则跳过后续源, 避免无谓请求) ----
    end_ymd = '%s1231' % str(datetime.datetime.now().year + max(0, args.years_ahead))
    sources_ok = []
    hist_days = set()
    for name, fn in (('eastmoney', lambda: fetch_eastmoney(end_ymd, args.timeout)),
                     ('tencent', lambda: fetch_tencent(end_ymd, args.timeout)),
                     ('akshare', lambda: fetch_akshare(args.timeout))):
        if len(hist_days) >= 5000:
            print('[抓取] %-10s 跳过(已有源返回 %d 天, 足够)' % (name, len(hist_days)))
            continue
        days = set()
        last_err = ''
        for attempt in range(1, max(1, args.retries) + 1):
            t0 = time.time()
            try:
                days, url = fn()
                if days:
                    sources_ok.append(name)
                    print('[抓取] %-10s 成功: %d 天 (%.1fs, 第%d次尝试) %s'
                          % (name, len(days), time.time() - t0, attempt, str(url)[:80]))
                    if len(days) < 2000:
                        print('       [告警] 该源返回天数偏少, 可能被截断; 已按并集处理, 不会丢失历史')
                else:
                    print('[抓取] %-10s 返回空 (%.1fs, 第%d次尝试)' % (name, time.time() - t0, attempt))
                break
            except Exception as e:
                last_err = repr(e)[:110]
                print('[抓取] %-10s 失败: %s (%.1fs, 第%d次尝试)'
                      % (name, last_err, time.time() - t0, attempt))
                if attempt < max(1, args.retries):
                    time.sleep(1.0 * (2 ** (attempt - 1)))   # 指数退避 1s/3s, 应对限流
        hist_days |= days

    if not hist_days and not old_days:
        print('[失败] 所有数据源均失败, 且磁盘无现有日历。原文件未改动。')
        print('       请检查网络/防火墙后重试; 或在主机已装 akshare 时再试。')
        return 1

    # T1：旧文件中的未来日期只是推测，不能永久并入。每次以真实历史重建未来段。
    _old_real = str((old_json or {}).get('last_real_date', '') or '')
    if hist_days:
        _new_real = max(d for d in hist_days if d <= datetime.datetime.now().strftime('%Y%m%d'))
        # P3-T1/NB1(2026-10-29 修复)：last_real_date 必须【单调不降】。
        #   原实现直接用本次抓取结果覆盖，一旦数据源返回部分/陈旧数据（接口限流、缓存过期、
        #   只回补了半年），last_real_date 会倒退 —— 于是原本已确认为「真实历史」的日期
        #   被降级成「推测未来」，下次重建时会用工作日+内置节假日表重新生成，把真历史换成假日期
        #   （报告 NB1：20260102/20260223 被当成交易日长期保留）。
        #   现取新旧较大者；只有新数据确实推进时才更新。
        if _old_real and _old_real > _new_real:
            last_real_date = _old_real
            print('[提示] 本次抓取历史(%s)早于已知真实边界(%s)，保留较新边界(防历史被降级)'
                  % (_new_real, _old_real))
        else:
            last_real_date = _new_real
        old_hist = {d for d in old_days if d <= last_real_date}
        all_days = old_hist | hist_days
    else:
        last_real_date = _old_real or max([d for d in old_days if d <= datetime.datetime.now().strftime('%Y%m%d')] or [''])
        all_days = {d for d in old_days if not last_real_date or d <= last_real_date}
    src_note = '+'.join(sources_ok) if sources_ok else 'local(仅沿用已标记真实历史)'

    # ---- 向前生成未来交易日（每次重建，不沿用旧推测未来段）----
    fwd_note = '未生成'
    if not args.no_forward:
        base_last = last_real_date or datetime.datetime.now().strftime('%Y%m%d')
        fwd = gen_forward(base_last, end_ymd, with_holidays=True)
        if fwd:
            all_days |= fwd
            fwd_note = '生成 %d 天 (%s ~ %s, 已扣除内置节假日表)' % (len(fwd), min(fwd), max(fwd))
        else:
            fwd_note = '无需生成(已覆盖到 %s)' % end_ymd
    print('未来日期: %s' % fwd_note)

    # ---- 校验 ----
    dl = sorted(all_days)
    if len(dl) < MIN_DAYS:
        print('[失败] 合并后仅 %d 个交易日(< %d), 母版会忽略该文件。原文件未改动。'
              % (len(dl), MIN_DAYS))
        return 2
    if not (dl[0][:8].isdigit() and dl[-1][:8].isdigit()):
        print('[失败] 日期格式校验失败。原文件未改动。')
        return 2

    # ---- 组装并写盘 ----
    meta = {
        'source': (sources_ok[0] if sources_ok else 'local'),
        'sources': sources_ok if sources_ok else ['local'],
        'last_update': _now_str(),
        'last_update_iso': _now_iso(),
        'first_date': dl[0],
        'last_date': dl[-1],
        'last_real_date': last_real_date,
        'count': len(dl),
        'trading_days': dl,
        'note': ('由 download_trading_calendar.py %s 生成; 历史来自 %s; '
                 '未来日期为工作日减内置节假日表生成, 周末调休上班日不生成(母版有行情探测兜底); '
                 '国务院发布次年节假日安排后请更新脚本内 _HOLIDAYS 表'
                 % (VERSION, src_note)),
    }
    try:
        atomic_write_json(out_path, meta)
    except Exception as e:
        print('[失败] 写盘失败(原文件未损坏): %s' % e)
        return 4

    print('[完成] 已写盘: %s (%s)' % (out_path, _now_str()))
    if os.path.exists(out_path):
        print('       原文件已备份为 %s.extbak.<时间戳>.json' % out_path)
    report(out_path, set(dl), '刷新后')
    if not sources_ok:
        print('[告警] 本次未抓到任何网络数据, 仅沿用旧文件(未损坏)。请检查网络后重试。')
        return 1
    print('[提醒] 未来日期中的 2027 年端午/中秋等未发布节假日未扣除;')
    print('       如当年交易日历与预期不符, 请更新脚本内 _HOLIDAYS 表后重跑。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('已取消')
        sys.exit(130)
