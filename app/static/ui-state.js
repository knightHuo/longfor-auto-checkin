/* Presentation only: these helpers never submit requests or change business state. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CheckinUI = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const classes = { success: "b-ok", neutral: "b-neutral", info: "b-info", warning: "b-warn", danger: "b-fail" };
  const text = value => typeof value === "string" ? value.trim() : "";
  const flag = value => value === true || value === 1 || value === "1";
  const disabled = value => value === false || value === 0 || value === "0";
  const appMode = context => text(context.appMode).toLowerCase() || "auto";
  const entryName = context => context.key === "app" ? "App" : "小程序";

  function shortTime(value) {
    const raw = text(value);
    const match = /(?:T|\s|^)(\d{2}:\d{2})/.exec(raw);
    return match ? match[1] : "";
  }

  function liveStatus(live, key) {
    if (!live || typeof live !== "object") return "";
    if (typeof live.status === "string") return text(live.status);
    return text(live[key]);
  }

  function liveSignedNote(previousStatus, previousMessage, liveCheck) {
    const checked = shortTime(liveCheck && liveCheck.checkedAt);
    const run = shortTime(liveCheck && liveCheck.lastRunAt);
    const head = checked ? ("官方 " + checked + " 核对已签") : "官方核对已签";
    const runPrefix = run ? (run + " ") : "";
    const detail = text(previousMessage);
    if (previousStatus === "risk") {
      if (/(?:已暂停提交|未继续提交)/.test(detail)) {
        return head + " · " + runPrefix + "因前序验证暂停提交";
      }
      return head + " · " + runPrefix + "自动提交遇到验证";
    }
    if (previousStatus === "fail") return head + " · " + runPrefix + "自动提交失败";
    if (previousStatus === "todo") return head + " · " + runPrefix + "自动记录待确认";
    return head;
  }

  function liveNotSignedNote(liveCheck, previousStatus) {
    const checked = shortTime(liveCheck && liveCheck.checkedAt);
    const head = checked ? ("官方 " + checked + " 核对未签") : "官方核对未签";
    if (previousStatus === "ok" || previousStatus === "already") {
      return head + " · 与上次记录不一致，请确认";
    }
    return head;
  }

  function state(label, tone, info, attention) {
    return { label, className: classes[tone], tone, info: info || "", attention: attention || null };
  }

  function notice(title, reason, next, action, actionLabel, tone) {
    return { title, reason, next, action, actionLabel, tone: tone || "warning" };
  }

  function expiredLogin(message) {
    // Suggestions to check login, or speculation about expiry, are not proof of expiry.
    const patterns = [
      /(?:登录(?:状态|会话|凭据|态)?|会话|凭据)\s*(?:已经|已)?(?:失效|过期|无效)/gi,
      /\btoken\s*(?:已经|已)?(?:失效|过期|无效)/gi,
      /鉴权失败|未登录|登录认证失败|需要重新登录|请重新登录|801001/g,
      /\b(?:token|session)\s+(?:has\s+)?(?:expired|invalid)\b/gi,
    ];
    return patterns.some(pattern => {
      let match;
      while ((match = pattern.exec(message))) {
        const prefix = message.slice(Math.max(0, match.index - 8), match.index);
        if (!/(?:可能|或许|疑似|也许|是否|检查|核对|\bif)\s*$/i.test(prefix)) return true;
      }
      return false;
    });
  }

  function loginNotice(message) {
    return notice("登录已失效", message || "已保存的登录凭据已失效。",
      "用本人有效凭据更新原账号登录，再核对签到状态。", "login", "更新登录", "danger");
  }

  function entryState(status, message, context) {
    const ctx = context || {};
    const name = entryName(ctx);
    const detail = text(message);
    const value = text(status) || "pending";
    if (disabled(ctx.enabled)) return state("已停用", "neutral");
    if (ctx.key === "app" && appMode(ctx) === "off") return state("已关闭", "neutral");
    const liveAuthExpired = Boolean(ctx.liveCheck && ctx.liveCheck.authExpired);
    if (liveAuthExpired) {
      return state("登录失效", "danger", "", loginNotice(
        text(ctx.liveCheck.authMessage) || "只读核对发现登录凭据已失效，请更新登录。"
      ));
    }
    const live = liveStatus(ctx.liveCheck, ctx.key);
    const notSignedInfo = live === "not_signed" ? liveNotSignedNote(ctx.liveCheck, value) : "";
    if (value === "ok" || value === "already") {
      if (live === "not_signed") {
        return state("待确认", "warning", notSignedInfo, notice(
          name + "核对未签", notSignedInfo,
          "请前往官方客户端确认今日实际签到状态，避免遗漏。",
          "check", "核对状态"));
      }
      return state(value === "ok" ? "已签到" : "今日已签", "success");
    }
    if (value === "skipped") {
      if (ctx.key === "app" && appMode(ctx) !== "off") {
        if (live === "signed") return state("今日已签", "success", liveSignedNote("pending", detail, ctx.liveCheck));
        return state("待执行", "neutral", notSignedInfo);
      }
      return state(ctx.key === "app" ? "已关闭" : "已跳过", "neutral");
    }
    if (flag(ctx.isBusy)) return state("执行中", "info", "账号正在处理中，等待本轮结果。");
    if (value === "busy") return state("执行中", "info", "已有任务进行中，稍后查看结果。");
    const hasExpiredLogin = value !== "pending" && expiredLogin(detail);
    if (hasExpiredLogin) return state("登录失效", "danger", "", loginNotice(detail));
    if (value !== "conflict" && live === "signed") {
      return state("今日已签", "success", liveSignedNote(value, detail, ctx.liveCheck));
    }
    if (value === "retry") {
      const scheduled = flag(ctx.scheduleEnabled) && text(ctx.nextRun);
      const base = scheduled
        ? "尚未提交签到；按下次排程重新检查。"
        : "尚未提交签到，可稍后核对状态。";
      return state("稍后重试", "info", notSignedInfo ? (notSignedInfo + " · " + base) : base);
    }
    if (value === "partial") return state("部分完成", "warning");
    // Today's pending result may still carry a previous day's diagnostic text.
    if (value === "pending") return state("待执行", "neutral", notSignedInfo);
    // Missing device information is explained once at account level.
    if (disabled(ctx.hasDevice)) return state("待补充", "warning", notSignedInfo);
    if (value === "risk") return state("需要验证", "warning", notSignedInfo, notice(
      name + "需要验证", detail || "服务端要求验证当前请求。",
      "在官方客户端核对并完成必要验证，再只读核对；不要重复提交签到。",
      "check", "核对状态"));
    if (value === "conflict") return state("指纹冲突", "danger", "", notice(
      "指纹冲突", detail || "当前设备标识与其他账号记录重复。",
      "核对重复记录及本人真实采集的设备环境，修正后再核对状态。",
      "dx", "检查指纹", "danger"));
    if (value === "fail") return state("本次失败", "danger", notSignedInfo, notice(
      name + "本次失败", detail || "本轮未能完成，暂未取得明确原因。",
      "先只读核对当前状态，再按实际错误原因处理。",
      "check", "核对状态", "danger"));
    if (value === "todo" && /(?:设备|指纹|标识).{0,12}来源.{0,12}(?:无效|错误|不符|待确认|校正|更正)/.test(detail)) {
      return state("来源需校正", "warning", notSignedInfo, notice("设备来源需校正", detail,
        "核对标识的实际采集环境和来源，使用真实资料修正后再检查。", "dx", "检查指纹"));
    }
    if (value === "todo" && ctx.key === "app" && (/(?:手动签|需手动)/.test(detail)
        || (appMode(ctx) === "manual" && /未签/.test(detail)))) {
      return state("需手动签到", "warning", notSignedInfo, notice("App 需手动签到", detail || "当前 App 入口只提醒手动签到。",
        "在官方 App 完成今日签到，然后只读核对状态。", "check", "核对状态"));
    }
    return state("待确认", "warning", notSignedInfo, notice(name + "结果待确认", detail || "暂时无法确认当前签到结果。",
      "先只读核对官方状态；结果未知时不要重复提交签到。", "check", "核对状态"));
  }

  function deviceState(account) {
    const value = account || {};
    if (!flag(value.has_dx)) return { label: "待补充指纹", className: "device-missing" };
    if (flag(value.dx_conflict)) return { label: "指纹冲突", className: "device-conflict" };
    return { label: "指纹已有", className: "device-ok" };
  }

  function entryLiveCheck(liveCheck, key, runAt) {
    if (!liveCheck || typeof liveCheck !== "object") return null;
    const authExpired = Boolean(liveCheck.authExpired);
    const authMessage = text(liveCheck.authMessage);
    const item = liveCheck[key];
    if (item && typeof item === "object") {
      return {
        status: text(item.status),
        checkedAt: item.checkedAt || liveCheck.checkedAt,
        lastRunAt: runAt,
        authExpired,
        authMessage,
      };
    }
    const status = liveStatus(liveCheck, key);
    if (!status && !authExpired) return null;
    return { status, checkedAt: liveCheck.checkedAt, lastRunAt: runAt, authExpired, authMessage };
  }

  function evaluateEntries(account, context) {
    const value = account || {};
    const ctx = context || {};
    const hasDevice = flag(value.has_dx);
    const enabled = !disabled(ctx.enabled === undefined ? value.enabled : ctx.enabled);
    const isBusy = flag(value.is_busy);
    const mode = appMode(ctx);
    const defs = [{
      key: "mini",
      status: text(value.mini_today_status) || "pending",
      message: value.last_message,
      runAt: value.last_run_at,
    }];
    if (mode !== "off") {
      defs.push({
        key: "app",
        status: text(value.app_today_status) || "pending",
        message: value.app_last_message,
        runAt: value.app_last_run_at,
      });
    }
    return defs.map(entry => {
      const live = entryLiveCheck(ctx.liveCheck, entry.key, entry.runAt);
      const display = entryState(entry.status, entry.message,
        Object.assign({}, ctx, { key: entry.key, hasDevice, enabled, isBusy, liveCheck: live }));
      return { key: entry.key, rawStatus: entry.status, display };
    });
  }

  function accountAttention(account, context) {
    const value = account || {};
    const ctx = context || {};
    const isDisabled = disabled(ctx.enabled === undefined ? value.enabled : ctx.enabled);
    if (isDisabled || flag(value.is_busy)) return [];
    const hasDevice = flag(value.has_dx);
    const conflict = hasDevice && flag(value.dx_conflict);
    const notices = [];
    const seen = new Map();
    const add = item => {
      if (!item) return;
      const reason = item.reason.replace(/^(?:APP(?:\s+|[:：]\s*)|小程序\s*)/i, "");
      const key = item.action + "\n" + reason + "\n" + item.next;
      if (!seen.has(key)) { seen.set(key, item); notices.push(item); }
      else {
        const existing = seen.get(key);
        const previousEntry = /^(小程序|App)/.exec(existing.title);
        const nextEntry = /^(小程序|App)/.exec(item.title);
        if (previousEntry && nextEntry && previousEntry[1] !== nextEntry[1]) {
          existing.title = "两个入口" + existing.title.slice(previousEntry[1].length);
        }
      }
    };
    if (!hasDevice) add(notice("缺少设备标识", "尚未保存本人设备标识，自动签到缺少必要资料。",
      "从本人登录环境正常采集标识，保留实际来源后保存。", "dx", "补充指纹"));
    if (conflict) {
      const names = Array.isArray(value.dx_shared_with) ? value.dx_shared_with.filter(name => typeof name === "string").join("、") : "";
      add(notice("指纹冲突", names ? "设备标识与「" + names + "」重复。" : "设备标识与其他账号记录重复。",
        "核对重复记录及本人真实采集的设备环境，修正后再核对状态。", "dx", "检查指纹", "danger"));
    }
    const evaluated = evaluateEntries(value, Object.assign({}, ctx, { enabled: true }));
    const states = evaluated.map(item => item.display);
    // A member session is shared by both entries: show one re-login action.
    const auth = states.find(item => item.attention && item.attention.action === "login");
    if (auth) add(auth.attention);
    for (const item of states) {
      if (!item.attention || item.attention.action === "login") continue;
      if (!hasDevice || conflict) continue;
      add(item.attention);
    }
    return notices;
  }

  function effectiveAccount(account, liveCheck, context) {
    const value = Object.assign({}, account || {});
    const ctx = Object.assign({}, context || {}, { liveCheck });
    const profile = liveCheck && liveCheck.profile && typeof liveCheck.profile === "object" ? liveCheck.profile : null;
    if (profile) {
      if (profile.growth_value !== undefined && profile.growth_value !== null) value.growth_value = profile.growth_value;
      if (typeof profile.level_name === "string" && profile.level_name) value.level_name = profile.level_name;
      if (profile.balance !== undefined && profile.balance !== null) value.balance = profile.balance;
    }
    const isDisabled = disabled(ctx.enabled === undefined ? value.enabled : ctx.enabled);
    if (isDisabled || flag(value.is_busy)) return value;
    const attention = accountAttention(value, ctx);
    const evaluated = evaluateEntries(value, ctx);
    const doneCount = evaluated.filter(item => item.display.tone === "success").length;
    if (attention.length === 0 && evaluated.length > 0 && doneCount === evaluated.length) {
      value.today_status = evaluated.some(item => item.rawStatus === "ok") ? "ok" : "already";
    } else if (doneCount > 0 && doneCount < evaluated.length) {
      value.today_status = "partial";
    } else if (attention.length > 0) {
      // Never report completed when account or entry has active attention notices.
      if (value.today_status === "ok" || value.today_status === "already") {
        value.today_status = "todo";
      }
    }
    return value;
  }

  function resolveLiveCheck(account, liveCheck, businessToday) {
    if (!liveCheck || typeof liveCheck !== "object") return null;
    const acc = account || {};
    const today = text(businessToday);
    const checkedMs = Date.parse(text(liveCheck.checkedAt));
    const isStaleAgainstRun = (refMs, runStamp) => {
      if (!Number.isFinite(refMs)) return false;
      const runMs = Date.parse(text(runStamp));
      if (!Number.isFinite(runMs)) return false;
      // Backend execution timestamps are persisted at second precision; treat any run
      // in the same second or later as superseding the readonly snapshot.
      const refFloorSec = Math.floor(refMs / 1000) * 1000;
      return runMs >= refFloorSec;
    };
    const resolveEntry = (entryKey, runStamp) => {
      const raw = liveCheck[entryKey];
      const entry = raw && typeof raw === "object"
        ? { status: text(raw.status), today: text(raw.today || liveCheck.today), checkedAt: text(raw.checkedAt || liveCheck.checkedAt) }
        : (typeof raw === "string" && text(raw)
          ? { status: text(raw), today: text(liveCheck.today), checkedAt: text(liveCheck.checkedAt) }
          : null);
      if (!entry || !entry.status) return null;
      if (entry.today && today && entry.today < today) return null;
      const entryCheckedMs = Date.parse(entry.checkedAt);
      const refMs = Number.isFinite(entryCheckedMs) ? entryCheckedMs : checkedMs;
      if (isStaleAgainstRun(refMs, runStamp)) return null;
      return entry;
    };
    const mini = resolveEntry("mini", acc.last_run_at);
    const app = resolveEntry("app", acc.app_last_run_at);
    const checkDay = text(liveCheck.today);
    const anyRunSupersedes = [acc.last_run_at, acc.app_last_run_at].some(stamp => isStaleAgainstRun(checkedMs, stamp));
    const authExpired = Boolean(liveCheck.authExpired)
      && (!checkDay || !today || checkDay >= today)
      && !anyRunSupersedes;
    if (!mini && !app && !authExpired) return null;
    const hasProfile = Boolean(liveCheck.profile && typeof liveCheck.profile === "object" && Object.keys(liveCheck.profile).length);
    const profileFresh = hasProfile && (!checkDay || !today || checkDay >= today) && !anyRunSupersedes;
    return {
      today: checkDay,
      checkedAt: text(liveCheck.checkedAt),
      mini,
      app,
      authExpired,
      authMessage: authExpired ? text(liveCheck.authMessage) : "",
      profile: profileFresh ? liveCheck.profile : {},
    };
  }

  return { entryState, accountAttention, deviceState, effectiveAccount, resolveLiveCheck };
});
