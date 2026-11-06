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
    if (value === "ok") return state("已签到", "success");
    if (value === "already") return state("今日已签", "success");
    if (value === "skipped") return state(ctx.key === "app" ? "已关闭" : "已跳过", "neutral");
    if (flag(ctx.isBusy)) return state("执行中", "info", "账号正在处理中，等待本轮结果。");
    if (value === "busy") return state("执行中", "info", "已有任务进行中，稍后查看结果。");
    if (value === "retry") {
      const scheduled = flag(ctx.scheduleEnabled) && text(ctx.nextRun);
      return state("稍后重试", "info", scheduled
        ? "尚未提交签到；按下次排程重新检查。"
        : "尚未提交签到，可稍后核对状态。");
    }
    if (value === "partial") return state("部分完成", "warning");
    // Today's pending result may still carry a previous day's diagnostic text.
    if (value === "pending") return state("待执行", "neutral");
    if (expiredLogin(detail)) return state("登录失效", "danger", "", loginNotice(detail));
    // Missing device information is explained once at account level.
    if (disabled(ctx.hasDevice)) return state("待补充", "warning");
    if (value === "risk") return state("需要验证", "warning", "", notice(
      name + "需要验证", detail || "服务端要求验证当前请求。",
      "在官方客户端核对并完成必要验证，再只读核对；不要重复提交签到。",
      "check", "核对状态"));
    if (value === "conflict") return state("指纹冲突", "danger", "", notice(
      "指纹冲突", detail || "当前设备标识与其他账号记录重复。",
      "核对重复记录及本人真实采集的设备环境，修正后再核对状态。",
      "dx", "检查指纹", "danger"));
    if (value === "fail") return state("本次失败", "danger", "", notice(
      name + "本次失败", detail || "本轮未能完成，暂未取得明确原因。",
      "先只读核对当前状态，再按实际错误原因处理。",
      "check", "核对状态", "danger"));
    if (value === "todo" && /(?:设备|指纹|标识).{0,12}来源.{0,12}(?:无效|错误|不符|待确认|校正|更正)/.test(detail)) {
      return state("来源需校正", "warning", "", notice("设备来源需校正", detail,
        "核对标识的实际采集环境和来源，使用真实资料修正后再检查。", "dx", "检查指纹"));
    }
    if (value === "todo" && ctx.key === "app" && (/(?:手动签|需手动)/.test(detail)
        || (appMode(ctx) === "manual" && /未签/.test(detail)))) {
      return state("需手动签到", "warning", "", notice("App 需手动签到", detail || "当前 App 入口只提醒手动签到。",
        "在官方 App 完成今日签到，然后只读核对状态。", "check", "核对状态"));
    }
    return state("待确认", "warning", "", notice(name + "结果待确认", detail || "暂时无法确认当前签到结果。",
      "先只读核对官方状态；结果未知时不要重复提交签到。", "check", "核对状态"));
  }

  function deviceState(account) {
    const value = account || {};
    if (!flag(value.has_dx)) return { label: "待补充指纹", className: "device-missing" };
    if (flag(value.dx_conflict)) return { label: "指纹冲突", className: "device-conflict" };
    return { label: "指纹已有", className: "device-ok" };
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
    const entries = [{ key: "mini", status: value.mini_today_status, message: value.last_message }];
    if (appMode(ctx) !== "off") entries.push({ key: "app", status: value.app_today_status, message: value.app_last_message });
    const states = entries.map(entry => entryState(entry.status, entry.message,
      Object.assign({}, ctx, { key: entry.key, hasDevice, enabled: true })));
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

  return { entryState, accountAttention, deviceState };
});
