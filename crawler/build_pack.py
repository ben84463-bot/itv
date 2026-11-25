#!/usr/bin/env python3
"""
每日官方資料打包（GitHub Actions 執行）
來源：證交所、櫃買中心 OpenAPI（政府資料開放授權條款第 1 版）
  - 上市／上櫃／興櫃公司基本資料：代號、簡稱、全名、統一編號、已發行股數
  - 持股逾 10% 大股東名單
  - 董監事持股餘額明細（只留「本人」為法人者：法人董監事、法人大股東）
輸出：
  data/pack/companies.json        代號 → {n 簡稱, full 全名, ban 統編, mkt 市場, shares 已發行股數}
  data/pack/holdings/{代號}.json   該公司的法人持股人清單
  data/pack/meta.json             建置時間與各來源筆數
參考：gplee789-afk/twstock-relations-map official.py 的端點與欄位
原則：抓不到就沿用上一次的檔案，不寫入空資料。
"""
import json, os, re, time, datetime as dt, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'pack')
TWSE = 'https://openapi.twse.com.tw/v1/opendata/'
TPEX = 'https://www.tpex.org.tw/openapi/v1/'
UA = 'Mozilla/5.0 (compatible; itv-pack/1.0)'
LISTED = [('上市', [TWSE + 't187ap03_L']), ('上櫃', [TPEX + 'mopsfin_t187ap03_O', TPEX + 'mopsfe_t187ap03_O']), ('興櫃', [TPEX + 'mopsfin_t187ap03_R', TPEX + 'mopsfe_t187ap03_R'])]
HOLDERS = [[TWSE + 't187ap02_L'], [TPEX + 'mopsfin_t187ap02_O', TPEX + 'mopsfe_t187ap02_O']]
INSIDERS = [[TWSE + 't187ap11_L'], [TPEX + 'mopsfin_t187ap11_O', TPEX + 'mopsfe_t187ap11_O'], [TPEX + 'mopsfin_t187ap11_R', TPEX + 'mopsfe_t187ap11_R']]
ORG = re.compile(r'公司|基金會|銀行|合作社|協會|委員會|基金|信託|部$|局$|院$|Limited|Ltd|Inc|Corp|LLC|B\.V\.|S\.A\.|L\.P\.', re.I)
SEATS = {'董事長本人': '董事長', '副董事長本人': '副董事長', '常務董事本人': '常務董事', '董事本人': '董事', '獨立董事本人': '獨立董事', '監察人本人': '監察人', '大股東本人': '大股東'}


def get(urls):
    for u in urls:
        for i in range(3):
            try:
                req = urllib.request.Request(u, headers={'User-Agent': UA, 'Accept': 'application/json'})
                rows = json.loads(urllib.request.urlopen(req, timeout=90).read().decode('utf-8-sig'))
                if isinstance(rows, list) and rows:
                    print(f'  {u}：{len(rows)} 筆', flush=True)
                    return rows
            except Exception as e:
                print(f'  {u} 失敗（{e}），重試', flush=True)
                time.sleep(5 * (i + 1))
    return []


def f(r, *keys):
    for k in keys:
        v = r.get(k)
        if v not in (None, ''):
            return str(v).strip()
    return ''


def to_int(s):
    s = re.sub(r'[^\d]', '', str(s or ''))
    return int(s) if s else None


def norm(s):
    return re.sub(r'\s', '', str(s or '')).replace('臺', '台').replace('（', '(').replace('）', ')')


def load(path, default):
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception:
        return default


def main():
    os.makedirs(os.path.join(OUT, 'holdings'), exist_ok=True)
    meta = {'built': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds'), 'source': '證交所、櫃買中心 OpenAPI（政府資料開放授權條款第1版）', 'counts': {}}
    companies = {}
    for mkt, urls in LISTED:
        rows = get(urls)
        meta['counts'][f'companies_{mkt}'] = len(rows)
        for r in rows:
            code = f(r, '公司代號', 'SecuritiesCompanyCode')
            if not code:
                continue
            ban = re.sub(r'\D', '', f(r, '營利事業統一編號', 'UnifiedBusinessNo.', 'UnifiedBusinessNo'))
            companies[code] = {'n': f(r, '公司簡稱', 'CompanyAbbreviation'), 'full': f(r, '公司名稱', 'CompanyName'), 'ban': ban if len(ban) == 8 and ban != '00000000' else '',
                               'mkt': mkt, 'shares': to_int(f(r, '已發行普通股數或TDR原股發行股數', 'IssueShares'))}
    old = load(os.path.join(OUT, 'companies.json'), {})
    if len(companies) < 500 and old:
        print('公司清單筆數過少，沿用舊檔', flush=True)
        companies = old
    by_full = {}
    for code, c in companies.items():
        if c.get('full'):
            by_full[norm(c['full'])] = code

    rec = {}

    def add(code, holder, kind, seat=None, shares=None):
        if not code or not holder or code not in companies:
            return
        key = (code, norm(holder))
        hc = by_full.get(key[1])
        h = rec.setdefault(key, {'holder': holder.strip(), 'holder_code': hc, 'holder_ban': companies.get(hc, {}).get('ban', '') if hc else '', 'kinds': set(), 'seat': None, 'shares': None})
        h['kinds'].add(kind)
        if seat and seat != '大股東':
            h['seat'] = seat
        if shares:
            h['shares'] = max(h['shares'] or 0, shares)

    n_ins = 0
    for urls in INSIDERS:
        rows = get(urls)
        n_ins += len(rows)
        for r in rows:
            seat = SEATS.get(f(r, '職稱', 'Title'))
            name = f(r, '姓名', 'Name')
            if seat and ORG.search(name):
                add(f(r, '公司代號', 'SecuritiesCompanyCode'), name, 'holder' if seat == '大股東' else 'director', seat, to_int(f(r, '目前持股', 'CurrentShareholding')))
    n_hold = 0
    for urls in HOLDERS:
        rows = get(urls)
        n_hold += len(rows)
        for r in rows:
            add(f(r, '公司代號', 'SecuritiesCompanyCode'), f(r, '大股東名稱', 'MajorShareholderName'), 'holder')
    meta['counts']['insiders_rows'] = n_ins
    meta['counts']['holders_rows'] = n_hold

    per = {}
    for (code, _), h in rec.items():
        if h['holder_code'] == code:
            continue
        total = companies[code].get('shares')
        h['pct'] = round(h['shares'] / total * 100, 2) if h['shares'] and total else None
        h['kinds'] = sorted(h['kinds'])
        per.setdefault(code, []).append(h)
    if n_ins + n_hold > 0:
        for code, lst in per.items():
            lst.sort(key=lambda x: -(x['pct'] or 0))
            json.dump({'code': code, 'built': meta['built'], 'holders': lst}, open(os.path.join(OUT, 'holdings', f'{code}.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    meta['counts']['companies_with_legal_holders'] = len(per)
    json.dump(companies, open(os.path.join(OUT, 'companies.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    json.dump(meta, open(os.path.join(OUT, 'meta.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(meta, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
