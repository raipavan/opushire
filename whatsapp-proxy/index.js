/**
 * WhatsApp Web sidecar (whatsapp-web.js).
 * Scan QR once → session persisted in ./whatsapp-session/
 * Inbound messages POST to FastAPI /api/whatsapp/proxy/message
 */

const express = require("express");
const QRCode = require("qrcode");
const { Client, LocalAuth } = require("whatsapp-web.js");

const PORT = parseInt(process.env.PORT || "3001", 10);
const FASTAPI_WEBHOOK_URL =
  process.env.FASTAPI_WEBHOOK_URL ||
  "http://127.0.0.1:8000/api/whatsapp/proxy/message";
const PROXY_SECRET = (process.env.WHATSAPP_PROXY_SECRET || "").trim();
const SESSION_PATH = process.env.WHATSAPP_SESSION_PATH || "./whatsapp-session";

let lastQrString = null;
let lastQrPng = null;
const state = {
  authenticated: false,
  connected: false,
  phone: "",
  pushname: "",
  last_error: "",
};

function waIdToPhone(waId) {
  if (!waId) return "";
  const base = String(waId).split("@")[0] || "";
  const digits = base.replace(/\D/g, "");
  if (!digits) return "";
  if (digits.length === 10) return `+91${digits}`;
  return `+${digits}`;
}

async function forwardToFastapi(payload) {
  const headers = { "Content-Type": "application/json" };
  if (PROXY_SECRET) headers["X-Proxy-Secret"] = PROXY_SECRET;
  const resp = await fetch(FASTAPI_WEBHOOK_URL, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`FastAPI ${resp.status}: ${text.slice(0, 300)}`);
  }
  return resp.json();
}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: SESSION_PATH }),
  puppeteer: {
    headless: true,
    executablePath: 'C:\\Users\\Mayur\\AppData\\Local\\ms-playwright\\chromium-1228\\chrome-win64\\chrome.exe',
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
    ],
  },
});

client.on("qr", async (qr) => {
  lastQrString = qr;
  state.authenticated = false;
  state.connected = false;
  try {
    lastQrPng = await QRCode.toBuffer(qr, { type: "png", width: 400, margin: 2 });
  } catch (e) {
    console.error("QR PNG encode failed:", e.message);
    lastQrPng = null;
  }
  console.log("[whatsapp-proxy] QR updated — scan with WhatsApp → Linked devices");
});

client.on("authenticated", () => {
  state.authenticated = true;
  lastQrString = null;
  lastQrPng = null;
  console.log("[whatsapp-proxy] Authenticated");
});

client.on("ready", async () => {
  state.connected = true;
  state.authenticated = true;
  lastQrString = null;
  lastQrPng = null;
  try {
    const wid = client.info?.wid?.user || "";
    state.phone = waIdToPhone(`${wid}@c.us`);
    state.pushname = client.info?.pushname || "";
  } catch (_) {
    /* ignore */
  }
  console.log("[whatsapp-proxy] Ready", state.phone || "(unknown number)");
});

client.on("auth_failure", (msg) => {
  state.last_error = String(msg || "auth_failure");
  state.authenticated = false;
  state.connected = false;
  console.error("[whatsapp-proxy] Auth failure:", state.last_error);
});

client.on("disconnected", (reason) => {
  state.connected = false;
  state.authenticated = false;
  state.last_error = String(reason || "disconnected");
  console.warn("[whatsapp-proxy] Disconnected:", state.last_error);
});

client.on("message_create", async (msg) => {
  try {
    if (msg.fromMe) return;
    const from = msg.from || "";
    if (from.endsWith("@g.us") || from === "status@broadcast") return;

    const text = (msg.body || "").trim();
    if (!text && msg.type !== "chat") return;

    const contact = await msg.getContact().catch(() => null);
    const profileName =
      (contact && (contact.pushname || contact.name || contact.shortName)) || "";

    const payload = {
      from_phone: waIdToPhone(from),
      from_wa_id: from,
      profile_name: profileName,
      text: text || `[${msg.type}]`,
      message_id: msg.id?.id || msg.id?._serialized || String(Date.now()),
      timestamp: msg.timestamp || Math.floor(Date.now() / 1000),
    };

    if (!payload.from_phone) {
      console.warn("[whatsapp-proxy] Skip message — no phone:", from);
      return;
    }

    const result = await forwardToFastapi(payload);
    console.log("[whatsapp-proxy] Forwarded", payload.from_phone, result);
  } catch (e) {
    console.error("[whatsapp-proxy] message_create error:", e.message);
  }
});

