// Cloudflare Worker: instant Telegram webhook handler for the Planet Cinema
// watchlist bot. Handles /movies, /watch, /unwatch, /list in real time.
// The actual "did a new date/movie become bookable" polling still happens in
// the GitHub Actions cron job (check.py), which reads the watchlist this
// Worker maintains via GET /watchlist (bearer-token protected).

const SITE = "10100";
const BASE = "https://www.planetcinema.co.il/il/data-api-service/v1";

async function getFeed(name) {
  const res = await fetch(`${BASE}/feed/${SITE}/byName/${name}?lang=he_IL`, {
    headers: { "User-Agent": "Mozilla/5.0" },
  });
  const d = await res.json();
  return d.body.posters.map((p) => {
    const code = p.url.replace(/\/$/, "").split("/").pop();
    return {
      code,
      featureTitle: p.featureTitle,
      url: p.url,
      attributes: p.attributes || [],
    };
  });
}

async function getCatalog() {
  const [nowPlaying, comingSoon] = await Promise.all([
    getFeed("now-playing"),
    getFeed("coming-soon"),
  ]);
  const seen = new Set();
  const out = [];
  for (const e of [...nowPlaying, ...comingSoon]) {
    if (seen.has(e.code)) continue;
    seen.add(e.code);
    out.push(e);
  }
  return out;
}

function extractFilmCode(arg) {
  arg = arg.trim();
  const m = arg.match(/planetcinema\.co\.il\/films\/[^/]+\/([\w-]+)/i);
  if (m) return m[1].toLowerCase();
  return arg.toLowerCase();
}

function searchCatalog(catalog, query) {
  if (!query) return catalog;
  const q = query.toLowerCase();
  return catalog.filter(
    (e) => e.featureTitle.toLowerCase().includes(q) || e.url.toLowerCase().includes(q)
  );
}

function formatMovieList(entries) {
  return entries
    .map((e, i) => `${i + 1}. ${e.featureTitle}${e.attributes.includes("imax") ? " [IMAX]" : ""}`)
    .join("\n");
}

function resolveWatchTarget(arg, lastMovieList, catalog) {
  let tokens = arg.trim().split(/\s+/);
  if (!tokens.length) return null;
  let imaxOnly = false;
  if (tokens[tokens.length - 1].toLowerCase() === "imax") {
    imaxOnly = true;
    tokens = tokens.slice(0, -1);
  }
  if (!tokens.length) return null;
  const ref = tokens.join(" ");

  if (/^\d+$/.test(ref) && lastMovieList && lastMovieList.length) {
    const idx = parseInt(ref, 10) - 1;
    if (idx >= 0 && idx < lastMovieList.length) {
      const entry = lastMovieList[idx];
      return { code: entry.code, name: entry.featureTitle, imaxOnly };
    }
    return null;
  }

  const code = extractFilmCode(ref);
  const match = catalog.find((e) => e.code === code);
  return { code, name: match ? match.featureTitle : code, imaxOnly };
}

async function sendTelegram(env, text, replyTo) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  for (let i = 0; i < text.length; i += 3500) {
    const chunk = text.slice(i, i + 3500);
    const params = new URLSearchParams({ chat_id: env.TELEGRAM_CHAT_ID, text: chunk });
    if (replyTo) params.set("reply_to_message_id", String(replyTo));
    await fetch(url, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: params });
  }
}

async function loadWatchlist(env) {
  const raw = await env.WATCHLIST_KV.get("watchlist", "json");
  return raw || { "7460s2r": { name: "The Odyssey / האודיסאה", imax: false } };
}

async function saveWatchlist(env, watchlist) {
  await env.WATCHLIST_KV.put("watchlist", JSON.stringify(watchlist));
}

async function handleTelegramUpdate(env, update) {
  const msg = update.message;
  if (!msg || !msg.text) return;
  const chatId = String(msg.chat?.id || "");
  if (chatId !== String(env.TELEGRAM_CHAT_ID)) return; // ignore anyone else

  const text = msg.text.trim();
  if (!text.startsWith("/")) return;
  const spaceIdx = text.indexOf(" ");
  const cmd = (spaceIdx === -1 ? text : text.slice(0, spaceIdx)).toLowerCase();
  const arg = spaceIdx === -1 ? "" : text.slice(spaceIdx + 1).trim();
  const msgId = msg.message_id;

  if (cmd === "/movies" || cmd === "/imax") {
    const catalog = await getCatalog();
    const pool = cmd === "/imax" ? catalog.filter((e) => e.attributes.includes("imax")) : catalog;
    const results = searchCatalog(pool, arg);
    await env.WATCHLIST_KV.put(
      "last_movie_list",
      JSON.stringify(results.map((e) => ({ code: e.code, featureTitle: e.featureTitle })))
    );
    if (results.length) {
      await sendTelegram(env, `${formatMovieList(results)}\n\nReply e.g. /watch 3  or  /watch 3 imax`, msgId);
    } else {
      await sendTelegram(env, cmd === "/imax" ? "No IMAX movies found." : "No matching movies.", msgId);
    }
  } else if (cmd === "/watch" && arg) {
    const catalog = await getCatalog();
    const lastMovieList = (await env.WATCHLIST_KV.get("last_movie_list", "json")) || [];
    const resolved = resolveWatchTarget(arg, lastMovieList, catalog);
    if (!resolved) {
      await sendTelegram(env, "Couldn't resolve that. Try /movies <search> first, then /watch <number>.", msgId);
    } else {
      const watchlist = await loadWatchlist(env);
      watchlist[resolved.code] = { name: resolved.name, imax: resolved.imaxOnly };
      await saveWatchlist(env, watchlist);
      await sendTelegram(env, `Watching: ${resolved.name}${resolved.imaxOnly ? " (IMAX only)" : ""} (${resolved.code})`, msgId);
    }
  } else if (cmd === "/unwatch" && arg) {
    const code = extractFilmCode(arg.split(/\s+/)[0]);
    const watchlist = await loadWatchlist(env);
    if (watchlist[code]) {
      const name = watchlist[code].name;
      delete watchlist[code];
      await saveWatchlist(env, watchlist);
      await sendTelegram(env, `Stopped watching: ${name} (${code})`, msgId);
    } else {
      await sendTelegram(env, `Not on watchlist: ${code}`, msgId);
    }
  } else if (cmd === "/list") {
    const watchlist = await loadWatchlist(env);
    const codes = Object.keys(watchlist);
    if (codes.length) {
      const lines = codes.map((code) => `- ${watchlist[code].name}${watchlist[code].imax ? " [IMAX only]" : ""} (${code})`);
      await sendTelegram(env, "Watchlist:\n" + lines.join("\n"), msgId);
    } else {
      await sendTelegram(env, "Watchlist is empty.", msgId);
    }
  } else if (cmd === "/start") {
    await sendTelegram(env, "Planet Cinema bot ready. Try /movies <search> or /imax <search>, then /watch <number> [imax], /unwatch <code>, /list.", msgId);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/telegram-webhook") {
      const update = await request.json();
      try {
        await handleTelegramUpdate(env, update);
      } catch (e) {
        console.error(e);
      }
      return new Response("ok");
    }

    if (request.method === "GET" && url.pathname === "/watchlist") {
      const auth = request.headers.get("Authorization") || "";
      if (auth !== `Bearer ${env.SYNC_SECRET}`) {
        return new Response("unauthorized", { status: 401 });
      }
      const watchlist = await loadWatchlist(env);
      return new Response(JSON.stringify(watchlist), { headers: { "Content-Type": "application/json" } });
    }

    return new Response("not found", { status: 404 });
  },
};
