import assert from 'node:assert/strict';
import { test } from 'node:test';
import { register, transport } from '../plugins/agent-efficiency/pi/core.mjs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('real Python transport and missing executable fail-open', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'ae-pi-'));
  const oldData = process.env.AGENT_EFFICIENCY_PI_DATA_DIR;
  const oldPython = process.env.AGENT_EFFICIENCY_PI_PYTHON;
  try {
    process.env.AGENT_EFFICIENCY_PI_DATA_DIR = dir;
    const reply = await transport({session_id:'pi:transport',cwd:dir,hook_event_name:'SessionStart'});
    assert.equal(reply.ok, true);
    assert.equal(reply.mode, 'observe');
    process.env.AGENT_EFFICIENCY_PI_PYTHON = join(dir, 'missing-python');
    assert.equal((await transport({})).ok, false);
    assert.equal((await transport({oversize:'x'.repeat(65537)})).ok, false);
  } finally {
    if (oldData === undefined) delete process.env.AGENT_EFFICIENCY_PI_DATA_DIR;
    else process.env.AGENT_EFFICIENCY_PI_DATA_DIR = oldData;
    if (oldPython === undefined) delete process.env.AGENT_EFFICIENCY_PI_PYTHON;
    else process.env.AGENT_EFFICIENCY_PI_PYTHON = oldPython;
    await rm(dir, {recursive:true,force:true});
  }
});

function harness(transport = async () => ({ ok: true, mode: 'observe' })) {
  const handlers = {}, commands = {}, requests = [], notices = [], widgets = [];
  const pi = { on: (name, fn) => { handlers[name] = fn; },
    registerCommand: (name, cmd) => { commands[name] = cmd; } };
  const ctx = { cwd: '/tmp/project', hasUI: true,
    sessionManager: { getSessionId: () => 'session' },
    ui: { setStatus() {}, setWidget: (key, lines) => widgets.push({key, lines}),
      notify: (msg) => notices.push(msg) } };
  register(pi, async (request) => { requests.push(request); return transport(request); });
  return { handlers, commands, requests, notices, widgets, ctx };
}

test('status remains visible in a persistent panel', async () => {
  const h = harness(async () => ({ok:true,mode:'observe',turns:2,tools:7,failures:1}));
  await h.commands.efficiency.handler('status', h.ctx);
  assert.equal(h.widgets.at(-1).key, 'agent-efficiency');
  assert.match(h.widgets.at(-1).lines.join('\n'), /tools 7/);
  assert.match(h.widgets.at(-1).lines.join('\n'), /failures 1/);
});

test('report shows hypothetical advice without model messages or blocking', async () => {
  const h = harness(async () => ({ok:true, mode:'observe', preview_count:1,
    previews:[{policy_id:'core.repeat-action',reason:'repeated_action',
      event:'PreToolUse',created_at:'2026-11-18',message:'Try a narrower check.'}]}));
  await h.commands.efficiency.handler('report', h.ctx);
  const panel = h.widgets.at(-1).lines.join('\n');
  assert.match(panel, /Would advise: core.repeat-action/);
  assert.match(panel, /Try a narrower check/);
  assert.match(panel, /not simulated/);
  assert.equal(h.requests.at(-1).action, 'status');
});

test('maps finalized errors once and does not forward content', async () => {
  const h = harness();
  await h.handlers.session_start({}, h.ctx);
  await h.handlers.before_agent_start({ prompt: 'SECRET_PROMPT' }, h.ctx);
  await h.handlers.tool_execution_start({toolCallId:'a', toolName:'edit',
    args:{path:'file.py', oldText:'SECRET_CODE', newText:'SECRET_NEW'}}, h.ctx);
  await h.handlers.tool_execution_end({toolCallId:'a', toolName:'edit',
    isError:true, result:{content:'SECRET_OUTPUT'}}, h.ctx);
  assert.equal(h.requests.at(-1).hook_event_name, 'PostToolUseFailure');
  assert.deepEqual(h.requests.at(-1).tool_input, {path:'file.py'});
  assert.ok(!JSON.stringify(h.requests).includes('SECRET'));
  assert.equal(h.requests.filter(x => x.hook_event_name === 'PreToolUse').length, 1);
});

test('observation failures never block tools', async () => {
  const h = harness(async () => { throw Error('SECRET_ERROR'); });
  assert.equal(await h.handlers.tool_execution_start({toolCallId:'x',toolName:'bash',args:{}}, h.ctx), undefined);
  assert.ok(!JSON.stringify(h.notices).includes('SECRET_ERROR'));
});

test('off prevents observation, observe restores it; guard is rejected', async () => {
  const h = harness(async req => ({ok:true,mode:req.action === 'off' ? 'off' : 'observe'}));
  await h.commands.efficiency.handler('off', h.ctx);
  const count = h.requests.length;
  await h.handlers.before_agent_start({}, h.ctx);
  assert.equal(h.requests.length, count);
  await h.commands.efficiency.handler('guard', h.ctx);
  assert.equal(h.requests.length, count);
  await h.commands.efficiency.handler('observe', h.ctx);
  await h.handlers.before_agent_start({}, h.ctx);
  assert.equal(h.requests.at(-1).hook_event_name, 'UserPromptSubmit');
});

test('parallel tools keep their own inputs and a common prompt id', async () => {
  const h = harness();
  await h.handlers.before_agent_start({}, h.ctx);
  await Promise.all(['a','b'].map(id => h.handlers.tool_execution_start({
    toolCallId:id,toolName:'read',args:{path:id}}, h.ctx)));
  await h.handlers.tool_execution_end({toolCallId:'b',toolName:'read',isError:false}, h.ctx);
  await h.handlers.tool_execution_end({toolCallId:'a',toolName:'read',isError:false}, h.ctx);
  assert.equal(h.requests.at(-2).tool_input.path, 'b');
  assert.equal(h.requests.at(-1).tool_input.path, 'a');
  assert.equal(h.requests.at(-2).turn_id, h.requests.at(-1).turn_id);
});
