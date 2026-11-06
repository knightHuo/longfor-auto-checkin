#!/usr/bin/env node
/**
 * Optional basic-slider adapter. Default off; auto is an alias for local OCR.
 * Input JSON on stdin: {device, user_agent}; output one JSON line.
 * --check-deps is offline. No image, SID, device or browser error is logged.
 *
 * Static SDK 1.3.41(372) exposes .dx_captcha_basic_bg and
 * .dx_captcha_basic_sub-slider img. Other layouts fail closed. DOM extraction,
 * threshold and actual verification still require a real authorized sample.
 */
const fs = require("fs");
const path = require("path");
const { spawn, spawnSync } = require("child_process");

const APP_ID = "d1a43734fc59aeae9f1562dbd70fdf54";
const API = "https://ly-ver.longhu.net";
const STA = "https://ly-sta.longhu.net/dx-captcha";
const COMP_URL = `${STA}/basic-captcha-js.js`;
const LOCAL_PAGE = "https://longzhu.longfor.com/__local_captcha_adapter__";
const MAX_IMAGE = 2 * 1024 * 1024;
const args = Object.fromEntries(process.argv.slice(2).map((arg) => {
  const match = arg.match(/^--([^=]+)=(.*)$/);
  return match ? [match[1], match[2]] : [arg.replace(/^--/, ""), true];
}));
const MODE = args.mode === "auto" ? "local" : args.mode || "off";
const PYTHON = process.env.LONGHU_PYTHON || (process.platform === "win32" ? "python" : "python3");
const MATCHER = path.join(__dirname, "slider_match.py");

function emit(value) { process.stdout.write(JSON.stringify(value) + "\n"); }
function error(code) { return Object.assign(new Error(code), { code }); }
function jsonLine(text) {
  for (const line of String(text || "").split(/\r?\n/).reverse()) {
    try { const value = JSON.parse(line); if (value && typeof value === "object") return value; } catch (_) {}
  }
  return null;
}

function dependencies() {
  if (Number(process.versions.node.split(".")[0]) < 20) throw error("dependency_missing");
  let chromium;
  try { ({ chromium } = require("playwright-core")); } catch (_) { throw error("dependency_missing"); }
  const candidates = [
    process.env.LONGHU_CHROMIUM_PATH,
    chromium.executablePath(),
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, "Google", "Chrome", "Application", "chrome.exe"),
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "Google", "Chrome", "Application", "chrome.exe"),
  ].filter(Boolean);
  const executablePath = candidates.find((candidate) => fs.existsSync(candidate));
  if (!executablePath) throw error("dependency_missing");
  if (MODE === "local") {
    const check = spawnSync(PYTHON, [MATCHER, "--check-deps"], {
      encoding: "utf8", timeout: 15000, maxBuffer: 16384, windowsHide: true,
    });
    if (check.status !== 0 || !jsonLine(check.stdout)?.ok) throw error("dependency_missing");
  }
  return { chromium, executablePath };
}

async function readInput() {
  let raw = "";
  for await (const chunk of process.stdin) {
    raw += chunk.toString("utf8");
    if (Buffer.byteLength(raw) > 12288) throw error("invalid_input");
  }
  let value;
  try { value = JSON.parse(raw); } catch (_) { throw error("invalid_input"); }
  if (!value || typeof value.device !== "string" || !value.device.trim() || value.device.length > 4096) {
    throw error("invalid_input");
  }
  if (value.user_agent != null && (typeof value.user_agent !== "string" || value.user_agent.length > 4096)) {
    throw error("invalid_input");
  }
  return value;
}

function runMatcher(payload, timeout, state) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [MATCHER], { windowsHide: true, stdio: ["pipe", "pipe", "ignore"] });
    state.matcher = child;
    let output = "", settled = false;
    const finish = (failure, result) => {
      if (settled) return;
      settled = true; clearTimeout(timer); state.matcher = null;
      failure ? reject(failure) : resolve(result);
    };
    const timer = setTimeout(() => { child.kill(); finish(error("timeout")); }, timeout);
    child.on("error", () => finish(error("dependency_missing")));
    child.stdout.on("data", (chunk) => {
      output += chunk.toString("utf8");
      if (output.length > 16384) { child.kill(); finish(error("unsupported_images")); }
    });
    child.on("close", (code) => {
      const result = jsonLine(output);
      if (code !== 0 || !result?.ok) return finish(error(result?.error || "unsupported_images"));
      if (!Number.isFinite(result.distance) || result.distance <= 0) return finish(error("invalid_geometry"));
      finish(null, result);
    });
    child.stdin.on("error", () => finish(error("unsupported_images")));
    child.stdin.end(JSON.stringify(payload));
  });
}

