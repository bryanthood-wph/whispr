import { expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const PANE = {
  component: 'Pane',
  requestId: 'whispr-graph',
  props: { title: 'whispr graph', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
} as const

// The plugin's options as a configured install has them (python and whispr_root are required).
const OPTIONS = { python: 'C:/whispr/python/python.exe', whispr_root: 'C:/whispr', config: '' }

const DANA = { id: 'e-dana', name: 'Dana Reyes', type: 'person' }
const ATLAS = { id: 'e-atlas', name: 'Atlas', type: 'project' }
const SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>Dana</title></svg>'

function neighbourhood(center: typeof DANA, other: typeof ATLAS, nextOffset: number | null = null) {
  return {
    center, hops: 2, reached: 1, offset: 0, next_offset: nextOffset, truncated: nextOffset !== null,
    nodes: [{ ...center, depth: 0, route_only: false }, { ...other, depth: 1, route_only: false }],
    svg: SVG, alt: `${center.name} (${center.type}) and 1 neighbor within 2 hops (${other.type} 1).`,
    text: `${center.name} (${center.type})\n  -works_on-> ${other.name} (${other.type})`,
  }
}

/** A fake `python -m kg.view`: answers by subcommand and entity, and records each argv (and its init). */
function fakeKgView(on: On, calls: string[][], replies: Record<string, { code: number; body: unknown }>,
                    inits: unknown[] = []) {
  on('process.run', async (_$, e) => {
    calls.push([...e.argv])
    inits.push(e.init)
    const [, , , , command, ref] = e.argv          // python -s -m kg.view <command> <ref>
    const reply = replies[`${command} ${ref}`] ?? replies[command ?? ''] ?? { code: 1, body: { error: 'unexpected call' } }
    return { value: { exitCode: reply.code, stdout: JSON.stringify(reply.body), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
}

test('search, re-centre, filter and Ask Claude, on every surface with input', { options: OPTIONS }, async ($, on) => {
  const calls: string[][] = []
  const inits: unknown[] = []
  const filled: string[] = []
  fakeKgView(on, calls, {
    search: { code: 0, body: { query: 'dana', results: [DANA] } },
    'neighborhood e-dana': { code: 0, body: neighbourhood(DANA, ATLAS, 1) },
    'neighborhood e-atlas': { code: 0, body: neighbourhood(ATLAS as typeof DANA, DANA as typeof ATLAS) },
  }, inits)
  on('prompt.fill', async (_$, e) => {
    filled.push(e.text)
    return { isFilled: true }
  })
  for (const surface of ['terminal', 'desktop'] as const) {
    calls.length = 0
    const ui = await $.ui.mount({ plugin: 'whispr', surface, ...PANE })
    await ui.input({ key: 'search', text: 'dana' })
    // The pinned interpreter, isolated from per-user site-packages, run in the whispr folder.
    expect(calls[0]?.slice(0, 6)).toEqual([OPTIONS.python, '-s', '-m', 'kg.view', 'search', 'dana'])
    expect(inits[0]).toMatchObject({ cwd: OPTIONS.whispr_root, env: { PYTHONNOUSERSITE: '1' } })
    await ui.press({ key: 'card-e-dana' })
    // The drawing: the SVG where the surface has one, the text tree on the terminal.
    if (surface === 'desktop') expect(await ui.find({ type: 'Svg' })).toBeDefined()
    else expect(await ui.find({ type: 'Text', text: /-works_on-> Atlas/ })).toBeDefined()
    expect(await ui.find({ key: 'more' })).toBeDefined()                        // next_offset given
    await ui.select({ key: 'hops', value: '1' })
    expect(calls.at(-1)).toContain('--hops')
    await ui.press({ key: 'n-e-atlas' })                                        // re-centre on a neighbour
    expect(calls.at(-1)?.[5]).toBe('e-atlas')
    expect(await ui.find({ type: 'Text', text: /^Atlas \(project\)$/ })).toBeDefined()
    await ui.press({ key: 'ask' })
    expect(filled.at(-1)).toContain('Atlas (project, entity e-atlas)')
    await ui.unmount()
  }
})

test('no database, an ambiguous name and a failed run each say so', { options: OPTIONS }, async ($, on) => {
  const calls: string[][] = []
  fakeKgView(on, calls, {
    'search nothing': { code: 3, body: { error: 'no database at C:/data/whispr.db' } },
    'search dana': { code: 0, body: { query: 'dana', results: [DANA] } },
    'neighborhood e-dana': { code: 5, body: { error: 'several entities are named Dana', candidates: [DANA, { ...DANA, id: 'e-dana2' }] } },
    'search broken': { code: 1, body: { error: 'kg.traverse.time_limit_ms reached' } },
  })
  const ui = await $.ui.mount({ plugin: 'whispr', surface: 'desktop', ...PANE })
  await ui.input({ key: 'search', text: 'nothing' })
  expect(await ui.find({ type: 'Text', text: /No graph yet/ })).toBeDefined()
  await ui.input({ key: 'search', text: 'dana' })
  await ui.press({ key: 'card-e-dana' })
  expect(await ui.find({ type: 'Text', text: /several entities are named Dana/ })).toBeDefined()
  expect(await ui.find({ key: 'card-e-dana2' })).toBeDefined()                // a candidate to pick
  await ui.input({ key: 'search', text: 'broken' })
  expect(await ui.find({ type: 'Text', text: /time_limit_ms reached/ })).toBeDefined()
  await ui.unmount()
})

test('the command opens the pane and draws the entity it names', { options: OPTIONS }, async ($, on) => {
  const calls: string[][] = []
  const opened: string[] = []
  fakeKgView(on, calls, { 'neighborhood Dana Reyes': { code: 0, body: neighbourhood(DANA, ATLAS) } })
  on('ui.open', async (_$, e) => {
    opened.push(e.id)
    return { value: { isPlaced: true as const } }
  })
  const { text } = await $.command.run({
    command: 'whispr-graph', args: 'Dana Reyes',
    origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 },
  })
  expect(opened).toEqual(['whispr-graph'])
  expect(text).toContain('Dana Reyes')
  const ui = await $.ui.mount({ plugin: 'whispr', surface: 'terminal', ...PANE })
  expect(await ui.find({ type: 'Text', text: /^Dana Reyes \(person\)$/ })).toBeDefined()
  await ui.unmount()
})

test('a config overlay is passed as --config', { options: { ...OPTIONS, config: 'C:/me/whispr.yaml' } }, async ($, on) => {
  const calls: string[][] = []
  fakeKgView(on, calls, { search: { code: 0, body: { query: 'dana', results: [DANA] } } })
  const ui = await $.ui.mount({ plugin: 'whispr', surface: 'desktop', ...PANE })
  await ui.input({ key: 'search', text: 'dana' })
  expect(calls[0]?.slice(-2)).toEqual(['--config', 'C:/me/whispr.yaml'])
  await ui.unmount()
})
