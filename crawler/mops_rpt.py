#!/usr/bin/env python3
"""
公開資訊觀測站 財報附註「關係人交易」擷取器
------------------------------------------------
來源：mopsov.twse.com.tw/server-java/t164sb01（Inline XBRL 財報頁，含附註與附表）
輸出：data/rpt/{代號}.json，每檔保留最近 N 季

原則：只搬運原文表格與文字，不推算、不補值；解析不到就留空並記錄在 notes。

用法：
  python crawler/mops_rpt.py --tickers 2313,2330      # 指定代號
  python crawler/mops_rpt.py --auto --budget 50       # 排程：觀察名單優先，再輪全體上市櫃
"""
import argparse, json, os, re, sys, time, datetime as dt
import urllib.request, urllib.error
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'rpt')
STATE = os.path.join(OUT, '_state.json')
INDEX = os.path.join(OUT, '_index.json')
WATCH = os.path.join(ROOT, 'watchlist.txt')
MOPS = 'https://mopsov.twse.com.tw/server-java/t164sb01'
UA = 'Mozilla/5.0 (compatible; itv-rpt-crawler/1.0; +public research, 4s interval)'
INTERVAL = float(os.environ.get('MOPS_INTERVAL', '8'))
KEEP_Q = int(os.environ.get('KEEP_QUARTERS', '12'))
FULL_DETAIL_Q = 4   # 最近 4 季保留完整表格；更早的季別只留關係人名單，控制資料量
PARSER_VERSION = 5
DEBUG_HTML = os.environ.get('DEBUG_HTML', '') in ('1', 'true', 'True')
DEBUG_DIR = os.path.join(ROOT, 'data', 'debug')

_last = [0.0]


class Blocked(Exception):
    pass


def http_get(url, timeout=60):
    wait = INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308, 403, 429):
            raise Blocked(f'HTTP {e.code}')
        raise


def get_mops(code, year, q, report_id):
    url = f'{MOPS}?step=1&CO_ID={code}&SYEAR={year}&SSEASON={q}&REPORT_ID={report_id}'
    backoff = 120
    for attempt in range(4):
        try:
            raw = http_get(url)
            html = raw.decode('cp950', errors='replace')
            return url, html
        except Blocked as e:
            print(f'  被擋（{e}），暫停 {backoff} 秒', flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 900)
        except Exception as e:
            print(f'  連線錯誤 {e}，暫停 {backoff} 秒', flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 900)
    raise Blocked('連續被擋')


def is_missing(html):
    return len(html) < 4000 and re.search(r'檔案不存在|查無|不存在', html)


# ---------------------------------------------------------------- parsing
# v2：依觀測站 t164sb01 實際格式改寫。
# 關係人交易以一連串小表格呈現：首列為標題（例：「應付關係人款項」），其後為「帳列項目｜關係人類別／名稱｜金額…」，
# 資料列的名稱多寫成「實質關係人－OYKA KAGIT」「主要管理階層相同－允成投資」；部分公司以整段文字（含 $ 金額）揭露。
WS = re.compile(r'[\s　\xa0]+')


def clean(s):
    return WS.sub(' ', s or '').strip()


