const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ui = require('../app/static/ui-state.js');

const account = extra => ({ has_dx: true, enabled: 1, mini_today_status: 'ok', app_today_status: 'already', ...extra });
const context = extra => ({ key: 'mini', hasDevice: true, appMode: 'auto', scheduleEnabled: true, nextRun: '2026-01-02 08:30', ...extra });

test('UMD exports the same three helpers in a browser without Node', () => {
  const sandbox = {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/static/ui-state.js'), 'utf8'), sandbox);
  assert.equal(typeof sandbox.CheckinUI.entryState, 'function');
  assert.equal(typeof sandbox.CheckinUI.accountAttention, 'function');
  assert.equal(typeof sandbox.CheckinUI.deviceState, 'function');
});

test('normal results are green, quiet, and free of redundant details', () => {
  for (const status of ['ok', 'already']) {
    const state = ui.entryState(status, '已按日历确认成功', context());
    assert.equal(state.className, 'b-ok');
    assert.equal(state.tone, 'success');
    assert.equal(state.info, '');
    assert.equal(state.attention, null);
  }
  assert.deepEqual(ui.accountAttention(account(), context()), []);
  assert.deepEqual(ui.deviceState(account()), { label: '指纹已有', className: 'device-ok' });
});

test('saved device plus risk asks for verification, never for another fingerprint', () => {
  const alerts = ui.accountAttention(account({ app_today_status: 'risk', app_last_message: '需要人机验证（code=8040012）' }), context());
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].tone, 'warning');
  assert.equal(alerts[0].action, 'check');
  assert.equal(alerts[0].reason, '需要人机验证（code=8040012）');
  assert.match(alerts[0].next, /官方客户端/);
  assert.doesNotMatch(alerts[0].title + alerts[0].next + alerts[0].actionLabel, /补|重新采集/);
  assert.equal(ui.deviceState(account({ dx_shared: true })).label, '指纹已有');
});

test('missing device produces one account action instead of one per entry', () => {
  const value = account({ has_dx: false, mini_today_status: 'todo', app_today_status: 'todo', last_message: '未配置设备标识', app_last_message: 'APP 未配置设备标识' });
  const alerts = ui.accountAttention(value, context());
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].action, 'dx');
  assert.equal(alerts[0].actionLabel, '补充指纹');
  const entry = ui.entryState('todo', value.last_message, context({ hasDevice: false }));
  assert.equal(entry.label, '待补充');
  assert.equal(entry.attention, null);
  assert.equal(ui.deviceState(value).className, 'device-missing');
});

test('missing device does not hide a second, definite expired-login problem', () => {
  const alerts = ui.accountAttention(account({ has_dx: false, mini_today_status: 'todo', app_today_status: 'todo', last_message: '鉴权失败：登录已失效', app_last_message: '登录已过期' }), context());
  assert.deepEqual(alerts.map(item => item.action), ['dx', 'login']);
});

test('busy and retry are informational rather than manual alarms', () => {
  for (const status of ['busy', 'retry']) {
    const state = ui.entryState(status, '暂时无法查询，请检查登录', context());
    assert.equal(state.className, 'b-info');
    assert.equal(state.attention, null);
    assert.ok(state.info);
  }
  assert.deepEqual(ui.accountAttention(account({ mini_today_status: 'retry', app_today_status: 'busy' }), context()), []);
  assert.match(ui.entryState('retry', '', context()).info, /下次排程/);
  assert.doesNotMatch(ui.entryState('retry', '', context({ scheduleEnabled: false })).info, /排程|自动重试|今天|分钟/);
});

test('pending and skipped entries remain quiet neutral states', () => {
  for (const status of ['pending', 'skipped']) {
    const state = ui.entryState(status, '', context());
    assert.equal(state.className, 'b-neutral');
    assert.equal(state.attention, null);
  }
});

test('a suggestion to check login or speculation is not treated as definite expiry', () => {
  for (const message of ['请检查登录', '查询失败，持续失败请检查登录', 'token 可能已失效', '可能登录已过期']) {
    const state = ui.entryState('todo', message, context());
    assert.notEqual(state.attention?.action, 'login');
  }
});

test('explicit expired sessions are combined into one re-login action', () => {
  for (const message of ['登录已失效', 'token 已过期', '鉴权失败：请重新导入', '未登录（801001）']) {
    assert.equal(ui.entryState('todo', message, context()).attention.action, 'login');
  }
  const alerts = ui.accountAttention(account({ mini_today_status: 'todo', app_today_status: 'todo', last_message: '登录已失效', app_last_message: 'APP 登录已失效' }), context());
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].actionLabel, '更新登录');
});

