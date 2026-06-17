const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, hashstring",
  "Content-Type": "text/plain",
};

const RENDER_HUB_URL = (
  process.env.RENDER_HUB_URL || "https://prime-automation-hub.onrender.com"
).replace(/\/$/, "");

const RENDER_TAP_WEBHOOK_URL = `${RENDER_HUB_URL}/webhook/tap`;

function headerValue(headers, name) {
  if (!headers) return "";
  const direct = headers[name];
  if (direct) return direct;
  const lower = headers[name.toLowerCase()];
  if (lower) return lower;
  const key = Object.keys(headers).find(
    (k) => k.toLowerCase() === name.toLowerCase()
  );
  return key ? headers[key] : "";
}

exports.handler = async (event) => {
  if (event.httpMethod === "OPTIONS") {
    return {
      statusCode: 200,
      headers: CORS_HEADERS,
      body: "",
    };
  }

  if (event.httpMethod !== "POST") {
    return {
      statusCode: 405,
      headers: CORS_HEADERS,
      body: "Method Not Allowed",
    };
  }

  const contentType =
    headerValue(event.headers, "content-type") || "application/json";
  const authorization = headerValue(event.headers, "authorization");
  const hashstring = headerValue(event.headers, "hashstring");
  const body = event.isBase64Encoded
    ? Buffer.from(event.body || "", "base64")
    : event.body || "";

  const forwardHeaders = { "Content-Type": contentType };
  if (authorization) forwardHeaders.Authorization = authorization;
  if (hashstring) forwardHeaders.hashstring = hashstring;

  try {
    const resp = await fetch(RENDER_TAP_WEBHOOK_URL, {
      method: "POST",
      headers: forwardHeaders,
      body,
    });
    const text = await resp.text();
    console.log(
      `proxied Tap webhook → ${RENDER_TAP_WEBHOOK_URL} status=${resp.status} bytes=${body.length}`
    );
    return {
      statusCode: resp.status,
      headers: CORS_HEADERS,
      body: text || "OK",
    };
  } catch (err) {
    console.error("Tap webhook proxy failed:", err);
    return {
      statusCode: 502,
      headers: CORS_HEADERS,
      body: "Bad Gateway: could not reach Render /webhook/tap",
    };
  }
};