NAME_COL = re.compile(r'關係人(類別)?[／/]?(名稱|姓名)|名稱[／/]類別|^關係人名稱|^名稱$|^關係人$')
REL_COL = re.compile(r'關係$|之關係|與.{0,6}關係')
CAT = r'(其他)?關係人|實質關係人|子公司|孫公司|關聯企業|合資|母公司|最終母公司|最終控制者|主要管理階層(相同)?|具重大影響(力)?(之)?投資者|投資者|關係企業|董事|監察人|股東|其他'
CAT_ONLY = re.compile(r'^(' + CAT + r')(之關係企業|之子公司)?$')
CAT_SPLIT = re.compile(r'^(?P<rel>.{0,30}?(?:' + CAT + r')[^－\-–/／]{0,12})\s*[－\-–/／]\s*(?P<name>.+)$')
PERIOD_ROW = re.compile(r'^(本期|去年|上期|當期|期末|期初|\d{2,3}[年.]|至|合計|小計|總計|單位|金額|\(?\d)')
NUMERIC = re.compile(r'^[\s$（）()\-－,.\d%]*$')
ACCT = re.compile(r'(工程|擴建|修繕|維修|材料款等|款等|備註|融資款|附註|註記|資金貸與|背書保證|流動|非流動|建物|土地|權利金|包工包料|費用|成本|收入|負債|資產|款項|帳款|票據|保證金|合計|小計|總計|利息|租金|股利|收益|損失|淨額|設備|價款|金額|進貨|銷貨|期末|期初|年度|本期|福利|薪酬|借款|放款|存款|權益|事項|說明)$|^(應收|應付|預付|預收|合計|營業|其他應|租賃|財務|帳列)')
SELF = re.compile(r'^(本公司|合併公司|母公司|子公司|孫公司|該公司|各公司|關係人|被投資公司|本集團|集團|兄弟公司|姊妹公司|關係企業|其他公司)$')
ENTITY = re.compile(r'(股份有限公司|有限公司|\(股\)公司|（股）公司|公司|基金會|協會|學校|醫院|銀行|Co\.|Corp|Inc|Ltd|Limited|LLC|GmbH|B\.V\.|S\.A\.|Pte|Holding|Group|L\.P\.|Trust)', re.I)
ALIAS = re.compile(r'[（(]\s*(?:以下)?簡稱\s*[「『]?([^）)」』]{1,20})[」』]?\s*[）)]')
NOTE = re.compile(r'[（(]\s*註\s*[\d一二三四五六七八九十]*\s*[）)]|\*+$')
SCHED = [
    ('資金貸與他人', re.compile(r'^資金貸與他人'), re.compile(r'貸與對象')),
    ('為他人背書保證', re.compile(r'^為他人背書保證'), re.compile(r'被背書保證對象')),
    ('與關係人進、銷貨達一億元或實收資本額20%以上', re.compile(r'與關係人進[、,，]?銷貨'), re.compile(r'交易對象|對象名稱|關係人名稱|名稱')),
    ('應收關係人款項達一億元或實收資本額20%以上', re.compile(r'應收關係人款項達'), re.compile(r'交易對象|對象名稱|關係人名稱|名稱')),
    ('母子公司間業務關係及重要交易往來', re.compile(r'母子公司間(之)?業務關係'), re.compile(r'交易往來對象')),
]


def table_rows(t):
    rows = []
    for tr in t.find_all('tr'):
        cells = [clean(c.get_text(' ')) for c in tr.find_all(['td', 'th'])]
        if any(cells):
            rows.append(cells)
    return rows


def caption_of(t):
    prev = t
    for _ in range(12):
        prev = prev.find_previous(string=True)
        if prev is None:
            break
        s = clean(prev)
        if 2 <= len(s) <= 60 and not re.fullmatch(r'[\d,.()\-]+', s):
            return s
    return ''


def blocks_of(soup):
    out = []
    for t in soup.find_all('table'):
        if t.find('table'):
            continue
        rows = table_rows(t)
        if rows:
            first = rows[0][0] if len(rows[0]) == 1 else ''
            out.append({'title': first or caption_of(t), 'rows': rows})
    return out


def is_rp(b):
    head = ' '.join(c for r in b['rows'][:4] for c in r)[:600]
    if re.search(r'關係人|關係企業', b['title']) and not re.search(r'^(資金貸與|為他人背書|母子公司間)', b['title']):
        return True
    if re.search(r'關係人(類別|名稱|姓名)', head):
        return True
    return any(len(c) > 150 and re.search(r'關係人(名稱|類別)', c) for r in b['rows'] for c in r)