const app = express();
app.use(express.json({ limit: "256kb" }));

app.get("/health", (_req, res) => {
  res.json({ ok: true, service: "whatsapp-proxy" });
});

app.get("/status", (_req, res) => {
  res.json({
    authenticated: state.authenticated,
    connected: state.connected,
    phone: state.phone,
    pushname: state.pushname,
    has_qr: Boolean(lastQrPng || lastQrString),
    last_error: state.last_error || null,
  });
});

app.get("/qr", async (_req, res) => {
  if (state.authenticated && state.connected) {
    return res.status(204).send();
  }
  if (lastQrPng) {
    res.set("Content-Type", "image/png");
    res.set("Cache-Control", "no-store, max-age=0");
    return res.send(lastQrPng);
  }
  if (lastQrString) {
    try {
      const buf = await QRCode.toBuffer(lastQrString, { type: "png", width: 400, margin: 2 });
      res.set("Content-Type", "image/png");
      res.set("Cache-Control", "no-store, max-age=0");
      return res.send(buf);
    } catch (e) {
      return res.status(503).json({ error: e.message });
    }
  }
  return res.status(503).json({ error: "QR not ready yet — wait a few seconds and refresh" });
});

app.post("/send", async (req, res) => {
  if (PROXY_SECRET) {
    const hdr = (req.headers["x-proxy-secret"] || "").trim();
    if (hdr !== PROXY_SECRET) {
      return res.status(403).json({ error: "invalid proxy secret" });
    }
  }
  if (!state.connected) {
    return res.status(503).json({ error: "WhatsApp not connected" });
  }
  const toRaw = String(req.body?.to || "").trim();
  const text = String(req.body?.text || "").trim();
  if (!toRaw || !text) {
    return res.status(400).json({ error: "to and text required" });
  }
  const digits = toRaw.replace(/\D/g, "");
  const chatId = `${digits}@c.us`;
  try {
    const sent = await client.sendMessage(chatId, text);
    return res.json({ ok: true, id: sent.id?.id || sent.id?._serialized });
  } catch (e) {
    console.error("[whatsapp-proxy] send failed:", e.message);
    return res.status(500).json({ error: e.message });
  }
});

// List all groups
app.get("/groups", async (_req, res) => {
  if (!state.connected) return res.status(503).json({ error: "Not connected" });
  try {
    const chats = await client.getChats();
    const groups = chats.filter(c => c.isGroup).map(c => ({
      id: c.id._serialized,
      name: c.name,
      participants: c.participants.map(p => ({
        id: p.id._serialized,
        phone: p.id.user,
        name: p.pushname || '',
        isAdmin: p.isAdmin
      }))
    }));
    res.json(groups);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Get group contacts
app.get("/group/:id/contacts", async (req, res) => {
  if (!state.connected) return res.status(503).json({ error: "Not connected" });
  try {
    const chat = await client.getChatById(req.params.id);
    if (!chat.isGroup) return res.status(400).json({ error: "Not a group" });
    const contacts = chat.participants.map(p => ({
      phone: p.id.user,
      name: p.pushname || '',
      isAdmin: p.isAdmin
    }));
    res.json({ group: chat.name, contacts });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(PORT, () => {
  console.log(`[whatsapp-proxy] HTTP listening on :${PORT}`);
  console.log(`[whatsapp-proxy] Forward → ${FASTAPI_WEBHOOK_URL}`);
  client.initialize().catch((e) => {
    state.last_error = e.message;
    console.error("[whatsapp-proxy] initialize failed:", e.message);
  });
});
