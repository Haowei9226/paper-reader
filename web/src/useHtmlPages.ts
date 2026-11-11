import {useEffect, useMemo, useRef, useState} from 'react';
import {api} from './api';
import type {PageData} from './types';

export type HtmlPageSlot = {number: number; data?: PageData; error?: string};
type LoadedSlot = HtmlPageSlot & {revision: string};
type Range = {first: number; last: number};
const around = (page: number, total: number): Range => ({first: Math.max(1, page - 1), last: Math.min(total, page + 1)});

export function useHtmlPages({paperId, total, enabled, anchor, visible, revision}: {
  paperId: string; total: number; enabled: boolean; anchor: number; visible: number; revision: string;
}) {
  const [range, setRange] = useState(() => around(anchor, total));
  const [loaded, setLoaded] = useState<Record<number, LoadedSlot>>({});
  const requests = useRef(new Map<number, AbortController>());
  const relocate = anchor < range.first || anchor > range.last;
  const {first, last} = relocate ? around(anchor, total) : {first: range.first, last: Math.min(total, range.last)};

  function load(number: number) {
    if (requests.current.has(number)) return;
    const controller = new AbortController();
    requests.current.set(number, controller);
    void api<PageData>(`/papers/${paperId}/pages/${number}?geometry=false`, {signal: controller.signal})
      .then(data => {
        if (!controller.signal.aborted) setLoaded(current => ({...current, [number]: {number, data, revision}}));
      })
      .catch(error => {
        if (!controller.signal.aborted) setLoaded(current => ({...current, [number]: {number, data: current[number]?.data, error: error.message, revision}}));
      })
      .finally(() => {if (requests.current.get(number) === controller) requests.current.delete(number)});
  }

  useEffect(() => {
    if (!enabled) return;
    return () => {
      for (const request of requests.current.values()) request.abort();
      requests.current.clear();
    };
  }, [enabled, paperId]);

  useEffect(() => {
    if (!enabled) return;
    if (relocate) {
      setRange({first, last});
      for (const [number, request] of requests.current) {
        if (number < first || number > last) {request.abort(); requests.current.delete(number)}
      }
      setLoaded(current => Object.fromEntries(Object.entries(current).filter(([number]) => +number >= first && +number <= last)));
    }
    // Fetch a few pages on entry, then only add pages near the viewport. Refresh
    // old text when it is needed again instead of refetching the whole document.
    for (let number = first; number <= last; number++) {
      const slot = loaded[number];
      if (!slot || (Math.abs(number - visible) <= 2 && slot.revision !== revision)) load(number);
    }
  }, [enabled, first, last, relocate, anchor, visible, revision, loaded, paperId]);

  const pages = useMemo(() => Array.from({length: Math.max(0, last - first + 1)}, (_, index) => loaded[first + index] ?? {number: first + index}), [first, last, loaded]);

  function extend(direction: 'before' | 'after') {
    if (relocate || !enabled) return;
    const earlier = direction === 'before';
    const edge = earlier ? first : last;
    if (!loaded[edge]?.data || (earlier ? edge <= 1 : edge >= total)) return;
    setRange(current => current.first !== first || Math.min(total, current.last) !== last ? current
      : earlier ? {first: first - 1, last} : {first, last: last + 1});
  }

  return {pages, target: loaded[anchor]?.data ?? null, extend, retry: load,
    canPrepend: first > 1 && !!loaded[first]?.data,
    canAppend: last < total && !!loaded[last]?.data};
}
