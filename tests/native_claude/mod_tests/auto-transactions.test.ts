import { expect, test } from 'claude-code/testing';
import { register } from '../hooks/register.ts';
import { options, start } from './auto-fixtures.ts';

const feedbackOptions = {...options, isaacExecutable:'/isaac/bin/isaac', isaacFeedbackDirectory:'/isaac/feedback'};

// The pinned native test host has no session.append implementation. Dispatch its
// real middleware and feedback form against controlled engine boundaries.
async function autoTransactionFlow() {
  const commands: Record<string, any[]> = {};
  const appendHandlers: any[] = [];
  const append = async (engine: any, event: any, next: any) => {
    const invoke = async (index: number, e: any): Promise<any> => index < appendHandlers.length
      ? appendHandlers[index](engine, e, (forwarded: any) => invoke(index + 1, forwarded)) : next(e);
    return invoke(0, event);
  };
  const startHandlers: any[] = [];
  let endHandler: any;
  const closeHandlers: Array<{id?: string; handler: any}> = [];
  let promptHandler: any;
  let turnHandler: any;
  let focusHandler: any;
  const renders: any[] = [];
  register(((event: string, ...args: any[]) => {
    const handler = args.at(-1);
    if (event === 'command.run') (commands[args[0].command] ??= []).push(handler);
    if (event === 'session.append' && args[0]?.door === 'command') appendHandlers.push(handler);
    if (event === 'session.start') startHandlers.push(handler);
    if (event === 'session.end' && !endHandler) endHandler = handler;
    if (event === 'ui.close') closeHandlers.push({id:args[0]?.id, handler});
    if (event === 'session.append' && args[0]?.door === 'prompt' && !promptHandler) promptHandler = handler;
    if (event === 'turn.step' && typeof args[0] === 'function') turnHandler = handler;
    if (event === 'ui.focus' && args[0]?.requestId === 'auto-main') focusHandler = handler;
    if (event === 'ui.render' && args[0]?.component === 'Pane' && !args[0]?.requestId) renders.push(handler);
  }) as any, feedbackOptions);
  const state = {
    model:'ug-auto-astra', calls:[] as any[], panes:[] as string[], logs:[] as string[], toasts:[] as string[],
    rejectManual:false, failOff:false, failPrepare:false, failOpen:false, deferConfirmations:false,
    throwModel:false, silentModel:false,
    controls:{} as Record<string,string>, reason:undefined as any, cancel:undefined as any,
    offWrite:undefined as undefined | (() => Promise<void>), nativeModels:[] as string[],
    deferModel:false, saved:undefined as unknown, transcripts:[] as string[], buttons:{} as Record<string,() => Promise<void>>, efforts:[] as string[],
  };
  const env: Record<string,string | undefined> = {
    ...options, ISAAC_SMART_ROUTER_FEEDBACK:'1', UCODE_SMART_ROUTER_PYTHON:'/ug/python',
    UCODE_SESSION_ENV_FILE:'/session/env.json',
  };
  const pending: Array<() => Promise<void>> = [];
  const engine: any = {
    session:{model:async () => state.model, id:async () => 'session-1'},
    store:{get:async () => state.saved, set:async (_key: string,value: unknown) => {state.saved = value;}},
    settings:{read:async () => ({})},
    env:{get:async (key: string) => env[key], set:async (key: string, value: string) => {env[key] = value;}},
    fs:{read:async () => JSON.stringify(state.controls), exists:async () => false},
    command:{register:async () => ({}), run:async (e: any) => {
      const handlers = commands[e.command] ?? [];
      const invoke = async (index: number, event: any): Promise<any> => {
        if (index < handlers.length) return handlers[index](engine, event, (nextEvent: any) => invoke(index + 1, nextEvent));
        if (event.command === 'model' && event.args) {
          state.nativeModels.push(event.args);
          if (state.throwModel) throw new Error('Native model command failed');
          if (state.silentModel) return {};
          if (state.deferModel) return {ref:1};
          if (!(state.rejectManual && event.args === 'opus')) state.model = event.args;
          const confirmation = async () => append(engine, {door:'command', origin:{kind:'plugin',name:'ug-smart-router'}, message:{content:[{type:'text',text:'<local-command-stdout>Set model to `model`</local-command-stdout>'}]}}, async () => ({}));
          if (state.deferConfirmations) pending.push(confirmation);
          else await confirmation();
        }
        if (event.command === 'effort') state.efforts.push(event.args);
        return {text:''};
      };
      return invoke(0, {...e,origin:{kind:'plugin',name:'ug-smart-router'}});
    }},
    process:{run:async (argv: string[], processOptions: any) => {
      state.calls.push({argv,...processOptions});
      if (argv.includes('--inspect-target')) return {exitCode:state.failPrepare ? 1 : 0,stdout:'{"message_id":"m1"}',stderr:''};
      const off = argv.includes('--disable-smart-routing');
      if (off && state.offWrite) await state.offWrite();
      const exitCode = off && state.failOff ? 1 : 0;
      if (!exitCode && (off || argv.includes('--enable-smart-routing'))) state.controls = {ENABLE_SMART_ROUTING_SUBAGENT_ONLY:off ? '0' : '1'};
      return {exitCode,stdout:'',stderr:exitCode ? 'controlled failure' : ''};
    }},
    ui:{
      invalidate:() => {}, open:async (e: any) => {if (state.failOpen) throw new Error('Pane unavailable'); state.panes.push(e.id); return {isPlaced:true};},
      focus:async (e: any) => focusHandler(engine, {element:e.key}, async () => ({})),
      close:async (e: any) => {
        for (const {id, handler} of closeHandlers) {
          if (!id || id === e.id) await handler(engine, {...e,origin:{kind:'plugin'}}, async () => ({}));
        }
        return {};
      },
      toast:(text: string) => {state.toasts.push(text);}, log:(text: string) => {state.logs.push(text);},
      resolve:() => ({Box:(props: any) => props, Text:(props: any) => props,
        Input:(props: any) => {state.reason = props.onSubmit; return props;},
        Button:(props: any) => {if (props.key === 'cancel') state.cancel = props.onPress; state.buttons[props.key] = props.onPress; return props;}}),
    },
    clock:{after:(_delay: number, callback: () => Promise<void>) => {
      let cancelled = false;
      pending.push(async () => {if (!cancelled) await callback();});
      return {cancel:() => {cancelled = true;}};
    }},
  };
  const flush = async () => {while (pending.length) await pending.shift()!();};
  const select = async (model: string) => {
    await engine.command.run({command:'model',args:''});
    state.model = model;
    await append(engine, {door:'command',origin:{kind:'composer'},message:{content:[{type:'text',text:'<local-command-stdout>Set model to `model`</local-command-stdout>'}]}}, async () => ({}));
    await flush();
  };
  const render = async () => {
    for (const handler of renders) await handler(engine, {requestId:'auto-main',surface:'terminal'}, async () => ({}));
  };
  for (const handler of startHandlers) await handler(engine, start, async () => ({}));
  const reconcile = async () => {
    const stream = turnHandler(engine, {}, async function* () {return {};});
    while (!(await stream.next()).done) {}
  };
  const finishModel = async (text: string, origin: {kind:string;name?:string} = {kind:'plugin',name:'ug-smart-router'}, agentId?: string) => {
    await append(engine, {door:'command',origin,agentId,message:{content:[{
      type:'text',text:text.startsWith('<command-name>') ? text : `<local-command-stdout>${text}</local-command-stdout>`,
    }]}}, async e => {state.transcripts.push(JSON.stringify(e.message));return {};});
    await flush();
  };
  const confirm = async (accepted: boolean) => {
    if (accepted) state.model = state.nativeModels.at(-1)!;
    await finishModel(`${accepted ? 'Set model to' : 'Kept model as'} \`model\``,
      accepted ? {kind:'plugin',name:'ug-smart-router'} : {kind:'engine'});
  };
  return {state,env,select,render,run:engine.command.run,end:async () => endHandler(engine, {}, async () => ({})),
    reconcile, flush, confirm, finishModel, focus:async (index: number) => engine.ui.focus({key:'auto-main-model-' + index}),
    prompt:async () => promptHandler(engine, {message:{}}, async () => ({})),
    offCalls:() => state.calls.filter(call => call.argv.includes('--disable-smart-routing'))};
}

