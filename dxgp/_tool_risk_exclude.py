#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""涨停缩量策略 · 提前排雷清单生成工具（独立运行，不依赖第三方库）

用途
----
自动抓取"该躲开的股票"，生成 zt_shrink_risk_exclude.json 供策略读取；
策略侧在【选股】与【买入发单前】双重拦截，命中即拒绝买入（不影响卖出/持仓管理）。

抓取五类（全部自动，无需人工录入）
--------------------------------
  1. ST / 退市风险        —— 全A实时名称含 ST / *ST / 退
  2. 临近解禁(默认45天)   —— 未来 N 个自然日内的限售股解禁
  3. 大股东减持           —— 过去 N 天内公告的减持（含竞价/大宗/协议转让，不区分主动被动）
  4. 业绩预亏/预减        —— 过去 N 天内公告的预亏、首亏、续亏、预减、略减
  5. 立案/处罚            —— 过去 N 天内标题含"立案/行政处罚/警示函/监管措施/纪律处分"的公告

用法
----
  python download_risk_exclude.py                 # 默认 45 天窗口，写当前目录
  python download_risk_exclude.py --days 60       # 放宽/收紧窗口
  python download_risk_exclude.py --account 12345678   # 生成带账号后缀的文件(与策略状态文件同规则)
  python download_risk_exclude.py --skip-ann      # 跳过公告抓取(最快, 约5秒)
  python download_risk_exclude.py --self-test     # 离线自检: 不联网, 用内置样例写一份JSON

退出码: 0=成功(至少一个数据源有效) / 2=全部数据源失败(不覆盖已有文件) / 3=参数错误

变更记录
--------
2026-10-20 (第4项: st 源失败时用本地策略清单兜底, 依据 2026-10-19 23:57 实跑 633 只)
  [6] st 兜底改读策略自写的 zt_shrink_st_coef_note_<账号>.txt(实测 145 只)
      —— 旧版 st 只剩"公告简称含ST"一条兜底, 数量随页数线性变(20页35只 / 60页80只),
      漏 65 只(多为近期无公告的 ST 票); 兜底只数写入 sources.st.fallback_n,
      策略侧据此改告警措辞, 不再说"该类覆盖不全"。
2026-10-20 (st 数据源切换 push2 -> 新浪 hs_a, 依据 push2 反爬封锁实测 + 新浪全市场探测)
  [7] fetch_st 换源 —— push2.eastmoney.com 对本机 IP 反爬封锁(RemoteDisconnected
      0.1~0.33s 服务端主动断连, 镜像 82./61. 同样, 重试无效), st 类从在线 203 只
      掉到本地兜底 145 只(少的 58 只多为近期无公告 ST 票, 选股/买入闸门漏排);
      改新浪 hs_a 列表接口(GBK, 100行/页, 实测 56 页 5574 只全市场, 命中 202 只,
      北交所 920 段全覆盖: 920023/920090/920575), 失败仍走 fetch_st_local。
  [8] fetch_st 增 n<100 熔断 —— 新浪若静默返回空页(UA/参数变更/深页被截),
      老逻辑会 ok:True+n=0 写进 JSON, 策略侧当天零 st 拦截且不报警; 现抛异常
      走本地兜底, 并让 sources.st 记 err。
  [9] http_json 增 enc 参数 —— 新浪返回 GBK, utf-8 强解会把"退市"解成乱符,
      "退"字检测失效; 默认 utf-8, 不影响其余四个东财数据源。
2026-10-19 (三项零风险修正, 依据当晚 23:31 实跑日志 594 只)
  [1] 920xxx 补 .BJ 后缀   —— 旧版只认 4/8 开头, 920720 落成裸码, 策略侧 key 是
      920720.BJ 永远查不到(北交所漏排)。
  [2] 公告"合规说明句"过滤  —— "最近五年未被处罚"这类再融资/IPO 说明句会命中关键词,
      实测 13 只 ann 里 5 只是假阳性, 正常票被白拉黑 45 天。
  [3] 解禁市值文案          —— 0.03 亿被打成 "0.0亿", 看起来像字段缺失; 现分
      正常值 / <0.1亿 / 市值未知 三档。
  [4] 公告覆盖区间实打实打印 —— 旧版"扫描2000条公告"其实是 20 页上限跑满(只覆盖4天),
      却显示 [OK]; 现打印实扫区间并估算覆盖 45 天所需页数。
  [5] 完成行补账目自洽       —— 股票数 / 标签合计 / 多标签重叠, 解释 594 与 612 的差额。