test('a new day does not reuse old diagnostic messages as a current login error', () => {
  const value = account({ mini_today_status: 'pending', app_today_status: 'pending', last_message: '登录已过期', app_last_message: '需要重新登录' });
  assert.deepEqual(ui.accountAttention(value, context()), []);
  const result = ui.entryState('pending', value.last_message, context());
  assert.equal(result.label, '待执行');
  assert.equal(result.className, 'b-neutral');
  assert.equal(result.attention, null);
});

test('an active task stays informational until the task lock is released', () => {
  const value = account({ is_busy: true, mini_today_status: 'todo', last_message: '签到请求已进入提交阶段，结果待确认' });
  assert.deepEqual(ui.accountAttention(value, context()), []);
  const active = ui.entryState('todo', value.last_message, context({ isBusy: true }));
  assert.equal(active.className, 'b-info');
  assert.equal(active.attention, null);
  assert.equal(active.label, '执行中');
  assert.equal(ui.entryState('already', '', context({ isBusy: true })).label, '今日已签');
  assert.equal(ui.accountAttention({ ...value, is_busy: false }, context()).length, 1);
  assert.equal(ui.entryState('todo', value.last_message, context()).attention.action, 'check');
});

test('unknown submission results call for readonly confirmation and no resubmission', () => {
  const state = ui.entryState('todo', '签到请求结果未知', context());
  assert.equal(state.attention.action, 'check');
  assert.match(state.attention.next, /只读/);
  assert.match(state.attention.next, /不要重复提交/);
});

test('manual App mode asks for a manual sign-in while off mode stays quiet', () => {
  const value = account({ app_today_status: 'todo', app_last_message: 'APP 未签到，需在 App 内手动签' });
  const manual = ui.accountAttention(value, context({ appMode: 'manual' }));
  assert.equal(manual.length, 1);
  assert.equal(manual[0].title, 'App 需手动签到');
  assert.deepEqual(ui.accountAttention(value, context({ appMode: 'off' })), []);
  const off = ui.entryState('risk', '需要验证', context({ key: 'app', appMode: 'off' }));
  assert.equal(off.label, '已关闭');
  assert.equal(off.attention, null);
});

test('partial account completion points only at the remaining entry', () => {
  const alerts = ui.accountAttention(account({ today_status: 'partial', app_today_status: 'fail', app_last_message: 'App 临时服务故障' }), context());
  assert.equal(alerts.length, 1);
  assert.match(alerts[0].title, /^App/);
  assert.equal(alerts[0].action, 'check');
  assert.equal(alerts[0].reason, 'App 临时服务故障');
  assert.equal(ui.entryState('partial', '', context()).label, '部分完成');
});

test('failed requests do not claim login expiry without evidence', () => {
  const state = ui.entryState('fail', '网络服务暂时不可用', context());
  assert.equal(state.className, 'b-fail');
  assert.equal(state.attention.action, 'check');
  assert.equal(state.attention.reason, '网络服务暂时不可用');
});

test('device conflicts are one existing-device correction rather than two missing-device prompts', () => {
  const value = account({ dx_conflict: true, dx_shared_with: ['虚构账号'], mini_today_status: 'conflict', app_today_status: 'conflict' });
  const alerts = ui.accountAttention(value, context());
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].actionLabel, '检查指纹');
  assert.match(alerts[0].reason, /虚构账号/);
  assert.equal(ui.deviceState(value).label, '指纹冲突');
});

test('a wrong source requires correcting actual device metadata', () => {
  const state = ui.entryState('todo', '设备标识来源无效，仅支持实际采集来源', context());
  assert.equal(state.attention.action, 'dx');
  assert.equal(state.attention.actionLabel, '检查指纹');
});

test('identical verification notices are deduplicated across both entries', () => {
  const alerts = ui.accountAttention(account({ mini_today_status: 'risk', app_today_status: 'risk', last_message: '需要验证', app_last_message: '需要验证' }), context());
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].title, '两个入口需要验证');
  const prefixed = ui.accountAttention(account({ mini_today_status: 'risk', app_today_status: 'risk', last_message: '签到失败：需要验证', app_last_message: 'APP 签到失败：需要验证' }), context());
  assert.equal(prefixed.length, 1);
  assert.equal(prefixed[0].title, '两个入口需要验证');
});

test('disabled accounts stay quiet even when credentials or device data need attention', () => {
  const value = account({ enabled: 0, has_dx: false, mini_today_status: 'todo', last_message: '登录已过期' });
  assert.deepEqual(ui.accountAttention(value, context()), []);
  assert.deepEqual(ui.accountAttention(account({ has_dx: false }), context({ enabled: false })), []);
  assert.equal(ui.entryState('risk', '需要验证', context({ enabled: false })).attention, null);
});
