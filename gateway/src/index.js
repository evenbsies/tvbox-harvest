/**
 * tvbox-harvest edge gateway
 * ---------------------------------------------------------------------------
 * Why this exists
 * ---------------
 * GitHub Actions is a batch process; TVBox is a runtime. For Tier-1 sites that gap is zero --
 * the site itself answers MacCMS-shaped requests, so the subscription can point straight at it.
 * For Tier-3/4 sites it is not: their play addresses only exist after the page's own JavaScript
 * runs, which a batch job can capture but a player cannot reproduce.
 *
 * So the harvest run writes a *snapshot* in exactly MacCMS's response shape, and this Worker
 * serves it back in that same shape. It is a thin read-only projection over static JSON, which
 * is what keeps it reliable: there is nothing to go stale, no upstream to rate-limit us, and
 * the client needs no plugin because `type: 1` already means "MacCMS JSON".
 *
 * Routes
 * ------
 *   GET  /vod/{key}?ac=class|list|detail[&ids=&wd=&t=&pg=&limit=]
 *   GET  /health
 *   GET  /            (self-documenting index of configured sites)
 *   POST /admin/refresh           HMAC-signed; triggers the harvest workflow
 *
 * Bindings (wrangler.toml [vars] / secrets)
 * ----------------------------------------
 *   REPO            "owner/name"                       required
 *   SNAPSHOT_BASE   CDN base for data/snapshot         optional, derived from REPO
 *   CACHE_TTL       seconds to keep parsed shards      optional, default 600
 *   DISPATCH_TOKEN  GitHub PAT (actions:write)         secret, only for /admin/refresh
 */

const DEFAULT_TTL = 600;
const DEFAULT_PAGE_SIZE = 20;
const MAX_PAGE_SIZE = 100;

/* -------------------------------------------------------------------------- */
/* Snapshot access                                                            */
/* -------------------------------------------------------------------------- */

/**
 * Parsed shards are memoised per Worker isolate. Snapshots change at most a few times a day
 * while a player may issue hundreds of requests, so this turns the common case into a pure
 * in-memory read.
 */
const cache = new Map(); // `${base}|${path}` -> { at, data }

function snapshotBase(env) {
  if (env.SNAPSHOT_BASE) return env.SNAPSHOT_BASE.replace(/\/+$/, "");
  if (!env.REPO) throw new Error("REPO binding is required");
  return `https://cdn.jsdelivr.net/gh/${env.REPO}@main/data/snapshot`;
}

/**
 * Where the generated TVBox configs live. Derived from SNAPSHOT_BASE rather than REPO, so a
 * mirror (S3, R2, a local directory during `wrangler dev`) serves *everything* -- the index
 * route used to be hard-wired to jsDelivr and returned 500 whenever SNAPSHOT_BASE was overridden.
 */
function configBase(env) {
  const base = snapshotBase(env);
  return base.replace(/\/snapshot$/, "") + "/tvbox";
}

