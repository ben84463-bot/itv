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
INTERVAL = float(os.environ.get('MOPS_INTERVAL', '4'))
KEEP_Q = int(os.environ.get('KEEP_QUARTERS', '4'))
PARSER_VERSION = 1

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
            print(f'  連線錯誤 {e}', flush=True)
            time.sleep(10)
    raise Blocked('連續被擋')


def is_missing(html):
    return len(html) < 4000 and re.search(r'檔案不存在|查無|不存在', html)


# ---------------------------------------------------------------- parsing
WS = re.compile(r'[\s　\xa0]+')


def clean(s):
    return WS.sub(' ', s or '').strip()


RPT_ID = re.compile(r'RelatedPart', re.I)
SCHED_ID = re.compile(r'(Endorsement|Guarantee|LoansToOthers|FinancingProvided|Intercompany|BusinessRelationship|SalesToRelated|PurchasesFromRelated|ReceivablesFromRelated)', re.I)
RPT_TITLE = re.compile(r'^(\(?[一二三四五六七八九十0-9]+[)、.．]?\s*)?關係人交易$')
SCHED_TITLES = [
    ('資金貸與他人', re.compile(r'資金貸與他人')),
    ('為他人背書保證', re.compile(r'為他人背書保證|背書保證')),
    ('與關係人進、銷貨達一億元或實收資本額20%以上', re.compile(r'與關係人進[、,，]?銷貨')),
    ('應收關係人款項達一億元或實收資本額20%以上', re.compile(r'應收關係人款項')),
    ('母子公司間業務關係及重要交易往來', re.compile(r'母子公司間(之)?業務關係')),
]
STOP_TITLE = re.compile(r'^(\(?[一二三四五六七八九十0-9]+[)、.．]?\s*)?(質押之資產|重大或有負債|重大之災害損失|重大之期後事項|其他|附註揭露事項|部門資訊)')
ENTITY = re.compile(r'(股份有限公司|有限公司|公司|基金會|協會|學校|醫院|銀行|Co\.|Corp|Inc|Ltd|Limited|LLC|GmbH|B\.V\.|S\.A\.|Pte|Holding|Group|L\.P\.|Trust)', re.I)
ALIAS = re.compile(r'[（(]\s*(?:以下)?簡稱\s*[「『]?([^）)」』]{1,20})[」』]?\s*[）)]')
CATEGORY_ONLY = re.compile(r'^(子公司|關聯企業|合資|其他關係人|主要管理階層|母公司|最終母公司|實質關係人|董事|監察人|本公司之董事長|合計|小計|總計|其他)$')


def table_rows(t):
    rows = []
    for tr in t.find_all('tr'):
        cells = [clean(c.get_text(' ')) for c in tr.find_all(['td', 'th'])]
        if any(cells):
            rows.append(cells)
    return rows


def caption_of(t):
    """表格前最近一段短文字，當作表格標題。"""
    prev = t
    for _ in range(12):
        prev = prev.find_previous(string=True)
        if prev is None:
            break
        s = clean(prev)
        if 2 <= len(s) <= 60 and not re.fullmatch(r'[\d,.()\-]+', s):
            return s
    return ''


def collect_from(nodes):
    """從一組節點收集表格與文字（去重）。"""
    tables, texts, seen = [], [], set()
    for n in nodes:
        for t in ([n] if n.name == 'table' else n.find_all('table')):
            if id(t) in seen or t.find_parent('table') is not None and t.find_parent('table') in nodes:
                continue
            seen.add(id(t))
            rows = table_rows(t)
            if rows:
                tables.append({'title': caption_of(t), 'rows': rows})
        txt = clean(n.get_text(' ')) if n.name != 'table' else ''
        if txt:
            texts.append(txt)
    return tables, texts


def section_by_ids(soup, pattern):
    divs = [d for d in soup.find_all(id=pattern)]
    # 只留最外層，避免巢狀重複
    out = []
    for d in divs:
        if not any(p in divs for p in d.parents):
            out.append(d)
    return out


def section_by_title(soup, title_re, stop_re=STOP_TITLE, max_nodes=400):
    start = None
    for s in soup.find_all(string=True):
        if title_re.match(clean(s)):
            start = s.parent
            break
    if start is None:
        return []
    nodes, n = [], start
    for _ in range(max_nodes):
        nxt = n.find_next_sibling()
        n = nxt if nxt is not None else n.find_next(True)
        if n is None:
            break
        txt = clean(n.get_text(' ')) if hasattr(n, 'get_text') else ''
        if n.name not in ('table', 'tr', 'td') and txt and len(txt) < 40 and stop_re.match(txt):
            break
        nodes.append(n)
    return nodes


