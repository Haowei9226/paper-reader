import type {Block, FindHit, Locator, PageData, ReadingSide} from './types';

export type Position = Locator & {page: number};
export type Move = {position: Position; heading?: string; hit?: FindHit; focus?: boolean};

const guide = 32;
const fraction = (value: number) => Math.round(Math.max(0, Math.min(1, value)) * 100000) / 100000;
const relativeTop = (element: Element, pane: HTMLElement) => element.getBoundingClientRect().top - pane.getBoundingClientRect().top;

function visibleAnchor(anchor: HTMLElement) {
  // Closed details can retain positive child rectangles in Chromium. Those
  // boxes describe hidden content, not a passage under the reading guide.
  if (anchor.checkVisibility?.() === false) return false;
  for (let details = anchor.parentElement?.closest<HTMLDetailsElement>('details:not([open])'); details;
    details = details.parentElement?.closest<HTMLDetailsElement>('details:not([open])')) {
    const summary = details.querySelector(':scope > summary');
    if (!summary?.contains(anchor)) return false;
  }
  return true;
}

function pageAt(pages: HTMLElement, y: number) {
  // Sections are ordered even as earlier and later pages are inserted.
  let left = 0, right = pages.children.length - 1;
  while (left < right) {
    const middle = Math.ceil((left + right) / 2);
    if (pages.children[middle].getBoundingClientRect().top <= y) left = middle;
    else right = middle - 1;
  }
  return pages.children[left] as HTMLElement | undefined;
}

export function headingBlock(blocks: Block[], heading: string) {
  const key = (text: string) => text.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
  const unnumber = (text: string) => text.normalize('NFKC').trim().replace(/^\d+(?:\.\d+)*(?:[.、:：]\s*|\s+)/, '');
  const wanted = key(heading);
  if (!wanted) return undefined;
  const headings = blocks.filter(block => block.kind === 'heading' || block.reading_role === 'heading' || block.reading_role === 'subheading');
  const title = key(unnumber(heading));
  // A figure label can repeat the bookmark title before the numbered section starts.
  return headings.find(block => key(block.text) === wanted)
    ?? headings.find(block => title && key(unnumber(block.text)) === title)
    ?? blocks.find(block => block.reading_role !== 'caption' && block.reading_role !== 'extra' && key(block.text) === wanted);
}

export function matchesBlock(hit: FindHit | undefined, block: Block, side: ReadingSide) {
  if (!hit || hit.block_id !== block.id || hit.side !== side) return false;
  const text = side === 'source' ? block.text : (hit.translation_id === block.translation?.id ? block.translation?.text : undefined);
  return text?.slice(hit.start, hit.end) === hit.match;
}

export function findRects(data: PageData | null, hit?: FindHit) {
  const block = hit?.side === 'source' && data?.number === hit.page ? data.blocks.find(block => block.id === hit.block_id) : undefined;
  if (!hit || !block || !matchesBlock(hit, block, 'source')) return [];
  return textRects(block,hit.start,hit.end);
}

export function textRects(block:Block, start:number, end:number) {
  const lines = new Map<number, number[]>();
  for (let i = start; i < Math.min(end, block.geometry.boxes.length); i++) {
    const box = block.geometry.boxes[i];
    if (!box) continue;
    const line = block.geometry.lines[i];
    const bounds = lines.get(line);
    if (bounds) lines.set(line, [Math.min(bounds[0], box[0]), Math.min(bounds[1], box[1]), Math.max(bounds[2], box[2]), Math.max(bounds[3], box[3])]);
    else lines.set(line, box);
  }
  return [...lines.values()];
}

