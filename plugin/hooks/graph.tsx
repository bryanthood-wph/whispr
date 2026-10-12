import { atom, read, update } from 'claude-code'
import type { EngineInterface, PluginOptions, Register } from 'claude-code'

import type { GraphAnswer, GraphCard, GraphView } from '../types'

const PANE = 'whispr-graph'
const TITLE = 'whispr graph'
// kg.view's exit codes (its module docstring): the contract this pane reads.
const OK = 0
const NO_DATABASE = 3
const AMBIGUOUS = 5
// Select values that stand for "kg.view's own default": every type, kg.view.default_hops.
const EVERY_TYPE = '*'
const DEFAULT_HOPS = '0'
// Not a kg.view exit code: the plugin's python / whispr_root options are unset, or kg.view did not run.
const NOT_RUN = -1
// The pinned interpreter never reads a per-user site-packages (CLAUDE.md, "Embeddable
// Python isolation"): -s on the command line, and the variable for anything it starts.
const ISOLATION_ARGS = ['-s']
const ISOLATION_ENV = { PYTHONNOUSERSITE: '1' }

const view = atom({ plugin: 'whispr', key: 'view' } as const, {
  ref: null,
  hops: 0,
  type: '',
  offset: 0,
  answer: null,
  cards: [],
  cardsLabel: '',
  message: null,
  loading: false,
  request: 0,
} as GraphView)

type Reply = { code: number; body: Record<string, unknown> }

/** A text option's value, or '' when it is unset. */
function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/**
 * One `<python> -s -m kg.view ...` call in the whispr folder (the plugin's `python` and
 * `whispr_root` options); its one JSON object, or an error the pane can show.
 */
async function kgView($: EngineInterface, options: PluginOptions, args: string[]): Promise<Reply> {
  const python = text(options.python)
  const root = text(options.whispr_root)
  if (!python || !root) {
    return { code: NOT_RUN, body: { error: "whispr is not set up here: set the whispr plugin's Python and whispr folder options (/config), then run /whispr-setup." } }
  }
  const overlay = text(options.config)
  const argv = [python, ...ISOLATION_ARGS, '-m', 'kg.view', ...args, ...(overlay ? ['--config', overlay] : [])]
  try {
    const run = await $.process.run(argv, { cwd: root, env: ISOLATION_ENV, timeoutMs: Number(options.graph_timeout_s) * 1000 })
    try {
      return { code: run.exitCode, body: JSON.parse(run.stdout) }
    } catch {
      const why = run.stderr.trim() || `kg.view printed no JSON (exit ${run.exitCode})`
      return { code: NOT_RUN, body: { error: why } }
    }
  } catch (err) {
    return { code: NOT_RUN, body: { error: `could not run ${argv[0]}: ${err instanceof Error ? err.message : String(err)}` } }
  }
}

/** Start a request: the state it runs on, and its number (a later request wins). */
async function begin($: EngineInterface, patch: Partial<GraphView>): Promise<GraphView> {
  await update($, view, v => ({ ...v, ...patch, loading: true, message: null, request: v.request + 1 }))
  return read($, view)
}

/** Apply a request's outcome unless a newer request has started since. */
function finish($: EngineInterface, request: number, patch: Partial<GraphView>): Promise<unknown> {
  return update($, view, v => (v.request === request ? { ...v, ...patch, loading: false } : v))
}

function failure(code: number, body: Record<string, unknown>): Partial<GraphView> {
  const error = String(body.error ?? 'unknown error')
  if (code === NO_DATABASE) {
    return { message: `No graph yet: it is built as calls are processed (python -m pipeline run). ${error}` }
  }
  if (code === AMBIGUOUS) {
    return { message: error, cards: (body.candidates as GraphCard[]) ?? [], cardsLabel: 'Several entities have that name:' }
  }
  return { message: error }
}

/** Draw the neighbourhood of `patch.ref` (or the current one) with the current filters. */
async function show($: EngineInterface, options: PluginOptions, patch: Partial<GraphView>): Promise<void> {
  const v = await begin($, patch)
  if (v.ref === null) {
    await finish($, v.request, {})
    return
  }
  const args = ['neighborhood', v.ref]
  if (v.hops > 0) args.push('--hops', String(v.hops))
  if (v.type) args.push('--types', v.type)
  if (v.offset > 0) args.push('--offset', String(v.offset))
  const { code, body } = await kgView($, options, args)
  await finish($, v.request, code === OK
    ? { answer: body as unknown as GraphAnswer, cards: [], cardsLabel: '' }
    : failure(code, body))
}

async function search($: EngineInterface, options: PluginOptions, text: string): Promise<void> {
  const words = text.trim().split(/\s+/).filter(Boolean)
  if (words.length === 0) return
  const v = await begin($, {})
  const { code, body } = await kgView($, options, ['search', ...words])
  const results = (body.results as GraphCard[]) ?? []
  await finish($, v.request, code === OK
    ? { cards: results, cardsLabel: results.length ? `Matches for "${text.trim()}":` : `Nothing matches "${text.trim()}".` }
    : failure(code, body))
}

