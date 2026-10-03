/**
 * Unofficial WhatsApp QR bridge — use a SPARE number, never anyone's main number.
 *
 * How it works:
 *  1. First run prints a QR code -> scan it with the SPARE number's WhatsApp
 *     (Settings > Linked Devices > Link a Device). Session is saved to ./auth/.
 *  2. Incoming messages are forwarded to the Python bot brain (FastAPI):
 *       POST {BOT_URL}/api/wa/process {owner, text} -> {replies}
 *       POST {BOT_URL}/api/wa/upload (multipart owner+file) -> {replies}
 *  3. The Python scheduler pushes alerts back through this bridge:
 *       POST http://127.0.0.1:8002/send {secret, to, text}
 *
 * Env (or wa-bridge/.env): BOT_URL, BRIDGE_PORT, WA_BRIDGE_SECRET
 */
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import qr from "qrcode-terminal";

import makeWASocket, {
  useMultiFileAuthState,
  downloadMediaMessage,
  DisconnectReason,
} from "@whiskeysockets/baileys";
import pino from "pino";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
for (const f of [path.join(__dirname, ".env"), path.join(__dirname, "..", ".env")]) {
  if (fs.existsSync(f)) {
    for (const line of fs.readFileSync(f, "utf8").split("\n")) {
      const m = line.match(/^\s*([A-Za-z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
    }
  }
}

const BOT_URL = (process.env.BOT_URL || "http://127.0.0.1:8001").replace(/\/$/, "");
const PORT = Number(process.env.BRIDGE_PORT || 8002);
const SECRET = process.env.WA_BRIDGE_SECRET || "";
const AUTH_DIR = path.join(__dirname, "auth");
const log = pino({ level: "info" });

let sock = null;
let lastQRAt = 0;

async function botProcess(owner, text) {
  const r = await fetch(`${BOT_URL}/api/wa/process`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Bridge-Secret": SECRET },
    body: JSON.stringify({ owner, text }),
  });
  if (!r.ok) throw new Error(`bot process HTTP ${r.status}`);
  return (await r.json()).replies || [];
}

async function botUpload(owner, buffer, filename, mime) {
  const fd = new FormData();
  fd.append("owner", owner);
  fd.append("file", new Blob([buffer], { type: mime || "application/octet-stream" }), filename);
  const r = await fetch(`${BOT_URL}/api/wa/upload`, {
    method: "POST",
    headers: { "X-Bridge-Secret": SECRET },
    body: fd,
  });
  if (!r.ok) throw new Error(`bot upload HTTP ${r.status}`);
  return (await r.json()).replies || [];
}

const toJid = (digits) => `${String(digits).replace(/\D/g, "")}@s.whatsapp.net`;

async function reply(jid, texts) {
  for (const t of texts || []) {
    if (!t) continue;
    // WhatsApp caps a message at ~64k chars; chunk to be safe.
    for (const chunk of String(t).match(/[\s\S]{1,4000}/g) || []) {
      await sock.sendMessage(jid, { text: chunk });
    }
  }
}

async function handleIncoming(msg) {
  try {
    if (!msg.message || msg.key?.fromMe) return;
    if (msg.key?.remoteJid === "status@broadcast") return;
    const jid = msg.key.remoteJid;
    if (!jid?.endsWith("@s.whatsapp.net")) return; // skip groups for family 1:1 bot
    const owner = jid.split("@")[0];

    const text =
      msg.message.conversation ||
      msg.message.extendedTextMessage?.text ||
      msg.message.imageMessage?.caption ||
      msg.message.documentMessage?.caption ||
      "";
    const media =
      msg.message.imageMessage || msg.message.documentMessage || null;

    let replies;
    if (media) {
      const buf = await downloadMediaMessage(msg, "buffer", {});
      const filename =
        msg.message.documentMessage?.fileName ||
        `photo-${Date.now()}.jpg`;
      const mime = media.mimetype || "application/octet-stream";
      replies = await botUpload(owner, buf, filename, mime);
    } else if (text.trim()) {
      replies = await botProcess(owner, text);
    } else {
      return;
    }
    await reply(jid, replies);
  } catch (e) {
    log.error({ err: String(e) }, "incoming handler failed");
  }
}

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  sock = makeWASocket({
    auth: state,
    logger: pino({ level: "silent" }),
    browser: ["FlightBot", "Desktop", "1.0"],
    syncFullHistory: false,
    markOnlineOnConnect: false,
  });
  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("messages.upsert", async ({ messages }) => {
    for (const m of messages || []) await handleIncoming(m);
  });
  sock.ev.on("connection.update", (u) => {
    if (u.qr && Date.now() - lastQRAt > 5000) {
      lastQRAt = Date.now();
      console.log("\n📱 Scan this QR with the SPARE number's WhatsApp (Linked Devices):\n");
      qr.generate(u.qr, { small: true });
    }
    if (u.connection === "open") console.log("✅ Bridge connected as", sock?.user?.id);
    if (u.connection === "close") {
      const code = u.lastDisconnect?.error?.output?.statusCode;
      console.log(`⚠️ Connection closed (${code}). Reconnecting in 5s...`);
      if (code === DisconnectReason.loggedOut) {
        console.log("❌ Logged out. Delete ./auth/ and rescan.");
        return;
      }
      setTimeout(connect, 5000);
    }
  });
}

// ---- /send endpoint for Python scheduler alerts ----
const server = http.createServer((req, res) => {
  if (req.method !== "POST" || req.url !== "/send") {
    res.writeHead(404); res.end("not found"); return;
  }
  let body = "";
  req.on("data", (c) => { body += c; if (body.length > 200000) req.destroy(); });
  req.on("end", async () => {
    try {
      const { secret, to, text } = JSON.parse(body || "{}");
      if (SECRET && secret !== SECRET) { res.writeHead(403); res.end("bad secret"); return; }
      if (!sock || !to || !text) { res.writeHead(503); res.end("not ready"); return; }
      await reply(toJid(to), [text]);
      res.writeHead(200); res.end("sent");
    } catch (e) {
      res.writeHead(500); res.end(String(e).slice(0, 200));
    }
  });
});
server.listen(PORT, "127.0.0.1", () => console.log(`🌉 Bridge /send on 127.0.0.1:${PORT}`));

connect();