"""
from __future__ import print_function

import argparse
import datetime
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

SCHEMA = 1
DEFAULT_OUT = 'zt_shrink_risk_exclude.json'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/120.0 Safari/537.36',
      'Referer': 'https://data.eastmoney.com/'}
DC = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
SINA_HS = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           'Market_Center.getHQNodeData')
ANN = 'https://np-anotice-stock.eastmoney.com/api/security/ann'

ANN_KEYWORDS = (u'立案', u'行政处罚', u'警示函', u'监管措施', u'纪律处分',
                u'退市风险警示', u'终止上市')
# 2026-10-19 修正(ann 假阳性 38%): 再融资/IPO/重组文件里的"合规说明句"会命中上面的关键词,
# 但说的不是当期处罚, 而是"最近五年未被处罚""最近五年被采取监管措施的情况"这类陈述。
# 实测 2026-10-19 那轮 13 只 ann 里有 5 只是这种(603150/603219/688545/600538/688360),
# 等于把正常票白拉黑 45 天。标题命中下列片段的公告直接跳过(不影响后面的 ST 简称兜底)。
ANN_SKIP_PAT = (u'最近五年', u'未被', u'不存在')
LOSS_TYPES = (u'预亏', u'首亏', u'续亏', u'预减', u'略减', u'增亏')


# ------------------------------------------------------------------ 基础
def _ctx():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


def http_json(url, timeout=15, retries=2, enc='utf-8'):
    last = None
    for k in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            kw = {}
            c = _ctx()
            if c is not None:
                kw['context'] = c
            with urllib.request.urlopen(req, timeout=timeout, **kw) as r:
                return json.loads(r.read().decode(enc, errors='replace'))
        except Exception as e:
            last = e
            if k < retries:
                time.sleep(0.6 * (k + 1))
    raise last


def ymd(d):
    return d.strftime('%Y%m%d')


def iso(d):
    return d.strftime('%Y-%m-%d')


def suffix_of(code):
    """6位代码 -> 交易所后缀"""
    c = str(code or '').strip()
    if not c:
        return ''
    if c[0] == '6':
        return c + '.SH'
    if c[0] in ('0', '3'):
        return c + '.SZ'
    if c[0] in ('4', '8'):
        return c + '.BJ'
    # 2026-10-19 修正(北交所漏排): 920xxx 是北交所新代码段, 旧版 '9' 开头原样返回,
    # 于是 920720 这种票在清单里是裸 6 位码, 而策略侧 key 是 '920720.BJ' → 永远查不到。
    # 定稿只认 '92' 前缀(900xxx 沪市B股不在此列, 避免误映射)。
    if c[:2] == '92':
        return c + '.BJ'
    return c


def _norm_code(secucode, seccode):
    """优先用东财 SECUCODE(000665.SZ), 缺失则按6位代码补后缀"""
    s = str(secucode or '').strip().upper()
    if '.' in s:
        return s
    return suffix_of(seccode)


class Bag(object):
    """按代码聚合排雷原因"""

    def __init__(self):
        self.d = {}

    def add(self, code, tag, reason):
        if not code:
            return
        it = self.d.get(code)
        if it is None:
            it = {'tags': [], 'reasons': []}
            self.d[code] = it
        if tag not in it['tags']:
            it['tags'].append(tag)
        if reason and reason not in it['reasons']:
            it['reasons'].append(reason)

    def to_dict(self):
        return self.d


# ------------------------------------------------------------------ 1) ST
def fetch_st(bag, timeout):
    u"""全A名称 -> 含 ST/*ST/退 的票。返回命中数。

    2026-10-20 换源: 旧走 push2.eastmoney.com 名称接口, 该 host 对本机 IP 反爬封锁
    (RemoteDisconnected 0.1~0.33s, 服务端主动断连, 镜像 82./61. 同样, 重试无效;
    同刻 datacenter-web / np-anotice / qt.gtimg 均 200 正常, 坐实单 host 封锁),
    st 类从在线 203 只掉到本地兜底 145 只。改新浪 hs_a 列表接口 —— GBK JSON,
    100 行/页; 2026-10-20 实测 56 页 5574 只全市场(沪2317/深2906/北351),
    名称含 ST/退 命中 202 只, 与 push2 健康时一致, 北交所 920 段全覆盖。
    失败时本函数抛异常由上层记录, 不影响其它数据源; ST 另有三条兜底 ——
    ①本地策略清单(fetch_st_local, 策略每日写入, 实测145只)
    ②下面的公告名称识别(fetch_ann)
    ③策略侧 QMT 实时名称检查(inst_name + P1-1 名称失败保守剔除), 后者才是权威。
    u"""
    n, page = 0, 1
    while page <= 80:
        u = (SINA_HS + '?page=%d&num=100&sort=symbol&asc=1&node=hs_a&symbol=' % page)
        rows = http_json(u, timeout=timeout, enc='gbk')
        if not rows:
            break
        for r in rows:
            code6 = str(r.get('code') or '')
            name = str(r.get('name') or '')
            if not code6 or not name:
                continue
            code = suffix_of(code6)
            if '.' not in code:      # 只收能映射到交易所后缀的代码, 挡掉杂项行
                continue
            up = name.upper().replace(' ', '')
            if ('ST' in up) or (u'退' in name):
                bag.add(code, 'st', u'ST/退市风险(名称:%s)' % name)
                n += 1
        if len(rows) < 100:
            break
        page += 1
        time.sleep(0.12)      # 轻微间隔, 降低被限流概率
    if n < 100:
        # 2026-10-20 防御: 新浪若静默返回空页/半页(UA 变更/参数变更/深页被截),
        # 老逻辑会 ok:True + n=0 写进 JSON, 策略侧当天零 st 拦截还不报警;
        # 现直接抛异常走 fetch_st_local 兜底(145 只), 并让 sources.st 记 err。
        raise IOError(u'st 源异常: 新浪 hs_a 仅命中 %d 只(正常约200), 疑似空响应或翻页被截' % n)
    return n


def fetch_st_local(bag, account=''):
    """ST 数据源失败时的本地兜底: 读策略自己写的 zt_shrink_st_coef_note_<账号>.txt。

    该文件由策略在启动/选股时按"当前名称含 ST"写入(2026-10-19 实测 145 只), 是本地最全的
    ST 快照; 比 fetch_ann 的"扫描窗口内发过公告的 ST 票"完整得多(60 页只捞到 80 只,
    且数量随 --ann-pages 线性波动, 20 页=35 只 / 60 页=80 只)。
    口径差异: 本文件只含名称带 ST/*ST 的票, 不含"退"字票 —— 退市/退市整理期由策略侧
    risk_blocked 的 "退" in name 实时拦截, 不依赖本清单, 所以不漏。
    返回 (读入只数, 实际文件名); 文件缺失/格式不符返回 (0, ''), 不抛异常
    (兜底失败不能掩盖 st 源本身的那条错误)。
    """
    cands = []
    _acc = str(account or '').strip()
    if _acc:
        cands.append('zt_shrink_st_coef_note_%s.txt' % _acc)
    cands.append('zt_shrink_st_coef_note.txt')
    for fn in cands:
        if not os.path.exists(fn):
            continue
        n = 0
        try:
            with open(fn, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    s = line.strip().lstrip(u'\ufeff')
                    # 只认 6位数字+'.'+交易所后缀 的纯代码行; 前3行中文注释自然不匹配
                    if len(s) == 9 and s[6] == '.' and s[:6].isdigit() \
                            and s[7:] in ('SH', 'SZ', 'BJ'):
                        bag.add(s, 'st', u'ST/退市风险(本地策略清单兜底, 源:%s)' % fn)
                        n += 1
        except Exception:
            continue
        if n > 0:
            return n, fn
    return 0, ''


# ------------------------------------------------------------------ 2) 解禁
def fetch_lift(bag, days, timeout):
    today = datetime.date.today()
    end = today + datetime.timedelta(days=int(days))
    flt = urllib.parse.quote("(FREE_DATE>='%s')(FREE_DATE<='%s')" % (iso(today), iso(end)))
    n, page = 0, 1
    while page <= 10:
        u = (DC + '?reportName=RPT_LIFT_STAGE&columns=ALL&pageNumber=%d&pageSize=500'
             '&sortColumns=FREE_DATE&sortTypes=1&filter=%s' % (page, flt))
        j = http_json(u, timeout=timeout)
        res = j.get('result') or {}
        rows = res.get('data') or []
        if not rows:
            break
        for r in rows:
            code = _norm_code(r.get('SECUCODE'), r.get('SECURITY_CODE'))
            fd = str(r.get('FREE_DATE') or '')[:10]
            cap = r.get('LIFT_MARKET_CAP')
            typ = str(r.get('FREE_SHARES_TYPE') or '')
            # 2026-10-19 修正: 旧版直接按亿四舍五入, 小额解禁(如0.03亿)打成"0.0亿",
            #   看起来像字段缺失。现分三档: 正常值 / 不足0.1亿 / 真缺失(市值未知)。
            try:
                _cap_yuan = float(cap) if cap not in (None, '') else 0.0
            except Exception:
                _cap_yuan = 0.0
            if _cap_yuan <= 0:
                cap_s = u'市值未知'
            elif _cap_yuan < 10000000.0:
                cap_s = u'<0.1亿'
            else:
                cap_s = u'%.1f亿' % (_cap_yuan / 100000000.0)
            reason = u'临近解禁(%s%s%s)' % (fd, u' ' + typ if typ else '', u' ' + cap_s if cap_s else u'')
            bag.add(code, 'lift', reason)
            n += 1
        if len(rows) < 500:
            break
        page += 1
    return n


# ------------------------------------------------------------------ 3) 减持
def fetch_decrease(bag, days, timeout):
    since = iso(datetime.date.today() - datetime.timedelta(days=int(days)))
    flt = urllib.parse.quote("(CHANGE_NUM_SYMBOL<0)(NOTICE_DATE>='%s')" % since)
    n, page = 0, 1
    while page <= 10:
        u = (DC + '?reportName=RPT_SHARE_HOLDER_INCREASE&columns=ALL&pageNumber=%d&pageSize=500'
             '&sortColumns=NOTICE_DATE&sortTypes=-1&filter=%s' % (page, flt))
        j = http_json(u, timeout=timeout)
        res = j.get('result') or {}
        rows = res.get('data') or []
        if not rows:
            break
        for r in rows:
            code = _norm_code(r.get('SECUCODE'), r.get('SECURITY_CODE'))
            nd = str(r.get('NOTICE_DATE') or '')[:10]
            holder = str(r.get('HOLDER_NAME') or u'股东')
            rate = r.get('CHANGE_RATE')
            try:
                rate_s = u'%.2f%%' % abs(float(rate))
            except Exception:
                rate_s = u''
            reason = u'大股东减持(%s %s%s)' % (nd, holder[:12], u' ' + rate_s if rate_s else u'')
            bag.add(code, 'decrease', reason)
            n += 1
        if len(rows) < 500:
            break
        page += 1
    return n


# ------------------------------------------------------------------ 4) 业绩
def fetch_loss(bag, days, timeout):
    since = iso(datetime.date.today() - datetime.timedelta(days=int(days)))
    flt = urllib.parse.quote("(NOTICE_DATE>='%s')" % since)
    n, page = 0, 1
    while page <= 10:
        u = (DC + '?reportName=RPT_PUBLIC_OP_NEWPREDICT&columns=ALL&pageNumber=%d&pageSize=500'
             '&sortColumns=NOTICE_DATE&sortTypes=-1&filter=%s' % (page, flt))
        j = http_json(u, timeout=timeout)
        res = j.get('result') or {}
        rows = res.get('data') or []
        if not rows:
            break
        for r in rows:
            # 只看归母净利润(code 004), 避免每股收益/营收/扣非重复计入
            if str(r.get('PREDICT_FINANCE_CODE') or '') != '004':
                continue
            ptype = str(r.get('PREDICT_TYPE') or '')
            if not any(k in ptype for k in LOSS_TYPES):
                continue
            code = _norm_code(r.get('SECUCODE'), r.get('SECURITY_CODE'))
            nd = str(r.get('NOTICE_DATE') or '')[:10]
            rd = str(r.get('REPORT_DATE') or '')[:10]
            bag.add(code, 'loss', u'业绩%s(%s公告, 报告期%s)' % (ptype, nd, rd))
            n += 1
        if len(rows) < 500:
            break
        page += 1
    return n


# ------------------------------------------------------------------ 5) 公告
def fetch_ann(bag, days, timeout, max_pages):
    cutoff = iso(datetime.date.today() - datetime.timedelta(days=int(days)))
    n, hit, skip = 0, 0, 0
    dmin, dmax = '', ''      # 2026-10-19新增: 本轮真正扫到的公告日期区间, 用于暴露"窗口没走完"
    for pg in range(1, int(max_pages) + 1):
        u = (ANN + '?sr=-1&page_size=100&page_index=%d&ann_type=A&client_source=web' % pg)
        j = http_json(u, timeout=timeout)
        lst = ((j.get('data') or {}).get('list') or [])
        if not lst:
            break
        oldest = ''
        for a in lst:
            nd = str(a.get('notice_date') or '')[:10]
            oldest = nd or oldest
            if nd:
                if not dmin or nd < dmin:
                    dmin = nd
                if not dmax or nd > dmax:
                    dmax = nd
            title = str(a.get('title') or '')
            codes0 = a.get('codes') or []
            c00 = codes0[0] if codes0 else {}
            code0 = suffix_of(c00.get('stock_code'))
            # ST 兜底识别: 公告简称带 ST/*ST 的票 —— 行情接口被限流时的第二条路径
            sn = str(c00.get('short_name') or '')
            if code0 and sn and (('ST' in sn.upper()) or (u'退' in sn)):
                bag.add(code0, 'st', u'ST/退市风险(公告简称:%s)' % sn)
            # 2026-10-19 修正: 先判"是不是风险关键词", 再排除"合规说明句"(两者都命中才计数)
            _kw = bool(title) and any(k in title for k in ANN_KEYWORDS)
            if _kw and any(_sp in title for _sp in ANN_SKIP_PAT):
                skip += 1
                continue
            if not _kw:
                continue
            codes = a.get('codes') or []
            if not codes:
                continue
            c0 = codes[0]
            code = suffix_of(c0.get('stock_code'))
            kw = u'/'.join([k for k in ANN_KEYWORDS if k in title][:2]) or u'风险公告'
            bag.add(code, 'ann', u'%s(%s %s)' % (kw, nd, title[:26]))
            hit += 1
        n += len(lst)
        if oldest and oldest < cutoff:
            break
    return hit, n, skip, dmin, dmax


# ------------------------------------------------------------------ 主流程
def build(days, timeout, skip_ann, ann_pages, account=''):
    bag = Bag()
    sources, errors = {}, []
    jobs = [('st', lambda: fetch_st(bag, timeout)),
            ('lift', lambda: fetch_lift(bag, days, timeout)),
            ('decrease', lambda: fetch_decrease(bag, days, timeout)),
            ('loss', lambda: fetch_loss(bag, days, timeout))]
    for name, fn in jobs:
        t0 = time.time()
        try:
            n = fn()
            sources[name] = {'ok': True, 'n': n, 'sec': round(time.time() - t0, 1)}
            print('  [OK] %-9s %4d 条 (%.1fs)' % (name, n, time.time() - t0))
        except Exception as e:
            sources[name] = {'ok': False, 'n': 0, 'err': repr(e)[:120]}
            errors.append('%s: %r' % (name, e))
            print('  [FAIL] %-9s %r' % (name, e))
            if name == 'st':
                # 2026-10-20 新增(第4项): st 源失败时读本地策略清单兜底。
                #   病因: push2 名称接口长期 RemoteDisconnected, st 类只能靠 fetch_ann 里
                #   "公告简称含 ST"兜底, 60 页只捞到 80 只, 而本地策略清单有 145 只 ——
                #   等于 65 只 ST 票(多为近期没发公告的)在清单里完全不存在。
                #   兜底文件是策略自己写的当前名称快照, 属同一台机器上的权威数据, 比公告兜底可靠。
                # 2026-10-20 晚: st 主源已切新浪 hs_a(见 fetch_st [7]), 本兜底降级为
                #   "新浪也失败"时的第二道防线, 兜底文件名与读取逻辑不变。
                _fbn, _fbf = fetch_st_local(bag, account)
                if _fbn > 0:
                    sources['st']['fallback_n'] = _fbn
                    sources['st']['fallback_file'] = _fbf
                    print(u'  [兜底] %-9s 从本地策略清单读入 %4d 只 (%s)' % ('st', _fbn, _fbf))
                    print(u'         st 类 = 本地清单 %d 只 + 公告简称兜底(去重后取并集), '
                          u'源虽失败但覆盖已补全' % _fbn)
                else:
                    print(u'  [兜底] %-9s 本地清单也未找到(zt_shrink_st_coef_note_<账号>.txt), '
                          u'st 类只剩公告简称兜底' % 'st')
    if skip_ann:
        sources['ann'] = {'ok': False, 'n': 0, 'skip': True}
        print('  [SKIP] ann       用户指定跳过')
    else:
        t0 = time.time()
        try:
            hit, seen, skip, dmin, dmax = fetch_ann(bag, days, timeout, ann_pages)
            sources['ann'] = {'ok': True, 'n': hit, 'seen': seen, 'skip_note': skip,
                              'cover_from': dmin, 'cover_to': dmax,
                              'sec': round(time.time() - t0, 1)}
            print('  [OK] %-9s %4d 条命中 / 扫描%d条公告 / 跳过合规说明句%d条 (%.1fs)'
                  % ('ann', hit, seen, skip, time.time() - t0))
            # 2026-10-19 修正: 旧版只打"扫描2000条公告", 看不出 45 天窗口其实只走了 4 天
            #   (2000条=20页上限跑满, 不是撞到窗口起点才停), 立案/处罚的漏检被伪装成[OK]。
            if dmin and dmax:
                _span = 0
                try:
                    _span = (datetime.datetime.strptime(dmax, '%Y-%m-%d').date()
                             - datetime.datetime.strptime(dmin, '%Y-%m-%d').date()).days + 1
                except Exception:
                    _span = 0
                _cut = iso(datetime.date.today() - datetime.timedelta(days=int(days)))
                if dmin > _cut:
                    _need = 0
                    if _span > 0:
                        _need = (int(seen * int(days) / _span) + 99) // 100
                    print(u'       公告实扫区间 %s ~ %s (%d天, 实扫%d页) —— 未走完%d天窗口, 更早的'
                          u'立案/处罚本轮没扫到; 按此密度覆盖%d天约需 --ann-pages %d'
                          % (dmin, dmax, _span, (seen + 99) // 100, int(days), int(days), _need))
                else:
                    print(u'       公告实扫区间 %s ~ %s (%d天), 已覆盖%d天窗口'
                          % (dmin, dmax, _span, int(days)))
        except Exception as e:
            sources['ann'] = {'ok': False, 'n': 0, 'err': repr(e)[:120]}
            errors.append('ann: %r' % (e,))
            print('  [FAIL] %-9s %r' % ('ann', e))

    ok_any = any(v.get('ok') for v in sources.values())
    counts = {}
    for it in bag.d.values():
        for t in it['tags']:
            counts[t] = counts.get(t, 0) + 1
    out = {
        'schema': SCHEMA,
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'asof': ymd(datetime.date.today()),
        'window_days': int(days),
        'counts': counts,
        'total_stocks': len(bag.d),
        'stocks': bag.to_dict(),
        'sources': sources,
        'errors': errors,
        'note': u'本文件由 download_risk_exclude.py 自动生成; 策略侧命中即拒绝买入(不影响卖出与持仓管理)',
    }
    return out, ok_any


def self_test(path):
    """离线自检: 写一份样例 JSON(仅用于验证策略侧解析, 不参与实盘)

    2026-10-19 修正: 自检绝不写生产清单文件名。原实现直接把 5 只样例票写进
    zt_shrink_risk_exclude.json, 跑一次自检就等于把 000001.SZ/600000.SH/000002.SZ/
    600519.SH/002142.SZ 全部标成"命中排雷", 策略侧当天会拒买这 5 只正常股票,
    且清单文件看起来"生成正常", 极难排查。现固定加 _selftest 后缀,
    策略侧的 _risk_exclude_candidates() 只按精确主文件名+账号后缀查找, 不会误读。
    """
    stem = path[:-5] if path.lower().endswith('.json') else path
    path = stem + '_selftest.json'
    t = datetime.date.today()
    sample = {
        'schema': SCHEMA,
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'asof': ymd(t),
        'window_days': 45,
        'counts': {'st': 1, 'lift': 1, 'decrease': 1, 'loss': 1, 'ann': 1},
        'total_stocks': 5,
        'stocks': {
            '000001.SZ': {'tags': ['st'], 'reasons': [u'ST/退市风险(名称:*ST样例)']},
            '600000.SH': {'tags': ['lift'], 'reasons': [u'临近解禁(%s 首发原股东限售股份 12.0亿)' % iso(t)]},
            '000002.SZ': {'tags': ['decrease'], 'reasons': [u'大股东减持(%s 某投资 1.00%%)' % iso(t)]},
            '600519.SH': {'tags': ['loss'], 'reasons': [u'业绩预亏(%s公告, 报告期%s)' % (iso(t), iso(t))]},
            '002142.SZ': {'tags': ['ann'], 'reasons': [u'立案(%s 关于收到立案告知书的公告)' % iso(t)]},
        },
        'sources': {'st': {'ok': True, 'n': 1, 'self_test': True}},
        'errors': [],
        'note': u'--self-test 样例文件, 仅用于验证策略侧解析, 请勿用于实盘',
        'self_test': True,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(sample, f, ensure_ascii=False, indent=1)
    print(u'自检样例已写入: %s (5 只样例票, 仅供解析验证)' % path)
    print(u'提示: 生产清单未改动(自检文件带 _selftest 后缀, 策略侧不会读取)')
    return 0


def main():
    ap = argparse.ArgumentParser(description=u'涨停缩量策略 · 提前排雷清单生成工具')
    ap.add_argument('--days', type=int, default=45, help=u'窗口天数(默认45)')
    ap.add_argument('--out', default=DEFAULT_OUT, help=u'输出文件名(纯文件名)')
    ap.add_argument('--account', default='', help=u'资金账号: 生成 <名>_<账号>.json, 与策略读取路径对齐')
    ap.add_argument('--timeout', type=int, default=15, help=u'单请求超时秒(默认15)')
    ap.add_argument('--ann-pages', type=int, default=600,
                    help=u'公告最多扫描页数(每页100条, 默认600=60000条); 实际覆盖天数会在运行时打印。'
                         u'V2.6.145: 实测日均约919条公告, 45天窗口需约414页; 500页扫到42天差3天, '
                         u'提到600确保45天跑满(2026-10-21实扫43800条覆盖47天)')
    ap.add_argument('--skip-ann', action='store_true', help=u'跳过公告抓取(最快)')
    ap.add_argument('--self-test', action='store_true', help=u'离线自检: 写样例JSON, 不联网')
    args = ap.parse_args()

    if args.days <= 0 or args.days > 365:
        print(u'参数错误: --days 必须在 1..365')
        return 3
    if args.ann_pages <= 0 or args.ann_pages > 5000:
        print(u'参数错误: --ann-pages 必须在 1..5000')
        return 3
    name = os.path.basename(args.out)
    if name != args.out or '..' in name or '/' in name or '\\' in name:
        print(u'参数错误: --out 只允许纯文件名(防止写到其它目录)')
        return 3
    stem, ext = os.path.splitext(name)
    if args.account:
        name = '%s_%s%s' % (stem, str(args.account).strip(), ext or '.json')

    if args.self_test:
        return self_test(name)

    print(u'=== 排雷清单抓取 开始 %s | 窗口%d天 ==='
          % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), args.days))
    out, ok_any = build(args.days, args.timeout, args.skip_ann, args.ann_pages, args.account)

    if not ok_any:
        print(u'\n[中止] 全部数据源失败, 未覆盖已有文件(避免把排雷表清空造成虚假安全)。')
        print(u'       请检查网络/代理后重试; 已有文件保持原样。')
        return 2

    # 完整性闸门：任何必需数据源失败/跳过都不生成生产清单；有旧版则保留，无旧版则失败退出。
    _incomplete = bool(out.get('errors')) or any(v.get('skip') for v in (out.get('sources') or {}).values())
    if _incomplete:
        print(u'\n[保护] 数据源不完整，生产清单不写入；旧版如存在则保留: %s' % name)
        return 1

    tmp = name + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, name)

    print(u'\n=== 完成: %d 只命中 | %s ===' % (out['total_stocks'], name))
    if out['counts']:
        print(u'    分类: ' + u'  '.join('%s=%d' % (k, v) for k, v in sorted(out['counts'].items())))
    # 2026-10-19 新增: 账目自洽行 —— 594只 vs 612标签长期被当成 bug 追, 直接写出来
    _tag_sum = sum(int(v or 0) for v in out['counts'].values())
    print(u'    账目: 股票%d只 / 标签合计%d个 / 多标签重叠%d只(一只票可命中多类, 合计>股票数属正常)'
          % (out['total_stocks'], _tag_sum, _tag_sum - out['total_stocks']))
    if out['errors']:
        print(u'    ! 部分数据源失败(其余仍已写入): %s' % u'; '.join(out['errors'])[:300])
    print(u'    策略侧读取: 与状态文件同目录, 文件名按账号后缀自动对齐(见策略 RISK_EXCLUDE_FILE)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
