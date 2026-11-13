import type {Block,PageReadingContinuation} from './types';

type ReadingPage = {
  number:number;
  data?:{
    blocks:Block[];
    reading_continuation?:PageReadingContinuation;
  };
};
export type ReadingContinuation = {
  hidden:ReadonlySet<string>;
  extensions:ReadonlyMap<string,readonly Block[]>;
  joinWords:ReadonlySet<string>;
};

export function readingBodyBlocks(blocks:readonly Block[], excluded:ReadonlySet<string> = new Set()) {
  return blocks.filter(block => {
    const role = block.reading_role ?? (block.kind === 'heading' ? 'heading' : block.kind);
    return !excluded.has(block.id) && !['title','extra','image','caption','table','embedded'].includes(role) &&
      (role !== 'formula' || !!block.equation);
  });
}

export function readingRuns(blocks:readonly Block[]) {
  const runs:Block[][] = [];
  for (const block of blocks) {
    if (block.continues_previous && runs.length) runs[runs.length - 1].push(block);
    else runs.push([block]);
  }
  return runs;
}

/** Move only mutually confirmed, fully loaded paragraph continuations. */
export function readingContinuations(pages:readonly ReadingPage[], excluded:ReadonlySet<string> = new Set()):ReadonlyMap<number,ReadingContinuation> {
  type Run = {page:number;blocks:Block[]};
  type Plan = {hidden:Set<string>;extensions:Map<string,readonly Block[]>;joinWords:Set<string>};
  const plans = new Map<number,Plan>();
  const runsByPage = new Map<number,Run[]>();
  const loaded = new Map(pages.filter(page => page.data).map(page => [page.number,page]));
  for (const page of loaded.values()) {
    plans.set(page.number,{hidden:new Set(),extensions:new Map(),joinWords:new Set()});
    runsByPage.set(page.number,readingRuns(readingBodyBlocks(page.data!.blocks,excluded))
      .map(blocks => ({page:page.number,blocks})));
  }
  const nextRun = new Map<Run,{run:Run;joinWord:boolean}>();
  const incoming = new Set<Run>();
  for (const left of loaded.values()) {
    const right = loaded.get(left.number + 1);
    const last = runsByPage.get(left.number)?.at(-1), first = runsByPage.get(left.number + 1)?.[0];
    const next = left.data?.reading_continuation?.next, previous = right?.data?.reading_continuation?.previous;
    if (!right || !last || !first || !next || !previous) continue;
    const source = last.blocks.at(-1)!, target = first.blocks[0];
    if (source.page !== left.number || target.page !== right.number ||
        next.page !== right.number || previous.page !== left.number ||
        next.block_id !== target.id || previous.block_id !== source.id ||
        (next.current_block_id && next.current_block_id !== source.id) ||
        (previous.current_block_id && previous.current_block_id !== target.id)) continue;
    nextRun.set(last,{run:first,joinWord:next.join_word && previous.join_word});
    incoming.add(first);
    first.blocks.forEach(block => plans.get(right.number)!.hidden.add(block.id));
  }
  for (const runs of runsByPage.values()) for (const first of runs) {
    if (incoming.has(first)) continue;
    const plan = plans.get(first.page)!;
    const extension:Block[] = [];
    let current = first;
    for (let edge = nextRun.get(current); edge; edge = nextRun.get(current)) {
      if (edge.joinWord) plan.joinWords.add(current.blocks.at(-1)!.id);
      extension.push(...edge.run.blocks);
      current = edge.run;
    }
    if (extension.length) plan.extensions.set(first.blocks.at(-1)!.id,extension);
  }
  return plans;
}
