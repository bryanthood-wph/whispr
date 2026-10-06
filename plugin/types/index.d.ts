/** One entity as `kg.view search` lists it, or as a candidate of an ambiguous name. */
export type GraphCard = { id: string; name: string; type: string }

/** One drawn entity of a `kg.view neighborhood` answer. */
export type GraphNode = GraphCard & { depth: number; route_only: boolean }

/** The part of a `kg.view neighborhood` answer the pane draws. */
export type GraphAnswer = {
  center: GraphCard
  hops: number
  reached: number
  offset: number
  next_offset: number | null
  truncated: boolean
  nodes: GraphNode[]
  svg: string
  alt: string
  text: string
}

/** What the pane shows: the last request, its answer, and what went wrong if anything. */
export type GraphView = {
  /** The entity asked for (an id, exact name or email), or null before the first ask. */
  ref: string | null
  hops: number
  /** An entity type to keep, or '' for every type. */
  type: string
  offset: number
  answer: GraphAnswer | null
  /** Search results, or the candidates of an ambiguous name. */
  cards: GraphCard[]
  cardsLabel: string
  message: string | null
  loading: boolean
  /** Bumped by each request; an answer lands only if no newer request started meanwhile. */
  request: number
}

declare module 'claude-code' {
  interface PluginState {
    'whispr': { view: GraphView }
  }
}