BAD_START = re.compile(r'^(本公司|合併公司|本集團|集團|由|為|係|與|依|對|向|之|及|份有限公司|有限公司|公司$)')
BAD_END = re.compile(r'(之個體|者|之合資|之董事|為該公司|。|，)$')
CJK_SP = re.compile(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])')


def split_name(v, rel=''):
    v = NOTE.sub('', clean(v)).strip(' ：:')
    v = CJK_SP.sub('', v) if re.fullmatch(r'[\u4e00-\u9fff\s]+', v) else v
    if not v or NUMERIC.match(v) or len(v) > 80 or ('，' in v or '。' in v) and len(v) > 20:
        return None
    m = CAT_SPLIT.match(v)
    if m:
        rel, v = m.group('rel').strip(), m.group('name').strip()
    v = CJK_SP.sub('', v) if re.fullmatch(r'[\u4e00-\u9fff\s]+', v) else v
    while v and v[-1] in ')）' and v.count('(') + v.count('（') < v.count(')') + v.count('）'):
        v = v[:-1].strip()
    if re.search(r'支付|總價|動支|借款|擔保|全體|變更後|房屋|建築|^董事長$|總經理|之子公司$|^限公司|^Co\.?$|^(Ltd|Inc|Corp|LLC|Limited|Co\., ?Ltd)\.?$|^其他$', v, re.I) or ('、' in v and len(v) > 12):
        return None
    if CAT_ONLY.match(v) or SELF.match(v) or len(v) < 2 or ACCT.search(v) or BAD_START.search(v) or BAD_END.search(v) or re.search(r'關係人|管理階層|關聯企業|合資|名稱|姓名|^金額$', v):
        return None
    alias = ''
    a = ALIAS.search(v)
    if a:
        alias, v = a.group(1).strip(), clean(ALIAS.sub('', v))
    return {'name': v, 'alias': alias, 'relation': clean(rel)}


def names_from_blob(txt):
    out = []
    toks = txt.split(' ')
    for i, t in enumerate(toks):
        t = NOTE.sub('', t).strip('：:，,。;；')
        cand = None
        if i + 1 < len(toks) and toks[i + 1] in ('$', '＄') and re.search(r'[一-鿿A-Za-z]', t):
            cand = t
        elif ENTITY.search(t) and re.search(r'(公司|Co\.|Ltd|Inc|Corp|Limited)$', t, re.I):
            cand = re.split(r'[，,。；;：:]', t)[-1]
        if cand and 2 <= len(cand) <= 40 and not re.search(r'\d', cand):
            r = split_name(cand)
            if r and not SELF.match(r['name']) and not re.search(r'^(對|與|向|之|及|係|由|為)', r['name']):
                out.append(r)
    return out


def row_stream(soup):
    """依文件順序展開所有表格列（巢狀表格：外層含子表格的列略過，改讀子表格的列），並保留表格前的短標題文字。"""
    out = []
    for t in soup.find_all('table'):
        if t.find_parent('table') is not None:
            continue
        cap = caption_of(t)
        if cap:
            out.append(['__CAP__', cap])
        for tr in t.find_all('tr'):
            if tr.find('table'):
                continue
            cells = [clean(c.get_text(' ')) for c in tr.find_all(['td', 'th'], recursive=False)] or [clean(c.get_text(' ')) for c in tr.find_all(['td', 'th'])]
            if any(cells):
                out.append(cells)
        out.append(['__END__'])
    return out


