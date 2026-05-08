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
  try {
    var proto = req.headers['x-forwarded-proto'] || 'https';
    var host = req.headers.host;
    if (Array.isArray(proto)) proto = proto[0];
    if (Array.isArray(host)) host = host[0];
    proto = String(proto).split(',')[0].trim();
    host = String(host || '').split(',')[0].trim();
    return host ? proto + '://' + host : '';
  } catch (e) {
    return '';
  }
}

function resolveIncomingPath(req) {
  try {
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
      if (s.indexOf('http://') === 0 || s.indexOf('https://') === 0) {
        try {
          var u = new URL(s);
          s = u.pathname + u.search;
        } catch (e) {
          continue;
        }
      }
      if (
        s === '/api/proxy' ||
        s.indexOf('/api/proxy?') === 0 ||
        s.indexOf('/api/proxy/') === 0
      ) {
        continue;
      }
      if (s.indexOf('/api/') === 0) return s;
    }

    var fallback = String(req.url || '');
    if (fallback.indexOf('/api/proxy/') === 0) {
      return '/api/' + fallback.slice('/api/proxy/'.length);
    }
    return fallback;
  } catch (e) {
    return String(req.url || '');
  }
}

function normalizeBackendUrl(raw) {
  if (!raw) return '';
  var s = String(raw).trim();
  if (!s) return '';
  if (s.indexOf('http://') !== 0 && s.indexOf('https://') !== 0) {
    s = 'https://' + s;
  }
  return s.replace(/\/+$/, '');
}

function safeJson(res, status, payload) {
  try {
    res.status(status);
    res.setHeader('content-type', 'application/json; charset=utf-8');
    res.end(JSON.stringify(payload));
  } catch (e) {
    try {
      res.statusCode = status;
      res.end(typeof payload === 'string' ? payload : JSON.stringify(payload));
    } catch (e2) {
      // last resort — nothing we can do
    }
  }
}

async function runProxy(req, res) {
  var rawBackend =
    process.env.BACKEND_URL ||
    process.env.FRONTEND_API_URL ||
    process.env.VITE_API_URL;

  var backendUrl = normalizeBackendUrl(rawBackend);

  if (!backendUrl) {
    return safeJson(res, 500, {
      error: 'No backend URL configured',
      hint: 'Set BACKEND_URL in Vercel project settings to your Railway backend URL (e.g. https://savage-backend.up.railway.app).',
    });
  }

  var incomingPath = resolveIncomingPath(req);
  var target = backendUrl + incomingPath;

  var headers = {};
  for (var key in req.headers) {
    if (Object.prototype.hasOwnProperty.call(req.headers, key)) {
      if (!HOP_BY_HOP.has(String(key).toLowerCase())) {
        headers[key] = req.headers[key];
      }
    }
  }

  var origin = frontendOrigin(req);
  if (origin) {
    headers['x-savage-frontend-origin'] = origin;
  }

  var body;
  if (req.method !== 'GET' && req.method !== 'HEAD' && req.body != null) {
    try {
      if (Buffer.isBuffer(req.body)) {
        body = req.body;
      } else if (typeof req.body === 'string') {
        body = req.body;
      } else {
        body = JSON.stringify(req.body);
      }
    } catch (e) {
      return safeJson(res, 400, {
        error: 'Bad request body',
        message: String(e && e.message ? e.message : e),
      });
    }
  }

  if (typeof fetch !== 'function') {
    return safeJson(res, 500, {
      error: 'Runtime missing global fetch',
      hint: 'Vercel function runtime is older than Node 18. Pin nodejs20.x in vercel.json functions config.',
      nodeVersion: process.version,
    });
  }

  var upstream;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers: headers,
      body: body,
      redirect: 'manual',
    });
  } catch (err) {
    return safeJson(res, 502, {
      error: 'Bad Gateway — upstream fetch failed',
      message: String(err && err.message ? err.message : err),
      target: target.replace(/(api[-_]?key=)[^&]+/gi, '$1***'),
    });
  }

  try {
    res.status(upstream.status);

    var setCookies = [];
    try {
      if (typeof upstream.headers.getSetCookie === 'function') {
        setCookies = upstream.headers.getSetCookie() || [];
      }
    } catch (e) {
      setCookies = [];
    }

    upstream.headers.forEach(function (value, key) {
      var lower = String(key).toLowerCase();
      if (lower === 'transfer-encoding') return;
      if (lower === 'set-cookie') return;
      if (lower === 'content-length') return;
      try {
        res.setHeader(key, value);
      } catch (e) {
        // skip headers Node refuses
      }
    });

    if (setCookies.length > 0) {
      try {
        res.setHeader('set-cookie', setCookies);
      } catch (e) {
        // ignore — some runtimes auto-merge
      }
    }

    var buf = Buffer.from(await upstream.arrayBuffer());
    res.end(buf);
  } catch (err) {
    return safeJson(res, 502, {
      error: 'Bad Gateway — response forwarding failed',
      message: String(err && err.message ? err.message : err),
      target: target.replace(/(api[-_]?key=)[^&]+/gi, '$1***'),
    });
  }
}

module.exports = async function handler(req, res) {
  try {
    await runProxy(req, res);
  } catch (err) {
    try {
      console.error('proxy fatal:', err && err.stack ? err.stack : err);
    } catch (e) {}
    return safeJson(res, 500, {
      error: 'Proxy fatal',
      message: String(err && err.message ? err.message : err),
      nodeVersion: process.version,
    });
  }
};
