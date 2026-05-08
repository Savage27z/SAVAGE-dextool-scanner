const HOP_BY_HOP_HEADERS = new Set([
  'connection',
  'content-length',
  'host',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);

function getBackendUrl() {
  const raw = process.env.BACKEND_URL || process.env.VITE_API_URL || process.env.FRONTEND_API_URL;
  return raw ? raw.replace(/\/$/, '') : '';
}

module.exports = async function handler(req, res) {
  const backendUrl = getBackendUrl();
  if (!backendUrl) {
    res.statusCode = 500;
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ error: 'BACKEND_URL or FRONTEND_API_URL is not configured on Vercel' }));
    return;
  }

  const path = Array.isArray(req.query.path) ? req.query.path.join('/') : req.query.path || '';
  const query = new URLSearchParams(req.query);
  query.delete('path');
  const targetUrl = `${backendUrl}/api/${path}${query.toString() ? `?${query}` : ''}`;

  const headers = {};
  for (const [key, value] of Object.entries(req.headers)) {
    if (!HOP_BY_HOP_HEADERS.has(key.toLowerCase()) && value !== undefined) {
      headers[key] = Array.isArray(value) ? value.join(', ') : value;
    }
  }

  let requestBody;
  if (!['GET', 'HEAD'].includes(req.method || 'GET')) {
    const chunks = [];
    for await (const chunk of req) {
      chunks.push(chunk);
    }
    requestBody = Buffer.concat(chunks);
  }

  const response = await fetch(targetUrl, {
    method: req.method,
    headers,
    body: requestBody,
    redirect: 'manual',
  });

  res.statusCode = response.status;
  response.headers.forEach((value, key) => {
    if (!HOP_BY_HOP_HEADERS.has(key.toLowerCase())) {
      res.setHeader(key, value);
    }
  });

  const setCookie = response.headers.getSetCookie?.();
  if (setCookie && setCookie.length > 0) {
    res.setHeader('set-cookie', setCookie);
  }

  const body = Buffer.from(await response.arrayBuffer());
  res.end(body);
};