def parties_from_stream(rows):
    out, seen = [], {}
    rp_tables, cur_tbl = [], None

    def add(r, src):
        if not r:
            return
        k = r['name']
        if k in seen:
            if r['relation'] and not seen[k]['relation']:
                seen[k]['relation'] = r['relation']
            return
        r['src'] = src
        seen[k] = r
        out.append(r)

    title, ni, ri, rp_ctx, since_title = '', None, None, False, 0
    for r in rows:
        if r[0] == '__END__':
            ni, ri = None, None
            continue
        if r[0] == '__CAP__':
            if re.search(r'關係人|關係企業', r[1]):
                title, rp_ctx, since_title = r[1], True, 0
            continue
        since_title += 1
        if len(r) == 1 and len(r[0]) <= 60:
            t = r[0]
            if re.search(r'關係人|關係企業', t) and not re.search(r'^(資金貸與|為他人背書|母子公司間)', t):
                title, rp_ctx, since_title, ni, ri = t, True, 0, None, None
                cur_tbl = {'title': t, 'rows': [r]}
                rp_tables.append(cur_tbl)
            elif re.match(r'^(\(?[一二三四五六七八九十0-9]+[)、.．]|附表|質押|重大|部門|其他重要)', t) or since_title > 60:
                rp_ctx, cur_tbl = False, None
            continue
        if cur_tbl is not None and rp_ctx:
            cur_tbl['rows'].append(r)
        if any(len(c) <= 40 and NAME_COL.search(c) for c in r) and (rp_ctx or any(len(c) <= 40 and '關係人' in c for c in r)):
            ni = next(i for i, c in enumerate(r) if len(c) <= 40 and NAME_COL.search(c))
            ri = next((i for i, c in enumerate(r) if len(c) <= 40 and REL_COL.search(c) and i != ni), None)
            rp_ctx = True
            if cur_tbl is None:
                cur_tbl = {'title': title or '關係人表格', 'rows': [r]}
                rp_tables.append(cur_tbl)
            continue
        if ni is not None and rp_ctx:
            if not (PERIOD_ROW.match(r[0]) and all(PERIOD_ROW.match(c) or NUMERIC.match(c) for c in r)):
                v = r[ni] if ni < len(r) else ''
                add(split_name(v, r[ri] if ri is not None and ri < len(r) else ''), title or '關係人表格')
        for c in r:
            if len(c) > 150 and re.search(r'關係人', c):
                for x in names_from_blob(c):
                    add(x, '附註文字（依 $ 金額前的名稱推測）')
            elif rp_ctx and len(c) <= 80 and CAT_SPLIT.match(c):
                add(split_name(c), title or '關係人表格')
    return out, rp_tables


def parties_from(blocks):
    """相容：把 blocks 轉成列串流後解析。"""
    rows = []
    for b in blocks:
        if b.get('title') and not (len(b['rows'][0]) == 1 and b['rows'][0][0] == b['title']):
            rows.append(['__CAP__', b['title']])
        rows.extend(b['rows'])
        rows.append(['__END__'])
    return parties_from_stream(rows)[0]


def sched_from(blocks):
    res, names = [], []
    for b in blocks:
        for title, trx, colrx in SCHED:
            if not trx.search(b['title']):
                continue
            res.append({'title': title, 'id': '', 'tables': [b]})
            rows = b['rows']
            hi = next((i for i, r in enumerate(rows[:4]) if any(colrx.search(c) for c in r)), None)
            if hi is None:
                break
            ci = next(i for i, c in enumerate(rows[hi]) if colrx.search(c))
            ri = next((i for i, c in enumerate(rows[hi]) if REL_COL.search(c)), None)
            for r in rows[hi + 1:]:
                if len(r) <= ci or not r[0] or not re.match(r'^\d+$', r[0]):
                    continue
                x = split_name(r[ci], r[ri] if ri is not None and ri < len(r) else '')
                if x and x['name'] not in [n['name'] for n in names if n['title'] == title]:
                    x['title'] = title
                    x['from'] = r[1] if len(r) > 1 else ''
                    names.append(x)
            break
    return res, names


NIL = re.compile(r'無此情形|免揭露|不適用')
NAME_IX = re.compile(r'RelatedParty.*Name|NameOfRelatedParty|NameOfParentEntity', re.I)


