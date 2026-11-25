/**
 * 投資理由逐季驗證台 — 資料中繼站（Cloudflare Worker）
 * 用途：替網頁向公開資訊觀測站、證交所、櫃買中心、商工資料等網站取資料，並加上允許跨網域讀取的標頭。
 * 安全：只轉送下方白名單網站；只接受 GET / POST；回應快取 6 小時，減少對觀測站的請求。
 * 可選：在 Worker 的 Settings → Variables 新增 ALLOWED_ORIGINS（例：https://帳號.github.io,null），只允許指定網頁使用。
 */
const ALLOW = [
  'mops.twse.com.tw', 'mopsov.twse.com.tw', 'doc.twse.com.tw', 'openapi.twse.com.tw', 'www.twse.com.tw',
  'www.tpex.org.tw', 'company.g0v.ronny.tw', 'pcc-api.openfun.app', 'data.gcis.nat.gov.tw', 'api.finmindtrade.com',
];
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36';
const TTL = 6 * 3600;

function cors(extra = {}) {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
    ...extra,
  };
}

async function sha(buf) {
  const d = await crypto.subtle.digest('SHA-256', buf || new ArrayBuffer(0));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export default {
  async fetch(req, env, ctx) {
    if (req.method === 'OPTIONS') return new Response(null, { headers: cors() });
    const u = new URL(req.url);
    const target = u.searchParams.get('url');
    if (!target) return new Response('itv proxy ok', { headers: cors({ 'Content-Type': 'text/plain; charset=utf-8' }) });

    const allowedOrigins = (env.ALLOWED_ORIGINS || '').split(',').map((s) => s.trim()).filter(Boolean);
    const origin = req.headers.get('Origin') || 'null';
    if (allowedOrigins.length && !allowedOrigins.includes(origin)) return new Response('origin not allowed', { status: 403, headers: cors() });

    let t;
    try { t = new URL(target); } catch { return new Response('bad url', { status: 400, headers: cors() }); }
    if (t.protocol !== 'https:' || !ALLOW.includes(t.hostname)) return new Response('host not allowed', { status: 403, headers: cors() });
    if (!['GET', 'POST'].includes(req.method)) return new Response('method not allowed', { status: 405, headers: cors() });

    const body = req.method === 'POST' ? await req.arrayBuffer() : undefined;
    const ct = req.headers.get('Content-Type');
    const key = new Request(`https://cache.itv/${await sha(new TextEncoder().encode(req.method + ' ' + t.toString() + ' ' + (ct || '')))}/${body ? await sha(body) : ''}`);
    const cache = caches.default;
    const hit = await cache.match(key);
    if (hit) {
      const h = new Headers(hit.headers);
      Object.entries(cors({ 'X-Itv-Cache': 'HIT' })).forEach(([k, v]) => h.set(k, v));
      return new Response(hit.body, { status: hit.status, headers: h });
    }

    const headers = { 'User-Agent': UA, Accept: '*/*', 'Accept-Language': 'zh-TW,zh;q=0.9' };
    if (ct) headers['Content-Type'] = ct;
    if (t.hostname.includes('twse.com.tw')) { headers.Referer = `https://${t.hostname}/`; headers.Origin = `https://${t.hostname}`; }
    let resp;
    try {
      resp = await fetch(t.toString(), { method: req.method, body, headers, redirect: 'follow' });
    } catch (e) {
      return new Response('upstream error: ' + e.message, { status: 502, headers: cors() });
    }
    const buf = await resp.arrayBuffer();
    const h = new Headers();
    h.set('Content-Type', resp.headers.get('Content-Type') || 'application/octet-stream');
    Object.entries(cors({ 'X-Itv-Cache': 'MISS', 'X-Upstream-Status': String(resp.status) })).forEach(([k, v]) => h.set(k, v));
    const out = new Response(buf, { status: resp.status, headers: h });
    // 只快取成功且像樣的回應（觀測站擋爬蟲時會回很短的頁面）
    if (resp.ok && buf.byteLength > 300) {
      const c = new Response(buf, { status: resp.status, headers: new Headers({ 'Content-Type': h.get('Content-Type'), 'Cache-Control': `public, max-age=${TTL}` }) });
      ctx.waitUntil(cache.put(key, c));
    }
    return out;
  },
};
