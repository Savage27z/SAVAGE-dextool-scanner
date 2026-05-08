const HOP_BY_HOP = new Set([
  'host',
  'connection',
  'keep-alive',
  'transfer-encoding',
  'te',
  'trailer',
  'upgrade',
  'proxy-authorization',
  'proxy-connection',
  'content-length',
  'x-savage-frontend-origin',
]);

function frontendOrigin(req) {
  var proto = req.headers['x-forwarded-proto'] || 'https';
  var host = req.headers.host;
  if (Array.isArray(proto)) proto = proto[0];
  if (Array.isArray(host)) host = host[0];
  proto = String(proto).split(',')[0].trim();
  host = String(host || '').split(',')[0].trim();
  return host ? proto + '://' + host : '';
}

function resolveIncomingPath(req) {
  // Headers come first — Vercel sets x-vercel-original-url to the pre-rewrite
  // URL. req.url can show the rewrite destination (/api/proxy) on some
  // Vercel runtime versions, so we only fall back to it after headers.
  var sources = [
    req.headers['x-vercel-original-url'],
    req.headers['x-forwarded-uri'],
    req.headers['x-original-url'],
    req.url,
  ];

  for (var i = 0; i < sources.length; i++) {
    var raw = sources[i];
    if (!raw) continue;
    if (Array.isArray(raw)) raw = raw[0];
    var s = String(raw);
    // Normalize full URLs (e.g. https://host/path?q=1) to path + search.
    if (s.indexOf('http://') === 0 || s.indexOf('https://') === 0) {
      try {
        var u = new URL(s);
        s = u.pathname + u.search;
      } catch (e) {
        continue;
      }
    }
    // Skip the rewrite destination — that's the function URL, not the
    // original request path. We never want to forward /api/proxy upstream.
    if (
      s === '/api/proxy' ||
      s.indexOf('/api/proxy?') === 0 ||
      s.indexOf('/api/proxy/') === 0
    ) {
      continue;
    }
    if (s.indexOf('/api/') === 0) return s;
  }

  // Last-resort recovery: if all candidates were /api/proxy/<sub>, strip the
  // /api/proxy prefix and reconstruct /api/<sub>. Note: cannot recover
  // /api/proxy?query (no sub-path), so callers needing query-only routes
  // must rely on the original-URL headers being present.
  var fallback = String(req.url || '');
  if (fallback.indexOf('/api/proxy/') === 0) {
    return '/api/' + fallback.slice('/api/proxy/'.length);
  }
  return fallback;
}

module.exports = async function handler(req, res) {
  var backendUrl =
    process.env.BACKEND_URL ||
    process.env.FRONTEND_API_URL ||
    process.env.VITE_API_URL;

  if (!backendUrl) {
    return res.status(500).json({
      error:
        'No backend URL configured. Set BACKEND_URL in Vercel project settings to your Railway backend URL (e.g. https://savage-backend.up.railway.app).',
    });
  }

  var base = backendUrl.replace(/\/+$/, '');
  var incomingPath = resolveIncomingPath(req);
  var target = base + incomingPath;

  var headers = {};
  for (var key in req.headers) {
    if (!HOP_BY_HOP.has(key.toLowerCase())) {
      headers[key] = req.headers[key];
    }
  }

  var origin = frontendOrigin(req);
  if (origin) {
    headers['x-savage-frontend-origin'] = origin;
  }

  var body;
  if (req.method !== 'GET' && req.method !== 'HEAD' && req.body != null) {
    if (Buffer.isBuffer(req.body)) {
      body = req.body;
    } else if (typeof req.body === 'string') {
      body = req.body;
    } else {
      body = JSON.stringify(req.body);
    }
  }

  try {
    var upstream = await fetch(target, {
      method: req.method,
      headers: headers,
      body: body,
      redirect: 'manual',
    });

    res.status(upstream.status);

    var setCookies =
      typeof upstream.headers.getSetCookie === 'function'
        ? upstream.headers.getSetCookie()
        : [];

    upstream.headers.forEach(function (value, key) {
      var lower = key.toLowerCase();
      if (lower === 'transfer-encoding') return;
      if (lower === 'set-cookie') return;
      res.setHeader(key, value);
    });

    if (setCookies.length > 0) {
      res.setHeader('set-cookie', setCookies);
    }

    var buf = Buffer.from(await upstream.arrayBuffer());
    return res.send(buf);
  } catch (err) {
    return res.status(502).json({
      error: 'Bad Gateway',
      message: err && err.message ? String(err.message) : String(err),
      target: target.replace(/(api[-_]?key=)[^&]+/gi, '$1***'),
    });
  }
};