async function fetchJson(url, ttl) {
  const hit = cache.get(url);
  const now = Math.floor(Date.now() / 1000);
  if (hit && now - hit.at < ttl) return hit.data;

  const resp = await fetch(url, {
    // Strip conditional-request headers: we want a plain body or a clear failure.
    headers: { accept: "application/json" },
    cf: { cacheTtl: ttl, cacheEverything: true },
  });
  if (!resp.ok) {
    if (hit) return hit.data; // stale-while-error: a CDN hiccup must not blank a subscription
    throw new HttpError(502, `snapshot fetch failed: ${resp.status} ${url}`);
  }
  const data = await resp.json();
  cache.set(url, { at: now, data });
  return data;
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function loadManifest(env, key) {
  const ttl = Number(env.CACHE_TTL || DEFAULT_TTL);
  return fetchJson(`${snapshotBase(env)}/${key}/manifest.json`, ttl);
}

/** Load either every `list-part-*` or every `detail-part-*` shard, concatenated. */
async function loadParts(env, key, kind) {
  const ttl = Number(env.CACHE_TTL || DEFAULT_TTL);
  const base = snapshotBase(env);
  const indexUrl = `${base}/${key}/manifest.json`;
  const manifest = await fetchJson(indexUrl, ttl);

  const total = kind === "list"
    ? (manifest.counts?.list_parts ?? 1)
    : (manifest.counts?.detail_parts ?? 1);

  const shards = [];
  for (let part = 1; part <= total; part += 1) {
    const url = `${base}/${key}/${kind}-part-${String(part).padStart(3, "0")}.json`;
    try {
      const shard = await fetchJson(url, ttl);
      if (Array.isArray(shard?.items)) shards.push(...shard.items);
    } catch (err) {
      // A missing tail shard means the catalogue shrank between the manifest write and now.
      // Returning what we have is better than failing the whole request.
      if (part === 1) throw err;
      break;
    }
  }
  return { manifest, items: shards };
}

/* -------------------------------------------------------------------------- */
/* Response shaping                                                           */
/* -------------------------------------------------------------------------- */

function pageParams(url) {
  const limitRaw = Number(url.searchParams.get("limit") || DEFAULT_PAGE_SIZE);
  const limit = Math.min(Math.max(Number.isFinite(limitRaw) ? limitRaw : DEFAULT_PAGE_SIZE, 1), MAX_PAGE_SIZE);
  const pgRaw = Number(url.searchParams.get("pg") || 1);
  const pg = Math.max(Number.isFinite(pgRaw) ? pgRaw : 1, 1);
  return { limit, pg };
}

function pageEnvelope({ items, pg, limit, classes, msg }) {
  const total = items.length;
  const pagecount = Math.max(1, Math.ceil(total / limit));
  const start = (pg - 1) * limit;
  const body = {
    code: 1,
    msg: msg || "数据列表",
    page: pg,
    pagecount,
    limit: String(limit),
    total,
    list: items.slice(start, start + limit),
  };
  if (classes) body.class = classes;
  return body;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      // Players cache aggressively; keep it short so a refreshed snapshot shows up promptly.
      "cache-control": "public, max-age=300",
      "access-control-allow-origin": "*",
    },
  });
}

function maccmsError(code, message) {
  // Mirrors MacCMS's own error envelope so a client that checks `code` behaves predictably.
  return json({ code, msg: message, page: 1, pagecount: 0, limit: "20", total: 0, list: [] }, 200);
}

/* -------------------------------------------------------------------------- */
/* Handlers                                                                   */
/* -------------------------------------------------------------------------- */

