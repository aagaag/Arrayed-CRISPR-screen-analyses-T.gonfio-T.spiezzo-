/**
 * ISAB shared auth Worker
 *
 * Deploy the same Worker to:
 * - auth.isab.science
 * - protected app hosts like crispr-tools.isab.science
 *
 * Required env vars:
 *   AUTH_COOKIE_KEY
 *   AUTH_USERNAME
 *   AUTH_PASSWORD
 *   AUTH_HOST                  e.g. auth.isab.science
 *   COOKIE_DOMAIN              e.g. .isab.science
 *   ALLOWED_HOST_SUFFIX        e.g. .isab.science
 *
 * One-origin deployments:
 *   ORIGIN_HOST                e.g. crispr-tools-origin.internal
 *
 * Multi-host deployments with one Worker:
 *   ORIGIN_MAP                 JSON object mapping incoming hostname -> origin hostname
 *                              e.g. {"isab.science":"isab-website.pages.dev","crispr-tools.isab.science":"appenzell.internet-box.ch"}
 *
 * Optional env vars:
 *   AUTH_EXPIRES_DAYS          default 30
 *   DEFAULT_REDIRECT_URL       fallback after login
 *   PUBLIC_ASSET_PREFIX        default /public-assets/
 *   LOGIN_LOGO_PATH            optional absolute/relative logo URL
 */

const DEFAULT_LOGIN_LOGO = `data:image/svg+xml;utf8,<?xml version="1.0" encoding="UTF-8" standalone="no"?><svg width="154" height="204" viewBox="0 0 154 204" version="1.1" xmlns="http://www.w3.org/2000/svg"><path d="M0.64,0.64h153.25v122.49c0,44.02-34.31,79.7-76.62,79.7C34.94,202.83,0.64,167.15,0.64,123.13Z" fill="%2316a74e" stroke="%23000" stroke-width="1.19"/><path d="M19.6,38.05V15.57c0-1.17.27-2.04.8-2.63.53-.58,1.22-.87,2.06-.87.87,0,1.57.29,2.11.87.54.58.81,1.46.81,2.64v22.47c0,1.18-.27,2.06-.81,2.65-.54.58-1.24.88-2.11.88-.83,0-1.51-.3-2.05-.89-.54-.59-.81-1.47-.81-2.64Z" fill="%235a00ff"/><path d="M98.63,85.22v.09c0,3.66-4.49,6.24-10.65,8.41-13.73-4.51-35.88-6.76-36.04-14.92v-.09c0-3.66,4.49-6.24,10.65-8.41,13.73,4.51,35.88,6.76,36.04,14.92Z" fill="%234d4b7e" stroke="%23282566" stroke-width=".98"/><path d="M99.55,132.45v.09c0,3.66-4.49,6.24-10.65,8.41-13.73-4.51-35.88-6.76-36.04-14.92v-.09c0-3.66,4.49-6.24,10.65-8.41,13.73,4.51,35.88,6.76,36.04,14.92Z" fill="%234d4b7e" stroke="%23282566" stroke-width=".98"/><path d="M75.49,167.09c-3.89-.95-7.72-1.91-11.05-3-6.16,2.17-10.65,4.74-10.65,8.41v.09c.11,5.59,10.54,8.41,21.51,11.08.05-5.8.11-12.79.19-16.57Z" fill="%234d4b7e" stroke="%23282566" stroke-width=".98"/><path d="M98.63,85.31v16.53c.16,8.1-21.54,10.91-35.12,15.7-6.17,2.17-10.65,4.75-10.65,8.41V109.41c-.16-8.1,21.54-10.91,35.12-15.7,6.17-2.17,10.65-4.75,10.65-8.41Z" fill="%23d4dfc1" stroke="%23282566" stroke-width=".98"/><path d="M99.55,131.86v16.53c.16,8.1-21.54,10.91-35.12,15.7-6.16,2.17-10.65,4.75-10.65,8.41v-16.53c-.16-8.1,21.55-10.91,35.12-15.7,6.16-2.17,10.65-4.75,10.65-8.41Z" fill="%23d4dfc1" stroke="%23282566" stroke-width=".98"/><path d="M19.6,38.05V15.57c0-1.17.27-2.04.8-2.63.53-.58,1.22-.87,2.06-.87.87,0,1.57.29,2.11.87.54.58.81,1.46.81,2.64v22.47c0,1.18-.27,2.06-.81,2.65-.54.58-1.24.88-2.11.88-.83,0-1.51-.3-2.05-.89-.54-.59-.81-1.47-.81-2.64Z" fill="%23ffd100"/></svg>`;

