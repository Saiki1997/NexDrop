"use strict";

const fs = require("fs");
const path = require("path");

function loadCatalog() {
  const candidates = [
    path.join(__dirname, "auth-catalog.json"),
    path.join(__dirname, "..", "desktop", "auth-catalog.json"),
    path.join(__dirname, "..", "..", "desktop", "auth-catalog.json"),
  ];
  const found = candidates.find((item) => fs.existsSync(item));
  if (!found) throw new Error("The account list is missing.");
  return JSON.parse(fs.readFileSync(found, "utf8"));
}

const CATALOG = loadCatalog();

function catalogEntry(id) {
  return CATALOG.find((item) => item.id === id) || null;
}

function createVault(file, encrypt, decrypt) {
  function read() {
    let text;
    try {
      text = fs.readFileSync(file, "utf8");
    } catch {
      return { accounts: {} };
    }
    try {
      const data = JSON.parse(text);
      if (data && typeof data.accounts === "object" && data.accounts) return data;
    } catch {
      /* fall through */
    }
    // Keep a damaged vault aside instead of overwriting it on the next save.
    try {
      fs.renameSync(file, `${file}.corrupt-${Date.now()}`);
    } catch {
      /* best effort */
    }
    return { accounts: {} };
  }

  function write(data) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const temp = `${file}.tmp`;
    fs.writeFileSync(temp, JSON.stringify(data), { mode: 0o600 });
    fs.renameSync(temp, file);
  }

  return {
    list() {
      const data = read();
      return CATALOG.map((item) => {
        const saved = data.accounts[item.id];
        return {
          id: item.id,
          name: item.name,
          methods: item.methods,
          note: item.note,
          hosts: item.hosts || [],
          configured: Boolean(saved),
          method: saved ? saved.method : "none",
          lastCheck: saved ? saved.lastCheck || null : null,
          lastError: saved ? saved.lastError || "" : "",
          label: saved ? saved.label || "" : "",
        };
      });
    },

    save(id, method, fields) {
      const entry = catalogEntry(id);
      if (!entry) throw new Error("That website has no account settings in this build.");
      if (!entry.methods.includes(method)) throw new Error("That sign-in method is not used by this website.");
      const secret = {};
      if (method === "api_key") {
        const key = String(fields.apiKey || "").trim();
        if (key.length < 8) throw new Error("Enter the API token. It was not saved.");
        secret.apiKey = key;
      } else if (method === "password") {
        const email = String(fields.email || "").trim();
        const password = String(fields.password || "");
        if (!email.includes("@") || password.length < 4) throw new Error("Enter the email and password. They were not saved.");
        secret.email = email;
        secret.password = password;
      } else if (method === "cookies") {
        const cookiePath = String(fields.cookieFile || "").trim();
        if (!cookiePath) throw new Error("Choose a cookie file first.");
        secret.cookieFile = cookiePath;
        secret.host = String(fields.host || "").trim().toLowerCase();
      } else if (method === "telegram") {
        const session = String(fields.session || "");
        const apiId = String(fields.apiId || "").trim();
        const apiHash = String(fields.apiHash || "").trim();
        if (!session || !/^\d+$/.test(apiId) || !/^[0-9a-f]{32}$/.test(apiHash)) throw new Error("The Telegram sign-in did not finish. Nothing was saved.");
        secret.session = session;
        secret.apiId = apiId;
        secret.apiHash = apiHash;
      } else if (method === "none") {
        this.remove(id);
        return { configured: false };
      } else {
        throw new Error("That sign-in method is not available.");
      }
      const blob = encrypt(JSON.stringify(secret));
      const preview = JSON.stringify({ secret: blob });
      if (secret.apiKey && preview.includes(secret.apiKey)) {
        throw new Error("The token would have been stored in the clear. Nothing was kept.");
      }
      if (secret.password && preview.includes(secret.password)) {
        throw new Error("The password would have been stored in the clear. Nothing was kept.");
      }
      if (secret.session && preview.includes(secret.session)) {
        throw new Error("The Telegram session would have been stored in the clear. Nothing was kept.");
      }
      const data = read();
      // label is only the account's display name (for example "Saiful (@saiful)"), never a secret.
      const label = method === "telegram" ? String(fields.label || "").slice(0, 80) : "";
      data.accounts[id] = { method, secret: blob, lastCheck: method === "telegram" ? new Date().toISOString() : null, lastError: "", label };
      write(data);
      return { configured: true, method };
    },

    remove(id) {
      const data = read();
      delete data.accounts[id];
      write(data);
      return { configured: false };
    },

    readSecret(id) {
      const data = read();
      const saved = data.accounts[id];
      if (!saved) return null;
      return { method: saved.method, ...JSON.parse(decrypt(saved.secret)) };
    },

    mark(id, lastCheck, lastError) {
      const data = read();
      if (!data.accounts[id]) return;
      data.accounts[id].lastCheck = lastCheck;
      data.accounts[id].lastError = String(lastError || "").slice(0, 180);
      write(data);
    },

    /** The Telegram account for the session process, or null. */
    telegramAuth() {
      const secret = this.readSecret("telegram");
      if (!secret || secret.method !== "telegram" || !secret.session) return null;
      return { apiId: secret.apiId, apiHash: secret.apiHash, session: secret.session };
    },

    engineAuth() {
      const auth = {};
      for (const item of CATALOG) {
        if (!item.methods.includes("api_key") && !item.methods.includes("password")) continue;
        const secret = this.readSecret(item.id);
        if (!secret) continue;
        if (secret.method === "api_key" && secret.apiKey) auth[item.id] = { api_key: secret.apiKey };
        if (secret.method === "password" && secret.password) {
          auth[item.id] = { email: secret.email, password: secret.password };
        }
      }
      return auth;
    },
  };
}

module.exports = { createVault, CATALOG };
