import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

// Work content travels only over stdin, never argv, disk, or stdout.
export function transport(payload) {
  return new Promise((resolve) => {
    const input = JSON.stringify(payload);
    if (Buffer.byteLength(input) > 65536) return resolve({ ok: false });
    const child = spawn(process.env.AGENT_EFFICIENCY_PI_PYTHON || 'python3',
      [fileURLToPath(new URL('./bridge.py', import.meta.url))],
      { stdio: ['pipe', 'pipe', 'ignore'] });
    let output = '', finished = false;
    const finish = (value) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      finish({ ok: false });
    }, 1500);
    child.on('error', () => finish({ ok: false }));
    child.stdin.on('error', () => finish({ ok: false }));
    child.stdout.on('data', (data) => {
      output += data;
      if (output.length > 8192) {
        child.kill('SIGKILL');
        finish({ ok: false });
      }
    });
    child.on('close', (code) => {
      try { finish(code === 0 ? JSON.parse(output) : { ok: false }); }
      catch { finish({ ok: false }); }
    });
    child.stdin.end(input);
  });
}

export function register(pi, send = transport) {
  let mode = 'observe', turn = randomUUID(), warned = false;
  let queue = Promise.resolve();
  const calls = new Map();
  const show = (ctx, text) => { if (ctx.hasUI) ctx.ui.notify(text, 'info'); };
  const request = (ctx, fields) => {
    // Capture identity before queued work, so a session switch cannot relabel it.
    const payload = { session_id: `pi:${ctx.sessionManager.getSessionId()}`,
      cwd: ctx.cwd, turn_id: turn, ...fields };
    const work = queue.then(async () => {
      try {
        const reply = await send(payload);
        if (!reply?.ok || !['observe', 'off'].includes(reply.mode)) throw Error();
        mode = reply.mode;
        if (ctx.hasUI) ctx.ui.setStatus('agent-efficiency',
          `efficiency: ${mode} | ${reply.preview_count ?? 0} advice previews`);
        return reply;
      } catch {
        if (ctx.hasUI) ctx.ui.setStatus('agent-efficiency', 'efficiency: unavailable');
        if (!warned) show(ctx, 'Agent Efficiency unavailable; monitoring failed open.');
        warned = true;
        return undefined;
      }
    });
    queue = work.then(() => undefined, () => undefined);
    return work;
  };
  const event = async (ctx, name, fields = {}) => {
    if (mode !== 'off') await request(ctx, { hook_event_name: name, ...fields });
  };
  pi.on('session_start', async (_ev, ctx) => {
    calls.clear();
    await request(ctx, { hook_event_name: 'SessionStart' });
  });
  pi.on('before_agent_start', async (_ev, ctx) => {
    turn = randomUUID();
    await event(ctx, 'UserPromptSubmit');
  });
  pi.on('tool_execution_start', async (ev, ctx) => {
    if (mode === 'off') return;
    // No source, prompts, tool output, images, or arbitrary custom arguments.
    const input = {};
    if (typeof ev.args?.path === 'string') input.path = ev.args.path;
    if (ev.toolName === 'bash' && typeof ev.args?.command === 'string') {
      input.command = ev.args.command;
    }
    const fields = { tool_name: ev.toolName, tool_use_id: ev.toolCallId,
      tool_input: input, turn_id: turn };
    calls.set(ev.toolCallId, fields);
    await event(ctx, 'PreToolUse', fields);
  });
  pi.on('tool_execution_end', async (ev, ctx) => {
    const fields = calls.get(ev.toolCallId);
    calls.delete(ev.toolCallId);
    if (!fields) return;
    await event(ctx, ev.isError ? 'PostToolUseFailure' : 'PostToolUse', fields);
  });
  pi.on('session_before_compact', async (_ev, ctx) => { await event(ctx, 'PreCompact'); });
  pi.on('session_compact', async (_ev, ctx) => { await event(ctx, 'PostCompact'); });
  pi.on('agent_settled', async (ev, ctx) => {
    if (!ev.aborted) await event(ctx, 'Stop');
  });
  pi.on('session_shutdown', async (_ev, ctx) => {
    await event(ctx, 'SessionEnd');
    calls.clear();
  });
  pi.registerCommand('efficiency', {
    description: 'Local dry-run monitoring: status | report | observe | off',
    handler: async (args, ctx) => {
      const action = args.trim() || 'status';
      if (!['status', 'report', 'observe', 'off'].includes(action)) {
        show(ctx, 'Usage: /efficiency status|report|observe|off. Advice delivery and guard are not supported yet.');
        return;
      }
      const reply = await request(ctx, { action: action === 'report' ? 'status' : action });
      const lines = reply ? [
        `Agent Efficiency: ${reply.mode} | turns ${reply.turns ?? 0} | tools ${reply.tools ?? 0} | failures ${reply.failures ?? 0}`,
        `Dry run: ${reply.preview_count ?? 0} advice candidates; none delivered. Guard enforcement is not simulated.`,
        ...(reply.previews ?? []).flatMap(preview => [
          `Would advise: ${preview.policy_id} (${preview.reason}; ${preview.event}; ${preview.created_at} UTC)`,
          `  ${preview.message}`,
        ]),
        'Latest five candidates; snapshot at command time. No historical replay. /efficiency report refreshes.',
      ] : ['Agent Efficiency unavailable; monitoring failed open.'];
      if (ctx.hasUI) ctx.ui.setWidget('agent-efficiency', lines);
    },
  });
}

export default function (pi) { register(pi); }