export function capturePosition(pane: HTMLElement | null, page: number, side: ReadingSide, pdf: PageData | null, bridgeGap = false): Position {
  const position: Position = {page, side, scroll: 0};
  if (!pane) return position;
  const pages = pane.querySelector<HTMLElement>('[data-reading-pages]');
  const paneTop = pane.getBoundingClientRect().top;
  const section = pages && pageAt(pages, paneTop + guide);
  if (section) {
    const rect = section.getBoundingClientRect();
    position.page = Number(section.dataset.readingPage);
    position.page_fraction = fraction((paneTop + guide - rect.top) / Math.max(1, rect.height));
    position.scroll = fraction((paneTop - rect.top) / Math.max(1, rect.height - pane.clientHeight));
  } else if (pane.scrollTop < 1) return position;
  else position.scroll = fraction(pane.scrollTop / Math.max(1, pane.scrollHeight - pane.clientHeight));
  if (pdf) {
    const sheet = pane.querySelector<HTMLElement>('.pdf-page');
    if (!sheet?.clientHeight) return position;
    const y = (guide - relativeTop(sheet, pane)) * pdf.height / sheet.clientHeight;
    const block = pdf.blocks.find(entry => entry.bbox[1] <= y && entry.bbox[3] > y);
    if (block) {
      position.block_id = block.id;
      position.offset = fraction((y - block.bbox[1]) / Math.max(.001, block.bbox[3] - block.bbox[1]));
    }
    return position;
  }
  let previous: HTMLElement | undefined;
  for (const anchor of (section ?? pane).querySelectorAll<HTMLElement>('[data-block-anchor]')) {
    if (!visibleAnchor(anchor)) continue;
    const rect = anchor.getBoundingClientRect();
    if (!rect.height) continue;
    const top = rect.top - paneTop;
    if (top + rect.height <= guide) {
      previous = anchor;
      continue;
    }
    // Keep the last paragraph paired through the small whitespace between blocks.
    const chosen = top <= guide ? anchor : bridgeGap && section && previous && paneTop + guide - previous.getBoundingClientRect().bottom < 32 ? previous : undefined;
    if (chosen) {
      position.block_id = chosen.dataset.blockAnchor;
      const actualPage = Number(chosen.dataset.blockPage || chosen.closest<HTMLElement>('[data-page-start]')?.dataset.pageStart);
      if (Number.isInteger(actualPage) && actualPage > 0) position.page = actualPage;
      position.offset = chosen === anchor ? fraction((guide - top) / rect.height) : 1;
      position.side = chosen.dataset.readingSide === 'translation' ? 'translation' : 'source';
      break;
    }
    if (top > guide) break;
  }
  return position;
}

export function scrollForPosition(pane: HTMLElement, position: Position, side: ReadingSide, pdf: PageData | null, move?: Move) {
  const inset = move?.focus ? Math.min(110, pane.clientHeight * .23) : guide;
  const pages = pane.querySelector('[data-reading-pages]');
  const section = pages?.querySelector<HTMLElement>(`[data-reading-page="${position.page}"]`);
  if (position.block_id && pdf) {
    const block = pdf.blocks.find(block => block.id === position.block_id);
    const sheet = pane.querySelector<HTMLElement>('.pdf-page');
    if (block && sheet?.clientHeight) {
      const y = findRects(pdf, move?.hit)[0]?.[1] ?? block.bbox[1] + (block.bbox[3] - block.bbox[1]) * (position.offset ?? 0);
      return pane.scrollTop + relativeTop(sheet, pane) + y / pdf.height * sheet.clientHeight - inset;
    }
  } else if (position.block_id) {
    const anchors = [...pane.querySelectorAll<HTMLElement>(`[data-block-anchor="${CSS.escape(position.block_id)}"]`)];
    let anchor = anchors.find(element => element.dataset.readingSide === side) ?? anchors[0];
    let offset = position.offset ?? 0;
    if (anchor?.dataset.readingOwner) {
      const owners = [...pane.querySelectorAll<HTMLElement>(`[data-block-anchor="${CSS.escape(anchor.dataset.readingOwner)}"]`)];
      const owner = owners.find(element => element.dataset.readingSide === side);
      if (owner) {
        const count = owner.querySelector('[data-text-block]')?.textContent?.length ?? 0;
        const start = Number(anchor.dataset.ownerStart) || 0, end = Number(anchor.dataset.ownerEnd) || start;
        offset = count ? (start + (end - start) * offset) / count : 0;
        anchor = owner;
      }
    }
    if (move) for(let details=anchor?.closest('details')??null;details;details=details.parentElement?.closest('details')??null) details.open=true;
    if (anchor && anchor.getBoundingClientRect().height > 0) {
      const hit = move?.hit?.side === side ? anchor.querySelector<HTMLElement>('[data-find-hit]') : null;
      return pane.scrollTop + relativeTop(hit ?? anchor, pane) + (hit ? 0 : anchor.getBoundingClientRect().height * offset) - inset;
    }
  }
  const starts = [...(pages?.querySelectorAll<HTMLElement>(`[data-page-start="${position.page}"]`) ?? [])];
  const pageStart = starts.find(element => element.dataset.readingSide === side && element.getBoundingClientRect().height > 0);
  if (pageStart) {
    const start = pageStart.getBoundingClientRect();
    const height = Math.max(1,(section?.getBoundingClientRect().bottom ?? start.bottom) - start.top);
    const pageTop = pane.scrollTop + relativeTop(pageStart,pane);
    if (position.page_fraction !== undefined) return pageTop + position.page_fraction * height - inset;
    return pageTop - inset + (position.scroll ?? 0) * Math.max(0,height - pane.clientHeight);
  }
  if (section) {
    const height = section.getBoundingClientRect().height;
    const pageTop = pane.scrollTop + relativeTop(section, pane);
    if (position.page_fraction !== undefined) return pageTop + position.page_fraction * height - inset;
    return pageTop - inset + (position.scroll ?? 0) * Math.max(0, height - pane.clientHeight);
  }
  if (pages) return pane.scrollTop;
  return (position.page_fraction ?? position.scroll ?? 0) * Math.max(0, pane.scrollHeight - pane.clientHeight);
}