def parties_from(tables, texts):
    """關係人名稱及關係。優先找表頭有「關係人名稱」的表；其次抓看起來像法人的儲存格。"""
    out, seen = [], set()

    def add(name, relation='', src=''):
        name = clean(name)
        if not name or len(name) > 80 or CATEGORY_ONLY.match(name):
            return
        alias = ''
        m = ALIAS.search(name)
        if m:
            alias = m.group(1).strip()
            name = clean(ALIAS.sub('', name))
        key = name
        if key in seen:
            return
        seen.add(key)
        out.append({'name': name, 'alias': alias, 'relation': clean(relation), 'src': src})

    for t in tables:
        rows = t['rows']
        hdr_i = next((i for i, r in enumerate(rows[:3]) if any(re.search(r'關係人(名稱|姓名)|公司名稱|名稱', c) for c in r) and any(re.search(r'關係', c) for c in r)), None)
        if hdr_i is None:
            continue
        hdr = rows[hdr_i]
        ni = next(i for i, c in enumerate(hdr) if re.search(r'名稱|姓名', c))
        ri = next((i for i, c in enumerate(hdr) if re.search(r'關係', c) and i != ni), None)
        for r in rows[hdr_i + 1:]:
            if ni < len(r):
                add(r[ni], r[ri] if ri is not None and ri < len(r) else '', '關係人名稱及關係表')
    if not out:
        # 後備：所有表格中像法人名稱的儲存格
        for t in tables:
            for r in t['rows']:
                for i, c in enumerate(r):
                    if ENTITY.search(c) and not re.search(r'應收|應付|收入|進貨|銷貨|合計|金額|帳列|款項', c):
                        add(c, r[i + 1] if i + 1 < len(r) and not re.fullmatch(r'[\d,.()\-\s]+', r[i + 1]) else '', '表格儲存格（後備規則）')
    return out


def parse_report(html):
    soup = BeautifulSoup(html, 'html.parser')
    notes = []
    unit = ''
    nodes = section_by_ids(soup, RPT_ID)
    how = 'id'
    if not nodes:
        nodes = section_by_title(soup, RPT_TITLE)
        how = 'title'
    if not nodes:
        notes.append('找不到「關係人交易」附註段落')
    tables, texts = collect_from(nodes) if nodes else ([], [])
    m = re.search(r'單位[:：]\s*(新臺幣|新台幣)?\s*(仟元|千元|元|百萬元)', ' '.join(texts[:5]))
    if m:
        unit = (m.group(1) or '新臺幣') + m.group(2)
    parties = parties_from(tables, texts)
    if nodes and not parties:
        notes.append('有關係人交易段落，但未辨識出具名關係人（可能全為類別揭露或格式特殊）')

    schedules = []
    sched_nodes = section_by_ids(soup, SCHED_ID)
    for d in sched_nodes:
        if d in nodes:
            continue
        st, sx = collect_from([d])
        if st:
            t1 = d.find('table')
            schedules.append({'title': (caption_of(t1) if t1 else '') or d.get('id', ''), 'id': d.get('id', ''), 'tables': st})
    if not schedules:
        for name, rx in SCHED_TITLES:
            ns = section_by_title(soup, re.compile(r'^(附表[一二三四五六七八九十0-9]*\s*)?.*' + rx.pattern), max_nodes=60)
            st, _ = collect_from([n for n in ns if n.name == 'table']) if ns else ([], [])
            if st:
                schedules.append({'title': name, 'id': '', 'tables': st[:6]})
    # 文字精簡：去掉重複、截斷
    uniq = []
    for t in texts:
        if t not in uniq:
            uniq.append(t[:4000])
    return {'how': how, 'unit': unit, 'parties': parties, 'tables': tables, 'texts': uniq[:20], 'schedules': schedules, 'notes': notes}


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
    quarters = []
    tried = 0
    for (y, q) in recent_quarters(8):
        if len(quarters) >= keep:
            break
        k = f'{y}Q{q}'
        if k in have:
            quarters.append(have[k])
            continue
        tried += 1
        got = None
        for rid in ('C', 'A'):
            url, html = get_mops(code, y, q, rid)
            if is_missing(html):
                continue
            rep = parse_report(html)
            got = {'year': y, 'quarter': q, 'roc': f'{y-1911}Q{q}', 'reportId': rid, 'reportType': '合併' if rid == 'C' else '個體',
                   'url': url, 'fetched': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds'), 'parser': PARSER_VERSION, **rep}
            break
        if got:
            quarters.append(got)
            print(f'  {code} {k}：關係人 {len(got["parties"])} 筆、表格 {len(got["tables"])} 張、附表 {len(got["schedules"])} 份 {got["notes"]}', flush=True)
        else:
            print(f'  {code} {k}：觀測站無此季財報', flush=True)
    quarters.sort(key=lambda x: (x['year'], x['quarter']), reverse=True)
    data = {'code': code, 'name': (known or {}).get('name', old.get('name', '')), 'updated': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds'),
            'source': '公開資訊觀測站 t164sb01 財報（Inline XBRL）附註「關係人交易」與附表', 'quarters': quarters[:keep]}
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
    for url, mkt in (('https://openapi.twse.com.tw/v1/opendata/t187ap03_L', '上市'), ('https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap03_O', '上櫃')):
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
        def stale(c):
            s = state.get(c)
            if not s:
                return 0  # 從未抓過，最優先（watchlist 之後）
            return 1 if s.get('latest') != f'{latest[0]}Q{latest[1]}' and time.time() - s.get('t', 0) > 2 * 86400 else 9
        rest = sorted([c for c in uni if c not in watch], key=lambda c: (stale(c), state.get(c, {}).get('t', 0)))
        targets = [c for c in watch if stale(c) < 9] + [c for c in rest if stale(c) < 9]
    done = 0
    for code in targets:
        if (time.time() - t0) / 60 > a.budget:
            print('時間預算用完，下一批再繼續', flush=True)
            break
        print(f'[{done+1}/{len(targets)}] {code} {uni.get(code, {}).get("name", "")}', flush=True)
        try:
            d = crawl_one(code, known=uni.get(code))
            lq = d['quarters'][0] if d['quarters'] else None
            state[code] = {'t': time.time(), 'latest': f"{lq['year']}Q{lq['quarter']}" if lq else '', 'n': len(d['quarters'])}
        except Blocked:
            print('觀測站持續阻擋，結束本批', flush=True)
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
