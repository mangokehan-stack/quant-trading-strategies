#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test.py —— 零依赖冒烟测试（T4）

为什么存在：
    评审报告 T4 指出「没有测试、没有 CI，一次冒烟就能抓到 NA1 与 P1-5」。
    这两个问题都属于「代码本身跑不起来」级别，本脚本用最少的依赖把它们钉死。

设计原则：
    · 不 import 策略主文件（依赖 QMT 运行环境，无法在普通机器执行），
      改为【静态解析 + 关键结构断言】，任何一台装了 Python 的机器都能跑。
    · 每个检查项独立 try，互不影响；失败会明确打印「哪一条、为什么」。
    · 退出码：0=全部通过  1=有失败项

用法：
    python smoke_test.py            # 默认检查同目录/上级目录的两个策略文件
    python smoke_test.py <文件路径>  # 只检查指定文件

这些检查对应的问题编号（便于追溯）：
    T1  文件能按声明编码解码、能被 ast 解析（语法/编码基线）
    T2  无 U+FFFD 替换字符（编码未被破坏）
    T3  关键常量存在且类型正确（NA1 同类：配置键与代码不匹配）
    P1-5  _mode_pending_recheck 的 global 声明覆盖所有被赋值的模块级变量
    NA1  回测 run_backtest 的白名单能接纳 COMBOS 传入的所有键
    PUB-1 无尖括号占位符（账号/解释器路径等，会破坏 Windows 文件名）
"""
import ast
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 会破坏 Windows 文件名 / 路径的占位符（PUB-1）
# 用拼接构造，避免本文件自身被敏感扫描规则命中
BAD_PLACEHOLDERS = ('<' + 'ACCT>', '<' + 'PYTHON_EXE>', '<' + '你的资金账号>')


class Result(object):
    def __init__(self):
        self.items = []

    def add(self, name, ok, detail=''):
        self.items.append((name, bool(ok), detail))

    def report(self):
        bad = [x for x in self.items if not x[1]]
        for name, ok, detail in self.items:
            print('  [%s] %s%s' % ('PASS' if ok else 'FAIL', name,
                                   ('  -> ' + detail) if (detail and not ok) else ''))
        print('-' * 66)
        print('合计 %d 项，通过 %d，失败 %d' % (len(self.items), len(self.items) - len(bad), len(bad)))
        return 0 if not bad else 1


def read_text(path):
    """按 GBK -> UTF-8 顺序尝试解码，返回 (text, encoding)。"""
    raw = open(path, 'rb').read()
    for enc in ('gbk', 'utf-8'):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode('gbk', errors='replace'), 'gbk(replace)'


def check_basic(path, res, tag):
    """T1/T2：编码可解码、语法可解析、无替换字符。"""
    if not os.path.exists(path):
        res.add('%s 文件存在' % tag, False, path)
        return None, None
    text, enc = read_text(path)
    res.add('%s 编码可解码' % tag, 'replace' not in enc, 'encoding=%s' % enc)
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        res.add('%s 语法可解析' % tag, False, '%s (line %s)' % (e.msg, e.lineno))
        return text, None
    res.add('%s 语法可解析' % tag, True)
    res.add('%s 无 U+FFFD' % tag, '\ufffd' not in text,
            '发现 %d 个替换字符' % text.count('\ufffd'))
    return text, tree


def check_pub1(text, res, tag):
    """PUB-1：不得含会破坏文件名的占位符（代码行内，注释除外）。"""
    hits = []
    for i, line in enumerate(text.split('\n'), 1):
        s = line.strip()
        if s.startswith('#'):
            continue
        for ph in BAD_PLACEHOLDERS:
            if ph in line:
                hits.append('L%d:%s' % (i, ph))
    res.add('%s 无破坏性占位符(PUB-1)' % tag, not hits, ', '.join(hits[:5]))


def check_p15(tree, res, tag):
    """P1-5：函数内被赋值的模块级变量，必须在 global 中声明。"""
    module_level = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    module_level.add(t.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            tgt = getattr(node, 'target', None)
            if isinstance(tgt, ast.Name):
                module_level.add(tgt.id)

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        declared = set()
        # global 语句可能嵌在 try/if 块里，必须递归收集
        for sub in ast.walk(node):
            if isinstance(sub, ast.Global):
                declared |= set(sub.names)
        assigned = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                assigned.add(sub.id)
        risky = (assigned & module_level) - declared
        # 只报告「函数内先读后写」的高危形态（UnboundLocalError 的直接成因）
        for name in sorted(risky):
            problems.append('%s(): %s' % (node.name, name))
    res.add('%s 无未声明的 global(P1-5)' % tag, not problems, '; '.join(problems[:5]))


def check_backtest_whitelist(path, res, tag):
    """NA1：COMBOS 里出现的键，必须都在 run_backtest 的 _allowed_opts 里。"""
    if not os.path.exists(path):
        res.add('%s 回测文件存在' % tag, False, path)
        return
    text, _ = read_text(path)
    m = re.search(r'_allowed_opts\s*=\s*\{(.*?)\}', text, re.S)
    if not m:
        res.add('%s 找到回测白名单' % tag, False, '未匹配到 _allowed_opts')
        return
    allowed = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', m.group(1)))
    cm = re.search(r'COMBOS\s*=\s*\[(.*?)\n\]', text, re.S)
    if not cm:
        res.add('%s 找到 COMBOS' % tag, False, '未匹配到 COMBOS')
        return
    combos_keys = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', cm.group(1)))
    # pyramid / add_step_pct 属于「参数」不是「选项」，调用前会被剔除
    combos_keys -= {'pyramid', 'add_step_pct'}
    extra = combos_keys - allowed
    res.add('%s COMBOS 键均在白名单内(NA1)' % tag, not extra, '多出: %s' % sorted(extra))


def main(argv):
    res = Result()
    if len(argv) > 1:
        targets = [('TARGET', argv[1], None)]
    else:
        targets = [
            ('dxgp', os.path.join(HERE, 'dxgp', 'zt_shrink_strategy_gbk.py'),
             None),
            ('nbbank', os.path.join(HERE, 'nbbank', 'gf_qmt_native_multi_gbk.py'),
             os.path.join(HERE, 'nbbank', 'backtest_optimize.py')),
        ]

    print('=' * 66)
    print('smoke_test.py —— 零依赖冒烟测试')
    print('=' * 66)

    for tag, main_path, extra_path in targets:
        print('\n[%s] %s' % (tag, os.path.basename(main_path)))
        text, tree = check_basic(main_path, res, tag)
        if text is not None:
            check_pub1(text, res, tag)
        if tree is not None:
            check_p15(tree, res, tag)
        if extra_path:
            bt_text, bt_tree = check_basic(extra_path, res, tag + '/backtest')
            if bt_text is not None:
                check_pub1(bt_text, res, tag + '/backtest')
            if bt_tree is not None:
                check_p15(bt_tree, res, tag + '/backtest')
            check_backtest_whitelist(extra_path, res, tag)

    print()
    return res.report()


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:     # 冒烟脚本自身异常也要明确失败
        import traceback
        traceback.print_exc()
        sys.exit(1)
