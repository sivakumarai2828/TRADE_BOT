const VM_BASE = "http://104.155.150.69:8000";

export default async (request) => {
  const url = new URL(request.url);
  const target = `${VM_BASE}${url.pathname}${url.search}`;

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers: { "content-type": "application/json" },
      body: ["GET", "HEAD"].includes(request.method) ? null : request.body,
    });

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
      },
    });
  } catch (err) {
    return new Response(JSON.stringify({ error: err.message }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }
};

export const config = {
  path: [
    "/health",
    "/status",
    "/signals",
    "/positions",
    "/logs",
    "/candles",
    "/start",
    "/stop",
    "/close",
    "/deposit",
    "/settings",
    "/daybot/start",
    "/daybot/stop",
    "/daybot/status",
    "/daybot/positions",
    "/daybot/signals",
    "/daybot/watchlist",
    "/daybot/logs",
    "/daybot/settings",
  ],
};
