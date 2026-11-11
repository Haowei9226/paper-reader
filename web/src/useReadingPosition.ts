import {useCallback, useEffect, useLayoutEffect, useRef, useState} from 'react';
import type {RefObject} from 'react';
import {api} from './api';
import {capturePosition, headingBlock, scrollForPosition} from './readerPosition';
import type {Move, Position} from './readerPosition';
import type {FindHit, Layout, Locator, PageData, Paper, ReadingMode, ReadingSide} from './types';

export function useReadingPosition({paper, page, mode, layout, singleSide, data, pdf, pdfLayout, sync, sourcePane, targetPane, onPageVisible, initialMove}: {
  paper: Paper; page: number; mode: ReadingMode; layout: Layout; singleSide: ReadingSide; data: PageData | null;
  pdf: boolean; pdfLayout: number; sync: boolean;
  sourcePane: RefObject<HTMLDivElement | null>; targetPane: RefObject<HTMLDivElement | null>;
  onPageVisible: (page: number) => void;
  initialMove?: Move;
}) {
  const [move, setMove] = useState<Move>(() => initialMove ?? {position: {...paper.locator, page}});
  const [hit, setHit] = useState<FindHit | undefined>(() => initialMove?.hit);
  const [flash, setFlash] = useState('');
  const applied = useRef<Move>(undefined);
  const side = useRef<ReadingSide>(move.position.side ?? 'source');
  const writtenScroll = useRef(new WeakMap<HTMLElement, number>());
  const saveTimer = useRef<number>(undefined);
  const flashTimer = useRef<number>(undefined);
  const lastStamp = useRef(paper.locator.saved_at ?? 0);
  const lastWrite = useRef<{key: string; promise: Promise<Locator | undefined>}>(undefined);
  const latestSave = useRef<() => Promise<Locator | undefined>>(async () => undefined);

  function current(): Position {
    if (applied.current !== move) {
      const {saved_at: _stamp, ...position} = move.position;
      return {...position, mode};
    }
    const active = layout === 'single' ? singleSide : side.current;
    const pane = layout === 'parallel' && active === 'translation' ? targetPane.current : sourcePane.current;
    return {...capturePosition(pane, page, active, pdf && active === 'source' ? data : null), mode};
  }

  function save() {
    window.clearTimeout(saveTimer.current);
    const position = current();
    const key = JSON.stringify(position);
    if (lastWrite.current?.key === key) return lastWrite.current.promise;
    lastStamp.current = Math.max(lastStamp.current + 1, Date.now());
    const promise = api<Paper>(`/papers/${paper.id}`, {
      method: 'PATCH', keepalive: true, body: JSON.stringify({locator: {...position, saved_at: lastStamp.current}}),
    }).then(paper => paper.locator).catch(() => {
      if (lastWrite.current?.promise === promise) lastWrite.current = undefined;
      return undefined;
    });
    lastWrite.current = {key, promise};
    return promise;
  }

  function scheduleSave() {
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => void latestSave.current(), 600);
  }

  const place = useCallback((pane: HTMLElement, top: number) => {
    const before = pane.scrollTop;
    if (Math.abs(top - before) < .75) return;
    pane.scrollTop = top;
    if (pane.scrollTop !== before) writtenScroll.current.set(pane, pane.scrollTop);
  }, []);

  function align(position = current()) {
    if (layout !== 'parallel') return;
    const from = position.side ?? 'source';
    const other = from === 'source' ? targetPane.current : sourcePane.current;
    if (other) place(other, scrollForPosition(other, position, from === 'source' ? 'translation' : 'source', pdf && from === 'translation' ? data : null));
  }

  function trackScroll(from: ReadingSide) {
    const pane = layout === 'parallel' && from === 'translation' ? targetPane.current : sourcePane.current;
    if (!pane) return;
    const written = writtenScroll.current.get(pane);
    writtenScroll.current.delete(pane);
    if (pane.scrollTop === written || applied.current !== move) return;
    side.current = from;
    scheduleSave();
    if (pdf && (!sync || layout !== 'parallel')) return;
    const position = capturePosition(pane, page, from, pdf && from === 'source' ? data : null, sync && layout === 'parallel');
    if (!pdf && position.page !== page) onPageVisible(position.page);
    if (sync) align(position);
  }

  function navigate(next: Move) {
    setMove(next);
    setHit(next.hit);
    setFlash('');
  }

  useLayoutEffect(() => {latestSave.current = save});
  useEffect(() => {
    const flush = () => void latestSave.current();
    window.addEventListener('pagehide', flush);
    return () => {
      window.removeEventListener('pagehide', flush);
      window.clearTimeout(saveTimer.current);
      window.clearTimeout(flashTimer.current);
    };
  }, []);

  useLayoutEffect(() => {
    const source = sourcePane.current;
    if (applied.current === move || !source || move.position.page !== page) return;
    if (pdf && !source.querySelector(`.pdf-page[data-laid-out-page="${page}"]`)) return;
    if (!data && (!pdf || ((move.position.block_id || move.heading || move.position.side === 'translation') && paper.parse_state === 'ready'))) return;
    const position = move.heading ? {...move.position, block_id: headingBlock(data?.blocks ?? [], move.heading)?.id} : move.position;
    const active = layout === 'single' ? singleSide : position.side ?? 'source';
    place(source, scrollForPosition(source, position, layout === 'parallel' ? 'source' : active, pdf ? data : null, move));
    if (targetPane.current) place(targetPane.current, scrollForPosition(targetPane.current, position, 'translation', null, move));
    side.current = active;
    applied.current = move;
    if (move.focus && position.block_id && !move.hit) {
      setFlash(position.block_id);
      window.clearTimeout(flashTimer.current);
      flashTimer.current = window.setTimeout(() => setFlash(''), 2200);
    }
    scheduleSave();
  }, [move, page, mode, layout, singleSide, data, pdf, pdfLayout, paper.parse_state, sourcePane, targetPane, place]);

  return {current, navigate, trackScroll, place, align, save, hit, clearHit: () => setHit(undefined), flash};
}