async function handleVod(url, env, key) {
  const ac = (url.searchParams.get("ac") || "list").toLowerCase();
  const { limit, pg } = pageParams(url);
  const { manifest, items } = await loadParts(env, key, ac === "detail" ? "detail" : "list");
  const classes = (manifest.classes || []).map((c) => ({
    type_id: String(c.type_id),
    type_name: c.type_name,
  }));

  if (ac === "class" || ac === "videolist" && false) {
    return json({ code: 1, msg: "分类", class: classes });
  }

  if (ac === "detail") {
    const ids = (url.searchParams.get("ids") || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const wanted = ids.length
      ? items.filter((item) => ids.includes(String(item.vod_id)))
      : items;
    return json(pageEnvelope({ items: wanted, pg, limit, msg: "数据列表" }));
  }

  // ac=list (or anything unrecognised, matching MacCMS's forgiving default)
  const keyword = (url.searchParams.get("wd") || "").trim().toLowerCase();
  if (keyword) {
    const sids = new Set(
      (manifest.search_index || [])
        .filter((entry) => String(entry.t || "").includes(keyword))
        .map((entry) => String(entry.sid)),
    );
    if (sids.size) {
      // Search results should carry play URLs, otherwise a client that jumps straight from
      // search to playback shows an empty detail page.
      const { items: details } = await loadParts(env, key, "detail");
      const hits = details.filter((item) => sids.has(String(item.vod_id)));
      return json(pageEnvelope({ items: hits, pg, limit, msg: "搜索列表" }));
    }
    return json(pageEnvelope({ items: [], pg, limit, msg: "搜索列表" }));
  }

  const typeId = url.searchParams.get("t");
  const filtered = typeId ? items.filter((item) => String(item.type_id) === String(typeId)) : items;
  // Only the first page carries the class table, exactly as MacCMS does.
  return json(pageEnvelope({ items: filtered, pg, limit, classes: pg === 1 ? classes : undefined }));
}

async function handleIndex(env) {
  if (!env.REPO) return json({ error: "REPO binding is not set" }, 500);
  const base = snapshotBase(env);
  const resp = await fetch(`${configBase(env)}/index.gateway.json`, {
    cf: { cacheTtl: 300, cacheEverything: true },
  });
  const config = resp.ok ? await resp.json() : { sites: [] };

  const sites = [];
  for (const entry of config.sites || []) {
    try {
      const manifest = await loadManifest(env, entry.key);
      sites.push({
        key: entry.key,
        name: entry.name,
        counts: manifest.counts,
        classes: (manifest.classes || []).length,
        generated_at: manifest.generated_at,
        subscribe: `/vod/${entry.key}`,
      });
    } catch {
      sites.push({ key: entry.key, name: entry.name, error: "no snapshot" });
    }
  }
  return json({ snapshot_base: base, sites });
}

/**
 * HMAC check for the admin route.
 *
 * The signature covers the raw body, and the comparison is constant-time. A shared secret is
 * used rather than a bearer token because this endpoint can spend the repository's Actions
 * quota, so a leaked log line should not be sufficient to trigger it.
 */
async function verifySignature(request, secret, body) {
  if (!secret) return false;
  const provided = request.headers.get("x-signature") || "";
  if (!provided) return false;

  const encoder = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", cryptoKey, encoder.encode(body));
  const expected = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");

  if (provided.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i += 1) {
    diff |= provided.charCodeAt(i) ^ expected.charCodeAt(i);
  }
  return diff === 0;
}

async function handleRefresh(request, env) {
  if (!env.REPO || !env.DISPATCH_TOKEN) {
    return json({ ok: false, error: "REPO / DISPATCH_TOKEN not configured" }, 500);
  }
  const body = await request.text();
  if (!(await verifySignature(request, env.DISPATCH_TOKEN, body))) {
    return json({ ok: false, error: "bad signature" }, 401);
  }

  let payload = {};
  try {
    payload = body ? JSON.parse(body) : {};
  } catch {
    return json({ ok: false, error: "body must be JSON" }, 400);
  }

  const resp = await fetch(`https://api.github.com/repos/${env.REPO}/dispatches`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.DISPATCH_TOKEN}`,
      accept: "application/vnd.github+json",
      "content-type": "application/json",
      "user-agent": "tvbox-harvest-gateway",
    },
    body: JSON.stringify({
      event_type: "refresh-site",
      client_payload: { site: String(payload.site || "").slice(0, 64) },
    }),
  });

  if (!resp.ok) {
    return json({ ok: false, error: `github dispatch failed: ${resp.status}` }, 502);
  }
  return json({ ok: true, dispatched: payload.site || "all" });
}

/* -------------------------------------------------------------------------- */
/* Router                                                                     */
/* -------------------------------------------------------------------------- */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    try {
      if (path === "/health") {
        return json({ ok: true, snapshot_base: snapshotBase(env), ttl: Number(env.CACHE_TTL || DEFAULT_TTL) });
      }
      if (path === "/") return await handleIndex(env);

      if (path === "/admin/refresh") {
        if (request.method !== "POST") return json({ ok: false, error: "POST required" }, 405);
        return await handleRefresh(request, env);
      }

      const match = path.match(/^\/vod\/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})$/);
      if (match) return await handleVod(url, env, match[1]);

      return json({ code: 404, msg: "not found", list: [] }, 404);
    } catch (err) {
      const status = err instanceof HttpError ? err.status : 500;
      // MacCMS-shaped errors keep clients from treating a transient failure as an empty source.
      return maccmsError(status === 502 ? 500 : status, String(err.message || err).slice(0, 200));
    }
  },
};