RP_ITEMS = ('關係人名稱及關係', '營業收入', '進貨', '應收關係人款項', '應付關係人款項', '預付款項', '取得不動產、廠房及設備',
            '處分不動產、廠房及設備', '取得投資性不動產', '處分投資性不動產', '取得金融資產', '處分金融資產', '取得其他資產',
            '處分其他資產', '對關係人放款', '背書保證', '其它', '向關係人借款', '主要管理階層薪酬')


def split_items(title, rows):
    """新版財報偶有表格未正確結束，後面的項目被併進同一張表：遇到已知項目名稱或薪酬表頭就切開。"""
    out, cur = [], {'title': title, 'rows': []}
    for i, r in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else []
        new = None
        if r and r[0] in RP_ITEMS and (cur['rows'] or r[0] != title):
            new = r[0]
        elif r and r[0] == '' and nxt[:1] == ['短期員工福利']:
            new = '主要管理階層薪酬'
        if cur['title'] == '主要管理階層薪酬' and cur['rows'] and cur['rows'][-1][:1] == ['說明']:
            break                     # 薪酬表是段落最後一項；之後的列屬於其他附註，丟棄
        if new and cur['rows']:
            out.append(cur)
            cur = {'title': new, 'rows': []}
        elif new:
            cur['title'] = new
        cur['rows'].append(r)
    if cur['rows']:
        out.append(cur)
    return out


def norm_co(n):
    return re.sub(r'[（(]股[)）]\s*公司', '股份有限公司', n)


def section_rp(soup):
    """v4：新版 iXBRL 財報以 <div id="TransactionsBetweenRelatedParties"> 為錨點，
    其下是 div.title（大標）、置中 div（項目名稱）＋「無此情形／免揭露」或表格。"""
    a = soup.find('div', id='TransactionsBetweenRelatedParties')
    if a is None:
        return None
    sec = {'found': True, 'parent': '', 'nil': [], 'items': [], 'kmp': [], 'ixNames': []}
    label, big = '', ''
    for el in a.find_all_next(['div', 'table']):
        if el.name == 'div' and el.get('id'):
            break
        if el.find_parent('table') is not None:
            continue
        if el.name == 'div':
            t = clean(el.get_text(' '))
            if not t or el.find('table'):
                continue
            if 'title' in (el.get('class') or []):
                big = label = t
            elif NIL.search(t) and len(t) <= 20:
                if label and label not in sec['nil']:
                    sec['nil'].append(label)
            elif len(t) <= 40 and 'rptidx' not in (el.get('class') or []):
                label = t
            continue
        rows = table_rows(el)
        if not rows:
            continue
        for ix in el.find_all(lambda x: x.name and x.name.lower().endswith('nonnumeric')):
            nm = ix.get('name', '')
            v = clean(ix.get_text(' '))
            if 'NameOfParentEntity' in nm:
                sec['parent'] = v
            elif NAME_IX.search(nm) and v and v not in sec['ixNames']:
                sec['ixNames'].append(v)
        head = ' '.join(c for r in rows[:2] for c in r)
        if re.search(r'母公司及最終控制者', head) and len(rows) <= 2:
            if not sec['parent']:
                sec['parent'] = rows[0][-1]
            continue
        if re.search(r'主要管理階層', head):
            sec['kmp'] = rows
            continue
        ttl = label or big or '關係人交易'
        if len(rows[0]) == 1 and len(rows[0][0]) <= 60:
            ttl = rows[0][0]
        parts = split_items(ttl, rows)
        for it in parts:
            if len(it['rows']) <= 1:
                continue
            if any(r[:1] == ['短期員工福利'] for r in it['rows']):
                sec['kmp'] = [r for r in it['rows'] if r != ['主要管理階層薪酬']] if it['title'] == '主要管理階層薪酬' else it['rows']
            elif it['title'] == '背書保證' and all(len(c) > 60 or c == '背書保證' for r in it['rows'] for c in r):
                continue   # 只有法定說明文字（七種關係），非實際交易
            else:
                sec['items'].append(it)
        label = ''
        if sec['kmp']:
            break
    return sec


