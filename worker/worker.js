// Cloudflare Worker: instant Telegram webhook handler for the Planet Cinema
// watchlist bot. Handles /movies, /watch, /unwatch, /list in real time.
// The actual "did a new date/movie become bookable" polling still happens in
// the GitHub Actions cron job (check.py), which reads the watchlist this
// Worker maintains via GET /watchlist (bearer-token protected).

const SITE = "10100";
const BASE = "https://www.planetcinema.co.il/il/data-api-service/v1";
const CINEMA_ID = "1072"; // Planet Rishon LeZion
const INFO_HORIZON_DAYS = 14;

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

async function getDayFilms(day) {
  const res = await fetch(
    `${BASE}/quickbook/${SITE}/film-events/in-cinema/${CINEMA_ID}/at-date/${day}?attr=&lang=he_IL`,
    { headers: { "User-Agent": "Mozilla/5.0" } }
  );
  const d = await res.json();
  const out = {};
  for (const f of d.body.films) out[f.id] = { name: f.name, attributes: f.attributeIds || [] };
  return out;
}

function isoDate(offsetDays) {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

async function getBookingStatus(codes) {
  // code -> { anyDates: [...], imaxDates: [...] }
  const status = {};
  for (const code of codes) status[code] = { anyDates: [], imaxDates: [] };
  const days = Array.from({ length: INFO_HORIZON_DAYS }, (_, i) => isoDate(i));
  const perDay = await Promise.all(days.map((day) => getDayFilms(day).catch(() => ({}))));
  perDay.forEach((films, i) => {
    const day = days[i];
    for (const code of codes) {
      const film = films[code];
      if (!film) continue;
      status[code].anyDates.push(day);
      if (film.attributes.includes("imax")) status[code].imaxDates.push(day);
    }
  });
  return status;
}

// Isolate LTR runs (dates, codes, URLs) inside RTL Hebrew text so Telegram
// doesn't garble the bidi ordering.
const ltr = (s) => `⁦${s}⁩`;

// "YYYY-MM-DD" (the API/URL format) -> "DD/MM/YY" for display only.
function displayDate(isoDate) {
  const [y, m, d] = isoDate.split("-");
  return `${d}/${m}/${y.slice(2)}`;
}

function formatFilmInfo(code, catalogEntry, watchEntry, status) {
  const name = catalogEntry?.featureTitle || watchEntry?.name || code;
  const lines = [`${name} ${ltr(`(${code})`)}`];
  if (catalogEntry?.dateStarted) {
    lines.push(`תאריך בכורה: ${ltr(displayDate(catalogEntry.dateStarted.slice(0, 10)))}`);
  }
  const bookingLink = (date) =>
    catalogEntry?.url
      ? `${catalogEntry.url}#/buy-tickets-by-film?for-movie=${code}&in-cinema=${CINEMA_ID}&at=${date}&view-mode=list`
      : null;

  if (status.anyDates.length) {
    lines.push(`ניתן להזמין (ראשון לציון): ${ltr(displayDate(status.anyDates[0]))} עד ${ltr(displayDate(status.anyDates[status.anyDates.length - 1]))}`);
  } else {
    lines.push("עדיין לא ניתן להזמין (ראשון לציון)");
  }
  if (status.imaxDates.length) {
    lines.push(`תאריכי IMAX: ${ltr(displayDate(status.imaxDates[0]))} עד ${ltr(displayDate(status.imaxDates[status.imaxDates.length - 1]))}`);
  }

  // Only one link is useful; prefer an IMAX-dated link if it exists.
  const linkDate = status.imaxDates[0] || status.anyDates[0];
  if (linkDate) {
    const link = bookingLink(linkDate);
    if (link) lines.push(`לך תזמין אח\n${ltr(link)}`);
  }
  return lines.join("\n");
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

const WELCOME_TEXT =
  "ברוך הבא אחשלי לבוט מזיין סרטים\n" +
  "תעשה /movies בשביל רשימה של כל הסרטים\n" +
  "/imax בשביל כל הסרטים באיימקס\n" +
  "/watch <number> בשביל להאזין לסרט הזה\n" +
  "/watch <number> imax בשביל להאזין רק להקרנות IMAX של הסרט הזה\n" +
  "/watchlist בשביל כל ההסרטים שאתה מאזין להם\n" +
  "/unwatch <code> בשביל להפסיק להאזין\n" +
  "/info <number|code> בשביל מידע על סרט ספציפי, או /info בלי כלום בשביל מידע על כל רשימת המעקב";

async function sendTelegram(env, text, replyTo, chatId) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  for (let i = 0; i < text.length; i += 3500) {
    const chunk = text.slice(i, i + 3500);
    const params = new URLSearchParams({ chat_id: chatId || env.TELEGRAM_CHAT_ID, text: chunk });
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

async function handleChatMemberUpdate(env, update) {
  // Fires when someone starts/unblocks/re-adds the bot, even without an
  // explicit /start message (e.g. via a shared bot link).
  const cm = update.my_chat_member;
  if (!cm) return;
  const wasActive = ["member", "administrator", "creator"].includes(cm.old_chat_member?.status);
  const isActive = ["member", "administrator", "creator"].includes(cm.new_chat_member?.status);
  if (!wasActive && isActive) {
    await sendTelegram(env, WELCOME_TEXT, null, cm.chat?.id);
  }
}

async function handleTelegramUpdate(env, update) {
  await handleChatMemberUpdate(env, update);

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
  } else if (cmd === "/info") {
    const watchlist = await loadWatchlist(env);
    const catalog = await getCatalog();

    if (!arg) {
      // /info with no arg -> every film currently on the watchlist
      const codes = Object.keys(watchlist);
      if (!codes.length) {
        await sendTelegram(env, "Watchlist is empty. Use /movies then /watch first.", msgId);
        return;
      }
      const status = await getBookingStatus(codes);
      const blocks = codes.map((code) =>
        formatFilmInfo(code, catalog.find((e) => e.code === code), watchlist[code], status[code])
      );
      await sendTelegram(env, blocks.join("\n\n"), msgId);
    } else {
      const lastMovieList = (await env.WATCHLIST_KV.get("last_movie_list", "json")) || [];
      const resolved = resolveWatchTarget(arg, lastMovieList, catalog);
      if (!resolved) {
        await sendTelegram(env, "Couldn't resolve that. Try /movies <search> first, then /info <number>.", msgId);
      } else {
        const status = await getBookingStatus([resolved.code]);
        const block = formatFilmInfo(
          resolved.code,
          catalog.find((e) => e.code === resolved.code),
          watchlist[resolved.code],
          status[resolved.code]
        );
        await sendTelegram(env, block, msgId);
      }
    }
  } else if (cmd === "/watchlist") {
    const watchlist = await loadWatchlist(env);
    const codes = Object.keys(watchlist);
    if (codes.length) {
      const lines = codes.map((code) => `- ${watchlist[code].name}${watchlist[code].imax ? " [IMAX only]" : ""} (${code})`);
      await sendTelegram(env, "Watchlist:\n" + lines.join("\n"), msgId);
    } else {
      await sendTelegram(env, "Watchlist is empty.", msgId);
    }
  } else if (cmd === "/start") {
    await sendTelegram(env, WELCOME_TEXT, msgId);
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
