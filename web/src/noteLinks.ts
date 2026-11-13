import type {Block} from './types';

export type NoteAnchor = {block_id:string;page:number;start:number;end:number;quote:string};
export type NoteEntry = {id:string;kind:'footnote'|'endnote';label:string};
export type ResolvedNoteMarker = {
  note_id:string;
  anchor:NoteAnchor;
  evidence:'superscript'|'pdf_link';
};
export type NoteLinks = {
  entries:readonly NoteEntry[];
  markers:readonly ResolvedNoteMarker[];
  open:(noteIds:string[])=>void;
};
export type NoteRange = {start:number;end:number;noteIds:string[]};
type TextRange = {start:number;end:number};

function boundary(text:string, offset:number) {
  const left = text.charCodeAt(offset - 1), right = text.charCodeAt(offset);
  return !(left >= 0xd800 && left <= 0xdbff && right >= 0xdc00 && right <= 0xdfff);
}

function validRange(text:string, range:TextRange) {
  return Number.isInteger(range.start) && Number.isInteger(range.end) &&
    0 <= range.start && range.start < range.end && range.end <= text.length &&
    boundary(text,range.start) && boundary(text,range.end);
}

function translatedRange(block:Block, marker:ResolvedNoteMarker) {
  const translation = block.translation;
  if (!translation) return null;
  const anchor = marker.anchor;
  const matches:TextRange[] = [];
  for (const span of translation.segments) {
    if (span.source_block_id && span.source_block_id !== block.id) continue;
    if (span.target_block_id && span.target_block_id !== block.id) continue;
    const source = {start:span.source_start,end:span.source_end};
    const target = {start:span.target_start,end:span.target_end};
    if (!validRange(block.text,source) || !validRange(translation.text,target) ||
        source.start > anchor.start || anchor.end > source.end) continue;
    const original = block.text.slice(source.start,source.end);
    const translated = translation.text.slice(target.start,target.end);
    if (original === translated) {
      matches.push({start:target.start + anchor.start - source.start,end:target.start + anchor.end - source.start});
    } else if (source.start === anchor.start && source.end === anchor.end &&
               original.normalize('NFKC') === translated.normalize('NFKC')) {
      matches.push(target);
    }
  }
  const unique = new Map(matches.map(range => [range.start + ':' + range.end,range]));
  return unique.size === 1 ? [...unique.values()][0] : null;
}

/** Use confirmed source anchors. A repeated printed label never determines note identity. */
export function noteRanges(block:Block, text:string, links:NoteLinks):NoteRange[] {
  const source = text === block.text;
  if (!source && text !== block.translation?.text) return [];
  const known = new Set(links.entries.map(entry => entry.id));
  const candidates:{start:number;end:number;noteId:string}[] = [];
  for (const marker of links.markers) {
    const anchor = marker.anchor;
    if (!['superscript','pdf_link'].includes(marker.evidence) || !known.has(marker.note_id) ||
        anchor.block_id !== block.id || anchor.page !== block.page ||
        typeof anchor.quote !== 'string' || !anchor.quote.trim() || !validRange(block.text,anchor) ||
        block.text.slice(anchor.start,anchor.end) !== anchor.quote) continue;
    const range = source ? {start:anchor.start,end:anchor.end} : translatedRange(block,marker);
    if (range && validRange(text,range)) candidates.push({...range,noteId:marker.note_id});
  }
  const unique = [...new Map(candidates.map(range => [range.noteId + ':' + range.start + ':' + range.end,range])).values()];
  const confirmed = unique.filter(range => !unique.some(other => other.noteId !== range.noteId &&
    range.start < other.end && other.start < range.end));
  confirmed.sort((left,right) => left.start - right.start || right.end - left.end);
  const result:NoteRange[] = [];
  for (const range of confirmed) {
    const previous = result.at(-1);
    if (previous && range.start < previous.end) continue;
    if (previous && /^\s*$/u.test(text.slice(previous.end,range.start))) {
      previous.end = range.end;
      previous.noteIds = [...new Set([...previous.noteIds,range.noteId])];
    } else result.push({start:range.start,end:range.end,noteIds:[range.noteId]});
  }
  return result;
}

export function noteLabel(noteIds:readonly string[], links?:Pick<NoteLinks,'entries'>) {
  const entries = new Map(links?.entries.map(entry => [entry.id,entry]));
  const selected = noteIds.map(id => entries.get(id));
  const allFootnotes = selected.length > 0 && selected.every(entry => entry?.kind === 'footnote');
  const allEndnotes = selected.length > 0 && selected.every(entry => entry?.kind === 'endnote');
  const kind = allFootnotes ? '脚注' : allEndnotes ? '尾注' : '注释';
  const labels = selected.map(entry => entry?.label).filter(Boolean);
  return '查看' + kind + (labels.length ? ' ' + labels.join('、') : '');
}