async function extractImages(page, images) {
  await page.waitForFunction(() => {
    const bg = document.querySelector(".dx_captcha_basic_bg img");
    const piece = document.querySelector(".dx_captcha_basic_sub-slider img");
    return bg?.complete && bg.naturalWidth > 0 && piece?.complete && piece.naturalWidth > 0;
  }, null, { timeout: 15000 }).catch(() => { throw error("unsupported_images"); });
  const info = await page.evaluate(() => {
    const bg = document.querySelector(".dx_captcha_basic_bg");
    const piece = document.querySelector(".dx_captcha_basic_sub-slider");
    const img = piece?.querySelector("img");
    const handle = document.querySelector(".dx_captcha_basic_slider");
    const bar = document.querySelector(".dx_captcha_basic_bar");
    if (!bg || !piece || !img || !handle || !bar) return null;
    const b = bg.getBoundingClientRect(), p = piece.getBoundingClientRect();
    const i = img.getBoundingClientRect(), h = handle.getBoundingClientRect(), t = bar.getBoundingClientRect();
    if ([b, p, i, h, t].some((rect) => rect.width <= 0 || rect.height <= 0)) return null;
    return {
      source: img.currentSrc || img.src,
      target_view: { width: p.width, height: p.height, image_width: i.width, image_height: i.height,
        offset_x: i.x - p.x, offset_y: i.y - p.y },
      geometry: { background_width: b.width, piece_left: p.x - b.x, piece_top: p.y - b.y,
        track_remaining: t.right - h.right },
      handle: { x: h.x + h.width / 2, y: h.y + h.height / 2 },
    };
  });
  if (!info) throw error("unsupported_images");
  let target;
  if (/^data:image\/[^;]+;base64,/.test(info.source)) {
    target = Buffer.from(info.source.split(",", 2)[1], "base64");
  } else {
    // Reuse the image response already loaded by SDK; no second request/fallback.
    target = await images.get(info.source);
  }
  if (!target || target.length === 0 || target.length > MAX_IMAGE) throw error("unsupported_images");
  const background = await page.locator(".dx_captcha_basic_bg").first().screenshot({
    type: "png", animations: "disabled", timeout: 10000,
  });
  const min = Number(process.env.LONGHU_CAPTCHA_MIN_CONFIDENCE || "0.75");
  return { info, payload: { target: target.toString("base64"), background: background.toString("base64"),
    target_view: info.target_view, geometry: info.geometry, min_confidence: min } };
}