def parse_report(html):
    soup = BeautifulSoup(html, 'html.parser')
    blocks = blocks_of(soup)
    parties, rp_tables = parties_from_stream(row_stream(soup))
    rp = [b for b in blocks if is_rp(b)]          # 另以最內層表格判斷，兩種結果合併
    names = {p['name'] for p in parties}
    for x in parties_from(rp):
        if x['name'] not in names:
            parties.append(x)
            names.add(x['name'])
    if rp and not rp_tables:
        rp_tables = rp
    sec = section_rp(soup)
    if sec:
        srows = []
        for it in sec['items']:
            srows.append(['__CAP__', '關係人交易－' + it['title'] if '關係人' not in it['title'] else it['title']])
            srows.extend(it['rows'])
            srows.append(['__END__'])
        sp, _ = parties_from_stream(srows)
        if sec['items']:          # 有段落表格時以段落為準，避免附註文字推測造成雜訊
            parties, names, rp_tables = [], set(), []
        for nm in sec['ixNames']:
            x = split_name(nm)
            if x:
                x['src'] = '財報 iXBRL 關係人名稱欄位'
                sp.append(x)
        byk = {norm_co(p['name']): p for p in parties}
        for x in sp:
            k = norm_co(x['name'])
            if k in byk:
                if x['relation'] and not byk[k]['relation']:
                    byk[k]['relation'] = x['relation']
                continue
            x['name'] = k
            byk[k] = x
            parties.append(x)
            names.add(k)
        core = lambda rows: [r for r in rows if len(r) > 1]
        for it in sec['items']:
            if not any(core(it['rows']) == core(b['rows']) for b in rp_tables):
                rp_tables.append(it)
    notes, info = [], []
    if sec and sec['nil'] and not sec['items'] and not parties:
        info.append(f"公司在財報關係人交易附註申報「無此情形／免揭露」（{len(sec['nil'])} 項：{'、'.join(sec['nil'][:6])}{'…' if len(sec['nil']) > 6 else ''}）")
    elif not rp_tables and not parties:
        notes.append('找不到「關係人交易」相關表格')
    elif not parties:
        notes.append('有關係人交易表格，但未辨識出具名關係人（可能全為類別揭露）')
    m = re.search(r'單位[:：]\s*(新臺幣|新台幣)?\s*(仟元|千元|元|百萬元)', soup.get_text(' ')[:200000])
    unit = ((m.group(1) or '新臺幣') + m.group(2)) if m else ''
    schedules, sched_names = sched_from(blocks)
    texts = []
    for b in rp_tables:
        for r in b['rows']:
            for c in r:
                if len(c) > 150 and c not in texts:
                    texts.append(c[:6000])
    tables = [b for b in rp_tables if not all(len(r) == 1 and len(r[0]) > 150 for r in b['rows'])]
    return {'how': 'section-v4' if sec else 'tables-v3', 'unit': unit, 'parties': parties, 'tables': tables[:80], 'texts': texts[:30],
            'schedules': schedules, 'schedNames': sched_names, 'notes': notes, 'info': info,
            'rpSec': ({'parent': sec['parent'], 'nil': sec['nil'], 'kmp': sec['kmp'][:12], 'items': len(sec['items'])} if sec else None)}


# ---------------------------------------------------------------- quarters & crawl
def recent_quarters(n=8, today=None):
    today = today or dt.date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        out.append((y, q))
    return out