for (const accepted of [true,false]) {
  test(`a deferred native Auto save ${accepted ? 'commits only after acceptance' : 'cancels without preference or effort writes'}`, async () => {
    const flow = await autoTransactionFlow();
    await flow.run({command:'auto',args:''});
    await flow.focus(0);
    await flow.render();
    await flow.state.buttons['auto-effort-more']();
    await flow.render();
    flow.state.deferModel = true;
    const originalPointer = flow.env.ANTHROPIC_CUSTOM_MODEL_OPTION;
    await flow.state.buttons['auto-main-model-0']();
    await flow.state.buttons['auto-main-model-0']();
    await flow.flush();
    expect(flow.state.nativeModels).toEqual(['ug-auto-opus']);
    expect(flow.state.saved).toBeUndefined();
    expect(flow.state.efforts.filter(args => args !== 'status')).toEqual([]);
    expect(flow.env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe('ug-auto-opus');
    expect(flow.state.toasts).toEqual([]);
    await flow.confirm(accepted);
    expect(flow.state.model).toBe(accepted ? 'ug-auto-opus' : 'ug-auto-astra');
    expect(flow.state.saved).toBe(accepted ? 'opus' : undefined);
    expect(flow.env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe(accepted ? 'ug-auto-opus' : originalPointer);
    expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    expect(flow.state.efforts.filter(args => args !== 'status')).toEqual(accepted ? ['low'] : []);
    expect(flow.state.toasts).toEqual(accepted ? ['Auto (Opus 5.5) saved'] : []);
  });
}

test('a thrown internal model command does not swallow the next model confirmation', async () => {
  const flow = await autoTransactionFlow();
  await flow.run({command:'auto',args:''});
  await flow.render();
  flow.state.throwModel = true;
  await flow.state.buttons['auto-main-model-0']();
  await flow.flush();
  expect(flow.state.saved).toBeUndefined();
  flow.state.throwModel = false;
  const panes = flow.state.panes.length;
  flow.state.model = 'ug-auto-opus';
  await flow.finishModel('Set model to `Opus`');
  expect(flow.state.panes.length).toBeGreaterThan(panes);
  expect(flow.state.model).toBe('ug-auto-opus');
});

for (const accepted of [true,false]) {
  test(`an empty native model return waits for later ${accepted ? 'acceptance' : 'cancellation'}`, async () => {
    const flow = await autoTransactionFlow();
    await flow.run({command:'auto',args:''});
    await flow.render();
    flow.state.silentModel = true;
    await flow.state.buttons['auto-main-model-0']();
    await flow.flush();
    expect(flow.state.saved).toBeUndefined();
    expect(flow.state.toasts).toEqual([]);
    if (accepted) flow.state.model = 'ug-auto-opus';
    await flow.finishModel(`${accepted ? 'Set model to' : 'Kept model as'} \`model\``,
      accepted ? {kind:'plugin',name:'ug-smart-router'} : {kind:'engine'});
    expect(flow.state.saved).toBe(accepted ? 'opus' : undefined);
    expect(flow.state.model).toBe(accepted ? 'ug-auto-opus' : 'ug-auto-astra');
    expect(flow.state.efforts.filter(args => args !== 'status')).toEqual([]);
  });
}

test('only the pending main Auto command relabels its engine-origin echo', async () => {
  const flow = await autoTransactionFlow();
  await flow.run({command:'auto',args:''});
  await flow.render();
  flow.state.deferModel = true;
  await flow.state.buttons['auto-main-model-0']();
  await flow.flush();
  const echo = (model: string) => `<command-name>/model</command-name><command-message>model</command-message><command-args>${model}</command-args>`;
  await flow.finishModel(echo('ug-auto-opus'), {kind:'engine'}, 'child');
  expect(flow.state.transcripts.at(-1)).toContain('ug-auto-opus');
  await flow.finishModel(echo('ug-auto-fable'), {kind:'engine'});
  expect(flow.state.transcripts.at(-1)).toContain('ug-auto-fable');
  await flow.finishModel(echo('ug-auto-opus'), {kind:'engine'});
  expect(flow.state.transcripts.at(-1)).toContain('<command-args>Auto</command-args>');
  expect(flow.state.transcripts.at(-1)).not.toContain('ug-auto-');
  await flow.confirm(false);
});

test('cancelling a deferred same-main confirmation does not save its draft effort', async () => {
  const flow = await autoTransactionFlow();
  await flow.run({command:'auto',args:''});
  await flow.focus(3);
  await flow.render();
  await flow.state.buttons['auto-effort-more']();
  await flow.render();
  flow.state.deferModel = true;
  await flow.state.buttons['auto-main-model-3']();
  await flow.flush();
  expect(flow.state.nativeModels).toEqual(['ug-auto-astra']);
  expect(flow.state.saved).toBeUndefined();
  expect(flow.state.toasts).toEqual([]);
  await flow.confirm(false);
  expect(flow.state.model).toBe('ug-auto-astra');
  expect(flow.state.saved).toBeUndefined();
  expect(flow.state.efforts.filter(args => args !== 'status')).toEqual([]);
  expect(flow.state.toasts).toEqual([]);
});

for (const output of ['Model switch cancelled',
  'Model switch to Opus 5.5 was blocked by a PreModelSwitch hook: Policy',
  'The model changed while you were confirming; pick again']) {
  test(`a deferred ${output} restores Auto and allows a new selection`, async () => {
    const flow = await autoTransactionFlow();
    await flow.run({command:'auto',args:''});
    await flow.focus(0);
    await flow.render();
    flow.state.deferModel = true;
    await flow.state.buttons['auto-main-model-0']();
    await flow.flush();
    const current = output.startsWith('The model changed') ? 'ug-auto-fable' : 'ug-auto-astra';
    flow.state.model = current;
    await flow.finishModel(output, {kind:'engine'});
    expect(flow.state.model).toBe(current);
    expect(flow.env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe(current);
    expect(flow.state.saved).toBeUndefined();
    expect(flow.state.efforts.filter(args => args !== 'status')).toEqual([]);
    expect(flow.state.toasts).toEqual([]);
    flow.state.deferModel = false;
    await flow.state.buttons['auto-main-model-0']();
    await flow.flush();
    expect(flow.state.model).toBe('ug-auto-opus');
    expect(flow.state.saved).toBe('opus');
  });
}

for (const actor of [{origin:{kind:'plugin',name:'another-plugin'},agentId:undefined},
  {origin:{kind:'engine'},agentId:'child'}]) {
  test(`a cancellation from ${actor.agentId ?? actor.origin.name} cannot settle the main Auto save`, async () => {
    const flow = await autoTransactionFlow();
    await flow.run({command:'auto',args:''});
    await flow.render();
    flow.state.deferModel = true;
    await flow.state.buttons['auto-main-model-0']();
    await flow.flush();
    await flow.finishModel('Model switch cancelled', actor.origin, actor.agentId);
    expect(flow.env.ANTHROPIC_CUSTOM_MODEL_OPTION).toBe('ug-auto-opus');
    expect(flow.state.saved).toBeUndefined();
    await flow.confirm(true);
    expect(flow.state.saved).toBe('opus');
  });
}

for (const newer of ['prompt','session end','Auto']) {
  test(`a pending Auto save cannot write after ${newer}`, async () => {
    const flow = await autoTransactionFlow();
    await flow.run({command:'auto',args:''});
    await flow.render();
    flow.state.deferModel = true;
    await flow.state.buttons['auto-main-model-0']();
    await flow.flush();
    if (newer === 'prompt') await flow.prompt();
    else if (newer === 'session end') await flow.end();
    else await flow.run({command:'auto',args:''});
    await flow.confirm(true);
    expect(flow.state.saved).toBeUndefined();
    expect(flow.state.efforts.filter(args => args !== 'status')).toEqual([]);
    expect(flow.state.toasts).toEqual([]);
  });
}

test('feedback-gated native manual selection restores the exact Auto until a valid reason commits', async () => {
  const flow = await autoTransactionFlow();
  await flow.select('opus');
  expect(flow.state.model).toBe('ug-auto-astra');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(flow.state.panes).toEqual(['auto-main']);
  expect(flow.offCalls()).toEqual([]);
  await flow.render();
  await flow.state.reason('too short');
  expect(flow.state.toasts.at(-1)).toContain('at least 16');
  expect(flow.offCalls()).toEqual([]);
  const reason = 'The routed model was too slow';
  await flow.state.reason(reason);
  expect(flow.offCalls().map(call => call.argv)).toEqual([
    ['/ug/python','-m','ucode.cli','claude','--disable-smart-routing','--smart-router-feedback',reason],
  ]);
  expect(flow.state.model).toBe('opus');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(flow.state.controls.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(flow.state.logs).toContain('Smart Router is off');
  await flow.select('ug-auto-fable');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(flow.state.controls.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

test('cancelling feedback leaves the restored Auto and routing on', async () => {
  const flow = await autoTransactionFlow();
  await flow.select('opus');
  await flow.render();
  await flow.state.cancel();
  expect(flow.state.model).toBe('ug-auto-astra');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  expect(flow.offCalls()).toEqual([]);
  flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY = '0';
  await flow.reconcile();
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

for (const failure of ['failOff','rejectManual','failPrepare','failOpen'] as const) {
  test(`feedback manual switch preserves Auto ON after ${failure}`, async () => {
    const flow = await autoTransactionFlow();
    flow.state[failure] = true;
    await flow.select('opus');
    if (failure !== 'failPrepare' && failure !== 'failOpen') {
      await flow.render();
      await flow.state.reason('The routed model was too slow');
      expect(flow.state.logs).not.toContain('Smart Router is off');
      expect(flow.state.logs.at(-1)).toContain('retry selecting a model in /model');
      expect(flow.state.logs.at(-1)).not.toContain('/smart-router off');
    } else {
      expect(flow.state.panes).toEqual([]);
      expect(flow.offCalls()).toEqual([]);
      expect(flow.state.toasts.at(-1)).toContain('Could not open the feedback form');
      expect(flow.state.toasts.at(-1)).toContain('retry selecting a model in /model');
      expect(flow.state.toasts.at(-1)).not.toContain('/smart-router off');
    }
    expect(flow.state.model).toBe('ug-auto-astra');
    expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    expect(flow.state.controls.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).not.toBe('0');
  });
}

for (const newer of ['Auto','manual','session end','prompt']) {
  test(`a stale feedback callback cannot disable routing after ${newer}`, async () => {
    const flow = await autoTransactionFlow();
    await flow.select('opus');
    await flow.render();
    const submission = flow.state.reason('The routed model was too slow');
    if (newer === 'session end') await flow.end();
    else if (newer === 'prompt') await flow.prompt();
    else await flow.select(newer === 'Auto' ? 'ug-auto-fable' : 'fable');
    const current = flow.state.model;
    await submission;
    expect(flow.state.model).toBe(current);
    expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    expect(flow.offCalls()).toEqual([]);
    expect(flow.state.logs).not.toContain('Smart Router is off');
  });
}

test('an Auto feedback request cannot admit a public off command or a bypass flag', async () => {
  const flow = await autoTransactionFlow();
  await flow.select('opus');
  for (const args of ['off','off --feedback-collected']) {
    expect((await flow.run({command:'smart-router',args})).text).toContain('Choose Auto in /model');
  }
  expect(flow.offCalls()).toEqual([]);
});

test('deferred native internal confirmations cannot reopen Auto or start another feedback flow', async () => {
  const flow = await autoTransactionFlow();
  flow.state.deferConfirmations = true;
  await flow.select('opus');
  expect(flow.state.model).toBe('ug-auto-astra');
  expect(flow.state.panes).toEqual(['auto-main']);
  await flow.render();
  await flow.state.reason('The routed model was too slow');
  await flow.flush();
  expect(flow.state.model).toBe('opus');
  expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  expect(flow.state.panes).toEqual(['auto-main']);
  expect(flow.offCalls()).toHaveLength(1);
});

for (const newer of ['prompt','session end','Auto']) {
  test(`a CLI write completed after ${newer} cannot commit or restore an obsolete model`, async () => {
    const flow = await autoTransactionFlow();
    await flow.select('opus');
    await flow.render();
    let release = () => {};
    let started = () => {};
    const writing = new Promise<void>(resolve => {started = resolve;});
    const gate = new Promise<void>(resolve => {release = resolve;});
    flow.state.offWrite = async () => {started(); await gate;};
    const submission = flow.state.reason('The routed model was too slow');
    await writing;
    if (newer === 'prompt') await flow.prompt();
    else if (newer === 'session end') await flow.end();
    else await flow.select('ug-auto-fable');
    const current = flow.state.model;
    const commands = flow.state.nativeModels.length;
    release();
    await submission;
    expect(flow.state.model).toBe(current);
    expect(flow.state.nativeModels).toHaveLength(commands);
    expect(flow.state.logs).not.toContain('Smart Router is off');
    if (newer !== 'session end') {
      expect(flow.env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
      expect(flow.state.controls.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
    }
  });
}