async function main() {
  if (!["off", "manual", "local"].includes(MODE) || args.dist || args.device) throw error("invalid_input");
  if (MODE === "off") return emit({ ok: false, error: "off" });
  const deps = dependencies();
  if (args["check-deps"]) return emit({ ok: true, mode: MODE });
  const input = await readInput();
  const seconds = Number(args.timeout || 120);
  if (!Number.isInteger(seconds) || seconds < 10 || seconds > 180) throw error("invalid_input");
  const state = { browser: null, matcher: null, expired: false, token: null, verification: null,
    generation: 0, sid: null, submitted: false };
  const timer = setTimeout(() => {
    state.expired = true; state.matcher?.kill(); state.browser?.close().catch(() => {});
  }, seconds * 1000);
  const started = Date.now();
  try {
    const response = await fetch(COMP_URL, { signal: AbortSignal.timeout(Math.min(seconds * 1000, 20000)) });
    if (!response.ok) throw error("sdk_load_failed");
    const component = await response.text();
    try {
      state.browser = await deps.chromium.launch({ executablePath: deps.executablePath,
        headless: MODE !== "manual", timeout: 20000 });
    } catch (_) { throw error("browser_start_failed"); }
    const context = await state.browser.newContext({ viewport: { width: 480, height: 760 },
      deviceScaleFactor: 1, ...(input.user_agent ? { userAgent: input.user_agent } : {}) });
    await context.addInitScript(([key, value]) => localStorage.setItem(key, value),
      ["_dx_app_" + APP_ID, input.device]);
    const page = await context.newPage();
    await page.route(LOCAL_PAGE, (route) => route.fulfill({ status: 200, contentType: "text/html", body:
      `<!doctype html><html><body><div id="mount"></div><script src="${STA}/index.js"></script></body></html>` }));
    await page.route("**/basic-captcha-js.js*", (route) => route.fulfill({ status: 200,
      contentType: "application/javascript", body: component }));
    // One submission per process, including retries the SDK might otherwise issue.
    await page.route(`${API}/api/v1**`, (route) => {
      if (route.request().method() === "OPTIONS") return route.continue();
      if (state.submitted) return route.abort();
      state.submitted = true; return route.continue();
    });
    const images = new Map();
    page.on("response", (res) => {
      if (res.request().resourceType() === "image" && images.size < 64) {
        images.set(res.url(), res.body().then((body) => body.length <= MAX_IMAGE ? body : null).catch(() => null));
      }
      if (res.url().startsWith(`${API}/api/a?`)) {
        res.json().then((data) => { state.generation++; state.sid = data.sid || null; }).catch(() => {});
      } else if (res.url().startsWith(`${API}/api/v1`)) {
        res.json().then((data) => {
          state.verification = data;
          if (data.success && typeof data.token === "string") state.token = data.token;
        }).catch(() => {});
      }
    });
    await page.goto(LOCAL_PAGE, { waitUntil: "networkidle", timeout: 30000 });
    await page.evaluate(([appId, apiServer]) => {
      const success = (token) => { window.__localCaptchaToken = token; };
      new window._dx.Captcha(document.getElementById("mount"), {
        appId, apiServer, success, onSuccess: success, onClose: () => {}, onError: () => {},
      }).show();
    }, [APP_ID, API]);
    await page.locator(".dx_captcha_basic_slider").first().waitFor({ state: "visible", timeout: 15000 });
    if (MODE === "local") {
      const generation = state.generation, sid = state.sid;
      const { info, payload } = await extractImages(page, images);
      const remaining = Math.max(1, seconds * 1000 - (Date.now() - started));
      const match = await runMatcher(payload, Math.min(30000, remaining), state);
      if (state.generation !== generation || state.sid !== sid) throw error("challenge_changed");
      if (state.expired) throw error("timeout");
      await page.mouse.move(info.handle.x, info.handle.y);
      await page.mouse.down();
      // One continuous movement; no retry, distance guessing or challenge refresh.
      await page.mouse.move(info.handle.x + match.distance, info.handle.y, { steps: 24 });
      await page.mouse.up();
    }
    while (!state.expired && Date.now() - started < seconds * 1000) {
      const callbackToken = await page.evaluate(() => window.__localCaptchaToken || null);
      if (typeof callbackToken === "string" && callbackToken && !callbackToken.startsWith("sl")) state.token = callbackToken;
      if (state.token) return emit({ ok: true, token: state.token, mode: MODE });
      if (state.verification && !state.verification.success) {
        throw error(String(state.verification.code) === "4011" ? "high_risk" : "verification_rejected");
      }
      await page.waitForTimeout(250);
    }
    throw error("timeout");
  } catch (failure) {
    throw state.expired ? error("timeout") : failure;
  } finally {
    clearTimeout(timer); state.matcher?.kill();
    await state.browser?.close().catch(() => {});
  }
}

main().catch((failure) => {
  const allowed = new Set(["invalid_input", "dependency_missing", "sdk_load_failed", "browser_start_failed",
    "unsupported_images", "low_confidence", "invalid_geometry", "challenge_changed", "timeout",
    "verification_rejected", "high_risk"]);
  emit({ ok: false, error: allowed.has(failure.code) ? failure.code : "verification_failed" });
  process.exitCode = 1;
});