def crawl_one(code, keep=KEEP_Q, known=None):
    path = os.path.join(OUT, f'{code}.json')
    old = {}
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding='utf-8'))
        except Exception:
            old = {}
    have = {f"{x['year']}Q{x['quarter']}": x for x in old.get('quarters', []) if x.get('parser') == PARSER_VERSION}
    missing = set(old.get('missing', []))
    recent2 = {f'{y}Q{q}' for (y, q) in recent_quarters(2)}
    quarters = []
    for (y, q) in recent_quarters(KEEP_Q + 3):
        if len(quarters) >= keep:
            break
        k = f'{y}Q{q}'
        if k in have and not (DEBUG_HTML and not quarters):
            quarters.append(have[k])
            continue
        if k in missing and k not in recent2:
            continue
        got = None
        try:
            fetched = [get_mops(code, y, q, rid) for rid in ('C',)]
        except Blocked:
            print(f'  {code} {k}：觀測站阻擋，先存已抓到的季別', flush=True)
            _save(code, path, old, quarters, missing, known)
            raise
        for rid in ('C', 'A'):
            try:
                url, html = fetched[0] if rid == 'C' else get_mops(code, y, q, rid)
            except Blocked:
                _save(code, path, old, quarters, missing, known)
                raise
            if is_missing(html):
                continue
            if DEBUG_HTML and not quarters:
                dump_debug(code, k, rid, html)
            rep = parse_report(html)
            got = {'year': y, 'quarter': q, 'roc': f'{y-1911}Q{q}', 'reportId': rid, 'reportType': '合併' if rid == 'C' else '個體',
                   'url': url, 'fetched': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds'), 'parser': PARSER_VERSION, **rep}
            break
        if got:
            quarters.append(got)
            missing.discard(k)
            print(f'  {code} {k}：關係人 {len(got["parties"])} 筆、附表對象 {len(got.get("schedNames",[]))} 筆、表格 {len(got["tables"])} 張 {got["notes"]}', flush=True)
        else:
            if k not in recent2:
                missing.add(k)
            print(f'  {code} {k}：觀測站無此季財報', flush=True)
    return _save(code, path, old, quarters, missing, known)


def dump_debug(code, k, rid, html):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    idxs = [m.start() for m in re.finditer('關係人', html)]
    info = {'code': code, 'quarter': k, 'reportId': rid, 'size': len(html), 'hits_關係人': len(idxs),
            'first_hits': idxs[:40], 'tables': html.count('<table'), 'divs': html.count('<div'), 'pre': html.count('<pre')}
    start = max(0, (idxs[0] if idxs else 0) - 20000)
    # 取第一個「關係人交易」標題附近，若無則取第一個「關係人」附近
    t = html.find('id="TransactionsBetweenRelatedParties"')
    if t < 0:
        t = html.find('關係人交易')
    if t >= 0:
        start = max(0, t - 20000)
    snippet = html[start:start + 400000]
    open(os.path.join(DEBUG_DIR, f'{code}_{k}_{rid}.html'), 'w', encoding='utf-8').write(snippet)
    json.dump(info, open(os.path.join(DEBUG_DIR, f'{code}_{k}_{rid}.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'  除錯：已存 {code} {k} 原始頁面片段（全頁 {len(html)} 字、「關係人」出現 {len(idxs)} 次、table {info["tables"]} 個）', flush=True)


def _save(code, path, old, quarters, missing, known):
    quarters = list(quarters)
    for x in old.get('quarters', []):
        if not any(q['year'] == x['year'] and q['quarter'] == x['quarter'] for q in quarters) and x.get('parser') == PARSER_VERSION:
            quarters.append(x)
    quarters.sort(key=lambda x: (x['year'], x['quarter']), reverse=True)
    quarters = quarters[:KEEP_Q]
    for i, x in enumerate(quarters):
        if i >= FULL_DETAIL_Q:
            x['tables'], x['texts'] = [], []
            x['schedules'] = [{'title': sc['title'], 'id': '', 'tables': []} for sc in x.get('schedules', [])]
            x['slim'] = True
    data = {'code': code, 'name': (known or {}).get('name', old.get('name', '')), 'updated': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds'),
            'source': '公開資訊觀測站 t164sb01 財報（Inline XBRL）附註「關係人交易」與附表', 'quarters': quarters, 'missing': sorted(missing)}
    os.makedirs(OUT, exist_ok=True)
    json.dump(data, open(path, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    return data


def load_json(p, d):
    try:
        return json.load(open(p, encoding='utf-8'))
    except Exception:
        return d


def universe():
    """上市（證交所 OpenAPI）＋上櫃（櫃買中心 OpenAPI）公司代號。"""
    out = {}
    for url, mkt in (('https://openapi.twse.com.tw/v1/opendata/t187ap03_L', '上市'), ('https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O', '上櫃'), ('https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap03_O', '上櫃')):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            rows = json.loads(urllib.request.urlopen(req, timeout=60).read().decode('utf-8'))
            for r in rows:
                code = str(r.get('公司代號') or r.get('SecuritiesCompanyCode') or r.get('CompanyCode') or '').strip()
                name = str(r.get('公司簡稱') or r.get('CompanyAbbreviation') or r.get('公司名稱') or r.get('CompanyName') or '').strip()
                if re.fullmatch(r'\d{4}', code):
                    out[code] = {'name': name, 'market': mkt}
        except Exception as e:
            print('universe 讀取失敗', url, e, flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', default='')
    ap.add_argument('--auto', action='store_true')
    ap.add_argument('--budget', type=float, default=50, help='分鐘')
    a = ap.parse_args()
    t0 = time.time()
    state = load_json(STATE, {})
    uni = universe()
    targets = [c for c in re.split(r'[,\s]+', a.tickers) if re.fullmatch(r'\d{4,6}', c)]
    if a.auto:
        watch = []
        if os.path.exists(WATCH):
            watch = [l.split('#')[0].strip() for l in open(WATCH, encoding='utf-8')]
            watch = [c for c in watch if re.fullmatch(r'\d{4,6}', c)]
        latest = recent_quarters(1)[0]
        lk = f'{latest[0]}Q{latest[1]}'

        def stale(c):
            st = state.get(c)
            if not st:
                return 0                                   # 從未抓過
            age = time.time() - st.get('t', 0)
            if st.get('latest') != lk and age > 2 * 86400:
                return 1                                   # 缺最新一季
            if not st.get('full') and age > 6 * 3600:
                return 2                                   # 補抓到 12 季
            return 9
        rest = sorted([c for c in uni if c not in watch], key=lambda c: (stale(c), state.get(c, {}).get('t', 0)))
        targets = [c for c in watch if stale(c) < 9] + [c for c in rest if stale(c) < 9]
        deep = set(watch) | {c for c in rest if stale(c) == 2}
    else:
        deep = set(targets)
    done = 0
    for code in targets:
        if (time.time() - t0) / 60 > a.budget:
            print('時間預算用完，下一批再繼續', flush=True)
            break
        print(f'[{done+1}/{len(targets)}] {code} {uni.get(code, {}).get("name", "")}', flush=True)
        try:
            full = code in deep
            d = crawl_one(code, keep=KEEP_Q if full else 1, known=uni.get(code))
            lq = d['quarters'][0] if d['quarters'] else None
            prev = state.get(code, {})
            state[code] = {'t': time.time(), 'latest': f"{lq['year']}Q{lq['quarter']}" if lq else '', 'n': len(d['quarters']), 'full': full or prev.get('full', False) and prev.get('latest') == (f"{lq['year']}Q{lq['quarter']}" if lq else '')}
        except Blocked:
            print('::warning::觀測站持續阻擋，已存下抓到的部分，結束本批（下一批會接著抓）', flush=True)
            break
        done += 1
        if done % 10 == 0:
            json.dump(state, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False)
    os.makedirs(OUT, exist_ok=True)
    json.dump(state, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False)
    idx = {c: {'name': uni.get(c, {}).get('name', ''), 'latest': s.get('latest', ''), 't': int(s.get('t', 0))} for c, s in state.items()}
    json.dump(idx, open(INDEX, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print(f'完成 {done} 檔，用時 {(time.time()-t0)/60:.1f} 分', flush=True)


if __name__ == '__main__':
    main()
