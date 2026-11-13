import type {Block,DocumentNote,Job} from './types';

/** Source indexes change with parsing, OCR and bibliography work, not AI progress. */
export function documentIndexRevision(jobs:readonly Pick<Job,'id'|'kind'|'state'>[]) {
  return JSON.stringify(jobs.filter(job=>['parse','ocr','metadata'].includes(job.kind))
    .map(job=>[job.id,job.kind,job.state])
    .sort((left,right)=>left[0].localeCompare(right[0])));
}

export function noteBlockIds(blocks:readonly Block[], notes:readonly DocumentNote[] = []) {
  const fragments = notes.flatMap(note=>note.fragments);
  const result = new Set<string>();
  for (const block of blocks) {
    const ranges = fragments.filter(fragment=>fragment.block_id===block.id && fragment.page===block.page && fragment.start>=0 &&
      fragment.end<=block.text.length && block.text.slice(fragment.start,fragment.end)===fragment.quote)
      .sort((left,right)=>left.start-right.start);
    if (!ranges.length) continue;
    let end = 0;
    let complete = true;
    for (const range of ranges) {
      if (block.text.slice(end,range.start).trim()) {complete=false;break}
      end = Math.max(end,range.end);
    }
    if (complete && !block.text.slice(end).trim()) result.add(block.id);
  }
  return result;
}