/** Put a question about `center` in the prompt box for the person to send (or edit). */
async function ask($: EngineInterface, center: GraphCard): Promise<void> {
  let filled = false
  try {
    filled = (await $.prompt.fill({ text: askText(center) })).isFilled
  } catch {
    // reported below, like a refused fill
  }
  if (!filled) await update($, view, v => ({ ...v, message: 'Could not put the question in the prompt box.' }))
}

function askText(center: GraphCard): string {
  return `Using the whispr-kg graph tools, tell me what I should know about ${center.name} ` +
    `(${center.type}, entity ${center.id}): my open tasks with it, recent meetings, and the people ` +
    'and projects connected to it. Quote the transcripts for each point.'
}

export const register: Register = (on, options) => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'whispr-graph',
      description: "Show whispr's knowledge graph around a person, project or topic (optional: its name)",
    })
    return next(e)
  })

  on('command.run', { command: 'whispr-graph' }, async ($, e) => {
    await $.ui.open({ id: PANE, title: TITLE })
    const ref = e.args.trim()
    if (ref) void show($, options, { ref, offset: 0 })
    return { text: ref ? `whispr graph pane opened on "${ref}".` : 'whispr graph pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    const v = await read($, view)
    const a = v.answer
    const recenter = (id: string) => () => void show($, options, { ref: id, offset: 0 })
    const hopChoices = [{ value: DEFAULT_HOPS, label: 'default hops' }]
    for (let n = 1; n <= Number(options.graph_max_hops); n++) hopChoices.push({ value: String(n), label: `${n} hop${n === 1 ? '' : 's'}` })
    const types = [...new Set([...(a ? a.nodes.map(n => n.type) : []), ...(v.type ? [v.type] : [])])].sort()
    const typeChoices = [{ value: EVERY_TYPE, label: 'every type' }, ...types.map(t => ({ value: t, label: t }))]
    const neighbours = a ? a.nodes.slice(1).filter(n => !n.route_only) : []

    return (
      <Box flexDirection="column" gap={1}>
        {'Input' in els && (
          <els.Input key="search" label="Find" placeholder="a person, project or topic" submitLabel="Search"
            onSubmit={text => void search($, options, text)} />
        )}
        {v.loading && <Text dimColor>Reading the graph...</Text>}
        {v.message && <Text color="yellow" wrap="wrap">{v.message}</Text>}
        {v.cards.length > 0 && (
          <Box flexDirection="column">
            <Text bold>{v.cardsLabel}</Text>
            {v.cards.map(c => (
              <Button key={`card-${c.id}`} plain label={`${c.name} (${c.type})`} onPress={recenter(c.id)} />
            ))}
          </Box>
        )}
        {a && (
          <Box flexDirection="column" gap={1}>
            <Text bold>{a.center.name} ({a.center.type})</Text>
            <Text dimColor wrap="wrap">{a.alt}</Text>
            {'Select' in els && (
              <Box flexDirection="row" gap={2}>
                <els.Select key="hops" label="Hops" options={hopChoices} value={String(v.hops)}
                  onSelect={value => void show($, options, { hops: Number(value), offset: 0 })} />
                <els.Select key="type" label="Type" options={typeChoices} value={v.type || EVERY_TYPE}
                  onSelect={value => void show($, options, { type: value === EVERY_TYPE ? '' : value, offset: 0 })} />
              </Box>
            )}
            {e.surface !== 'terminal' && 'Svg' in els ? <els.Svg source={a.svg} alt={a.alt} isInteractive /> : (
              <Box flexDirection="column">{a.text.split('\n').map(line => <Text wrap="truncate-end">{line}</Text>)}</Box>
            )}
            <Box flexDirection="row" gap={1}>
              <Button key="ask" variant="primary" label="Ask Claude" onPress={() => void ask($, a.center)} />
              {a.offset > 0 && <Button key="first" label="First page" onPress={() => void show($, options, { offset: 0 })} />}
              {a.next_offset !== null && (
                <Button key="more" label="Next page" onPress={() => void show($, options, { offset: a.next_offset ?? 0 })} />
              )}
            </Box>
            {neighbours.length > 0 && <Text bold>Neighbours (select one to re-centre)</Text>}
            {neighbours.map(n => (
              <Button key={`n-${n.id}`} plain label={`${'  '.repeat(n.depth - 1)}${n.name} (${n.type})`} onPress={recenter(n.id)} />
            ))}
          </Box>
        )}
        {!a && v.cards.length === 0 && !v.message && !v.loading && (
          <Text dimColor>Search for a person, project or topic, or run /whispr-graph NAME.</Text>
        )}
      </Box>
    )
  })
}
