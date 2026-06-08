const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Content-Type": "text/html; charset=utf-8",
};

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

exports.handler = async (event) => {
  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 200, headers: CORS_HEADERS, body: "" };
  }

  if (event.httpMethod !== "GET") {
    return {
      statusCode: 405,
      headers: { ...CORS_HEADERS, Allow: "GET, OPTIONS" },
      body: "Method Not Allowed",
    };
  }

  const params = event.queryStringParameters || {};
  const error = params.error;
  const code = params.code;
  const realmId = params.realmId;
  const state = params.state;

  if (error) {
    return {
      statusCode: 200,
      headers: CORS_HEADERS,
      body: `<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px;">
  <h2>QuickBooks authorization failed</h2>
  <p><strong>Error:</strong> ${escapeHtml(error)}</p>
  <p>Return to your terminal and try again.</p>
</body></html>`,
    };
  }

  const callbackUrl = `https://${event.headers.host || "prime-qbo-webhook.netlify.app"}${event.path}${
    event.rawQuery ? `?${event.rawQuery}` : ""
  }`;

  return {
    statusCode: 200,
    headers: CORS_HEADERS,
    body: `<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.5;">
  <h2>QuickBooks authorization complete</h2>
  <p>Copy the <strong>full URL</strong> from your browser address bar and paste it into your terminal when prompted.</p>
  <p style="word-break:break-all;background:#f4f4f4;padding:12px;border-radius:8px;">${escapeHtml(callbackUrl)}</p>
  <p><strong>realmId:</strong> ${escapeHtml(realmId || "(missing)")}</p>
  <p>You can close this tab after pasting the URL in the terminal.</p>
</body></html>`,
  };
};