export default {
  async fetch(request, env) {
    const rid = crypto.randomUUID();
    const start = Date.now();

    try {
      const res = await handle(request, env, rid);
      console.log(
        `[${rid}] ${request.method} ${new URL(request.url).host}${new URL(request.url).pathname} -> ${res.status} (${Date.now() - start}ms)`
      );
      return res;
    } catch (e) {
      console.error(`[${rid}] unhandled`, e && e.stack ? e.stack : String(e));
      return new Response("Worker error", {
        status: 500,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }
  },
};

async function handle(request, env, rid) {
  validateEnv(env);

  const url = new URL(request.url);
  const path = url.pathname;
  const host = url.hostname.toLowerCase();
  const isAuthHost = sameHost(host, env.AUTH_HOST);

  if (isLoginPage(path)) {
    if (!isAuthHost) {
      return redirectToCentralLogin(url, env);
    }
    if (request.method !== "GET") {
      return new Response("Method Not Allowed", { status: 405 });
    }
    return renderLoginPage(url, env);
  }

  if (isLoginSubmit(path)) {
    if (!isAuthHost) {
      return redirectToCentralLogin(url, env);
    }
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }
    return handleLoginPost(request, env, rid);
  }

  if (isLogout(path)) {
    return handleLogout(request, env);
  }

  if (isPublicAsset(path, env)) {
    if (!hasOriginConfig(env, host)) {
      return new Response("Not Found", { status: 404 });
    }
    return proxyToOrigin(request, env, rid);
  }

  const token = getCookie(request.headers.get("Cookie") || "", "isab_auth");
  const claims = token ? await verifyToken(token, env.AUTH_COOKIE_KEY, rid) : null;

  if (!claims) {
    if (isAuthHost) {
      return redirectToCentralLogin(url, env);
    }
    return redirectToCentralLogin(url, env);
  }

  if (path === "/__debug") {
    return json({
      rid,
      host,
      isAuthHost,
      authenticated: true,
      claims,
      originHost: resolveOriginHost(host, env, false),
    });
  }

  if (isAuthHost && !hasOriginConfig(env, host)) {
    const dest = normalizeReturnTo(claims.return_to || env.DEFAULT_REDIRECT_URL || "/", env);
    return Response.redirect(dest, 302);
  }

  if (!hasOriginConfig(env, host)) {
    return new Response(`Worker misconfigured: no origin configured for ${host}.`, { status: 500 });
  }

  return proxyToOrigin(request, env, rid, claims);
}

function validateEnv(env) {
  const required = [
    "AUTH_COOKIE_KEY",
    "AUTH_USERNAME",
    "AUTH_PASSWORD",
    "AUTH_HOST",
    "COOKIE_DOMAIN",
    "ALLOWED_HOST_SUFFIX",
  ];
  for (const key of required) {
    if (!env[key]) {
      throw new Error(`Missing env var ${key}`);
    }
  }
}

function isLoginPage(path) {
  return path === "/login" || path === "/login/";
}

function isLoginSubmit(path) {
  return path === "/auth/login" || path === "/auth/login/";
}

function isLogout(path) {
  return path === "/logout" || path === "/logout/" || path === "/auth/logout" || path === "/auth/logout/";
}

function isPublicAsset(path, env) {
  const prefix = env.PUBLIC_ASSET_PREFIX || "/public-assets/";
  return (
    path.startsWith(prefix) ||
    path === "/favicon.ico" ||
    path === "/site.webmanifest" ||
    path.startsWith("/apple-touch-icon") ||
    path.startsWith("/favicon-")
  );
}

function sameHost(a, b) {
  return String(a || "").trim().toLowerCase() === String(b || "").trim().toLowerCase();
}

function hasOriginConfig(env, host) {
  return !!resolveOriginHost(host, env, false);
}

function resolveOriginHost(host, env, strict = true) {
  const normalizedHost = String(host || "").trim().toLowerCase();

  if (env.ORIGIN_MAP) {
    let map;
    try {
      map = JSON.parse(String(env.ORIGIN_MAP));
    } catch {
      throw new Error("Invalid ORIGIN_MAP JSON");
    }
    const direct = map[normalizedHost];
    if (direct) return String(direct).trim();
  }

  if (env.ORIGIN_HOST) {
    return String(env.ORIGIN_HOST).trim();
  }

  if (strict) {
    throw new Error(`No origin configured for host: ${normalizedHost}`);
  }
  return "";
}

function redirectToCentralLogin(currentUrl, env) {
  const loginUrl = new URL(`https://${env.AUTH_HOST}/login/`);
  loginUrl.searchParams.set("next", currentUrl.toString());
  return Response.redirect(loginUrl.toString(), 302);
}

function renderLoginPage(currentUrl, env) {
  const next = normalizeReturnTo(
    currentUrl.searchParams.get("next") || env.DEFAULT_REDIRECT_URL || `https://${env.AUTH_HOST}/`,
    env
  );
  const error = currentUrl.searchParams.get("error") === "1";
  const logoPath = env.LOGIN_LOGO_PATH || DEFAULT_LOGIN_LOGO;
  const helpText =
    env.LOGIN_HELP_TEXT ||
    'To request credentials, please send a request to "contact [AT] isab [D0T] science".';

  const html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Sign in at ISAB</title>
  <style>
    body { font-family: system-ui, sans-serif; background:#fff; margin:0; color:#111827; }
    header { border-bottom:1px solid #e5e7eb; background:#fff; }
    #mainnav { max-width:1200px; margin:0 auto; padding:14px 24px; display:flex; gap:14px; align-items:center; font-weight:800; }
    #mainnav img { height:42px; width:auto; }
    .wrap { min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px; }
    .card { width:min(720px, 96vw); border:1px solid #e5e7eb; border-radius:18px; padding:28px; box-shadow:0 12px 30px rgba(0,0,0,.06); }
    .row { display:flex; gap:18px; align-items:center; margin-bottom:10px; }
    .row img { height:44px; width:auto; }
    h1 { margin:0 0 10px; font-size:30px; }
    p { color:#4b5563; }
    label { display:block; font-weight:700; margin:14px 0 6px; }
    input { width:100%; box-sizing:border-box; padding:12px; border:1px solid #cbd5e1; border-radius:10px; font-size:16px; }
    button { margin-top:18px; padding:12px 18px; border:0; border-radius:10px; background:#0b7ef4; color:#fff; font-weight:800; cursor:pointer; }
    .err { background:#fff1f2; border:1px solid #fecdd3; color:#b91c1c; padding:10px 12px; border-radius:10px; margin:12px 0; }
    .muted { margin-top:16px; font-size:14px; color:#6b7280; }
  </style>
</head>
<body>
  <header>
    <div id="mainnav">
      <img src="${escapeAttr(logoPath)}" alt="ISAB">
      <span>Institute for the Science of the Aging Brain</span>
    </div>
  </header>
  <main class="wrap">
    <section class="card">
      <div class="row">
        <img src="${escapeAttr(logoPath)}" alt="ISAB">
        <h1>Sign in</h1>
      </div>
      <p>Please enter the credentials you received from ISAB.</p>
      ${error ? `<div class="err">Invalid username or password.</div>` : ``}
      <form method="post" action="/auth/login">
        <input type="hidden" name="next" value="${escapeAttr(next)}">
        <label for="u">Username</label>
        <input id="u" name="username" autocomplete="username" required>
        <label for="p">Password</label>
        <input id="p" name="password" type="password" autocomplete="current-password" required>
        <button type="submit">Sign in</button>
      </form>
      <div class="muted">${escapeHtml(helpText)}</div>
    </section>
  </main>
</body>
</html>`;

  return new Response(html, {
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

async function handleLoginPost(request, env, rid) {
  const ct = request.headers.get("content-type") || "";
  if (!ct.includes("application/x-www-form-urlencoded") && !ct.includes("multipart/form-data")) {
    return new Response("Bad Request", { status: 400 });
  }

  const form = await request.formData();
  const username = String(form.get("username") || "");
  const password = String(form.get("password") || "");
  const next = normalizeReturnTo(
    String(form.get("next") || env.DEFAULT_REDIRECT_URL || `https://${env.AUTH_HOST}/`),
    env
  );

  const u = username.trim().toLowerCase();
  const p = password.trim().toLowerCase();
  const eu = String(env.AUTH_USERNAME).trim().toLowerCase();
  const ep = String(env.AUTH_PASSWORD).trim().toLowerCase();
  const ok = constantTimeEqual(u, eu) && constantTimeEqual(p, ep);

  console.log(`[${rid}] login user=${u} ok=${ok}`);

  if (!ok) {
    const back = new URL(`https://${env.AUTH_HOST}/login/`);
    back.searchParams.set("error", "1");
    back.searchParams.set("next", next);
    return Response.redirect(back.toString(), 302);
  }

  const days = clampInt(env.AUTH_EXPIRES_DAYS, 1, 365, 30);
  const exp = Date.now() + days * 24 * 60 * 60 * 1000;
  const token = await signToken(
    {
      u: eu,
      host: env.AUTH_HOST,
      return_to: next,
      exp,
    },
    env.AUTH_COOKIE_KEY
  );

  const headers = new Headers();
  headers.append(
    "Set-Cookie",
    buildCookie("isab_auth", token, {
      maxAge: days * 24 * 60 * 60,
      domain: env.COOKIE_DOMAIN,
    })
  );
  headers.set("Location", next);

  return new Response(null, { status: 302, headers });
}

function handleLogout(request, env) {
  const headers = new Headers();
  headers.append("Set-Cookie", clearCookieHostOnly("isab_auth"));
  headers.append("Set-Cookie", clearCookieDomain("isab_auth", env.COOKIE_DOMAIN));

  const target = sameHost(new URL(request.url).hostname, env.AUTH_HOST)
    ? `https://${env.AUTH_HOST}/login/`
    : new URL(`https://${env.AUTH_HOST}/login/`).toString();
  headers.set("Location", target);

  return new Response(null, { status: 302, headers });
}

async function proxyToOrigin(request, env, rid, claims) {
  const incomingUrl = new URL(request.url);
  const originUrl = new URL(request.url);
  originUrl.protocol = "https:";
  const originHost = resolveOriginHost(incomingUrl.hostname, env, true);
  originUrl.hostname = originHost;

  const headers = new Headers(request.headers);
  headers.set("X-Forwarded-Host", incomingUrl.host);
  headers.set("X-Auth-User", claims?.u || "");
  headers.delete("authorization");

  const resp = await fetch(originUrl.toString(), {
    method: request.method,
    headers,
    body: request.body,
    redirect: "manual",
  });

  const outHeaders = new Headers(resp.headers);
  const loc = outHeaders.get("Location");
  if (loc) {
    try {
      const parsed = new URL(loc, originUrl);
      if (sameHost(parsed.hostname, originHost)) {
        parsed.hostname = incomingUrl.hostname;
        parsed.protocol = incomingUrl.protocol;
        outHeaders.set("Location", parsed.toString());
      }
    } catch {
      console.log(`[${rid}] location rewrite skipped`);
    }
  }

  return new Response(resp.body, {
    status: resp.status,
    statusText: resp.statusText,
    headers: outHeaders,
  });
}

function normalizeReturnTo(raw, env) {
  const fallback = env.DEFAULT_REDIRECT_URL || `https://${env.AUTH_HOST}/`;
  try {
    const url = new URL(String(raw || fallback));
    if (url.protocol !== "https:") return fallback;
    const suffix = String(env.ALLOWED_HOST_SUFFIX || "").trim().toLowerCase();
    const host = url.hostname.toLowerCase();
    if (host === env.AUTH_HOST.toLowerCase()) return url.toString();
    if (suffix && host.endsWith(suffix)) return url.toString();
    return fallback;
  } catch {
    return fallback;
  }
}

function getCookie(cookieHeader, name) {
  const parts = cookieHeader.split(";");
  for (const p of parts) {
    const [k, ...rest] = p.trim().split("=");
    if (k === name) return rest.join("=") || "";
  }
  return "";
}

async function verifyToken(token, secret, rid) {
  try {
    const [p, s] = token.split(".");
    if (!p || !s) return null;

    const payloadBytes = base64urlToBytes(p);
    const sigBytes = base64urlToBytes(s);
    if (!payloadBytes || !sigBytes) return null;

    const expected = await hmacSha256(secret, payloadBytes);
    if (!timingSafeEqual(sigBytes, expected)) return null;

    const payload = JSON.parse(new TextDecoder().decode(payloadBytes));
    if (!payload?.exp || typeof payload.exp !== "number") return null;
    if (Date.now() > payload.exp) return null;
    return payload;
  } catch (e) {
    console.error(`[${rid}] verifyToken`, e && e.stack ? e.stack : String(e));
    return null;
  }
}

async function signToken(payloadObj, secret) {
  const payloadBytes = new TextEncoder().encode(JSON.stringify(payloadObj));
  const sig = await hmacSha256(secret, payloadBytes);
  return `${bytesToBase64url(payloadBytes)}.${bytesToBase64url(sig)}`;
}

async function hmacSha256(secret, data) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, data);
  return new Uint8Array(sig);
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a[i] ^ b[i];
  return out === 0;
}

function base64urlToBytes(s) {
  try {
    const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  } catch {
    return null;
  }
}

function bytesToBase64url(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function constantTimeEqual(a, b) {
  const A = new TextEncoder().encode(String(a));
  const B = new TextEncoder().encode(String(b));
  if (A.length !== B.length) return false;
  let out = 0;
  for (let i = 0; i < A.length; i++) out |= A[i] ^ B[i];
  return out === 0;
}

function clampInt(v, min, max, def) {
  const n = parseInt(String(v ?? ""), 10);
  if (!Number.isFinite(n)) return def;
  return Math.min(max, Math.max(min, n));
}

function buildCookie(name, value, { maxAge, domain }) {
  const attrs = [
    `${name}=${value}`,
    "Path=/",
    "HttpOnly",
    "Secure",
    "SameSite=Lax",
  ];
  if (domain) attrs.push(`Domain=${domain}`);
  if (typeof maxAge === "number") attrs.push(`Max-Age=${maxAge}`);
  return attrs.join("; ");
}

function clearCookieHostOnly(name) {
  return [
    `${name}=`,
    "Path=/",
    "HttpOnly",
    "Secure",
    "SameSite=Lax",
    "Max-Age=0",
    "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
  ].join("; ");
}

function clearCookieDomain(name, domain) {
  return [
    `${name}=`,
    "Path=/",
    `Domain=${domain}`,
    "HttpOnly",
    "Secure",
    "SameSite=Lax",
    "Max-Age=0",
    "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
  ].join("; ");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (m) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
  }[m]));
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

function json(obj) {
  return new Response(JSON.stringify(obj, null, 2), {
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}
